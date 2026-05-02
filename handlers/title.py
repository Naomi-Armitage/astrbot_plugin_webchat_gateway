"""Title HTTP handler — LLM-generated short Chinese session titles.

Pipeline mirrors /chat (origin → IP guard → auth → quota → LLM call →
audit + debit) but skips the per-token concurrency lock: titles and chat
are both user-driven, racing them is fine, and the daily quota already
caps abuse.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from aiohttp import web

from astrbot.api import logger

from ..core.audit import AuditLogger
from ..core.ip_guard import IpGuard
from ..core.llm_bridge import LlmBridge
from ..storage.base import AbstractStorage
from .common import extract_origin, gate_request, json_response


@dataclass
class TitleDeps:
    storage: AbstractStorage
    audit: AuditLogger
    ip_guard: IpGuard
    llm_bridge: LlmBridge
    allowed_origins: set[str]
    max_message_length: int
    auto_title_enabled: bool
    trust_forwarded_for: bool
    trust_referer_as_origin: bool = False
    allow_missing_origin: bool = False


def _parse_payload(
    payload: object, *, max_message_length: int
) -> tuple[str, list[dict]] | None:
    if not isinstance(payload, dict):
        return None
    session_id = str(
        payload.get("session_id") or payload.get("sessionId") or ""
    ).strip()
    if not session_id:
        return None
    raw_conv = payload.get("conversation")
    if not isinstance(raw_conv, list) or not raw_conv:
        return None
    turns: list[dict] = []
    total = 0
    for item in raw_conv:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in ("user", "bot"):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        # Cap each turn so a malicious payload can't bypass max_message_length
        # by stuffing one giant turn; bound aggregate too — even legitimate
        # clients shouldn't need >32 turns to title a session.
        if len(text) > max_message_length:
            text = text[:max_message_length]
        total += len(text)
        turns.append({"role": role, "text": text})
        if len(turns) >= 32 or total > max_message_length * 4:
            break
    if not turns:
        return None
    return session_id[:128], turns


def make_title_handler(deps: TitleDeps):
    async def handle(request: web.Request) -> web.Response:
        if not deps.auto_title_enabled:
            # Skip auth: no point spending a quota slot when titling is off.
            return json_response(
                {"error": "title_disabled"},
                status=503,
                origin=extract_origin(
                    request, trust_referer_as_origin=deps.trust_referer_as_origin
                ),
                allowed_origins=deps.allowed_origins,
                same_origin_host=request.host,
            )

        gated = await gate_request(request, deps)
        if isinstance(gated, web.Response):
            return gated

        def err(payload: dict, status: int) -> web.Response:
            return json_response(
                payload,
                status=status,
                origin=gated.origin,
                allowed_origins=gated.allowed,
                same_origin_host=gated.same_host,
            )

        try:
            payload = await request.json()
        except web.HTTPRequestEntityTooLarge:
            return err({"error": "payload_too_large"}, 413)
        except json.JSONDecodeError:
            return err({"error": "invalid_json"}, 400)
        except Exception:
            logger.exception("[WebChatGateway] unexpected JSON parse error")
            return err({"error": "invalid_json"}, 400)

        parsed = _parse_payload(payload, max_message_length=deps.max_message_length)
        if parsed is None:
            return err({"error": "bad_request"}, 400)
        session_id, conversation = parsed

        token = gated.token
        today = date.today()
        today_count = await deps.storage.get_today_usage(token.name, day=today)
        if today_count >= token.daily_quota:
            await deps.audit.write(
                "quota_exceeded",
                name=token.name,
                ip=gated.ip,
                detail={
                    "today_count": today_count,
                    "quota": token.daily_quota,
                    "endpoint": "title",
                },
            )
            return err(
                {
                    "error": "quota_exceeded",
                    "remaining": 0,
                    "daily_quota": token.daily_quota,
                },
                429,
            )

        try:
            title = await deps.llm_bridge.generate_title(
                token_name=token.name,
                session_id=session_id,
                conversation=conversation,
            )
        except RuntimeError as exc:
            code = str(exc)
            if code != "llm_timeout":
                logger.exception("[WebChatGateway] title generation failed")
            await deps.audit.write(
                "title_failed",
                name=token.name,
                ip=gated.ip,
                detail={"error": code[:200]},
            )
            return err({"error": "llm_call_failed"}, 503)
        except Exception as exc:
            logger.exception("[WebChatGateway] title generation failed")
            await deps.audit.write(
                "title_failed",
                name=token.name,
                ip=gated.ip,
                detail={"error": str(exc)[:200]},
            )
            return err({"error": "llm_call_failed"}, 503)

        new_count = await deps.storage.increment_daily_usage(token.name, day=today)
        remaining = max(0, token.daily_quota - new_count)

        await deps.audit.write(
            "title_generated",
            name=token.name,
            ip=gated.ip,
            detail={
                "title_len": len(title),
                "turns": len(conversation),
                "remaining": remaining,
            },
        )
        return json_response(
            {
                "title": title,
                "remaining": remaining,
                "daily_quota": token.daily_quota,
            },
            origin=gated.origin,
            allowed_origins=gated.allowed,
            same_origin_host=gated.same_host,
        )

    return handle


__all__ = ["TitleDeps", "make_title_handler"]
