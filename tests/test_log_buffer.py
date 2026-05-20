"""Regression tests for `core/log_buffer.py` + `handlers/admin_logs.py`.

The log-viewer surface has three layers worth pinning:

  * `LogBuffer` — pure data structure: bounded FIFO, monotonic ids,
    cursored snapshot with filter predicates, subscriber wake-up.
  * `LogBufferHandler` — Python-logging integration: emit() must
    survive bad records and capture exc_info correctly.
  * HTTP handlers — `gate_admin` enforcement, query-param parsing,
    SSE handshake.

The SSE pump (`stream_logs`) is harder to test in isolation because
it's a long-running coroutine, so we cover the gate + initial-frame
shape via the TestClient pattern already used by other admin tests,
and leave the long-poll keepalive cadence to the audit-log e2e.
"""

from __future__ import annotations

import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


# ---------------------------------------------------------------------
# LogBuffer
# ---------------------------------------------------------------------


class TestLogBuffer:
    def _entry(self, buf, *, level="INFO", message="hi", logger_name="x"):
        from astrbot_plugin_webchat_gateway.core.log_buffer import LogEntry

        return LogEntry(
            id=buf.next_id(),
            ts=1_700_000_000.0,
            level=level,
            logger=logger_name,
            message=message,
        )

    def test_append_and_snapshot_in_order(self):
        from astrbot_plugin_webchat_gateway.core.log_buffer import LogBuffer

        buf = LogBuffer(capacity=10)
        for i in range(5):
            buf.append(self._entry(buf, message=f"m{i}"))
        entries, max_id = buf.snapshot(since=0, limit=10)
        assert [e.message for e in entries] == ["m0", "m1", "m2", "m3", "m4"]
        # Ids are monotonic 1..5.
        assert [e.id for e in entries] == [1, 2, 3, 4, 5]
        assert max_id == 5

    def test_capacity_evicts_oldest(self):
        from astrbot_plugin_webchat_gateway.core.log_buffer import LogBuffer

        buf = LogBuffer(capacity=3)
        for i in range(5):
            buf.append(self._entry(buf, message=f"m{i}"))
        entries, max_id = buf.snapshot(since=0, limit=10)
        # Only the last 3 survive; ids 1+2 were evicted FIFO.
        assert [e.message for e in entries] == ["m2", "m3", "m4"]
        assert max_id == 5

    def test_since_cursor_skips_older(self):
        from astrbot_plugin_webchat_gateway.core.log_buffer import LogBuffer

        buf = LogBuffer(capacity=10)
        for i in range(5):
            buf.append(self._entry(buf, message=f"m{i}"))
        # First 2 entries match since=2 → return m2..m4
        entries, max_id = buf.snapshot(since=2, limit=10)
        assert [e.id for e in entries] == [3, 4, 5]
        assert max_id == 5

    def test_level_filter_at_or_above(self):
        from astrbot_plugin_webchat_gateway.core.log_buffer import LogBuffer

        buf = LogBuffer(capacity=10)
        buf.append(self._entry(buf, level="DEBUG", message="d"))
        buf.append(self._entry(buf, level="INFO", message="i"))
        buf.append(self._entry(buf, level="WARNING", message="w"))
        buf.append(self._entry(buf, level="ERROR", message="e"))
        entries, _ = buf.snapshot(since=0, level="WARNING", limit=10)
        # WARNING also pulls ERROR; INFO + DEBUG dropped.
        assert [e.message for e in entries] == ["w", "e"]

    def test_grep_filter_substring_case_insensitive(self):
        from astrbot_plugin_webchat_gateway.core.log_buffer import LogBuffer

        buf = LogBuffer(capacity=10)
        buf.append(self._entry(buf, message="image gen failed"))
        buf.append(self._entry(buf, message="chat ok"))
        buf.append(self._entry(buf, message="ImageBridge error"))
        entries, _ = buf.snapshot(since=0, grep="IMAGE", limit=10)
        assert [e.message for e in entries] == ["image gen failed", "ImageBridge error"]

    def test_limit_truncates_and_advances_cursor(self):
        from astrbot_plugin_webchat_gateway.core.log_buffer import LogBuffer

        buf = LogBuffer(capacity=10)
        for i in range(10):
            buf.append(self._entry(buf, message=f"m{i}"))
        entries, max_id = buf.snapshot(since=0, limit=3)
        assert len(entries) == 3
        # max_id is the id of the last RETURNED entry, not the last
        # buffer id — the cursor must advance only past what was
        # actually delivered so the next call resumes cleanly.
        assert max_id == entries[-1].id

    @pytest.mark.asyncio
    async def test_wait_for_new_wakes_on_append(self):
        import asyncio

        from astrbot_plugin_webchat_gateway.core.log_buffer import LogBuffer

        buf = LogBuffer(capacity=10)

        async def _appender():
            await asyncio.sleep(0.05)
            buf.append(self._entry(buf, message="late"))

        task = asyncio.create_task(_appender())
        # If wait_for_new didn't actually wake on append, this would
        # hit the 2.0s timeout. The append happens at ~50ms.
        await asyncio.wait_for(buf.wait_for_new(timeout=2.0), timeout=2.5)
        await task
        entries, _ = buf.snapshot(since=0, limit=10)
        assert [e.message for e in entries] == ["late"]

    @pytest.mark.asyncio
    async def test_wait_for_new_returns_on_timeout(self):
        from astrbot_plugin_webchat_gateway.core.log_buffer import LogBuffer

        buf = LogBuffer(capacity=10)
        # No appends in flight — timeout returns without raising. The
        # SSE pump relies on this for the keepalive heartbeat cycle.
        await buf.wait_for_new(timeout=0.05)


