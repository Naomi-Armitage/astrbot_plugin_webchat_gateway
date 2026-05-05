"""Chat HTTP handler.

Pipeline order (parse-before-lock, so a slow body cannot pin the per-token
concurrency slot):
    1. Origin allow-list
    2. IP brute-force gate
    3. Auth (token lookup + IP guard accounting)
    4. Parse JSON body + length check
    5. Per-token concurrency lock (single-flight)
    6. Daily quota check (under the lock, paired with increment)
    7. LLM call
    8. Increment usage
    9. Audit + respond
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

from aiohttp import web

from astrbot.api import logger

from ..core.audit import AuditLogger
from ..core.auth import extract_bearer, hash_token
from ..core.ip_guard import IpGuard
from ..core.llm_bridge import LlmBridge
from ..core.ratelimit import PerTokenConcurrency
from ..storage.base import AbstractStorage, TokenRow
from .common import (
    build_cors_headers,
    client_ip,
    extract_origin,
    gate_request,
    is_origin_allowed,
    json_response,
    preflight_response,
)


_HEARTBEAT_INTERVAL = 20.0


def _is_expired(token: TokenRow, now: int) -> bool:
    return token.expires_at is not None and token.expires_at <= now


@dataclass
class ChatDeps:
    storage: AbstractStorage
    audit: AuditLogger
    ip_guard: IpGuard
    concurrency: PerTokenConcurrency
    llm_bridge: LlmBridge
    conv_service: Any  # handlers.conversations.ConversationService — avoid import cycle
    allowed_origins: set[str]
    max_message_length: int
    trust_forwarded_for: bool
    trust_referer_as_origin: bool = False
    allow_missing_origin: bool = False


@dataclass
class _ParsedRequest:
    session_id: str
    user_id: str
    username: str
    message: str


def _parse_payload(payload: Any) -> _ParsedRequest | None:
    if not isinstance(payload, dict):
        return None
    message = str(payload.get("message") or "").strip()
    if not message:
        return None
    session_id = str(
        payload.get("sessionId") or payload.get("session_id") or "webchat"
    ).strip() or "webchat"
    user_id = str(payload.get("userId") or payload.get("user_id") or "").strip()
    username = (str(payload.get("username") or "").strip() or "WebUser")[:64]
    return _ParsedRequest(
        session_id=session_id[:128],
        user_id=user_id[:128],
        username=username,
        message=message,
    )


async def _parse_chat_body(
    request: web.Request,
    max_message_length: int,
    *,
    origin: str | None,
    allowed: set[str],
    same_host: str,
) -> _ParsedRequest | web.Response:
    """Parse the JSON body for /chat-style requests, applying the same
    error-shape contract both /chat and /chat/stream advertise. Returns
    either the parsed payload or an already-serialized error Response."""
    try:
        payload = await request.json()
    except web.HTTPRequestEntityTooLarge:
        return json_response(
            {"error": "payload_too_large"}, status=413,
            origin=origin, allowed_origins=allowed, same_origin_host=same_host,
        )
    except json.JSONDecodeError:
        return json_response(
            {"error": "invalid_json"}, status=400,
            origin=origin, allowed_origins=allowed, same_origin_host=same_host,
        )
    except Exception:
        logger.exception("[WebChatGateway] unexpected JSON parse error")
        return json_response(
            {"error": "invalid_json"}, status=400,
            origin=origin, allowed_origins=allowed, same_origin_host=same_host,
        )
    data = _parse_payload(payload)
    if data is None:
        return json_response(
            {"error": "invalid_payload"}, status=400,
            origin=origin, allowed_origins=allowed, same_origin_host=same_host,
        )
    if len(data.message) > max_message_length:
        return json_response(
            {"error": "message_too_long", "max_length": max_message_length},
            status=400,
            origin=origin, allowed_origins=allowed, same_origin_host=same_host,
        )
    return data


def make_chat_handler(deps: ChatDeps):
    async def handle(request: web.Request) -> web.Response:
        origin = extract_origin(
            request, trust_referer_as_origin=deps.trust_referer_as_origin
        )
        allowed = deps.allowed_origins
        same_host = request.host

        # 1. Origin allow-list
        if not is_origin_allowed(
            origin,
            allowed,
            same_origin_host=same_host,
            allow_missing=deps.allow_missing_origin,
        ):
            return json_response(
                {"error": "forbidden_origin"},
                status=403,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)

        # 2. IP brute-force gate
        blocked, retry_after = await deps.ip_guard.is_blocked(ip)
        if blocked:
            return json_response(
                {"error": "ip_blocked", "retry_after": retry_after},
                status=429,
                origin=origin,
                allowed_origins=allowed,
                extra_headers={"Retry-After": str(retry_after)},
                same_origin_host=same_host,
            )

        # 3. Auth
        presented = extract_bearer(request)
        if not presented:
            await deps.ip_guard.record_failure(ip)
            await deps.audit.write("auth_fail", ip=ip, detail={"reason": "no_token"})
            return json_response(
                {"error": "unauthorized"},
                status=401,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )
        token = await deps.storage.get_token_by_hash(hash_token(presented))
        now_ts = int(time.time())
        expired = token is not None and _is_expired(token, now_ts)
        if token is None or token.revoked_at is not None or expired:
            # Only blind probing (token not found) counts toward IP brute-force.
            # A friend retrying a freshly revoked OR expired token is
            # misconfiguration, not an attacker — penalising their IP would
            # lock them out for ip_brute_force_block_seconds with no recourse.
            if token is None:
                await deps.ip_guard.record_failure(ip)
            if token is None:
                reason = "invalid"
            elif token.revoked_at is not None:
                reason = "revoked"
            else:
                reason = "expired"
            await deps.audit.write(
                "auth_fail",
                ip=ip,
                detail={"reason": reason},
            )
            return json_response(
                {"error": "unauthorized"},
                status=401,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )
        # Valid auth — clear failures for this IP.
        await deps.ip_guard.reset(ip)

        # 4. Parse + length check (before taking the per-token lock so a slow
        # body cannot pin the slot).
        try:
            payload = await request.json()
        except web.HTTPRequestEntityTooLarge:
            return json_response(
                {"error": "payload_too_large"},
                status=413,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )
        except json.JSONDecodeError:
            return json_response(
                {"error": "invalid_json"},
                status=400,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )
        except Exception:
            logger.exception("[WebChatGateway] unexpected JSON parse error")
            return json_response(
                {"error": "invalid_json"},
                status=400,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )
        data = _parse_payload(payload)
        if data is None:
            return json_response(
                {"error": "invalid_payload"},
                status=400,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )
        if len(data.message) > deps.max_message_length:
            return json_response(
                {
                    "error": "message_too_long",
                    "max_length": deps.max_message_length,
                },
                status=400,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

        # 5. Concurrency lock
        async with deps.concurrency.acquire(token.name) as acquired:
            if not acquired:
                await deps.audit.write(
                    "concurrent_block", name=token.name, ip=ip, detail=None
                )
                return json_response(
                    {"error": "concurrent_request"},
                    status=429,
                    origin=origin,
                    allowed_origins=allowed,
                    same_origin_host=same_host,
                )

            # 6. Daily quota check (read-then-increment is racy across processes,
            # but per-token concurrency=1 guarantees serial use of a single token).
            today = date.today()
            today_count = await deps.storage.get_today_usage(token.name, day=today)
            if today_count >= token.daily_quota:
                await deps.audit.write(
                    "quota_exceeded",
                    name=token.name,
                    ip=ip,
                    detail={"today_count": today_count, "quota": token.daily_quota},
                )
                return json_response(
                    {
                        "error": "quota_exceeded",
                        "remaining": 0,
                        "daily_quota": token.daily_quota,
                    },
                    status=429,
                    origin=origin,
                    allowed_origins=allowed,
                    same_origin_host=same_host,
                )

            # 7. LLM call
            try:
                reply = await deps.llm_bridge.generate_reply(
                    token_name=token.name,
                    session_id=data.session_id,
                    username=data.username,
                    message=data.message,
                )
            except RuntimeError as exc:
                if str(exc) == "llm_timeout":
                    await deps.audit.write(
                        "llm_timeout",
                        name=token.name,
                        ip=ip,
                        detail={"msg_len": len(data.message)},
                    )
                    return json_response(
                        {"error": "llm_timeout"},
                        status=504,
                        origin=origin,
                        allowed_origins=allowed,
                        same_origin_host=same_host,
                    )
                # Internal exception text may leak provider names, paths, or
                # context near credentials — keep it in audit/log only and
                # return a stable error code to the caller.
                logger.exception("[WebChatGateway] LLM call failed")
                await deps.audit.write(
                    "chat_error",
                    name=token.name,
                    ip=ip,
                    detail={"error": str(exc)[:200]},
                )
                return json_response(
                    {"error": "llm_call_failed"},
                    status=500,
                    origin=origin,
                    allowed_origins=allowed,
                    same_origin_host=same_host,
                )
            except Exception as exc:
                logger.exception("[WebChatGateway] LLM call failed")
                await deps.audit.write(
                    "chat_error",
                    name=token.name,
                    ip=ip,
                    detail={"error": str(exc)[:200]},
                )
                return json_response(
                    {"error": "llm_call_failed"},
                    status=500,
                    origin=origin,
                    allowed_origins=allowed,
                    same_origin_host=same_host,
                )

            # 8. Increment usage (atomic)
            new_count = await deps.storage.increment_daily_usage(token.name, day=today)
            remaining = max(0, token.daily_quota - new_count)

            # Record the user/assistant pair into the chat-sync event log so
            # peer devices on the same token long-poll their way to the new
            # state. record_chat_pair swallows its own errors — a failure
            # here must NOT block the chat reply that's already complete.
            await deps.conv_service.record_chat_pair(
                token_name=token.name,
                session_id=data.session_id,
                user_text=data.message,
                assistant_text=reply,
            )

            # 9. Audit + respond
            await deps.audit.write(
                "chat_ok",
                name=token.name,
                ip=ip,
                detail={
                    "msg_len": len(data.message),
                    "reply_len": len(reply),
                    "remaining": remaining,
                },
            )
            return json_response(
                {
                    "reply": reply,
                    "remaining": remaining,
                    "daily_quota": token.daily_quota,
                },
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

    return handle


def make_chat_stream_handler(deps: ChatDeps):
    # SSE variant of /chat. Quota and CM persist run only on successful end;
    # client disconnect mid-stream audits chat_stream_aborted and skips both
    # (matches PLAN_chat_streaming.md "Locked decisions").

    async def handle(request: web.Request) -> web.StreamResponse:
        # Steps 1-3: origin / IP / auth (incl. revoked + expired). Reuse the
        # gate_request helper so /chat and /chat/stream don't drift on the
        # auth-side wire contract; the helper returns either a typed
        # GatePass or an already-CORS'd error Response.
        gated = await gate_request(request, deps)
        if isinstance(gated, web.Response):
            return gated
        token = gated.token
        ip = gated.ip
        origin = gated.origin
        allowed = gated.allowed
        same_host = gated.same_host

        # Step 4: parse JSON body + length cap before taking the per-token
        # lock so a slow/large body cannot pin a streaming slot.
        parsed = await _parse_chat_body(
            request, deps.max_message_length,
            origin=origin, allowed=allowed, same_host=same_host,
        )
        if isinstance(parsed, web.Response):
            return parsed
        data = parsed

        # 5. Concurrency lock
        async with deps.concurrency.acquire(token.name) as acquired:
            if not acquired:
                # Lock contention is a precondition failure, not a stream
                # error: the client never gets a 200 SSE response. Returning
                # JSON keeps parity with /chat so the frontend's existing 429
                # handler covers both endpoints.
                await deps.audit.write(
                    "concurrent_block", name=token.name, ip=ip, detail=None
                )
                return json_response(
                    {"error": "concurrent_request"},
                    status=429,
                    origin=origin,
                    allowed_origins=allowed,
                    same_origin_host=same_host,
                )

            # 6. Quota check
            today = date.today()
            today_count = await deps.storage.get_today_usage(token.name, day=today)
            if today_count >= token.daily_quota:
                await deps.audit.write(
                    "quota_exceeded",
                    name=token.name,
                    ip=ip,
                    detail={"today_count": today_count, "quota": token.daily_quota},
                )
                return json_response(
                    {
                        "error": "quota_exceeded",
                        "remaining": 0,
                        "daily_quota": token.daily_quota,
                    },
                    status=429,
                    origin=origin,
                    allowed_origins=allowed,
                    same_origin_host=same_host,
                )

            # 7. Open SSE response
            cors = build_cors_headers(origin, allowed, same_origin_host=same_host)
            response = web.StreamResponse(
                status=200,
                headers={
                    **cors,
                    "Content-Type": "text/event-stream; charset=utf-8",
                    "Cache-Control": "no-store",
                    # nginx: disable response buffering. Apache mod_proxy obeys
                    # this same header. Without it, intermediate proxies hold
                    # chunks until a buffer fills, defeating streaming.
                    "X-Accel-Buffering": "no",
                },
            )
            await response.prepare(request)
            # Comment frame so the browser sees bytes immediately and any
            # transparent proxy flushes its buffer.
            await response.write(b": ready\n\n")

            collected: list[str] = []
            aborted = False

            async def _write_frame(frame: bytes) -> bool:
                # Write returns when the chunk has been handed to the transport.
                # ConnectionResetError is the canonical "peer dropped" signal
                # from aiohttp; transport.is_closing() catches the half-closed
                # window where the FIN has arrived but the next write hasn't
                # tripped a reset yet.
                if request.transport is None or request.transport.is_closing():
                    return False
                try:
                    await response.write(frame)
                except (ConnectionResetError, ConnectionError):
                    return False
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("[WebChatGateway] SSE write failed")
                    return False
                return True

            stream = deps.llm_bridge.generate_reply_stream(
                token_name=token.name,
                session_id=data.session_id,
                username=data.username,
                message=data.message,
            )
            stream_iter = stream.__aiter__()

            try:
                while True:
                    try:
                        # `asyncio.shield` is required: wrapping `__anext__`
                        # directly with `wait_for` lets the timeout cancel
                        # the awaitable, which on an async generator
                        # corrupts the generator state (PEP 533 / cpython
                        # issue 82624) — subsequent calls raise
                        # "asynchronous generator is already running".
                        # `shield` keeps the inner await alive across
                        # heartbeats; only an outer cancel propagates.
                        chunk = await asyncio.wait_for(
                            asyncio.shield(stream_iter.__anext__()),
                            timeout=_HEARTBEAT_INTERVAL,
                        )
                    except asyncio.TimeoutError:
                        # No chunk arrived in 20s — emit a comment frame to
                        # keep the connection alive through idle proxies and
                        # to surface a peer disconnect on the next write.
                        if not await _write_frame(b": keepalive\n\n"):
                            aborted = True
                            break
                        continue
                    except StopAsyncIteration:
                        break

                    collected.append(chunk)
                    frame = (
                        "data: "
                        + json.dumps({"chunk": chunk}, ensure_ascii=False)
                        + "\n\n"
                    ).encode("utf-8")
                    if not await _write_frame(frame):
                        aborted = True
                        break
            except RuntimeError as exc:
                code = str(exc)
                if code == "llm_timeout":
                    await deps.audit.write(
                        "llm_timeout",
                        name=token.name,
                        ip=ip,
                        detail={"msg_len": len(data.message), "streamed": True},
                    )
                    error_code = "llm_timeout"
                else:
                    logger.exception("[WebChatGateway] LLM stream failed")
                    await deps.audit.write(
                        "chat_error",
                        name=token.name,
                        ip=ip,
                        detail={"error": code[:200], "streamed": True},
                    )
                    error_code = "llm_call_failed"
                # Best-effort error frame; if the peer is already gone the
                # write fails silently — the audit row above is the source of
                # truth either way.
                await _write_frame(
                    (
                        "data: "
                        + json.dumps({"error": error_code}, ensure_ascii=False)
                        + "\n\n"
                    ).encode("utf-8")
                )
                # Close out the upstream generator so its `finally` runs and
                # the provider connection is released.
                await stream_iter.aclose()
                return response
            except asyncio.CancelledError:
                # Server-initiated shutdown. Abort cleanly; no quota, no CM.
                await stream_iter.aclose()
                await deps.audit.write(
                    "chat_stream_aborted",
                    name=token.name,
                    ip=ip,
                    detail={"reason": "cancelled", "msg_len": len(data.message)},
                )
                raise
            except Exception as exc:
                logger.exception("[WebChatGateway] LLM stream failed")
                await deps.audit.write(
                    "chat_error",
                    name=token.name,
                    ip=ip,
                    detail={"error": str(exc)[:200], "streamed": True},
                )
                await _write_frame(
                    (
                        "data: "
                        + json.dumps({"error": "internal_error"}, ensure_ascii=False)
                        + "\n\n"
                    ).encode("utf-8")
                )
                await stream_iter.aclose()
                return response

            if aborted:
                # Client disconnect or transport reset. Skip quota + CM.
                # generate_reply_stream's `finally` aclose'd its inner async
                # for, so the provider connection is already released.
                await stream_iter.aclose()
                await deps.audit.write(
                    "chat_stream_aborted",
                    name=token.name,
                    ip=ip,
                    detail={
                        "reason": "client_disconnect",
                        "msg_len": len(data.message),
                        "partial_len": sum(len(c) for c in collected),
                    },
                )
                return response

            # 8. Successful stream end → quota + CM + audit + done frame.
            full_reply = "".join(collected)
            new_count = await deps.storage.increment_daily_usage(token.name, day=today)
            remaining = max(0, token.daily_quota - new_count)
            await deps.conv_service.record_chat_pair(
                token_name=token.name,
                session_id=data.session_id,
                user_text=data.message,
                assistant_text=full_reply,
            )
            await deps.audit.write(
                "chat_ok",
                name=token.name,
                ip=ip,
                detail={
                    "msg_len": len(data.message),
                    "reply_len": len(full_reply),
                    "remaining": remaining,
                    "streamed": True,
                },
            )
            done_frame = (
                "data: "
                + json.dumps(
                    {
                        "done": True,
                        "remaining": remaining,
                        "daily_quota": token.daily_quota,
                    },
                    ensure_ascii=False,
                )
                + "\n\n"
            ).encode("utf-8")
            await _write_frame(done_frame)
            return response

    return handle


def make_preflight_handler(
    allowed: set[str],
    *,
    trust_referer_as_origin: bool = False,
):
    async def handle(request: web.Request) -> web.Response:
        return preflight_response(
            origin=extract_origin(
                request, trust_referer_as_origin=trust_referer_as_origin
            ),
            allowed=allowed,
            same_origin_host=request.host,
        )

    return handle


def make_me_handler(deps: ChatDeps):
    """GET /me — token-authed quota probe.

    Same Origin / IP-guard / bearer auth as the chat handler so an
    attacker can't use it to enumerate tokens any more cheaply than
    /chat itself. Skips the per-token concurrency lock and audit
    logging because legitimate clients call this on every chat-page
    load and refresh; auditing it would flood the table.
    """

    async def handle(request: web.Request) -> web.Response:
        origin = extract_origin(
            request, trust_referer_as_origin=deps.trust_referer_as_origin
        )
        allowed = deps.allowed_origins
        same_host = request.host

        if not is_origin_allowed(origin, allowed, same_origin_host=same_host):
            return json_response(
                {"error": "forbidden_origin"},
                status=403,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)

        blocked, retry_after = await deps.ip_guard.is_blocked(ip)
        if blocked:
            return json_response(
                {"error": "ip_blocked", "retry_after": retry_after},
                status=429,
                origin=origin,
                allowed_origins=allowed,
                extra_headers={"Retry-After": str(retry_after)},
                same_origin_host=same_host,
            )

        presented = extract_bearer(request)
        if not presented:
            await deps.ip_guard.record_failure(ip)
            return json_response(
                {"error": "unauthorized"},
                status=401,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )
        token = await deps.storage.get_token_by_hash(hash_token(presented))
        now_ts = int(time.time())
        if token is None or token.revoked_at is not None or _is_expired(token, now_ts):
            if token is None:
                await deps.ip_guard.record_failure(ip)
            return json_response(
                {"error": "unauthorized"},
                status=401,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )
        await deps.ip_guard.reset(ip)

        today = date.today()
        today_count = await deps.storage.get_today_usage(token.name, day=today)
        remaining = max(0, token.daily_quota - today_count)
        return json_response(
            {
                "name": token.name,
                "remaining": remaining,
                "daily_quota": token.daily_quota,
            },
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=same_host,
            extra_headers={"Cache-Control": "no-store"},
        )

    return handle


__all__ = [
    "ChatDeps",
    "make_chat_handler",
    "make_chat_stream_handler",
    "make_me_handler",
    "make_preflight_handler",
    "build_cors_headers",
]
