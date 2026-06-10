"""Atomic daily-quota reservation.

Replaces the old check-then-increment (which relied on the per-token
concurrency lock for serialization) with a reserve-at-start / refund-on-
non-consumption model, so different SESSIONS of one token can run
concurrently without ever overshooting the daily quota.

Usage:

    res = await QuotaReservation.open(
        storage, name=token.name, day=today, quota=token.daily_quota
    )
    if res is None:
        return 429 quota_exceeded            # already at/over quota
    try:
        ...produce the reply...
        res.commit()                          # the turn consumed its unit
        ...use res.count / res.remaining...
    finally:
        await res.close()                     # refund unless committed
"""

from __future__ import annotations

from datetime import date
from typing import Any


class QuotaReservation:
    """One reserved unit of a token's daily quota.

    Created via :meth:`open`, which atomically reserves the unit (or returns
    ``None`` when the token is already at/over quota). ``commit()`` marks the
    unit as consumed so ``close()`` keeps it; otherwise ``close()`` refunds.
    ``close()`` is idempotent and a no-op after ``commit()``, so it is safe to
    call from a ``finally`` regardless of which path was taken.
    """

    def __init__(
        self,
        storage: Any,
        *,
        name: str,
        day: date,
        count: int,
        quota: int,
    ) -> None:
        self._storage = storage
        self._name = name
        self._day = day
        # Post-reservation values, fixed at reserve time so response frames
        # report a stable count/remaining for this turn.
        self.count = count
        self.remaining = max(0, quota - count)
        self._settled = False

    @classmethod
    async def open(
        cls, storage: Any, *, name: str, day: date, quota: int
    ) -> "QuotaReservation | None":
        new_count = await storage.try_reserve_daily_usage(
            name, day=day, quota=quota
        )
        if new_count is None:
            return None
        return cls(storage, name=name, day=day, count=new_count, quota=quota)

    def commit(self) -> None:
        """Mark the reservation consumed — ``close()`` will not refund."""
        self._settled = True

    async def close(self) -> None:
        """Refund the reservation unless it was committed. Idempotent."""
        if self._settled:
            return
        self._settled = True
        await self._storage.refund_daily_usage(self._name, day=self._day)