# ---------------------------------------------------------------------
# LogBufferHandler
# ---------------------------------------------------------------------


class TestLogBufferHandler:
    def test_emit_records_into_buffer(self):
        from astrbot_plugin_webchat_gateway.core.log_buffer import (
            LogBuffer,
            LogBufferHandler,
        )

        buf = LogBuffer(capacity=10)
        handler = LogBufferHandler(buf)
        # Build a synthetic LogRecord since we don't want to depend on
        # any specific logger configuration.
        record = logging.LogRecord(
            name="astrbot.test",
            level=logging.WARNING,
            pathname=__file__,
            lineno=42,
            msg="hello %s",
            args=("world",),
            exc_info=None,
        )
        handler.emit(record)
        entries, _ = buf.snapshot(since=0, limit=10)
        assert len(entries) == 1
        assert entries[0].level == "WARNING"
        assert entries[0].logger == "astrbot.test"
        assert entries[0].message == "hello world"
        assert entries[0].exc is None

    def test_emit_captures_exc_info(self):
        from astrbot_plugin_webchat_gateway.core.log_buffer import (
            LogBuffer,
            LogBufferHandler,
        )

        buf = LogBuffer(capacity=10)
        handler = LogBufferHandler(buf)
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="astrbot.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=42,
            msg="failed",
            args=(),
            exc_info=exc_info,
        )
        handler.emit(record)
        entries, _ = buf.snapshot(since=0, limit=10)
        assert entries[0].exc is not None
        assert "ValueError: boom" in entries[0].exc
        # Traceback line for our own raise site should be in there.
        assert __file__ in entries[0].exc

    def test_emit_truncates_oversized_message(self):
        from astrbot_plugin_webchat_gateway.core.log_buffer import (
            LogBuffer,
            LogBufferHandler,
        )

        buf = LogBuffer(capacity=10)
        handler = LogBufferHandler(buf)
        big = "x" * 10000
        record = logging.LogRecord(
            name="astrbot.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=42,
            msg=big,
            args=(),
            exc_info=None,
        )
        handler.emit(record)
        entries, _ = buf.snapshot(since=0, limit=10)
        # Per-message cap is 4000 chars + ellipsis.
        assert len(entries[0].message) <= 4001
        assert entries[0].message.endswith("…")

    def test_emit_survives_format_failure(self):
        """%-formatting failures (mismatched args) used to break
        third-party log viewers. Our handler should fall back to the
        raw msg rather than re-raising into the caller's hot path."""
        from astrbot_plugin_webchat_gateway.core.log_buffer import (
            LogBuffer,
            LogBufferHandler,
        )

        buf = LogBuffer(capacity=10)
        handler = LogBufferHandler(buf)
        record = logging.LogRecord(
            name="astrbot.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=42,
            msg="malformed %d %d",
            args=("not", "ints"),
            exc_info=None,
        )
        # Must not raise.
        handler.emit(record)
        entries, _ = buf.snapshot(since=0, limit=10)
        assert len(entries) == 1

    def test_filter_drops_records_outside_plugin_dir(self):
        """`astrbot.api.logger` is shared across the whole bot — other
        plugins and framework code emit through it too. The handler
        must filter to records whose source file lives under the
        configured plugin_dir, otherwise the admin panel's "本插件
        日志" tab would mix everyone's output together."""
        from astrbot_plugin_webchat_gateway.core.log_buffer import (
            LogBuffer,
            LogBufferHandler,
        )

        buf = LogBuffer(capacity=10)
        # Pin plugin_dir to the test file's parent (the tests/ dir
        # itself) so any path outside is "foreign". With this scope,
        # a record from /tmp/other_plugin.py must be dropped.
        import os

        handler = LogBufferHandler(
            buf, plugin_dir=os.path.dirname(__file__)
        )

        foreign = logging.LogRecord(
            name="astrbot.other",
            level=logging.WARNING,
            pathname="/tmp/some_other_plugin/main.py",
            lineno=10,
            msg="not us",
            args=(),
            exc_info=None,
        )
        handler.emit(foreign)
        # Buffer is empty — record was filtered out.
        entries, _ = buf.snapshot(since=0, limit=10)
        assert entries == []

        # Sanity: a record FROM this test file does land in the
        # buffer, so the filter isn't dropping everything.
        own = logging.LogRecord(
            name="astrbot.plugin.webchat_gateway",
            level=logging.WARNING,
            pathname=__file__,
            lineno=10,
            msg="us",
            args=(),
            exc_info=None,
        )
        handler.emit(own)
        entries, _ = buf.snapshot(since=0, limit=10)
        assert len(entries) == 1
        assert entries[0].message == "us"

    def test_filter_drops_empty_pathname(self):
        """A LogRecord without a pathname (rare, but possible from
        some logging adapters / threading shims) can't be classified
        as "ours" and should be dropped — defensive default."""
        from astrbot_plugin_webchat_gateway.core.log_buffer import (
            LogBuffer,
            LogBufferHandler,
        )

        buf = LogBuffer(capacity=10)
        handler = LogBufferHandler(buf)
        record = logging.LogRecord(
            name="astrbot.test",
            level=logging.WARNING,
            pathname="",
            lineno=10,
            msg="no path",
            args=(),
            exc_info=None,
        )
        handler.emit(record)
        entries, _ = buf.snapshot(since=0, limit=10)
        assert entries == []


