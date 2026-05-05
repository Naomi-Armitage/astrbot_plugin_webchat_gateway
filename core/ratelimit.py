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

    The `acquire_with_id` / `release` pair is the manual-release variant
    used by the streaming registry: the lock is held across multiple async
    boundaries (LLM stream → persist → emit), and the caller stamps the
    holder with a stream_id so other code can introspect via
    `current_stream_id` and so `release` can be made stream-id-scoped (a
    no-op if the holder has changed since acquisition).
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        # Parallel dict tagging which stream_id currently holds the lock.
        # Only populated by `acquire_with_id`; the `acquire` contextmanager
        # leaves this untouched, so non-stream callers don't appear as
        # holders to `current_stream_id`.
        self._holders: dict[str, str] = {}
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

    async def acquire_with_id(self, name: str, stream_id: str) -> bool:
        """Manual-release acquire tagged with `stream_id`.

        Returns True if the lock was acquired (caller MUST eventually call
        `release(name, stream_id)`), False if the lock is already held.
        Mirrors the non-blocking semantics of `acquire`.
        """
        async with self._mutex:
            lock = self._locks.get(name)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[name] = lock
            if lock.locked():
                return False
            # Atomic with the mutex held: the lock is known free, the await
            # cannot block, and no other coroutine can race us on this name
            # until we exit the mutex with the holder recorded.
            await lock.acquire()
            self._holders[name] = stream_id
            return True

    async def current_stream_id(self, name: str) -> str | None:
        """Return the stream_id currently holding `name`, or None.

        Returns None if the lock is unheld OR if it was acquired via the
        plain `acquire` contextmanager (which doesn't tag a holder).
        """
        async with self._mutex:
            return self._holders.get(name)

    async def release(self, name: str, stream_id: str) -> None:
        """Release IFF `stream_id` matches the recorded holder.

        No-op when the lock isn't held, or when the holder is a different
        stream_id (defensive: prevents a late-arriving cleanup from
        cancelling a freshly acquired stream on the same token).
        """
        async with self._mutex:
            holder = self._holders.get(name)
            if holder != stream_id:
                return
            lock = self._locks.get(name)
            self._holders.pop(name, None)
            if lock is None:
                return
            if lock.locked():
                lock.release()
            if not lock.locked():
                self._locks.pop(name, None)
