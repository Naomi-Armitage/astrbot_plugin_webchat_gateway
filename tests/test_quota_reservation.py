"""QuotaReservation — the reserve / commit / refund contract every quota
consumer (chat, stream, regenerate, title) relies on. A reservation grants a
unit up front; ``commit()`` keeps it (a reply was produced), otherwise
``close()`` refunds it. ``close()`` is idempotent so it is safe in a finally.
"""

from __future__ import annotations

from datetime import date

import pytest


class _FakeQuotaStorage:
    """Records reserve/refund calls; grants until `quota` then returns None."""

    def __init__(self) -> None:
        self.count = 0
        self.reserves = 0
        self.refunds = 0

    async def try_reserve_daily_usage(self, name, *, day, quota):
        self.reserves += 1
        if self.count >= quota:
            return None
        self.count += 1
        return self.count

    async def refund_daily_usage(self, name, *, day):
        self.refunds += 1
        if self.count > 0:
            self.count -= 1


@pytest.mark.asyncio
async def test_reservation_open_returns_none_over_quota():
    from astrbot_plugin_webchat_gateway.core.quota import QuotaReservation

    store = _FakeQuotaStorage()
    store.count = 5
    res = await QuotaReservation.open(
        store, name="x", day=date(2026, 6, 10), quota=5
    )
    assert res is None
    assert store.count == 5  # untouched


@pytest.mark.asyncio
async def test_reservation_commit_keeps_the_unit():
    from astrbot_plugin_webchat_gateway.core.quota import QuotaReservation

    store = _FakeQuotaStorage()
    res = await QuotaReservation.open(
        store, name="x", day=date(2026, 6, 10), quota=10
    )
    assert res is not None
    assert res.count == 1
    assert res.remaining == 9
    res.commit()
    await res.close()  # committed → no refund
    assert store.count == 1
    assert store.refunds == 0


@pytest.mark.asyncio
async def test_reservation_close_without_commit_refunds():
    from astrbot_plugin_webchat_gateway.core.quota import QuotaReservation

    store = _FakeQuotaStorage()
    res = await QuotaReservation.open(
        store, name="x", day=date(2026, 6, 10), quota=10
    )
    await res.close()  # not committed → refund
    assert store.count == 0
    assert store.refunds == 1


@pytest.mark.asyncio
async def test_reservation_close_is_idempotent():
    from astrbot_plugin_webchat_gateway.core.quota import QuotaReservation

    store = _FakeQuotaStorage()
    res = await QuotaReservation.open(
        store, name="x", day=date(2026, 6, 10), quota=10
    )
    await res.close()
    await res.close()  # second close must not double-refund
    assert store.refunds == 1
    assert store.count == 0
