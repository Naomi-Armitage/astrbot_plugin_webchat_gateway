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
    admin_settings_update — admin saved one or more whitelist config
                      fields via /admin/settings; detail: {keys: [str]}.
                      VALUES are deliberately omitted — origin lists,
                      welcome messages, and similar fields may contain
                      operator-typed text or non-public infrastructure
                      hostnames that shouldn't enter the audit log.
    admin_restart   — admin triggered a service restart via the panel's
                      "重启服务" action; detail: {phase: "requested"}.
                      Emitted BEFORE the actual _stop/_start cycle so
                      the row survives the lifecycle bounce.
    admin_logs_view — admin subscribed to /admin/logs/stream (one row
                      per SSE subscription, not per GET poll); detail:
                      {mode, since, level, grep}. Lets operators see
                      WHO opened the live log viewer without drowning
                      audit_log in poll rows.
    admin_auth_fail — admin auth attempt failed at the gate;
                      detail: {reason: no_token|invalid_key|admin_disabled|ip_blocked,
                               retry_after?: int}

Chat path (per request):
    auth_fail       — bearer missing/invalid/revoked; detail: {reason}.
                      `/files/{id}` adds `endpoint: "files"` and uses
                      `reason: bad_cookie` for cookies that were
                      presented but didn't verify (sig mismatch / exp /
                      logout-invalidated / token rotated / revoked).
                      Only `no_token` (no credential at all) increments
                      IP-guard counters.
    concurrent_block — per-token concurrency lock rejected the request
    quota_exceeded  — daily quota hit; detail: {today_count, quota}
    llm_timeout     — provider call exceeded llm_timeout_seconds; detail: {msg_len}
    chat_error      — provider call failed; detail: {error: <truncated>}
    chat_ok         — request completed; detail: {msg_len, reply_len, remaining}
    image_generated — /image command produced an attachment; detail:
                      {prompt_len, model, size, file_id}.
    image_failed    — /image command raised; detail:
                      {code, prompt_len}. ``code`` matches the
                      ImageBridgeError taxonomy (image_disabled /
                      image_timeout / image_call_failed /
                      empty_image_reply).
    file_release_failed — `commit_attachments_or_release` hit the double-
                      failure path: mark_files_committed raised AND the
                      compensating release also raised. Detail:
                      {label, row_count, file_ids[≤20]}. Surface for
                      operator: any rows the commit partially flipped
                      to committed=1 are now permanently outside the
                      orphan-GC sweep and occupy the user's quota
                      until manually cleaned up. Rare — usually
                      requires the storage backend (R2 / local FS) to
                      be fully unavailable AND the DB write to fail at
                      the same time.

Detail values are JSON-serialized strings, truncated to 1024 chars.
"""

from __future__ import annotations

import json
import time
from typing import Any

from astrbot.api import logger

from ..storage.base import AUDIT_DETAIL_MAX, AbstractStorage


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
        # Defence in depth: storage.write_audit also caps to
        # AUDIT_DETAIL_MAX, but truncating here avoids serialising a
        # huge JSON blob into the network/storage path only to have
        # the backend slice off the tail.
        if len(detail_str) > AUDIT_DETAIL_MAX:
            detail_str = detail_str[:AUDIT_DETAIL_MAX]
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
