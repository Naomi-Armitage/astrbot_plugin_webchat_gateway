"""Thin wrapper around storage IP-failure tracking."""

from __future__ import annotations

import time

from ..storage.base import AbstractStorage


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
        if not self.enabled or not ip or ip == "unknown":
            return False, 0
        return await self._storage.is_ip_blocked(ip, now=int(time.time()))

    async def record_failure(self, ip: str) -> int:
        if not self.enabled or not ip or ip == "unknown":
            return 0
        return await self._storage.record_ip_failure(
            ip,
            now=int(time.time()),
            max_fails=self._max_fails,
            block_seconds=self._block_seconds,
        )

    async def reset(self, ip: str) -> None:
        if not ip or ip == "unknown":
            return
        await self._storage.reset_ip_failures(ip)
