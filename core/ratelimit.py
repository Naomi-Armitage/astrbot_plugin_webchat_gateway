"""Per-token concurrency limiter (single-flight)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


class PerTokenConcurrency:
    """Allow at most one in-flight request per token name.

    Non-blocking acquire: if the lock is already held, the context manager
    yields False so the caller can short-circuit with 429. Otherwise it
    yields True and releases on exit.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        # Mutex for the dict itself (not for the held locks).
        self._mutex = asyncio.Lock()

    async def _get_lock(self, name: str) -> asyncio.Lock:
        async with self._mutex:
            lock = self._locks.get(name)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[name] = lock
            return lock

    @asynccontextmanager
    async def acquire(self, name: str) -> AsyncIterator[bool]:
        lock = await self._get_lock(name)
        if lock.locked():
            yield False
            return
        await lock.acquire()
        try:
            yield True
        finally:
            lock.release()
