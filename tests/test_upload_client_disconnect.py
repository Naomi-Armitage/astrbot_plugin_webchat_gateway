"""Regression: a client disconnect mid-upload must not log ERROR + traceback.

When the browser drops the connection while POSTing a multipart image
(tab closed, network lost, request cancelled), aiohttp surfaces a
`ConnectionResetError: Connection lost` out of `part.read_chunk()`. The
upload handler's broad `except Exception: logger.exception(...)` used to
swallow that into an ERROR-level stack trace — pure noise for an expected,
client-side event.

The handler now catches the connection-error family BEFORE the broad
arm and logs it quietly at debug, returning a graceful 400 (which the
already-gone client never reads). This test pins that contract:

  1. The disconnect path logs at debug, NOT exception / error.
  2. It still returns a well-formed response (no crash) carrying the
     `client_disconnected` marker — distinct from the generic
     `invalid_payload` the broad arm returns.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from astrbot_plugin_webchat_gateway.handlers import files as files_mod
from astrbot_plugin_webchat_gateway.handlers.common import GatePass


class _DisconnectingPart:
    """A multipart 'file' part whose body read fails like a dropped peer —
    exactly the aiohttp signature in the reported traceback."""

    name = "file"

    async def read_chunk(self, size: int = 0) -> bytes:
        raise ConnectionResetError("Connection lost")


class _OnePartReader:
    """Yields one file part, then end-of-stream."""

    def __init__(self) -> None:
        self._served = False

    async def next(self):  # noqa: A003 - mirrors aiohttp MultipartReader.next
        if self._served:
            return None
        self._served = True
        return _DisconnectingPart()


class _FakeRequest:
    content_type = "multipart/form-data"

    async def multipart(self):
        return _OnePartReader()


@pytest.mark.asyncio
async def test_upload_client_disconnect_logs_debug_not_exception(monkeypatch):
    # Bypass the auth/origin/IP gate: this test targets the parse loop's
    # exception handling, not the gate (covered by the IP-guard tests).
    async def _pass_gate(request, deps):
        return GatePass(
            token=SimpleNamespace(name="alice"),
            ip="203.0.113.7",
            origin=None,
            allowed={"*"},
            same_host="localhost",
        )

    monkeypatch.setattr(files_mod, "gate_request", _pass_gate)

    fake_logger = MagicMock()
    monkeypatch.setattr(files_mod, "logger", fake_logger)

    deps = SimpleNamespace(
        max_file_size_mb=20,
        per_token_storage_mb=500,
        allowed_mime=("image/jpeg",),
    )
    handle = files_mod.make_upload_handler(deps)

    resp = await handle(_FakeRequest())

    # Graceful response, not a propagated crash.
    assert resp.status == 400
    assert b"client_disconnected" in resp.body

    # The disconnect was logged quietly — never as an ERROR + traceback.
    fake_logger.exception.assert_not_called()
    fake_logger.error.assert_not_called()
    assert fake_logger.debug.call_count == 1


@pytest.mark.asyncio
async def test_upload_genuine_parse_error_still_logs_exception(monkeypatch):
    """Guard the OTHER side of the branch: a non-connection error in the
    parse loop must still hit logger.exception so real bugs stay visible."""

    class _BrokenReader:
        async def next(self):
            raise ValueError("malformed multipart boundary")

    class _BrokenRequest:
        content_type = "multipart/form-data"

        async def multipart(self):
            return _BrokenReader()

    async def _pass_gate(request, deps):
        return GatePass(
            token=SimpleNamespace(name="alice"),
            ip="203.0.113.7",
            origin=None,
            allowed={"*"},
            same_host="localhost",
        )

    monkeypatch.setattr(files_mod, "gate_request", _pass_gate)
    fake_logger = MagicMock()
    monkeypatch.setattr(files_mod, "logger", fake_logger)

    deps = SimpleNamespace(
        max_file_size_mb=20,
        per_token_storage_mb=500,
        allowed_mime=("image/jpeg",),
    )
    handle = files_mod.make_upload_handler(deps)

    resp = await handle(_BrokenRequest())

    assert resp.status == 400
    assert b"invalid_payload" in resp.body
    fake_logger.exception.assert_called_once()
    fake_logger.debug.assert_not_called()
