"""Per-token concurrency limiter (single-flight)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


class PerTokenConcurrency:
    """Allow at most one in-flight request per token name.

    Non-blocking acquire: if the lock is already held, the context manager
    yields False so the caller can short-circuit with 429. Otherwise it
    yields True and releases on exit. Idle locks are evicted from the
    internal dict on release so revoked/rotated tokens don't leak.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._mutex = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, name: str) -> AsyncIterator[bool]:
        acquired = False
        async with self._mutex:
            lock = self._locks.get(name)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[name] = lock
            if not lock.locked():
                # Acquire while still holding the mutex: lock is known free,
                # so this is non-blocking and cannot be cancelled mid-
                # acquisition without raising before ownership transfers.
                await lock.acquire()
                acquired = True
        # Mutex released. Yielding False below no longer cross-blocks
        # acquires for OTHER token names while the caller writes its 429.
        if not acquired:
            yield False
            return
        try:
            yield True
        finally:
            lock.release()
            async with self._mutex:
                current = self._locks.get(name)
                if current is lock and not lock.locked():
                    self._locks.pop(name, None)
