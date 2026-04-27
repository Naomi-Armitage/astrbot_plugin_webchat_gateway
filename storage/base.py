"""Storage abstract base class and row dataclasses."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class TokenRow:
    name: str
    token_hash: str
    daily_quota: int
    note: str
    created_at: int
    revoked_at: int | None


@dataclass(frozen=True)
class UsageRow:
    name: str
    day: date
    count: int


@dataclass(frozen=True)
class AuditRow:
    id: int
    ts: int
    name: str | None
    ip: str | None
    event: str
    detail: str


class AbstractStorage(ABC):
    """Pluggable storage interface for tokens, usage, IP failures, and audit."""

    @abstractmethod
    async def initialize(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    # ----- tokens -----
    @abstractmethod
    async def create_token(
        self,
        *,
        name: str,
        token_hash: str,
        daily_quota: int,
        note: str,
        now: int,
    ) -> None: ...

    @abstractmethod
    async def get_token_by_hash(self, token_hash: str) -> TokenRow | None: ...

    @abstractmethod
    async def get_token_by_name(self, name: str) -> TokenRow | None: ...

    @abstractmethod
    async def revoke_token(self, name: str, *, now: int) -> bool: ...

    @abstractmethod
    async def list_tokens(self, *, include_revoked: bool = False) -> list[TokenRow]: ...

    # ----- daily usage -----
    @abstractmethod
    async def increment_daily_usage(self, name: str, *, day: date) -> int:
        """Atomically +1 today's counter and return the new value."""

    @abstractmethod
    async def get_today_usage(self, name: str, *, day: date) -> int: ...

    @abstractmethod
    async def get_usage_stats(self, name: str, *, days: int) -> list[UsageRow]: ...

    # ----- ip brute-force -----
    @abstractmethod
    async def record_ip_failure(
        self, ip: str, *, now: int, max_fails: int, block_seconds: int
    ) -> int:
        """Increment and return the new fail count; sets blocked_until when threshold crossed."""

    @abstractmethod
    async def is_ip_blocked(self, ip: str, *, now: int) -> tuple[bool, int]:
        """Return (blocked, retry_after_seconds)."""

    @abstractmethod
    async def reset_ip_failures(self, ip: str) -> None: ...

    # ----- audit -----
    @abstractmethod
    async def write_audit(
        self,
        *,
        ts: int,
        name: str | None,
        ip: str | None,
        event: str,
        detail: str,
    ) -> None: ...

    @abstractmethod
    async def get_recent_audit(self, *, limit: int) -> list[AuditRow]: ...