# ---------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------


class _RecordingAudit:
    def __init__(self) -> None:
        self.writes: list[tuple[str, dict]] = []

    async def write(self, event: str, **kwargs):
        self.writes.append((event, dict(kwargs)))


class _StubIpGuard:
    def __init__(self) -> None:
        self.record_failure_calls = 0
        self.reset_calls = 0

    async def is_blocked(self, ip):
        return (False, 0)

    async def record_failure(self, ip):
        self.record_failure_calls += 1
        return self.record_failure_calls

    async def reset(self, ip):
        self.reset_calls += 1


_ADMIN_KEY = "0123456789abcdef0123456789abcdef"


def _make_deps(buffer):
    from astrbot_plugin_webchat_gateway.handlers.admin_logs import (
        AdminLogsDeps,
    )

    return AdminLogsDeps(
        buffer=buffer,
        audit=_RecordingAudit(),
        allowed_origins={"*"},
        master_admin_key=_ADMIN_KEY,
        ip_guard=_StubIpGuard(),
        trust_forwarded_for=False,
        trust_referer_as_origin=False,
        allow_missing_origin=True,
    )


async def _client(deps):
    from astrbot_plugin_webchat_gateway.handlers.admin_logs import (
        make_admin_logs_handlers,
    )

    handlers = make_admin_logs_handlers(deps)
    app = web.Application()
    app.router.add_get("/api/webchat/admin/logs", handlers["get_logs"])
    app.router.add_get(
        "/api/webchat/admin/logs/stream", handlers["stream_logs"]
    )
    server = TestServer(app)
    await server.start_server()
    client = TestClient(server)
    await client.start_server()
    return client, server


