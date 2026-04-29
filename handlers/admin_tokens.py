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
from ..core.auth import extract_bearer, generate_token, hash_token, is_master_admin
from ..storage.base import AbstractStorage


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


@dataclass(frozen=True)
class TokenSummary:
    name: str
    daily_quota: int
    note: str
    created_at: int
    revoked_at: int | None
    today_usage: int


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

    async def issue(
        self,
        *,
        name: str,
        daily_quota=None,
        note: str = "",
        ip: str | None = None,
    ) -> IssueResult:
        name = self._validate_name(name)
        quota = self._coerce_quota(daily_quota)
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
        now = int(time.time())
        await self._storage.create_token(
            name=name,
            token_hash=token_hash,
            daily_quota=quota,
            note=(note or "").strip()[:255],
            now=now,
        )
        await self._audit.write(
            "issue",
            name=name,
            ip=ip,
            detail={"daily_quota": quota, "note_len": len(note or "")},
        )
        return IssueResult(
            name=name,
            token=token,
            daily_quota=quota,
            note=note or "",
            issued_at=now,
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

    Pipeline (mirrors `chat.py` ordering, intentionally):
        1. IpGuard.is_blocked → 429 ip_blocked, no master-key probe.
        2. master_key empty → 403 admin_disabled (config issue, not attack;
           do NOT count against ip_guard).
        3. Bearer missing → record_failure + audit + 401.
        4. Bearer mismatch → record_failure + audit + 401.
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
    presented = extract_bearer(request)
    if not presented:
        await ip_guard.record_failure(ip)
        await audit.write(
            "admin_auth_fail", ip=ip, detail={"reason": "no_token"}
        )
        raise ServiceError("unauthorized", status=401)
    if not is_master_admin(request, master_key):
        await ip_guard.record_failure(ip)
        await audit.write(
            "admin_auth_fail", ip=ip, detail={"reason": "invalid_key"}
        )
        raise ServiceError("unauthorized", status=401)
    await ip_guard.reset(ip)
