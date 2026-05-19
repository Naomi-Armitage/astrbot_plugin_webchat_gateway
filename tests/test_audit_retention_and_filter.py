"""Regression: audit retention prune + filtered/paginated list.

Covers the two storage primitives wired in this change:

  * `SqliteStorage.list_audit` — pagination + event/name/ip/ts filters,
    returning `(rows, total)` so the UI can build a page count.
  * `SqliteStorage.prune_audit` — drops rows whose `ts < before_ts`.

Both methods are also defined on `MysqlStorage`, but the test suite
runs against sqlite only (the project's existing pattern).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest


@pytest.mark.asyncio
@pytest.mark.usefixtures("tmp_data_dir")
class TestAuditListAndPrune:
    async def _seed(self, tmp_path: Path):
        from astrbot_plugin_webchat_gateway.storage.sqlite_backend import (
            SqliteStorage,
        )

        storage = SqliteStorage(str(tmp_path / "audit.db"))
        await storage.initialize()
        now = int(time.time())
        # 20 rows, alternating event types, 4 names, 3 IPs.
        for i in range(20):
            await storage.write_audit(
                ts=now - i,
                name=f"user{i % 4}",
                ip=f"10.0.0.{i % 3}",
                event=("chat_ok" if i % 2 == 0 else "auth_fail"),
                detail=str(i),
            )
        return storage, now

    async def test_pagination_total_matches_unfiltered_count(self, tmp_path: Path):
        storage, _ = await self._seed(tmp_path)
        try:
            rows, total = await storage.list_audit(limit=5, offset=0)
            assert total == 20
            assert len(rows) == 5
            # Page 2 returns the next slice and the same total.
            rows2, total2 = await storage.list_audit(limit=5, offset=5)
            assert total2 == 20
            assert len(rows2) == 5
            # No row overlap between page 1 and page 2.
            assert {r.id for r in rows}.isdisjoint({r.id for r in rows2})
        finally:
            await storage.close()

    async def test_event_filter_narrows_total(self, tmp_path: Path):
        storage, _ = await self._seed(tmp_path)
        try:
            rows, total = await storage.list_audit(limit=100, offset=0, event="chat")
            assert total == 10
            assert all("chat" in r.event for r in rows)
        finally:
            await storage.close()

    async def test_combined_filters_compose(self, tmp_path: Path):
        storage, now = await self._seed(tmp_path)
        try:
            # event=chat_ok AND ip=10.0.0.1: ids where (i%2==0) and (i%3==1)
            # → i in {4,10,16} → 3 rows.
            rows, total = await storage.list_audit(
                limit=100, offset=0, event="chat_ok", ip="10.0.0.1"
            )
            assert total == 3
            assert all(r.event == "chat_ok" for r in rows)
            assert all(r.ip == "10.0.0.1" for r in rows)
        finally:
            await storage.close()

    async def test_ts_range_filter(self, tmp_path: Path):
        storage, now = await self._seed(tmp_path)
        try:
            rows, total = await storage.list_audit(
                limit=100, offset=0, ts_from=now - 5, ts_to=now - 2
            )
            # ts ∈ [now-5, now-2] → 4 rows.
            assert total == 4
            assert all(now - 5 <= r.ts <= now - 2 for r in rows)
        finally:
            await storage.close()

    async def test_prune_removes_old_rows(self, tmp_path: Path):
        storage, now = await self._seed(tmp_path)
        try:
            removed = await storage.prune_audit(before_ts=now - 10)
            # ts < now-10 → i in {11..19} → 9 rows. (ts=now-10 itself is kept.)
            assert removed == 9
            _, total = await storage.list_audit(limit=1, offset=0)
            assert total == 20 - 9
        finally:
            await storage.close()

    async def test_prune_with_no_match_is_noop(self, tmp_path: Path):
        storage, now = await self._seed(tmp_path)
        try:
            removed = await storage.prune_audit(before_ts=now - 1_000_000)
            assert removed == 0
            _, total = await storage.list_audit(limit=1, offset=0)
            assert total == 20
        finally:
            await storage.close()


@pytest.mark.usefixtures("tmp_data_dir")
class TestAuditRetentionConfig:
    def test_audit_retention_days_default_is_7(self):
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        cfg = ConfigView.from_raw({})
        assert cfg.audit_retention_days == 7

    def test_audit_retention_days_clamped(self):
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        # 0 / negative → clamped up to lo=1
        assert ConfigView.from_raw({"audit_retention_days": 0}).audit_retention_days == 1
        assert ConfigView.from_raw({"audit_retention_days": -5}).audit_retention_days == 1
        # Out-of-range high → clamped to hi=3650
        assert (
            ConfigView.from_raw({"audit_retention_days": 999_999}).audit_retention_days
            == 3650
        )
        # Garbage → default
        assert (
            ConfigView.from_raw({"audit_retention_days": "junk"}).audit_retention_days
            == 7
        )

    def test_audit_retention_days_honors_custom_value(self):
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        cfg = ConfigView.from_raw({"audit_retention_days": 30})
        assert cfg.audit_retention_days == 30
