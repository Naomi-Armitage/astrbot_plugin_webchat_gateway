"""ConfigView master_admin_key threshold tests.

v0.3.2 BREAKING: hard floor raised 16 → 24 chars, warning threshold
raised 24 → 32. Keys 16-23 chars long that previously worked are now
cleared at parse time (admin endpoints DISABLED until rotated).

These tests pin both the clearing behavior at the new hard floor and
the warning emission at the higher threshold so a future bump or
relaxation doesn't go unnoticed.
"""

from __future__ import annotations

import logging

import pytest


class TestAdminKeyHardFloor:
    def test_too_short_is_cleared(self, caplog: pytest.LogCaptureFixture):
        """16-char key (previously legal) is now rejected at parse and
        cleared. The error log must surface so an operator pulling the
        bot logs sees why admin auth stopped working."""
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        sixteen = "A" * 16  # ex-legal length, now insufficient
        with caplog.at_level(logging.ERROR, logger="astrbot.stub"):
            cfg = ConfigView.from_raw({"master_admin_key": sixteen})
        assert cfg.master_admin_key == "", (
            "Pre-v0.3.2 16-char key must be cleared by the new 24-char "
            "hard floor — otherwise the BREAKING change wasn't enforced."
        )
        error_messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any(
            "MUST be >= 24 chars" in m for m in error_messages
        ), f"Expected the new 24-char error; got: {error_messages!r}"

    def test_exactly_minimum_is_kept(self):
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        key = "B" * 24
        cfg = ConfigView.from_raw({"master_admin_key": key})
        assert cfg.master_admin_key == key, (
            "24-char key is at the hard floor and must be accepted"
        )

    def test_just_below_minimum_is_cleared(self):
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        key = "C" * 23
        cfg = ConfigView.from_raw({"master_admin_key": key})
        assert cfg.master_admin_key == ""

    def test_above_minimum_is_kept(self):
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        key = "D" * 40
        cfg = ConfigView.from_raw({"master_admin_key": key})
        assert cfg.master_admin_key == key

    def test_empty_key_is_kept_empty(self):
        """No admin key configured at all is a legitimate state
        (admin endpoints intentionally disabled). The hard-floor
        check must NOT log an error for the empty case."""
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        cfg = ConfigView.from_raw({"master_admin_key": ""})
        assert cfg.master_admin_key == ""


class TestAdminKeyWarningThreshold:
    def test_warning_fires_below_32_chars(
        self, caplog: pytest.LogCaptureFixture
    ):
        """24-31 char keys are legal but below the recommended 32.
        v0.3.2 raised the warning threshold from 24 to 32."""
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        twenty_eight = "E" * 28
        with caplog.at_level(logging.WARNING, logger="astrbot.stub"):
            _cfg = ConfigView.from_raw({"master_admin_key": twenty_eight})
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
        ]
        assert any(
            "shorter than 32 chars" in w for w in warnings
        ), f"Expected the new 32-char warning; got: {warnings!r}"

    def test_no_warning_at_or_above_32(
        self, caplog: pytest.LogCaptureFixture
    ):
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        thirty_two = "F" * 32
        with caplog.at_level(logging.WARNING, logger="astrbot.stub"):
            _cfg = ConfigView.from_raw({"master_admin_key": thirty_two})
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING
            and "shorter than 32 chars" in r.getMessage()
        ]
        assert not warnings, (
            f"32-char key should NOT trigger the warning; got: {warnings!r}"
        )
