"""Audit logger that writes through to the storage backend.

Canonical event vocabulary
--------------------------
Token lifecycle (admin):
    issue          — admin issued a new token; detail: {daily_quota, note_len}
    revoke         — admin revoked an existing token; detail: {revoked: true}
    revoke_miss    — admin tried to revoke a non-existent or already-revoked
                     token; detail: {revoked: false}

Admin reads (audit-trail-only, mirrors lifecycle vocabulary):
    admin_list      — admin listed tokens (HTTP or `/webchat list`);
                      detail: {include_revoked, count}
    admin_stats     — admin read per-token stats; detail: {days}
    admin_audit     — admin pulled the audit log; detail: {limit, count}
    admin_auth_fail — admin auth attempt failed at the gate;
                      detail: {reason: no_token|invalid_key|admin_disabled|ip_blocked,
                               retry_after?: int}

Chat path (per request):
    auth_fail       — bearer missing/invalid/revoked; detail: {reason}
    concurrent_block — per-token concurrency lock rejected the request
    quota_exceeded  — daily quota hit; detail: {today_count, quota}
    llm_timeout     — provider call exceeded llm_timeout_seconds; detail: {msg_len}
    chat_error      — provider call failed; detail: {error: <truncated>}
    chat_ok         — request completed; detail: {msg_len, reply_len, remaining}

Detail values are JSON-serialized strings, truncated to 1024 chars.
"""

from __future__ import annotations

import json
import time
from typing import Any

from astrbot.api import logger

from ..storage.base import AbstractStorage


class AuditLogger:
    def __init__(self, storage: AbstractStorage) -> None:
        self._storage = storage

    async def write(
        self,
        event: str,
        *,
        name: str | None = None,
        ip: str | None = None,
        detail: Any = None,
    ) -> None:
        if isinstance(detail, str):
            detail_str = detail
        elif detail is None:
            detail_str = ""
        else:
            try:
                detail_str = json.dumps(detail, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                detail_str = str(detail)
        if len(detail_str) > 1024:
            detail_str = detail_str[:1024]
        try:
            await self._storage.write_audit(
                ts=int(time.time()),
                name=name,
                ip=ip,
                event=event,
                detail=detail_str,
            )
        except Exception:
            logger.exception("[WebChatGateway] audit write failed event=%s", event)
