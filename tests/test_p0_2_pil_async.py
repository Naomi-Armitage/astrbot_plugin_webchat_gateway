"""P0-2 verification: detect_image_mime async wrapper.

Covers:
  * `detect_image_mime_async` returns same result as sync for every
    supported / unsupported input
  * It's actually awaitable (catches accidental sync-return regression)
  * Event loop stays responsive while the decode runs (the WHOLE point
    of the change) — measured by counting concurrent heartbeat ticks
    during a deliberately heavy decode
"""

from __future__ import annotations

import asyncio
import io
import time

import pytest
from PIL import Image


# --- image bytes helpers ----------------------------------------------------


def _png_bytes(size: tuple[int, int] = (16, 16)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, (255, 0, 0, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes(size: tuple[int, int] = (16, 16)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (0, 128, 255)).save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _webp_bytes(size: tuple[int, int] = (16, 16)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 200, 0)).save(buf, format="WEBP", quality=80)
    return buf.getvalue()


def _gif_bytes(size: tuple[int, int] = (16, 16)) -> bytes:
    buf = io.BytesIO()
    Image.new("P", size, 7).save(buf, format="GIF")
    return buf.getvalue()


def _big_png_bytes(side: int = 1800) -> bytes:
    """Larger PNG used to exercise event-loop responsiveness. Random
    pixels resist compression so the on-disk file scales linearly with
    side² × 4, and Pillow's verify() walks every IDAT chunk — making the
    decode pass actually take measurable wall-clock time."""
    import os

    rgba = os.urandom(side * side * 4)
    img = Image.frombytes("RGBA", (side, side), rgba)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --- correctness ------------------------------------------------------------


class TestAsyncWrapperCorrectness:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("content_fn", "expected"),
        [
            (_png_bytes, "image/png"),
            (_jpeg_bytes, "image/jpeg"),
            (_webp_bytes, "image/webp"),
            (_gif_bytes, "image/gif"),
        ],
    )
    async def test_returns_canonical_mime_for_valid_formats(
        self, content_fn, expected: str
    ):
        from astrbot_plugin_webchat_gateway.core.image_util import (
            detect_image_mime_async,
        )

        mime = await detect_image_mime_async(content_fn())
        assert mime == expected

    @pytest.mark.asyncio
    async def test_empty_bytes_returns_none(self):
        from astrbot_plugin_webchat_gateway.core.image_util import (
            detect_image_mime_async,
        )

        assert await detect_image_mime_async(b"") is None

    @pytest.mark.asyncio
    async def test_garbage_bytes_returns_none(self):
        from astrbot_plugin_webchat_gateway.core.image_util import (
            detect_image_mime_async,
        )

        assert await detect_image_mime_async(b"not-an-image-at-all") is None

    @pytest.mark.asyncio
    async def test_truncated_png_returns_none(self):
        from astrbot_plugin_webchat_gateway.core.image_util import (
            detect_image_mime_async,
        )

        png = _png_bytes((32, 32))
        # Lop off the trailing IEND chunk — verify() should refuse this
        truncated = png[:30]
        assert await detect_image_mime_async(truncated) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content_fn",
        [_png_bytes, _jpeg_bytes, _webp_bytes, _gif_bytes],
    )
    async def test_async_matches_sync(self, content_fn):
        from astrbot_plugin_webchat_gateway.core.image_util import (
            detect_image_mime,
            detect_image_mime_async,
        )

        content = content_fn()
        sync_result = detect_image_mime(content)
        async_result = await detect_image_mime_async(content)
        assert sync_result == async_result

    @pytest.mark.asyncio
    async def test_is_actually_awaitable(self):
        """Defense against a future regression that 'optimizes away'
        asyncio.to_thread and returns sync."""
        from astrbot_plugin_webchat_gateway.core.image_util import (
            detect_image_mime_async,
        )

        coro = detect_image_mime_async(_png_bytes())
        assert asyncio.iscoroutine(coro), (
            "detect_image_mime_async must return a coroutine"
        )
        result = await coro
        assert result == "image/png"


# --- event loop responsiveness ---------------------------------------------


class TestEventLoopResponsiveness:
    """The whole point of P0-2. If `detect_image_mime` runs on the loop
    thread, concurrent tasks starve for the duration of the decode. The
    async wrapper offloads to a worker thread; concurrent tasks should
    continue to make progress."""

    @pytest.mark.asyncio
    async def test_async_decode_does_not_block_loop(self):
        from astrbot_plugin_webchat_gateway.core.image_util import (
            detect_image_mime_async,
        )

        # Side 4000 produces a ~60 MB random-pixel RGBA buffer; the
        # resulting PNG is large enough that Pillow's verify() pass
        # measurably blocks the calling thread (~100+ ms on a modern
        # machine). Anything smaller decodes faster than our heartbeat
        # interval can resolve.
        content = _big_png_bytes(side=4000)

        ticks = 0
        stop = False

        async def heartbeat():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.001)  # 1 ms — fine-grained

        hb_task = asyncio.create_task(heartbeat())
        try:
            t0 = time.monotonic()
            result = await detect_image_mime_async(content)
            elapsed = time.monotonic() - t0
        finally:
            stop = True
            await asyncio.sleep(0)
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass

        assert result == "image/png"

        print(
            f"\n  decode={elapsed * 1000:.1f}ms ticks={ticks} "
            f"({ticks / max(elapsed, 0.001):.0f}/s)"
        )
        # Threshold scaled to the actual decode time: at minimum we
        # should see one heartbeat per 5 ms of decode. If the loop were
        # blocked, ticks would be ~1 regardless of how long decode took.
        min_ticks = max(3, int(elapsed * 1000 / 5))
        assert ticks >= min_ticks, (
            f"Event loop appears blocked: only {ticks} heartbeat ticks "
            f"during {elapsed * 1000:.1f}ms decode (expected >= {min_ticks})"
        )

    @pytest.mark.asyncio
    async def test_sync_decode_in_loop_does_block_loop(self):
        """Inverse check: the SYNC function called directly in the loop
        DOES block. This isn't testing our code so much as proving the
        test methodology works — if this test fails (i.e., sync doesn't
        block), our async test is meaningless."""
        from astrbot_plugin_webchat_gateway.core.image_util import (
            detect_image_mime,
        )

        content = _big_png_bytes(side=4000)

        ticks = 0
        stop = False

        async def heartbeat():
            nonlocal ticks
            while not stop:
                ticks += 1
                await asyncio.sleep(0.001)

        hb_task = asyncio.create_task(heartbeat())
        try:
            # Let heartbeat tick a few times so we know it's running
            await asyncio.sleep(0.02)
            baseline = ticks
            t0 = time.monotonic()
            result = detect_image_mime(content)  # blocks the loop
            elapsed = time.monotonic() - t0
            ticks_during = ticks - baseline
        finally:
            stop = True
            await asyncio.sleep(0)
            hb_task.cancel()
            try:
                await hb_task
            except asyncio.CancelledError:
                pass

        assert result == "image/png"
        print(
            f"\n  sync_decode={elapsed * 1000:.1f}ms ticks_during={ticks_during} "
            f"baseline={baseline}"
        )
        # If the sync call really blocks the loop, no heartbeat can run
        # while it executes — so ticks_during should be ~0. If decode
        # was too fast to be conclusive, skip the assert.
        if elapsed > 0.02:
            assert ticks_during <= 2, (
                f"Expected sync decode to block the loop but heartbeat "
                f"ticked {ticks_during} times during {elapsed * 1000:.1f}ms "
                f"of sync work. Loop policy may have changed."
            )
