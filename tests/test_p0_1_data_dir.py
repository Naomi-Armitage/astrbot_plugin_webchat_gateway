"""P0-1 verification: data directory migration to StarTools.get_data_dir.

Covers:
  * `_default_data_dir` returns the StarTools path for the right plugin name
  * `ConfigView.from_raw` default sqlite_path / local_path resolve under it
  * Custom paths are still honored
  * Whitespace / empty inputs fall back to defaults
  * SqliteStorage.initialize creates the DB at the configured path
  * Legacy-DB warning fires when the old default exists and the new path
    doesn't — but NOT when both exist (operator already migrated) and
    NOT when the configured path IS the legacy path
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest


@pytest.mark.usefixtures("tmp_data_dir")
class TestDefaultDataDirHelper:
    def test_returns_stars_data_dir_for_plugin_name(self, tmp_data_dir: Path):
        from astrbot_plugin_webchat_gateway.core.config import (
            _PLUGIN_NAME,
            _default_data_dir,
        )

        result = _default_data_dir()
        assert isinstance(result, Path)
        assert result == tmp_data_dir / _PLUGIN_NAME
        assert result.is_dir(), "StarTools.get_data_dir should mkdir(parents=True)"

    def test_plugin_name_pinned_to_package_dir(self):
        from astrbot_plugin_webchat_gateway.core.config import _PLUGIN_NAME

        assert _PLUGIN_NAME == "astrbot_plugin_webchat_gateway"


@pytest.mark.usefixtures("tmp_data_dir")
class TestFromRawDefaults:
    def test_sqlite_path_default(self, tmp_data_dir: Path):
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        cfg = ConfigView.from_raw({})
        expected = tmp_data_dir / "astrbot_plugin_webchat_gateway" / "webchat_gateway.db"
        assert Path(cfg.storage.sqlite_path) == expected

    def test_local_path_default(self, tmp_data_dir: Path):
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        cfg = ConfigView.from_raw({})
        expected = tmp_data_dir / "astrbot_plugin_webchat_gateway" / "webchat_uploads"
        assert Path(cfg.uploads.local_path) == expected

    def test_sqlite_path_empty_string_falls_back(self, tmp_data_dir: Path):
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        cfg = ConfigView.from_raw({"storage": {"sqlite_path": ""}})
        expected = tmp_data_dir / "astrbot_plugin_webchat_gateway" / "webchat_gateway.db"
        assert Path(cfg.storage.sqlite_path) == expected

    def test_sqlite_path_whitespace_falls_back(self, tmp_data_dir: Path):
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        cfg = ConfigView.from_raw({"storage": {"sqlite_path": "   "}})
        expected = tmp_data_dir / "astrbot_plugin_webchat_gateway" / "webchat_gateway.db"
        assert Path(cfg.storage.sqlite_path) == expected

    def test_local_path_empty_string_falls_back(self, tmp_data_dir: Path):
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        cfg = ConfigView.from_raw({"uploads": {"local_path": ""}})
        expected = tmp_data_dir / "astrbot_plugin_webchat_gateway" / "webchat_uploads"
        assert Path(cfg.uploads.local_path) == expected

    def test_local_path_whitespace_falls_back(self, tmp_data_dir: Path):
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        cfg = ConfigView.from_raw({"uploads": {"local_path": "  \t "}})
        expected = tmp_data_dir / "astrbot_plugin_webchat_gateway" / "webchat_uploads"
        assert Path(cfg.uploads.local_path) == expected

    def test_custom_sqlite_path_preserved(self, tmp_path: Path):
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        custom = str(tmp_path / "custom_db.sqlite")
        cfg = ConfigView.from_raw({"storage": {"sqlite_path": custom}})
        assert cfg.storage.sqlite_path == custom

    def test_custom_local_path_preserved(self, tmp_path: Path):
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        custom = str(tmp_path / "custom_uploads")
        cfg = ConfigView.from_raw({"uploads": {"local_path": custom}})
        assert cfg.uploads.local_path == custom


@pytest.mark.asyncio
@pytest.mark.usefixtures("tmp_data_dir")
class TestSqliteStorageInitialize:
    async def test_creates_db_at_configured_path(self, tmp_path: Path):
        """Clean-env smoke: fresh install, no legacy DB, configured path
        is honored and the file is actually created on disk."""
        from astrbot_plugin_webchat_gateway.storage.sqlite_backend import (
            SqliteStorage,
        )

        db_path = tmp_path / "subdir" / "fresh.db"
        # Directory does NOT exist yet — initialize() must mkdir it.
        assert not db_path.parent.exists()

        storage = SqliteStorage(str(db_path))
        try:
            await storage.initialize()
            assert db_path.exists(), f"DB file not created at {db_path}"
            assert db_path.parent.is_dir()
        finally:
            await storage.close()

    async def test_legacy_warning_fires_when_old_exists_and_new_missing(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Operator on v0.3.0 upgrades to v0.3.1 without moving the file.
        New default path is empty → warning surfaces the migration step."""
        from astrbot_plugin_webchat_gateway.storage.sqlite_backend import (
            SqliteStorage,
        )

        # Legacy detection is relative to CWD (`os.path.join("data", "webchat_gateway.db")`).
        # Run the test from a fresh cwd that contains a fake legacy DB.
        cwd = tmp_path / "astrbot_root"
        legacy = cwd / "data" / "webchat_gateway.db"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"")  # empty file is enough to satisfy os.path.exists
        monkeypatch.chdir(cwd)

        new_path = tmp_path / "plugin_data" / "webchat_gateway.db"
        storage = SqliteStorage(str(new_path))

        with caplog.at_level(logging.WARNING, logger="astrbot.stub"):
            try:
                await storage.initialize()
            finally:
                await storage.close()

        messages = [r.getMessage() for r in caplog.records]
        joined = "\n".join(messages)
        assert "legacy SQLite DB detected" in joined, (
            f"Expected legacy-warning, got: {messages!r}"
        )
        # Warning embeds the *relative* legacy path (the same constant the
        # detection branch uses) — don't compare to the resolved absolute.
        assert "data/webchat_gateway.db" in joined
        assert str(new_path) in joined

    async def test_no_legacy_warning_when_path_is_legacy(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """If the operator explicitly configures the legacy path, the
        warning should NOT fire — they're not migrating, just running v0.3.0
        layout intentionally."""
        from astrbot_plugin_webchat_gateway.storage.sqlite_backend import (
            SqliteStorage,
        )

        cwd = tmp_path / "astrbot_root"
        legacy = cwd / "data" / "webchat_gateway.db"
        legacy.parent.mkdir(parents=True)
        monkeypatch.chdir(cwd)

        # Configured path == legacy path
        storage = SqliteStorage(str(legacy))
        with caplog.at_level(logging.WARNING, logger="astrbot.stub"):
            try:
                await storage.initialize()
            finally:
                await storage.close()

        messages = [r.getMessage() for r in caplog.records]
        assert not any("legacy SQLite DB detected" in m for m in messages), (
            f"Unexpected legacy warning when configured == legacy: {messages!r}"
        )

    async def test_no_legacy_warning_when_new_already_populated(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Operator already migrated: legacy file still on disk, new path
        also populated. No warning — they're done."""
        from astrbot_plugin_webchat_gateway.storage.sqlite_backend import (
            SqliteStorage,
        )

        cwd = tmp_path / "astrbot_root"
        legacy = cwd / "data" / "webchat_gateway.db"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"")
        monkeypatch.chdir(cwd)

        new_path = tmp_path / "plugin_data" / "webchat_gateway.db"
        new_path.parent.mkdir(parents=True)
        new_path.write_bytes(b"")  # pre-existing — looks like a real migrated DB

        storage = SqliteStorage(str(new_path))
        with caplog.at_level(logging.WARNING, logger="astrbot.stub"):
            try:
                await storage.initialize()
            finally:
                await storage.close()

        messages = [r.getMessage() for r in caplog.records]
        assert not any("legacy SQLite DB detected" in m for m in messages), (
            f"Unexpected legacy warning when new path is populated: {messages!r}"
        )
