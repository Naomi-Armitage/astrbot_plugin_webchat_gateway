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
from ..core.stream_registry import STREAM_ID_PATTERN, StreamRegistry
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
    registry: StreamRegistry
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
                if str(exc) == "empty_reply":
                    # Upstream returned finish_reason=stop with zero tokens.
                    # Surface the specific code so the frontend can render
                    # actionable copy ("model produced nothing — try again or
                    # rephrase") rather than the generic llm_call_failed.
                    await deps.audit.write(
                        "chat_empty_reply",
                        name=token.name,
                        ip=ip,
                        detail={"msg_len": len(data.message)},
                    )
                    return json_response(
                        {"error": "empty_reply"},
                        status=502,
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
    # SSE variant of /chat. Stream lifecycle is owned by StreamRegistry —
    # the lock is held by the registry across the LLM stream + persist, the
    # buffer carries chunks for resume/peer-attach, and `close_*` releases
    # the lock + emits the chat-sync `stream_ended` event.
    #
    # Key wire contract additions vs. the v1 stream:
    #   * First data frame is `{"stream_id": "..."}` so the client can
    #     persist for resume.
    #   * Each chunk frame carries `seq` (registry-assigned, monotonic).
    #   * `done`/`error` frames carry their own `seq` (last_chunk_seq + 1)
    #     and `done` carries `incomplete: bool`.
    #
    # Client-disconnect semantics: a write failure on this handler ONLY
    # tears down the live SSE response. The registry's driver task — i.e.
    # the LLM iteration itself — keeps running so the buffer fills, peer
    # subscribers continue receiving chunks, and the stream persists with
    # `incomplete=False` (full reply, just no live viewer for one POSTer).

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

        # Step 5: open stream via the registry (acquires per-token lock,
        # creates buffer entry, emits chat-sync `stream_started`, audits).
        handle_obj = await deps.registry.open(
            token_name=token.name,
            session_id=data.session_id,
            user_text=data.message,
        )
        if handle_obj is None:
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

        # Step 6: quota check — under the registry lock, before any SSE
        # bytes hit the wire so we can still return a JSON 429.
        today = date.today()
        try:
            today_count = await deps.storage.get_today_usage(token.name, day=today)
        except Exception:
            logger.exception("[WebChatGateway] get_today_usage failed")
            today_count = 0
        if today_count >= token.daily_quota:
            await deps.registry.close_failed(handle_obj, error_code="quota_exceeded")
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

        # Step 7: open SSE response.
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
        # First data frame announces the stream_id so the client can
        # persist it and reconnect via /resume on transient failure.
        await response.write(
            (
                "data: "
                + json.dumps(
                    {"stream_id": handle_obj.stream_id}, ensure_ascii=False
                )
                + "\n\n"
            ).encode("utf-8")
        )

        collected: list[str] = []
        client_gone = False
        terminal_emitted = False  # registry.close_* called → don't double-close

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
        # Persistent pull task across heartbeats. Reusing the same
        # task is the only correct way to combine a heartbeat timeout
        # with `__anext__`: `wait_for(shield(...))` on a *fresh*
        # `__anext__()` each iteration would leave the prior call
        # still running in the background, and the next iteration's
        # `__anext__()` would crash with "asynchronous generator is
        # already running". Here we drive a single in-flight Task and
        # only allocate a new one after the previous chunk has been
        # consumed (or the previous task has terminated).
        pull_task: asyncio.Task | None = None

        async def _drain_pull(task: asyncio.Task | None) -> None:
            # Cancel and await the pull task so the inner `__anext__`
            # finishes (cancelled or otherwise) before we touch the
            # generator. Calling `stream_iter.aclose()` while a pull
            # is in flight races with that pull and can raise
            # "aclose() got called when the generator was already
            # running" — drain first.
            if task is None or task.done():
                return
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, StopAsyncIteration, Exception):
                pass

        try:
            try:
                while True:
                    if pull_task is None:
                        pull_task = asyncio.ensure_future(stream_iter.__anext__())
                    try:
                        # shield protects the persistent task from being
                        # cancelled when wait_for times out — the task
                        # keeps running in the background and we re-await
                        # it on the next iteration.
                        chunk = await asyncio.wait_for(
                            asyncio.shield(pull_task),
                            timeout=_HEARTBEAT_INTERVAL,
                        )
                    except asyncio.TimeoutError:
                        # No chunk in 20s — heartbeat keeps the connection
                        # alive across idle proxies and surfaces a peer
                        # disconnect on the next write. The pull_task is
                        # still in flight; we'll await it again next loop.
                        if not client_gone:
                            if not await _write_frame(b": keepalive\n\n"):
                                client_gone = True
                        # Either way, keep iterating — the LLM stream
                        # continues feeding the buffer for peer subscribers
                        # and partial-on-abort persistence even if the
                        # POSTer's transport has dropped.
                        continue
                    except StopAsyncIteration:
                        # The persistent task completed with end-of-stream.
                        pull_task = None
                        break

                    # Chunk consumed → task is done; the next iteration
                    # creates a fresh task for the next pull.
                    pull_task = None

                    collected.append(chunk)
                    seq = await deps.registry.append(handle_obj, chunk)
                    if not client_gone:
                        frame = (
                            "data: "
                            + json.dumps(
                                {"chunk": chunk, "seq": seq}, ensure_ascii=False
                            )
                            + "\n\n"
                        ).encode("utf-8")
                        if not await _write_frame(frame):
                            client_gone = True
                            # Do NOT break: keep pulling so the buffer
                            # fills, peers see the rest, and the stream
                            # persists with the full reply.
            except RuntimeError as exc:
                code = str(exc)
                full_text = "".join(collected)
                last_seq = handle_obj.next_seq  # next unused seq → terminal frame uses it
                if code == "llm_timeout":
                    if collected:
                        # Aborted with content — persist as incomplete.
                        new_count = await deps.storage.increment_daily_usage(
                            token.name, day=today
                        )
                        remaining = max(0, token.daily_quota - new_count)
                        await deps.registry.close_incomplete(
                            handle_obj,
                            user_text=data.message,
                            partial_text=full_text,
                            remaining=remaining,
                            daily_quota=token.daily_quota,
                            reason="llm_timeout",
                        )
                        terminal_emitted = True
                        if not client_gone:
                            await _write_frame(
                                (
                                    "data: "
                                    + json.dumps(
                                        {
                                            "done": True,
                                            "seq": last_seq,
                                            "remaining": remaining,
                                            "daily_quota": token.daily_quota,
                                            "incomplete": True,
                                        },
                                        ensure_ascii=False,
                                    )
                                    + "\n\n"
                                ).encode("utf-8")
                            )
                    else:
                        # Zero chunks before timeout → no persist.
                        await deps.registry.close_failed(
                            handle_obj, error_code="llm_timeout"
                        )
                        terminal_emitted = True
                        if not client_gone:
                            await _write_frame(
                                (
                                    "data: "
                                    + json.dumps(
                                        {"error": "llm_timeout", "seq": last_seq},
                                        ensure_ascii=False,
                                    )
                                    + "\n\n"
                                ).encode("utf-8")
                            )
                else:
                    # `empty_reply` is a soft outcome (upstream returned
                    # finish_reason=stop with zero tokens) — by definition no
                    # chunks were yielded, so the close path is always
                    # close_failed, never close_incomplete. The error frame
                    # carries the specific code so the frontend can render
                    # actionable copy instead of generic "请求失败".
                    if code == "empty_reply":
                        await deps.registry.close_failed(
                            handle_obj, error_code="empty_reply"
                        )
                        terminal_emitted = True
                        if not client_gone:
                            await _write_frame(
                                (
                                    "data: "
                                    + json.dumps(
                                        {"error": "empty_reply", "seq": last_seq},
                                        ensure_ascii=False,
                                    )
                                    + "\n\n"
                                ).encode("utf-8")
                            )
                        await _drain_pull(pull_task)
                        pull_task = None
                        await stream_iter.aclose()
                        return response
                    logger.exception("[WebChatGateway] LLM stream failed")
                    if collected:
                        new_count = await deps.storage.increment_daily_usage(
                            token.name, day=today
                        )
                        remaining = max(0, token.daily_quota - new_count)
                        await deps.registry.close_incomplete(
                            handle_obj,
                            user_text=data.message,
                            partial_text=full_text,
                            remaining=remaining,
                            daily_quota=token.daily_quota,
                            reason="llm_call_failed",
                        )
                        terminal_emitted = True
                        if not client_gone:
                            await _write_frame(
                                (
                                    "data: "
                                    + json.dumps(
                                        {
                                            "done": True,
                                            "seq": last_seq,
                                            "remaining": remaining,
                                            "daily_quota": token.daily_quota,
                                            "incomplete": True,
                                        },
                                        ensure_ascii=False,
                                    )
                                    + "\n\n"
                                ).encode("utf-8")
                            )
                    else:
                        await deps.registry.close_failed(
                            handle_obj, error_code="llm_call_failed"
                        )
                        terminal_emitted = True
                        if not client_gone:
                            await _write_frame(
                                (
                                    "data: "
                                    + json.dumps(
                                        {"error": "llm_call_failed", "seq": last_seq},
                                        ensure_ascii=False,
                                    )
                                    + "\n\n"
                                ).encode("utf-8")
                            )
                # Drain pull then aclose the upstream generator so its
                # `finally` runs and the provider connection is released.
                await _drain_pull(pull_task)
                pull_task = None
                await stream_iter.aclose()
                return response
            except asyncio.CancelledError:
                # Server-initiated shutdown. Mark the buffer as failed so
                # the lock releases; subscribers see the error frame too.
                await _drain_pull(pull_task)
                pull_task = None
                try:
                    await stream_iter.aclose()
                except Exception:
                    pass
                if not terminal_emitted:
                    if collected:
                        # Best-effort partial persist on shutdown.
                        try:
                            new_count = await deps.storage.increment_daily_usage(
                                token.name, day=today
                            )
                            remaining = max(0, token.daily_quota - new_count)
                        except Exception:
                            remaining = 0
                        await deps.registry.close_incomplete(
                            handle_obj,
                            user_text=data.message,
                            partial_text="".join(collected),
                            remaining=remaining,
                            daily_quota=token.daily_quota,
                            reason="cancelled",
                        )
                    else:
                        await deps.registry.close_failed(
                            handle_obj, error_code="cancelled"
                        )
                    terminal_emitted = True
                raise
            except Exception as exc:
                logger.exception("[WebChatGateway] LLM stream failed")
                last_seq = handle_obj.next_seq
                if collected:
                    new_count = await deps.storage.increment_daily_usage(
                        token.name, day=today
                    )
                    remaining = max(0, token.daily_quota - new_count)
                    await deps.registry.close_incomplete(
                        handle_obj,
                        user_text=data.message,
                        partial_text="".join(collected),
                        remaining=remaining,
                        daily_quota=token.daily_quota,
                        reason="internal_error",
                    )
                    terminal_emitted = True
                    if not client_gone:
                        await _write_frame(
                            (
                                "data: "
                                + json.dumps(
                                    {
                                        "done": True,
                                        "seq": last_seq,
                                        "remaining": remaining,
                                        "daily_quota": token.daily_quota,
                                        "incomplete": True,
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n\n"
                            ).encode("utf-8")
                        )
                else:
                    await deps.registry.close_failed(
                        handle_obj, error_code="internal_error"
                    )
                    terminal_emitted = True
                    if not client_gone:
                        await _write_frame(
                            (
                                "data: "
                                + json.dumps(
                                    {"error": "internal_error", "seq": last_seq},
                                    ensure_ascii=False,
                                )
                                + "\n\n"
                            ).encode("utf-8")
                        )
                await _drain_pull(pull_task)
                pull_task = None
                try:
                    await stream_iter.aclose()
                except Exception:
                    pass
                logger.debug(
                    "[WebChatGateway] stream handler exception suppressed: %s",
                    str(exc)[:200],
                )
                return response

            # Successful stream end. Persist + audit + done frame.
            full_reply = "".join(collected)
            last_seq = handle_obj.next_seq
            if not full_reply:
                # Provider returned end-of-stream without any chunks. No
                # persist, no quota — but distinct from `llm_timeout` for
                # observability.
                await deps.registry.close_failed(
                    handle_obj, error_code="empty_reply"
                )
                terminal_emitted = True
                if not client_gone:
                    await _write_frame(
                        (
                            "data: "
                            + json.dumps(
                                {"error": "empty_reply", "seq": last_seq},
                                ensure_ascii=False,
                            )
                            + "\n\n"
                        ).encode("utf-8")
                    )
                return response

            try:
                new_count = await deps.storage.increment_daily_usage(
                    token.name, day=today
                )
            except Exception:
                logger.exception(
                    "[WebChatGateway] increment_daily_usage failed; persisting anyway"
                )
                new_count = today_count + 1
            remaining = max(0, token.daily_quota - new_count)
            await deps.registry.close_ok(
                handle_obj,
                user_text=data.message,
                full_text=full_reply,
                remaining=remaining,
                daily_quota=token.daily_quota,
            )
            terminal_emitted = True
            if not client_gone:
                done_frame = (
                    "data: "
                    + json.dumps(
                        {
                            "done": True,
                            "seq": last_seq,
                            "remaining": remaining,
                            "daily_quota": token.daily_quota,
                            "incomplete": False,
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                ).encode("utf-8")
                await _write_frame(done_frame)
            return response
        finally:
            # Belt-and-suspenders: if any path above forgot to call a
            # registry.close_*, the lock would stay held indefinitely.
            # close_failed is idempotent — the registry no-ops on an
            # already-closed handle (see core/stream_registry.py).
            #
            # Reaching this branch means a code path returned without
            # setting terminal_emitted — almost always a refactor bug.
            # Log it loudly (with a stack via logger.exception) so the
            # operator can find it; otherwise the buffer transitions to
            # closed_failed("internal_error") silently and any peer
            # device that resumes onto it sees a generic error frame
            # with no server-side trace.
            if not terminal_emitted:
                logger.warning(
                    "[WebChatGateway] stream handler missed terminal close "
                    "(stream_id=%s, collected=%d) — forcing close_failed",
                    handle_obj.stream_id,
                    len("".join(collected)),
                    stack_info=True,
                )
                try:
                    await deps.registry.close_failed(
                        handle_obj, error_code="internal_error"
                    )
                except Exception:
                    logger.exception(
                        "[WebChatGateway] terminal close_failed in finally raised"
                    )

    return handle


def make_chat_stream_resume_handler(deps: ChatDeps):
    """GET /chat/stream/{stream_id}/resume — replay missed chunks then
    attach as a live subscriber.

    Auth gate identical to the POST handler (origin/IP/bearer/revoked/expired).
    Cross-token resume returns 404 (NOT 403) so attackers can't enumerate
    stream existence across tokens. Cross-stream-id mismatch on the buffer
    is also 404. The resume call does NOT take the per-token lock — multiple
    subscribers attach freely; only the original POST holds the lock.
    """

    async def handle(request: web.Request) -> web.StreamResponse:
        gated = await gate_request(request, deps)
        if isinstance(gated, web.Response):
            return gated
        token = gated.token
        ip = gated.ip
        origin = gated.origin
        allowed = gated.allowed
        same_host = gated.same_host

        stream_id = (request.match_info.get("stream_id") or "").strip()
        if not stream_id or not STREAM_ID_PATTERN.match(stream_id):
            return json_response(
                {"error": "invalid_stream_id"},
                status=400,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

        after_seq_raw = request.query.get("after_seq")
        try:
            after_seq = int(after_seq_raw) if after_seq_raw is not None else -1
        except (TypeError, ValueError):
            return json_response(
                {"error": "invalid_after_seq"},
                status=400,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )
        # Allow -1 sentinel; clamp negative-but-not-(-1) to -1 so the client
        # is forgiving of edge cases without exposing a parser quirk.
        if after_seq < -1:
            after_seq = -1

        snapshot = await deps.registry.fetch(
            stream_id=stream_id, token_name=token.name
        )
        if snapshot is None:
            # `fetch` enforces the cross-token-returns-404 invariant: a
            # missing entry AND a wrong-owner entry both come back as
            # None, so there's no timing leak between the two cases.
            return json_response(
                {"error": "stream_not_found"},
                status=404,
                origin=origin,
                allowed_origins=allowed,
                same_origin_host=same_host,
            )

        # Heuristic for the `peer` audit flag: a STREAMING/PENDING stream
        # with a registered driver lock means another connection is the
        # POSTer; this resume call therefore comes from a peer (or the
        # same device after a network blip — indistinguishable, leans
        # peer-true). A CLOSED_* snapshot means the original POST has
        # already finished and any resumer is "self-after-the-fact".
        is_live = snapshot.state in ("pending", "streaming")
        peer = bool(is_live)
        await deps.audit.write(
            "chat_stream_resumed",
            name=token.name,
            ip=ip,
            detail={
                "stream_id": stream_id,
                "after_seq": after_seq,
                "peer": peer,
            },
        )

        cors = build_cors_headers(origin, allowed, same_origin_host=same_host)
        response = web.StreamResponse(
            status=200,
            headers={
                **cors,
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        await response.write(b": ready\n\n")

        async def _write_frame(frame: bytes) -> bool:
            if request.transport is None or request.transport.is_closing():
                return False
            try:
                await response.write(frame)
            except (ConnectionResetError, ConnectionError):
                return False
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[WebChatGateway] SSE resume write failed")
                return False
            return True

        # Replay all chunks with seq > after_seq. The snapshot from
        # registry.fetch contains the entire chunk list; filter on the
        # way out so we don't double-emit anything the client already
        # consumed.
        last_replayed = after_seq
        for seq, text in snapshot.chunks:
            if seq <= after_seq:
                continue
            ok = await _write_frame(
                (
                    "data: "
                    + json.dumps({"chunk": text, "seq": seq}, ensure_ascii=False)
                    + "\n\n"
                ).encode("utf-8")
            )
            if not ok:
                return response
            if seq > last_replayed:
                last_replayed = seq

        # If the snapshot was already terminal, emit the appropriate done
        # / error frame and return — no point attaching to a closed buffer.
        if snapshot.state in ("closed_ok", "closed_incomplete"):
            final = snapshot.final or {}
            last_seq_terminal = last_replayed + 1
            done_payload = {
                "done": True,
                "seq": last_seq_terminal,
                "remaining": final.get("remaining", 0),
                "daily_quota": final.get("daily_quota", 0),
                "incomplete": snapshot.state == "closed_incomplete",
            }
            await _write_frame(
                ("data: " + json.dumps(done_payload, ensure_ascii=False) + "\n\n").encode("utf-8")
            )
            return response
        if snapshot.state == "closed_failed":
            final = snapshot.final or {}
            last_seq_terminal = last_replayed + 1
            err_payload = {
                "error": final.get("error", "internal_error"),
                "seq": last_seq_terminal,
            }
            await _write_frame(
                ("data: " + json.dumps(err_payload, ensure_ascii=False) + "\n\n").encode("utf-8")
            )
            return response

        # Live subscription: yields (seq, text) tuples of chunks newer
        # than `last_replayed`. The iterator returns (StopAsyncIteration)
        # when the buffer reaches a terminal state OR is evicted; the
        # closing frame itself is read separately via registry.fetch
        # after the loop. Heartbeats keep the connection alive via the
        # same persistent-task pattern used by the POST handler.
        sub = deps.registry.buffer.iter_subscribe(stream_id, last_replayed)
        sub_iter = sub.__aiter__()
        pull_task: asyncio.Task | None = None

        async def _drain_pull(task: asyncio.Task | None) -> None:
            if task is None or task.done():
                return
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, StopAsyncIteration, Exception):
                pass

        try:
            while True:
                if pull_task is None:
                    pull_task = asyncio.ensure_future(sub_iter.__anext__())
                try:
                    event = await asyncio.wait_for(
                        asyncio.shield(pull_task), timeout=_HEARTBEAT_INTERVAL
                    )
                except asyncio.TimeoutError:
                    if not await _write_frame(b": keepalive\n\n"):
                        await _drain_pull(pull_task)
                        pull_task = None
                        return response
                    continue
                except StopAsyncIteration:
                    pull_task = None
                    break

                pull_task = None
                seq, text = event
                ok = await _write_frame(
                    (
                        "data: "
                        + json.dumps(
                            {"chunk": text, "seq": seq}, ensure_ascii=False
                        )
                        + "\n\n"
                    ).encode("utf-8")
                )
                if not ok:
                    return response
                if seq > last_replayed:
                    last_replayed = seq

            # Iterator returned → buffer is in a terminal state OR has
            # been evicted. Re-fetch via the registry to read the
            # terminal payload and decide which closing frame to emit.
            terminal_snapshot = await deps.registry.fetch(
                stream_id=stream_id, token_name=token.name
            )
            if terminal_snapshot is None:
                # Evicted before we could read the close. Surface as
                # stream_not_found so the client falls back to history.
                await _write_frame(
                    (
                        "data: "
                        + json.dumps(
                            {
                                "error": "stream_not_found",
                                "seq": last_replayed + 1,
                            },
                            ensure_ascii=False,
                        )
                        + "\n\n"
                    ).encode("utf-8")
                )
                return response
            # In rare races, more chunks may have appeared between the
            # iterator returning and this fetch (close() sets the
            # terminal Event, but the buffer can have late chunks queued
            # ahead of close in the same coroutine — defensive only).
            # The snapshot from registry.fetch contains all chunks; only
            # emit those past last_replayed.
            for seq, text in terminal_snapshot.chunks:
                if seq <= last_replayed:
                    continue
                ok = await _write_frame(
                    (
                        "data: "
                        + json.dumps(
                            {"chunk": text, "seq": seq}, ensure_ascii=False
                        )
                        + "\n\n"
                    ).encode("utf-8")
                )
                if not ok:
                    return response
                if seq > last_replayed:
                    last_replayed = seq
            final = terminal_snapshot.final or {}
            last_seq_terminal = last_replayed + 1
            if terminal_snapshot.state in ("closed_ok", "closed_incomplete"):
                done_payload = {
                    "done": True,
                    "seq": last_seq_terminal,
                    "remaining": final.get("remaining", 0),
                    "daily_quota": final.get("daily_quota", 0),
                    "incomplete": terminal_snapshot.state == "closed_incomplete",
                }
                await _write_frame(
                    (
                        "data: "
                        + json.dumps(done_payload, ensure_ascii=False)
                        + "\n\n"
                    ).encode("utf-8")
                )
                return response
            if terminal_snapshot.state == "closed_failed":
                err_payload = {
                    "error": final.get("error", "internal_error"),
                    "seq": last_seq_terminal,
                }
                await _write_frame(
                    (
                        "data: "
                        + json.dumps(err_payload, ensure_ascii=False)
                        + "\n\n"
                    ).encode("utf-8")
                )
                return response
            # Should be unreachable: iter_subscribe only returns on
            # terminal/evicted, and the snapshot is non-None here. Defensive
            # error frame keeps the wire contract clean.
            await _write_frame(
                (
                    "data: "
                    + json.dumps(
                        {"error": "internal_error", "seq": last_seq_terminal},
                        ensure_ascii=False,
                    )
                    + "\n\n"
                ).encode("utf-8")
            )
            return response
        except asyncio.CancelledError:
            await _drain_pull(pull_task)
            pull_task = None
            try:
                await sub_iter.aclose()
            except Exception:
                pass
            raise
        finally:
            await _drain_pull(pull_task)
            try:
                await sub_iter.aclose()
            except Exception:
                pass

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
    "make_chat_stream_resume_handler",
    "make_me_handler",
    "make_preflight_handler",
    "build_cors_headers",
]
