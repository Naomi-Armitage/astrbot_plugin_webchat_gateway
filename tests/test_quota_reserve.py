"""Atomic daily-quota reservation/refund — the race-safe replacement for
the old check-then-increment that relied on the per-token concurrency lock
for serialization.

These pin the invariant that lets different sessions of one token run
concurrently: `try_reserve_daily_usage` is atomic, so N concurrent reserves
against a quota of K grant exactly K and never overshoot. `refund_daily_usage`
returns an unconsumed reservation and floors at 0.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def storage(tmp_path):
    from astrbot_plugin_webchat_gateway.storage.sqlite_backend import (
        SqliteStorage,
    )

    store = SqliteStorage(str(tmp_path / "quota.db"))
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reserve_increments_until_quota_then_none(storage):
    day = date(2026, 6, 10)
    # Quota 3 → first three reservations return 1, 2, 3; the fourth is over.
    assert await storage.try_reserve_daily_usage("alice", day=day, quota=3) == 1
    assert await storage.try_reserve_daily_usage("alice", day=day, quota=3) == 2
    assert await storage.try_reserve_daily_usage("alice", day=day, quota=3) == 3
    assert await storage.try_reserve_daily_usage("alice", day=day, quota=3) is None
    # The rejected reservation did not bump the counter.
    assert await storage.get_today_usage("alice", day=day) == 3


@pytest.mark.asyncio
async def test_reserve_quota_zero_never_grants(storage):
    day = date(2026, 6, 10)
    assert await storage.try_reserve_daily_usage("bob", day=day, quota=0) is None
    assert await storage.get_today_usage("bob", day=day) == 0


@pytest.mark.asyncio
async def test_concurrent_reserves_never_exceed_quota(storage):
    day = date(2026, 6, 10)
    quota = 10
    # 50 concurrent reservations against quota 10: exactly 10 succeed, the
    # rest get None, and the stored counter lands on precisely 10. This is
    # the property the per-token lock used to guarantee by serialization.
    results = await asyncio.gather(
        *(
            storage.try_reserve_daily_usage("carol", day=day, quota=quota)
            for _ in range(50)
        )
    )
    granted = [r for r in results if r is not None]
    assert len(granted) == quota
    # Each grant returned a distinct count in 1..quota.
    assert sorted(granted) == list(range(1, quota + 1))
    assert await storage.get_today_usage("carol", day=day) == quota


@pytest.mark.asyncio
async def test_refund_decrements_and_floors_at_zero(storage):
    day = date(2026, 6, 10)
    await storage.try_reserve_daily_usage("dave", day=day, quota=5)
    await storage.try_reserve_daily_usage("dave", day=day, quota=5)
    assert await storage.get_today_usage("dave", day=day) == 2
    await storage.refund_daily_usage("dave", day=day)
    assert await storage.get_today_usage("dave", day=day) == 1
    await storage.refund_daily_usage("dave", day=day)
    await storage.refund_daily_usage("dave", day=day)  # already 0 → no-op
    assert await storage.get_today_usage("dave", day=day) == 0


@pytest.mark.asyncio
async def test_refund_frees_a_slot_for_a_new_reservation(storage):
    day = date(2026, 6, 10)
    assert await storage.try_reserve_daily_usage("erin", day=day, quota=1) == 1
    assert await storage.try_reserve_daily_usage("erin", day=day, quota=1) is None
    # A cancelled turn refunds → the slot is available again.
    await storage.refund_daily_usage("erin", day=day)
    assert await storage.try_reserve_daily_usage("erin", day=day, quota=1) == 1


@pytest.mark.asyncio
async def test_reserve_is_per_day(storage):
    d1 = date(2026, 6, 10)
    d2 = date(2026, 6, 11)
    assert await storage.try_reserve_daily_usage("frank", day=d1, quota=1) == 1
    assert await storage.try_reserve_daily_usage("frank", day=d1, quota=1) is None
    # New day → fresh counter.
    assert await storage.try_reserve_daily_usage("frank", day=d2, quota=1) == 1