def _seed_buffer(buf, count: int, *, level="INFO"):
    from astrbot_plugin_webchat_gateway.core.log_buffer import LogEntry

    for i in range(count):
        buf.append(
            LogEntry(
                id=buf.next_id(),
                ts=1_700_000_000.0 + i,
                level=level,
                logger="astrbot.test",
                message=f"m{i}",
            )
        )


def _auth_headers():
    return {"Authorization": f"Bearer {_ADMIN_KEY}"}


@pytest.mark.asyncio
class TestAdminLogsGet:
    async def test_returns_buffer_entries(self):
        from astrbot_plugin_webchat_gateway.core.log_buffer import LogBuffer

        buf = LogBuffer(capacity=20)
        _seed_buffer(buf, 5)
        deps = _make_deps(buf)
        client, server = await _client(deps)
        try:
            resp = await client.get(
                "/api/webchat/admin/logs?limit=10",
                headers=_auth_headers(),
            )
            assert resp.status == 200
            data = await resp.json()
        finally:
            await client.close()
            await server.close()
        assert len(data["entries"]) == 5
        assert data["max_id"] == 5
        assert data["capacity"] == 20

    async def test_since_cursor_resumes(self):
        from astrbot_plugin_webchat_gateway.core.log_buffer import LogBuffer

        buf = LogBuffer(capacity=20)
        _seed_buffer(buf, 5)
        deps = _make_deps(buf)
        client, server = await _client(deps)
        try:
            resp = await client.get(
                "/api/webchat/admin/logs?since=3&limit=10",
                headers=_auth_headers(),
            )
            data = await resp.json()
        finally:
            await client.close()
            await server.close()
        assert [e["id"] for e in data["entries"]] == [4, 5]

    async def test_level_filter(self):
        from astrbot_plugin_webchat_gateway.core.log_buffer import LogBuffer

        buf = LogBuffer(capacity=20)
        _seed_buffer(buf, 2, level="INFO")
        _seed_buffer(buf, 2, level="ERROR")
        deps = _make_deps(buf)
        client, server = await _client(deps)
        try:
            resp = await client.get(
                "/api/webchat/admin/logs?level=ERROR",
                headers=_auth_headers(),
            )
            data = await resp.json()
        finally:
            await client.close()
            await server.close()
        # Filter "at or above ERROR" returns only ERROR rows.
        assert all(e["level"] == "ERROR" for e in data["entries"])
        assert len(data["entries"]) == 2

    async def test_grep_filter(self):
        from astrbot_plugin_webchat_gateway.core.log_buffer import (
            LogBuffer,
            LogEntry,
        )

        buf = LogBuffer(capacity=20)
        for i, msg in enumerate(["image fail", "chat ok", "Image bridge"]):
            buf.append(
                LogEntry(
                    id=buf.next_id(),
                    ts=1_700_000_000.0 + i,
                    level="INFO",
                    logger="astrbot.test",
                    message=msg,
                )
            )
        deps = _make_deps(buf)
        client, server = await _client(deps)
        try:
            resp = await client.get(
                "/api/webchat/admin/logs?grep=image",
                headers=_auth_headers(),
            )
            data = await resp.json()
        finally:
            await client.close()
            await server.close()
        assert {e["message"] for e in data["entries"]} == {"image fail", "Image bridge"}

    async def test_without_auth_returns_401(self):
        from astrbot_plugin_webchat_gateway.core.log_buffer import LogBuffer

        buf = LogBuffer(capacity=20)
        _seed_buffer(buf, 3)
        deps = _make_deps(buf)
        client, server = await _client(deps)
        try:
            resp = await client.get("/api/webchat/admin/logs")
            assert resp.status == 401
        finally:
            await client.close()
            await server.close()

    async def test_limit_clamped_to_capacity(self):
        from astrbot_plugin_webchat_gateway.core.log_buffer import LogBuffer

        buf = LogBuffer(capacity=5)
        _seed_buffer(buf, 5)
        deps = _make_deps(buf)
        client, server = await _client(deps)
        try:
            resp = await client.get(
                "/api/webchat/admin/logs?limit=99999",
                headers=_auth_headers(),
            )
            data = await resp.json()
        finally:
            await client.close()
            await server.close()
        # limit gets clamped to buffer capacity (5), not the operator
        # supplied 99999 — defensive against a polling client with
        # a busted query.
        assert len(data["entries"]) == 5


