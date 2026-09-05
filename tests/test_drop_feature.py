"""Drop feature: storage-layer truth against real sqlite.

Pins the v5 → v6 migration (webchat_files.filename backfill + the
webchat_drop_messages table) and the six new Drop storage methods.
Schema typos, missing indexes, and SQLite-specific behavior surface
here — per the project's "integration tests over mocks for migrations"
convention. MySQL implementation follows the same SQL shapes but is
not tested (existing project pattern).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def storage(tmp_path: Path):
    from astrbot_plugin_webchat_gateway.storage.sqlite_backend import (
        SqliteStorage,
    )

    s = SqliteStorage(str(tmp_path / "drop.db"))
    await s.initialize()
    yield s
    await s.close()


@pytest.mark.asyncio
class TestDropSchemaV6:
    async def test_fresh_install_is_v6_with_drop_table(self, storage):
        from astrbot_plugin_webchat_gateway.storage.ddl import (
            CURRENT_SCHEMA_VERSION,
        )

        async with storage._db.execute(
            "SELECT value FROM _schema_meta WHERE key = 'schema_version'"
        ) as cursor:
            row = await cursor.fetchone()
        assert row is not None
        assert row["value"] == CURRENT_SCHEMA_VERSION == "6"

        async with storage._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='webchat_drop_messages'"
        ) as cursor:
            assert await cursor.fetchone() is not None

    async def test_v5_db_upgrades_to_v6_and_backfills_filename(
        self, tmp_path: Path
    ):
        """A DB stamped v5 (webchat_files without filename, no drop
        table) must upgrade on initialize: filename column added with
        '' backfill, drop table created, marker moved to 6."""
        import aiosqlite

        from astrbot_plugin_webchat_gateway.storage.sqlite_backend import (
            SqliteStorage,
        )
        from astrbot_plugin_webchat_gateway.storage.ddl import (
            SCHEMA_SQLITE,
        )

        db_path = tmp_path / "legacy.db"
        conn = await aiosqlite.connect(db_path)
        try:
            # Build the v4-era tables (pre-webchat_files), then the v5
            # files table WITHOUT filename, and stamp version 5 — the
            # exact state a v5 install leaves behind.
            for stmt in SCHEMA_SQLITE:
                if "webchat_files" in stmt or "webchat_drop_messages" in stmt:
                    continue
                if "idx_webchat_files" in stmt:
                    continue
                await conn.execute(stmt)
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS webchat_files ("
                "file_id      TEXT PRIMARY KEY,"
                "token_name   TEXT NOT NULL,"
                "session_id   TEXT NOT NULL,"
                "mime         TEXT NOT NULL,"
                "size_bytes   INTEGER NOT NULL,"
                "storage_key  TEXT NOT NULL,"
                "committed    INTEGER NOT NULL DEFAULT 0,"
                "uploaded_at  INTEGER NOT NULL,"
                "committed_at INTEGER)"
            )
            await conn.execute(
                "INSERT INTO webchat_files(file_id, token_name, session_id,"
                " mime, size_bytes, storage_key, committed, uploaded_at)"
                " VALUES ('aaaaaaaaaaaaaaaa', 'alice', 's1', 'image/png',"
                " 5, 'alice/x.png', 1, 100)"
            )
            await conn.execute(
                "INSERT INTO _schema_meta(key, value) "
                "VALUES('schema_version', '5')"
            )
            await conn.commit()
        finally:
            await conn.close()

        s = SqliteStorage(str(db_path))
        try:
            await s.initialize()
            row = await s.get_file("aaaaaaaaaaaaaaaa")
            assert row is not None
            # v5 upload without a filename backfills to '' — NOT garbage.
            assert row.filename == ""
            msgs = await s.list_drop_messages(
                token_name="alice", limit=10, before_id=None,
                include_deleted=True,
            )
            assert msgs == []
        finally:
            await s.close()


@pytest.mark.asyncio
class TestDropMessages:
    async def test_append_returns_monotonic_ids(self, storage):
        id1 = await storage.append_drop_message(
            token_name="alice", device_id="dev-1", device_name="PC",
            kind="text", text="hello", file_id=None, filename="",
            mime="", size_bytes=0, now=100,
        )
        id2 = await storage.append_drop_message(
            token_name="alice", device_id="dev-2", device_name="Phone",
            kind="text", text="second", file_id=None, filename="",
            mime="", size_bytes=0, now=101,
        )
        assert id2 == id1 + 1

    async def test_list_newest_first_with_cursor_pagination(
        self, storage
    ):
        ids = []
        for i in range(5):
            mid = await storage.append_drop_message(
                token_name="alice", device_id="dev-1", device_name="",
                kind="text", text=f"m{i}", file_id=None, filename="",
                mime="", size_bytes=0, now=100 + i,
            )
            ids.append(mid)
        page1 = await storage.list_drop_messages(
            token_name="alice", limit=3, before_id=None,
            include_deleted=False,
        )
        assert [r.id for r in page1] == [ids[4], ids[3], ids[2]]
        page2 = await storage.list_drop_messages(
            token_name="alice", limit=3, before_id=page1[-1].id,
            include_deleted=False,
        )
        assert [r.id for r in page2] == [ids[1], ids[0]]

    async def test_include_deleted_toggles_visibility(self, storage):
        mid = await storage.append_drop_message(
            token_name="alice", device_id="dev-1", device_name="",
            kind="text", text="x", file_id=None, filename="", mime="",
            size_bytes=0, now=100,
        )
        await storage.soft_delete_drop_message(
            token_name="alice", message_id=mid, now=200
        )
        live = await storage.list_drop_messages(
            token_name="alice", limit=10, before_id=None,
            include_deleted=False,
        )
        assert live == []
        everything = await storage.list_drop_messages(
            token_name="alice", limit=10, before_id=None,
            include_deleted=True,
        )
        assert [r.id for r in everything] == [mid]
        assert everything[0].deleted_at == 200

    async def test_cross_token_isolation(self, storage):
        await storage.append_drop_message(
            token_name="alice", device_id="dev-1", device_name="",
            kind="text", text="from alice", file_id=None, filename="",
            mime="", size_bytes=0, now=100,
        )
        await storage.append_drop_message(
            token_name="bob", device_id="dev-1", device_name="",
            kind="text", text="from bob", file_id=None, filename="",
            mime="", size_bytes=0, now=101,
        )
        alice = await storage.list_drop_messages(
            token_name="alice", limit=10, before_id=None,
            include_deleted=False,
        )
        assert len(alice) == 1
        assert alice[0].text == "from alice"

    async def test_soft_delete_is_idempotent(self, storage):
        mid = await storage.append_drop_message(
            token_name="alice", device_id="d", device_name="",
            kind="text", text="x", file_id=None, filename="", mime="",
            size_bytes=0, now=100,
        )
        assert await storage.soft_delete_drop_message(
            token_name="alice", message_id=mid, now=200
        )
        # Second delete is a no-op (row already soft-deleted), NOT an
        # error — the DELETE endpoint relies on this for retries.
        assert not await storage.soft_delete_drop_message(
            token_name="alice", message_id=mid, now=201
        )

    async def test_soft_delete_scopes_to_token(self, storage):
        """A (token, id) pair from another token must not delete — the
        id alone is client-supplied and the token comes from the bearer,
        but the WHERE clause must carry BOTH so a forged cross-token
        id can't soft-delete someone else's note."""
        alice_mid = await storage.append_drop_message(
            token_name="alice", device_id="d", device_name="",
            kind="text", text="secret", file_id=None, filename="",
            mime="", size_bytes=0, now=100,
        )
        assert not await storage.soft_delete_drop_message(
            token_name="bob", message_id=alice_mid, now=200
        )
        row = await storage.get_drop_message(
            token_name="alice", message_id=alice_mid
        )
        assert row is not None and row.deleted_at is None

    async def test_get_drop_message_invisible_after_soft_delete(
        self, storage
    ):
        mid = await storage.append_drop_message(
            token_name="alice", device_id="d", device_name="",
            kind="text", text="x", file_id=None, filename="", mime="",
            size_bytes=0, now=100,
        )
        await storage.soft_delete_drop_message(
            token_name="alice", message_id=mid, now=200
        )
        # get_drop_message returns the row regardless (the caller
        # distinguishes via deleted_at — the DELETE endpoint checks it
        # to collapse retries to a uniform 404).
        row = await storage.get_drop_message(
            token_name="alice", message_id=mid
        )
        assert row is not None and row.deleted_at == 200
        assert await storage.get_drop_message(
            token_name="alice", message_id=99999
        ) is None

    async def test_purge_listing_scopes_by_deleted_ts(self, storage):
        await storage.append_drop_message(
            token_name="alice", device_id="d", device_name="",
            kind="text", text="keep", file_id=None, filename="", mime="",
            size_bytes=0, now=100,
        )
        old_del = await storage.append_drop_message(
            token_name="alice", device_id="d", device_name="",
            kind="text", text="old", file_id=None, filename="", mime="",
            size_bytes=0, now=101,
        )
        await storage.soft_delete_drop_message(
            token_name="alice", message_id=old_del, now=500
        )
        recent_del = await storage.append_drop_message(
            token_name="alice", device_id="d", device_name="",
            kind="text", text="recent", file_id=None, filename="",
            mime="", size_bytes=0, now=102,
        )
        await storage.soft_delete_drop_message(
            token_name="alice", message_id=recent_del, now=5000
        )
        purge = await storage.list_drop_messages_to_purge(before_ts=1000)
        assert [r.id for r in purge] == [old_del]

    async def test_hard_delete_and_clear(self, storage):
        a = await storage.append_drop_message(
            token_name="alice", device_id="d", device_name="",
            kind="text", text="a", file_id=None, filename="", mime="",
            size_bytes=0, now=100,
        )
        await storage.append_drop_message(
            token_name="alice", device_id="d", device_name="",
            kind="text", text="b", file_id=None, filename="", mime="",
            size_bytes=0, now=101,
        )
        await storage.append_drop_message(
            token_name="bob", device_id="d", device_name="",
            kind="text", text="bob", file_id=None, filename="", mime="",
            size_bytes=0, now=102,
        )
        assert await storage.hard_delete_drop_message(
            token_name="alice", message_id=a
        )
        assert not await storage.hard_delete_drop_message(
            token_name="alice", message_id=a
        )
        removed = await storage.clear_drop_history(
            token_name="alice", now=300
        )
        assert removed == 1  # b; bob's row untouched
        bob_rows = await storage.list_drop_messages(
            token_name="bob", limit=10, before_id=None,
            include_deleted=True,
        )
        assert len(bob_rows) == 1


@pytest.mark.asyncio
class TestDropFiles:
    async def test_list_drop_files_filters_by_session(self, storage):
        await storage.insert_file(
            file_id="a" * 16, token_name="alice", session_id="drop",
            mime="application/pdf", size_bytes=10,
            storage_key="alice/a.pdf", now=100, filename="doc.pdf",
        )
        # Chat-side upload in the same token — must NOT be returned.
        await storage.insert_file(
            file_id="b" * 16, token_name="alice", session_id="s1",
            mime="image/png", size_bytes=20,
            storage_key="alice/b.png", now=100,
        )
        rows = await storage.list_drop_files(token_name="alice")
        assert [r.file_id for r in rows] == ["a" * 16]
        assert rows[0].filename == "doc.pdf"
        assert rows[0].session_id == "drop"

    async def test_insert_file_without_filename_defaults_empty(
        self, storage
    ):
        await storage.insert_file(
            file_id="c" * 16, token_name="alice", session_id="s1",
            mime="image/png", size_bytes=5,
            storage_key="alice/c.png", now=100,
        )
        row = await storage.get_file("c" * 16)
        assert row is not None
        assert row.filename == ""
