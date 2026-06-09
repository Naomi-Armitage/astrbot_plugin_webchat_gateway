"""Tests for image-generation cancellation on client disconnect.

aiohttp's ``handler_cancellation`` defaults to False, so a client abort
doesn't cancel the /chat handler. The image branch races the generation
task against a ``transport.is_closing()`` poll so a stopped generation is
actually aborted — before it charges quota or writes CM. This pins the
helper that drives that race.
"""

from __future__ import annotations

import asyncio

import pytest


class _FakeTransport:
    def __init__(self, closing: bool = False) -> None:
        self._closing = closing

    def is_closing(self) -> bool:
        return self._closing


class _FakeRequest:
    def __init__(self, transport) -> None:
        self.transport = transport


@pytest.mark.asyncio
class TestAwaitOrCancelOnDisconnect:
    async def test_returns_result_on_completion(self):
        from astrbot_plugin_webchat_gateway.handlers.chat import (
            _await_or_cancel_on_disconnect,
        )

        async def work():
            return "ok"

        task = asyncio.ensure_future(work())
        req = _FakeRequest(_FakeTransport(closing=False))
        assert await _await_or_cancel_on_disconnect(req, task) == "ok"

    async def test_propagates_task_exception(self):
        from astrbot_plugin_webchat_gateway.handlers.chat import (
            _await_or_cancel_on_disconnect,
        )

        async def work():
            raise ValueError("boom")

        task = asyncio.ensure_future(work())
        req = _FakeRequest(_FakeTransport(closing=False))
        with pytest.raises(ValueError):
            await _await_or_cancel_on_disconnect(req, task)

    async def test_cancels_on_disconnect(self):
        from astrbot_plugin_webchat_gateway.handlers.chat import (
            _await_or_cancel_on_disconnect,
        )

        async def slow():
            await asyncio.sleep(30)
            return "should-not-reach"

        task = asyncio.ensure_future(slow())
        req = _FakeRequest(_FakeTransport(closing=True))
        out = await _await_or_cancel_on_disconnect(req, task)
        assert out is None
        assert task.cancelled()

    async def test_none_transport_treated_as_disconnect(self):
        from astrbot_plugin_webchat_gateway.handlers.chat import (
            _await_or_cancel_on_disconnect,
        )

        async def slow():
            await asyncio.sleep(30)

        task = asyncio.ensure_future(slow())
        req = _FakeRequest(None)
        out = await _await_or_cancel_on_disconnect(req, task)
        assert out is None
        assert task.cancelled()