@pytest.mark.asyncio
class TestAdminLogsStreamShutdown:
    """Regression test for the SSE-pump teardown hang.

    Before the shutdown_event hook, `/admin/logs/stream` ran
    `while True: await buffer.wait_for_new(timeout=20s)`. When the
    plugin's `_stop` called `runner.cleanup()`, aiohttp waited the
    full TCPSite shutdown_timeout (default 60s) for every open SSE
    connection to drain — which never happened, because the pump
    didn't watch any shutdown signal. AstrBot's "正在终止插件 ..."
    retry storm on a ~minute cadence is the operational signature.

    This test pins that the shutdown_event short-circuits the loop:
    the request handler returns within milliseconds of `event.set()`,
    so `runner.cleanup()` can complete promptly.
    """

    async def test_shutdown_event_short_circuits_stream(self):
        import asyncio

        from astrbot_plugin_webchat_gateway.core.log_buffer import LogBuffer
        from astrbot_plugin_webchat_gateway.handlers.admin_logs import (
            AdminLogsDeps,
            make_admin_logs_handlers,
        )

        buf = LogBuffer(capacity=10)
        event = asyncio.Event()
        deps = AdminLogsDeps(
            buffer=buf,
            audit=_RecordingAudit(),
            allowed_origins={"*"},
            master_admin_key=_ADMIN_KEY,
            ip_guard=_StubIpGuard(),
            trust_forwarded_for=False,
            trust_referer_as_origin=False,
            allow_missing_origin=True,
            shutdown_event=event,
        )
        handlers = make_admin_logs_handlers(deps)
        app = web.Application()
        app.router.add_get(
            "/api/webchat/admin/logs/stream", handlers["stream_logs"]
        )
        server = TestServer(app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            # Kick off the SSE subscription. We don't await the body —
            # we want the pump running so we can race the shutdown
            # event against it.
            resp_task = asyncio.create_task(
                client.get(
                    "/api/webchat/admin/logs/stream",
                    headers=_auth_headers(),
                )
            )
            # Let the handler reach its main `while True` loop.
            await asyncio.sleep(0.1)
            # Fire shutdown. The pump should exit on the next tick,
            # NOT after the 20s keepalive timer.
            event.set()
            # Generous wall-clock budget — the pump's
            # `_SSE_KEEPALIVE_SECONDS` is 20s, so anything well below
            # that proves the shutdown-event path triggered. 2s gives
            # plenty of headroom for slow CI.
            resp = await asyncio.wait_for(resp_task, timeout=2.0)
            # Consume the streamed body so the connection closes
            # cleanly — otherwise the TestClient teardown can hang
            # waiting for the read.
            await resp.read()
            assert resp.status == 200
        finally:
            await client.close()
            await server.close()
