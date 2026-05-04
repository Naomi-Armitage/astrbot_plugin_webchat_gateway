"""Per-token long-poll wakeup primitive.

Every token gets its own `asyncio.Condition`, lazily allocated on first
use. Mutators (`record_chat_pair`, PATCH/clear handlers) call `notify`
after the storage write commits; `get_events` callers `wait` when the
DB has nothing newer than `since_pts`.

Lost notifications are not a correctness issue. The waiter re-reads the
DB on wakeup AND on every cycle, so a missed notify only delays the
response by `timeout` seconds. Server restart drops the dict; clients
reconnect with `since=last_pts` and pick up events from the DB. The DB
is canonical; this module is a latency optimization.

The bus also tracks a per-token waiter count so the service layer can
cap concurrent long-polls (a single user with many open tabs would
otherwise hold one socket + one async task per tab indefinitely). The
count is incremented on `wait` entry and decremented in the `finally`
clause so cancellation paths (visibility-aborted long-poll, client
disconnect) decrement correctly.

GC: the dict grows by token. Token count is bounded by issued tokens
(small, operator-issued), so we let it grow rather than carry the
extra complexity of refcounting waiters and reaping idle conditions.
"""

from __future__ import annotations

import asyncio


class EventBus:
    def __init__(self) -> None:
        self._conds: dict[str, asyncio.Condition] = {}
        self._waiter_counts: dict[str, int] = {}
        # Single mutex over the bookkeeping dicts. Acquiring it is cheap;
        # the per-token Condition is what callers actually wait on.
        self._dict_lock = asyncio.Lock()

    async def _get_cond(self, token: str) -> asyncio.Condition:
        async with self._dict_lock:
            cond = self._conds.get(token)
            if cond is None:
                cond = asyncio.Condition()
                self._conds[token] = cond
            return cond

    async def waiter_count(self, token: str) -> int:
        """Return the current number of waiters parked on `token`.

        Read-only; service layer uses this to decide whether to admit a new
        long-poll or short-circuit to `timeout=0` for over-quota tokens.
        """
        async with self._dict_lock:
            return self._waiter_counts.get(token, 0)

    async def wait(self, token: str, *, timeout: float) -> None:
        """Block until `notify(token)` fires or `timeout` elapses.

        Returns silently in either case — the caller re-reads the DB on
        return rather than relying on the wake itself to carry data.
        Raises asyncio.CancelledError if the calling task is cancelled.
        """
        if timeout <= 0:
            return
        cond = await self._get_cond(token)
        async with self._dict_lock:
            self._waiter_counts[token] = self._waiter_counts.get(token, 0) + 1
        try:
            async with cond:
                try:
                    await asyncio.wait_for(cond.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    return
        finally:
            async with self._dict_lock:
                remaining = self._waiter_counts.get(token, 0) - 1
                if remaining <= 0:
                    self._waiter_counts.pop(token, None)
                else:
                    self._waiter_counts[token] = remaining

    async def notify(self, token: str) -> None:
        """Wake every waiter on `token`. No-op if there are no waiters."""
        cond = await self._get_cond(token)
        async with cond:
            cond.notify_all()


__all__ = ["EventBus"]
