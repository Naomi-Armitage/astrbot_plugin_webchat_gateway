"""Per-token concurrency limiter (single-flight)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


def session_key(token: str, session: str) -> str:
    """Composite concurrency-lock key scoping single-flight to one
    ``(token, session)`` pair instead of the whole token.

    Different sessions of the same token then run concurrently (a 180s image
    generation in one session no longer blocks chat in another), while two
    turns in the SAME session still serialize — which is what keeps history
    ordering and the optimistic-echo dedup correct. NUL-separated so the key
    can't collide across a token/session boundary (neither value contains a
    NUL byte). Quota stays exact via the atomic reservation in ``core.quota``,
    no longer relying on per-token serialization.
    """
    return f"{token}\x00{session}"


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


class PerTokenUploadGate:
    """Serialize uploads within a single token.

    Unlike `PerTokenConcurrency` (single-flight, returns False on
    contention), this is a BLOCKING per-token lock — concurrent uploads
    for the same token queue up and execute one at a time. The lock
    scopes just the quota-check + insert critical section: a Reader
    seeing `total_size_for_token = 450MB` followed by a writer doing
    `insert_file(size=30MB)` must observe each other's effects without
    a check-then-act race that lets multiple uploads pass the same
    cap.

    Two concurrent uploads to DIFFERENT tokens still run in parallel
    (different lock objects). Idle locks evict on release.

    Outside the gate, the file_store.save() (disk/R2 I/O) runs without
    holding the lock — keeps the critical section short.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._mutex = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, name: str) -> AsyncIterator[None]:
        async with self._mutex:
            lock = self._locks.get(name)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[name] = lock
        # Manual acquire/release (instead of `async with lock`) so the
        # eviction check below runs AFTER the lock has been released —
        # otherwise `lock.locked()` is always True at the check point
        # (we're still inside our own `async with` scope) and the dict
        # entry would never get popped. The token-locks dict would
        # grow monotonically over time across distinct token names.
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()
            async with self._mutex:
                # Idle-evict only if (a) the dict still points at the
                # same lock object (no concurrent re-create racing us)
                # and (b) nobody else has taken it in the gap between
                # our release and re-acquiring the mutex. If another
                # coroutine grabbed it, THEY will run this cleanup on
                # their own release.
                current = self._locks.get(name)
                if current is lock and not lock.locked():
                    self._locks.pop(name, None)
