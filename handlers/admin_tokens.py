"""TokenService — shared business logic for HTTP admin endpoints and bot commands.

Exposes the same operations to both the aiohttp admin handlers and the
`/webchat ...` AstrBot command group, so there is one source of truth.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date

from aiohttp import web

from ..core.audit import AuditLogger
from ..core.auth import (
    extract_bearer,
    extract_session_cookie,
    generate_token,
    has_admin_credentials,
    hash_token,
)
from ..storage.base import _UNSET, AbstractStorage, _Sentinel


_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


class ServiceError(Exception):
    """Service-level error with a stable code + HTTP status."""

    def __init__(self, code: str, *, status: int = 400, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class IssueResult:
    name: str
    token: str
    daily_quota: int
    note: str
    issued_at: int
    expires_at: int | None = None


@dataclass(frozen=True)
class TokenSummary:
    name: str
    daily_quota: int
    note: str
    created_at: int
    revoked_at: int | None
    today_usage: int
    expires_at: int | None


@dataclass(frozen=True)
class RegenResult:
    name: str
    token: str
    daily_quota: int
    note: str
    expires_at: int | None
    revoked_at: int | None


class TokenService:
    def __init__(
        self,
        storage: AbstractStorage,
        audit: AuditLogger,
        *,
        default_daily_quota: int,
    ) -> None:
        self._storage = storage
        self._audit = audit
        self._default_quota = default_daily_quota

    @staticmethod
    def _validate_name(name: str) -> str:
        name = (name or "").strip()
        if not _NAME_RE.match(name):
            raise ServiceError(
                "invalid_name",
                status=400,
                message="name must match [A-Za-z0-9_.-]{1,64}",
            )
        return name

    def _coerce_quota(self, raw) -> int:
        if raw is None or raw == "" or raw == 0:
            return self._default_quota
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ServiceError("invalid_quota", status=400) from None
        if value < 1 or value > 1_000_000:
            raise ServiceError("invalid_quota", status=400)
        return value

    @staticmethod
    def _coerce_expires_at(raw, *, now: int) -> int | None:
        # null / missing → never expires.
        if raw is None:
            return None
        # bool would slip through `int(raw)` (True == 1) — reject explicitly.
        if isinstance(raw, bool):
            raise ServiceError("invalid_expires_at", status=400)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ServiceError("invalid_expires_at", status=400) from None
        if value <= now:
            raise ServiceError("invalid_expires_at", status=400)
        return value

    @staticmethod
    def _coerce_custom_token(raw) -> str | None:
        # None / missing → caller should auto-generate.
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise ServiceError("invalid_custom_token", status=400)
        value = raw.strip()
        if not value:
            return None
        # Reject any whitespace inside the token: pasted plaintext that
        # accidentally captures a newline or tab is the most common
        # mistake, and a token containing whitespace can't be transported
        # in `Authorization: Bearer ...` cleanly.
        if any(ch.isspace() for ch in value):
            raise ServiceError("invalid_custom_token", status=400)
        if len(value) > 256:
            raise ServiceError("invalid_custom_token", status=400)
        return value

    @staticmethod
    def _coerce_note(raw) -> str:
        return (raw or "").strip()[:255]

    async def _summary(self, row, *, today_usage: int) -> TokenSummary:
        return TokenSummary(
            name=row.name,
            daily_quota=row.daily_quota,
            note=row.note,
            created_at=row.created_at,
            revoked_at=row.revoked_at,
            today_usage=today_usage,
            expires_at=row.expires_at,
        )

    async def _summary_for(self, name: str) -> TokenSummary:
        row = await self._storage.get_token_by_name(name)
        if not row:
            raise ServiceError("not_found", status=404)
        usage = await self._storage.get_today_usage(name, day=date.today())
        return await self._summary(row, today_usage=usage)

    async def issue(
        self,
        *,
        name: str,
        daily_quota=None,
        note: str = "",
        expires_at=None,
        ip: str | None = None,
    ) -> IssueResult:
        name = self._validate_name(name)
        quota = self._coerce_quota(daily_quota)
        now = int(time.time())
        expires_value = self._coerce_expires_at(expires_at, now=now)
        existing = await self._storage.get_token_by_name(name)
        if existing and existing.revoked_at is None:
            raise ServiceError("name_exists", status=409)
        if existing and existing.revoked_at is not None:
            # Reuse the row by name? Simpler: refuse to recycle names — keeps audit history clean.
            raise ServiceError(
                "name_exists",
                status=409,
                message="name was used by a revoked token; pick a different name",
            )
        token = generate_token()
        token_hash = hash_token(token)
        await self._storage.create_token(
            name=name,
            token_hash=token_hash,
            daily_quota=quota,
            note=self._coerce_note(note),
            now=now,
            expires_at=expires_value,
        )
        await self._audit.write(
            "issue",
            name=name,
            ip=ip,
            detail={
                "daily_quota": quota,
                "note_len": len(note or ""),
                "expires_at": expires_value,
            },
        )
        return IssueResult(
            name=name,
            token=token,
            daily_quota=quota,
            note=note or "",
            issued_at=now,
            expires_at=expires_value,
        )

    async def revoke(self, *, name: str, ip: str | None = None) -> bool:
        name = self._validate_name(name)
        now = int(time.time())
        ok = await self._storage.revoke_token(name, now=now)
        await self._audit.write(
            "revoke" if ok else "revoke_miss",
            name=name,
            ip=ip,
            detail={"revoked": ok},
        )
        return ok

    async def update_fields(
        self,
        *,
        name: str,
        daily_quota=None,
        note=None,
        expires_at: int | None | _Sentinel = _UNSET,
        ip: str | None = None,
    ) -> TokenSummary:
        name = self._validate_name(name)
        changed: list[str] = []
        quota_value: int | None = None
        if daily_quota is not None:
            quota_value = self._coerce_quota(daily_quota)
            changed.append("daily_quota")
        note_value: str | None = None
        if note is not None:
            note_value = self._coerce_note(note)
            changed.append("note")
        expires_value: int | None | _Sentinel = _UNSET
        if expires_at is not _UNSET:
            expires_value = self._coerce_expires_at(
                expires_at, now=int(time.time())
            )
            changed.append("expires_at")
        if not changed:
            # No-op: still return the current summary so callers (HTTP and
            # bot) don't have to special-case the empty-PATCH path.
            return await self._summary_for(name)
        ok = await self._storage.update_token(
            name,
            daily_quota=quota_value,
            note=note_value,
            expires_at=expires_value,
        )
        if not ok:
            await self._audit.write(
                "update_fields_miss",
                name=name,
                ip=ip,
                detail={"fields": changed},
            )
            raise ServiceError("not_found", status=404)
        await self._audit.write(
            "update_fields",
            name=name,
            ip=ip,
            # NB: detail only records WHICH fields changed, never the
            # values themselves. Keeps audit logs free of operator-typed
            # notes and quota numbers.
            detail={"fields": changed},
        )
        return await self._summary_for(name)

    async def set_revoked(
        self, *, name: str, revoked: bool, ip: str | None = None
    ) -> TokenSummary:
        name = self._validate_name(name)
        now = int(time.time())
        ok = await self._storage.set_token_revoked(name, revoked=revoked, now=now)
        if not ok:
            await self._audit.write(
                "revoke_miss" if revoked else "restore_miss",
                name=name,
                ip=ip,
                detail={"revoked": revoked},
            )
            raise ServiceError("not_found", status=404)
        await self._audit.write(
            "revoke" if revoked else "restore",
            name=name,
            ip=ip,
            detail={"revoked": revoked},
        )
        return await self._summary_for(name)

    async def regenerate(
        self,
        *,
        name: str,
        custom_token: str | None = None,
        ip: str | None = None,
    ) -> RegenResult:
        name = self._validate_name(name)
        custom = self._coerce_custom_token(custom_token)
        plaintext = custom or generate_token()
        new_hash = hash_token(plaintext)
        ok = await self._storage.regenerate_token(name, new_hash)
        if not ok:
            await self._audit.write(
                "regenerate_miss",
                name=name,
                ip=ip,
                detail={"custom": custom is not None},
            )
            raise ServiceError("not_found", status=404)
        await self._audit.write(
            "regenerate",
            name=name,
            ip=ip,
            # `custom` is a bool — we never write the plaintext or its hash.
            detail={"custom": custom is not None},
        )
        row = await self._storage.get_token_by_name(name)
        if not row:
            # Race: row removed between regenerate and re-read. Surface the
            # standard not_found rather than reporting partial success.
            raise ServiceError("not_found", status=404)
        return RegenResult(
            name=row.name,
            token=plaintext,
            daily_quota=row.daily_quota,
            note=row.note,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
        )

    async def rename(
        self,
        *,
        old_name: str,
        new_name: str,
        ip: str | None = None,
    ) -> TokenSummary:
        old = self._validate_name(old_name)
        new = self._validate_name(new_name)
        if old == new:
            return await self._summary_for(old)
        # Pre-check for clearer error codes. The storage rename also guards
        # against collisions atomically; this just lets us return 404 vs 409
        # without inspecting the boolean return.
        src = await self._storage.get_token_by_name(old)
        if not src:
            await self._audit.write(
                "rename_miss",
                name=old,
                ip=ip,
                detail={"from": old, "to": new, "reason": "not_found"},
            )
            raise ServiceError("not_found", status=404)
        clash = await self._storage.get_token_by_name(new)
        if clash:
            await self._audit.write(
                "rename_miss",
                name=old,
                ip=ip,
                detail={"from": old, "to": new, "reason": "name_exists"},
            )
            raise ServiceError("name_exists", status=409)
        ok = await self._storage.rename_token(old, new)
        if not ok:
            # Only reachable on the race where another caller renamed/created
            # `new` between the pre-checks above and the rename.
            await self._audit.write(
                "rename_miss",
                name=old,
                ip=ip,
                detail={"from": old, "to": new, "reason": "race"},
            )
            raise ServiceError("name_exists", status=409)
        await self._audit.write(
            "rename",
            name=new,
            ip=ip,
            # NB: chat-sync tables (webchat_session_meta, webchat_updates)
            # cascade with the rename, but AstrBot's conversation_manager
            # keys LLM context on `webchat_gateway:{name}:{session_id}` and
            # has no public rename API — prior LLM history is detached
            # post-rename. Audit detail flags the cm_detached side-effect
            # so operators reviewing log can correlate user reports of
            # "history is empty after rename" without guessing.
            detail={"from": old, "to": new, "cm_detached": True},
        )
        return await self._summary_for(new)

    async def list_with_today(
        self,
        *,
        include_revoked: bool = False,
        ip: str | None = None,
    ) -> list[TokenSummary]:
        rows = await self._storage.list_tokens(include_revoked=include_revoked)
        today = date.today()
        if rows:
            usage_map = await self._storage.get_today_usage_bulk(
                [row.name for row in rows], day=today
            )
        else:
            usage_map = {}
        await self._audit.write(
            "admin_list",
            ip=ip,
            detail={"include_revoked": include_revoked, "count": len(rows)},
        )
        return [
            TokenSummary(
                name=row.name,
                daily_quota=row.daily_quota,
                note=row.note,
                created_at=row.created_at,
                revoked_at=row.revoked_at,
                today_usage=usage_map.get(row.name, 0),
                expires_at=row.expires_at,
            )
            for row in rows
        ]

    async def stats(
        self,
        *,
        name: str,
        days: int,
        ip: str | None = None,
    ) -> dict:
        name = self._validate_name(name)
        days = max(1, min(days, 90))
        token = await self._storage.get_token_by_name(name)
        if not token:
            raise ServiceError("not_found", status=404)
        history = await self._storage.get_usage_stats(name, days=days)
        await self._audit.write(
            "admin_stats",
            name=name,
            ip=ip,
            detail={"days": days},
        )
        return {
            "name": name,
            "daily_quota": token.daily_quota,
            "created_at": token.created_at,
            "revoked_at": token.revoked_at,
            "expires_at": token.expires_at,
            "revoked": token.revoked_at is not None,
            "history": [
                {"day": row.day.isoformat(), "count": row.count} for row in history
            ],
        }


# ----- HTTP wrappers -----


async def gate_admin(
    request: web.Request,
    *,
    master_key: str,
    ip: str,
    ip_guard,
    audit: AuditLogger,
) -> None:
    """Authenticate an admin request.

    Accepts EITHER:
      - `Authorization: Bearer <master_admin_key>` / `X-API-Key: <key>` (CLI/script callers)
      - A valid `wcg_session` cookie (admin panel after login)

    Pipeline (mirrors `chat.py` ordering, intentionally):
        1. IpGuard.is_blocked → 429 ip_blocked, no credential probe.
        2. master_key empty → 403 admin_disabled (config issue, not attack;
           do NOT count against ip_guard).
        3. Neither bearer nor session cookie present → record_failure + audit + 401.
        4. Both present but neither valid → record_failure + audit + 401.
        5. Success → ip_guard.reset, return.

    `ip_guard` is typed loosely (no annotation) so this module need not
    import IpGuard, avoiding a tighter coupling with the core package.
    """
    blocked, retry_after = await ip_guard.is_blocked(ip)
    if blocked:
        await audit.write(
            "admin_auth_fail",
            ip=ip,
            detail={"reason": "ip_blocked", "retry_after": retry_after},
        )
        raise ServiceError("ip_blocked", status=429, message=str(retry_after))
    if not master_key:
        await audit.write(
            "admin_auth_fail", ip=ip, detail={"reason": "admin_disabled"}
        )
        raise ServiceError("admin_disabled", status=403)
    bearer_present = bool(extract_bearer(request))
    cookie_present = bool(extract_session_cookie(request))
    if not bearer_present and not cookie_present:
        await ip_guard.record_failure(ip)
        await audit.write(
            "admin_auth_fail", ip=ip, detail={"reason": "no_token"}
        )
        raise ServiceError("unauthorized", status=401)
    if not has_admin_credentials(request, master_key):
        await ip_guard.record_failure(ip)
        # Distinguish bearer-only mismatch from session-only mismatch so
        # operators reading audit logs can tell whether someone is
        # brute-forcing the master key vs. replaying a stolen cookie.
        reason = (
            "invalid_key"
            if bearer_present and not cookie_present
            else "invalid_session"
            if cookie_present and not bearer_present
            else "invalid_credentials"
        )
        await audit.write("admin_auth_fail", ip=ip, detail={"reason": reason})
        raise ServiceError("unauthorized", status=401)
    await ip_guard.reset(ip)
