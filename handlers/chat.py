"""Chat HTTP handler — main 9-step defense pipeline."""

from __future__ import annotations

import json
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
from ..storage.base import AbstractStorage
from .common import (
    build_cors_headers,
    client_ip,
    extract_origin,
    is_origin_allowed,
    json_response,
    preflight_response,
)


@dataclass
class ChatDeps:
    storage: AbstractStorage
    audit: AuditLogger
    ip_guard: IpGuard
    concurrency: PerTokenConcurrency
    llm_bridge: LlmBridge
    allowed_origins: set[str]
    max_message_length: int
    trust_forwarded_for: bool


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


def make_chat_handler(deps: ChatDeps):
    async def handle(request: web.Request) -> web.Response:
        origin = extract_origin(request)
        allowed = deps.allowed_origins

        # 1. Origin allow-list
        if not is_origin_allowed(origin, allowed):
            return json_response(
                {"error": "forbidden_origin"},
                status=403,
                origin=origin,
                allowed_origins=allowed,
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
            )
        token = await deps.storage.get_token_by_hash(hash_token(presented))
        if token is None or token.revoked_at is not None:
            await deps.ip_guard.record_failure(ip)
            await deps.audit.write(
                "auth_fail",
                ip=ip,
                detail={"reason": "revoked" if token else "invalid"},
            )
            return json_response(
                {"error": "unauthorized"},
                status=401,
                origin=origin,
                allowed_origins=allowed,
            )
        # Valid auth — clear failures for this IP.
        await deps.ip_guard.reset(ip)

        # 4. Concurrency lock
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
                )

            # 5. Daily quota check (read-then-increment is racy across processes,
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
                )

            # 6. Parse + length check
            try:
                payload = await request.json()
            except json.JSONDecodeError:
                return json_response(
                    {"error": "invalid_json"},
                    status=400,
                    origin=origin,
                    allowed_origins=allowed,
                )
            except Exception:
                logger.exception("[WebChatGateway] unexpected JSON parse error")
                return json_response(
                    {"error": "invalid_json"},
                    status=400,
                    origin=origin,
                    allowed_origins=allowed,
                )
            data = _parse_payload(payload)
            if data is None:
                return json_response(
                    {"error": "invalid_payload"},
                    status=400,
                    origin=origin,
                    allowed_origins=allowed,
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
                )

            # 7. LLM call
            try:
                reply = await deps.llm_bridge.generate_reply(
                    session_id=data.session_id,
                    username=data.username,
                    message=data.message,
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
                    {"error": "llm_call_failed", "detail": str(exc)[:200]},
                    status=500,
                    origin=origin,
                    allowed_origins=allowed,
                )

            # 8. Increment usage (atomic)
            new_count = await deps.storage.increment_daily_usage(token.name, day=today)
            remaining = max(0, token.daily_quota - new_count)

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
            )

    return handle


def make_preflight_handler(allowed: set[str]):
    async def handle(request: web.Request) -> web.Response:
        return preflight_response(origin=extract_origin(request), allowed=allowed)

    return handle


__all__ = [
    "ChatDeps",
    "make_chat_handler",
    "make_preflight_handler",
    "build_cors_headers",
]
