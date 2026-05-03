"""Storage abstract base class and row dataclasses."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date


# Sentinel for partial updates where None is a meaningful value (e.g.
# clearing tokens.expires_at to NULL). `_UNSET` means "leave the column
# alone"; `None` means "set to NULL". Implementations check identity.
class _Sentinel:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return "_UNSET"


_UNSET: _Sentinel = _Sentinel()


@dataclass(frozen=True)
class TokenRow:
    name: str
    token_hash: str
    daily_quota: int
    note: str
    created_at: int
    revoked_at: int | None
    expires_at: int | None


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
        expires_at: int | None = None,
    ) -> None: ...

    @abstractmethod
    async def get_token_by_hash(self, token_hash: str) -> TokenRow | None: ...

    @abstractmethod
    async def get_token_by_name(self, name: str) -> TokenRow | None: ...

    @abstractmethod
    async def revoke_token(self, name: str, *, now: int) -> bool: ...

    @abstractmethod
    async def list_tokens(self, *, include_revoked: bool = False) -> list[TokenRow]: ...

    @abstractmethod
    async def update_token(
        self,
        name: str,
        *,
        daily_quota: int | None = None,
        note: str | None = None,
        expires_at: int | None | _Sentinel = _UNSET,
    ) -> bool:
        """Partial UPDATE on tokens. Returns True if a row matched.

        `expires_at` uses a sentinel because None is a meaningful value
        (clear expiry). Pass `_UNSET` to leave the column alone.
        """

    @abstractmethod
    async def set_token_revoked(
        self, name: str, *, revoked: bool, now: int
    ) -> bool:
        """Set or clear `revoked_at`. Returns True if a row matched."""

    @abstractmethod
    async def regenerate_token(self, name: str, new_token_hash: str) -> bool:
        """Rotate `token_hash` for an existing token. Returns True if matched.

        Does NOT touch `revoked_at`, `daily_quota`, `note`, `expires_at`,
        or `daily_usage`.
        """

    @abstractmethod
    async def rename_token(self, old_name: str, new_name: str) -> bool:
        """Rename a token, cascading to `daily_usage` + `audit_log`.

        Atomic (single transaction). Returns False if `old_name` is missing
        or `new_name` already exists. Caller must validate names beforehand.
        """

    # ----- daily usage -----
    @abstractmethod
    async def increment_daily_usage(self, name: str, *, day: date) -> int:
        """Atomically +1 today's counter and return the new value."""

    @abstractmethod
    async def get_today_usage(self, name: str, *, day: date) -> int: ...

    @abstractmethod
    async def get_today_usage_bulk(
        self, names: list[str], *, day: date
    ) -> dict[str, int]:
        """Fetch today's usage for many names in one query. Missing names map to 0."""

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
