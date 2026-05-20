"""Thin wrapper around storage IP-failure tracking."""

from __future__ import annotations

import time

from ..storage.base import AbstractStorage

# Stable bucket for requests where the client IP could not be determined
# (request.remote is None and trust_forwarded_for is off, or empty XFF).
# Tracking under one key means a flood from such clients still trips the
# brute-force guard instead of silently bypassing it.
_UNKNOWN_IP_KEY = "__unknown__"


def _normalize_ip(ip: str) -> str | None:
    if not ip:
        return None
    if ip == "unknown":
        return _UNKNOWN_IP_KEY
    return ip


class IpGuard:
    def __init__(
        self,
        storage: AbstractStorage,
        *,
        max_fails: int,
        block_seconds: int,
    ) -> None:
        self._storage = storage
        self._max_fails = max_fails
        self._block_seconds = block_seconds

    @property
    def enabled(self) -> bool:
        return self._max_fails > 0

    async def is_blocked(self, ip: str) -> tuple[bool, int]:
        if not self.enabled:
            return False, 0
        key = _normalize_ip(ip)
        if key is None:
            return False, 0
        return await self._storage.is_ip_blocked(key, now=int(time.time()))

    async def record_failure(self, ip: str) -> int:
        if not self.enabled:
            return 0
        key = _normalize_ip(ip)
        if key is None:
            return 0
        return await self._storage.record_ip_failure(
            key,
            now=int(time.time()),
            max_fails=self._max_fails,
            block_seconds=self._block_seconds,
        )

    async def reset(self, ip: str) -> None:
        """Pay back ONE failure to `ip` after a successful auth.

        Despite the historical name, this no longer wipes the counter
        to zero — that let an attacker with one valid token on the
        same IP zero out the brute-force counter at will and probe
        OTHER tokens at full rate forever. The current semantic is
        decrement-by-one: legit users still recover from typos
        (single typo + correct attempt → counter back to 0), but
        an attacker holding a single valid bearer is bounded to
        exactly one probe credit per legitimate auth.

        Per-IP rather than per-(ip, token) because the failed-auth
        path has no token identity (the bearer didn't match any row),
        so per-token tracking would not constrain the actual attack
        surface — unauthenticated guesses.
        """
        key = _normalize_ip(ip)
        if key is None:
            return
        await self._storage.decrement_ip_failure(key)
