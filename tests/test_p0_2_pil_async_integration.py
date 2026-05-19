"""P0-2 integration test: real aiohttp server, 10 concurrent /events
long-polls, 1 /upload of a 20MB JPEG. The actual test of the property
the user cares about — heartbeats keep arriving across all 10 clients
while detect_image_mime_async is decoding the 20 MB body.

We mount a thin aiohttp app that exercises the actual
`detect_image_mime_async` (the production function under test) and an
SSE-style /events route that emits heartbeats on a fixed cadence. The
full production /upload + /events handlers carry auth / quota / IP
guard / event-bus wiring that doesn't bear on the property under test
— mounting them would require ~13 deps each to stub and would dilute
the signal. The handler bodies here mirror the relevant call paths
(read body → await detect_image_mime_async; loop → write SSE chunk
→ asyncio.sleep) so a future regression that reintroduces a sync call
upstream of the wrapper still fails this test.
"""

from __future__ import annotations

import asyncio
import io
import time

import pytest
from aiohttp import ClientTimeout, web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image


# Per-client tally so we can assert heartbeat coverage during the
# upload window in addition to total ticks (a client that received all
# its heartbeats AFTER the upload finished would still pass a naive
# total-count check).
_HEARTBEAT_INTERVAL_SECONDS = 0.01  # 10 ms — fine-grained enough that a
                                    # tens-of-ms decode produces several
                                    # ticks per client


def _twenty_mb_jpeg() -> bytes:
    """Build a JPEG that exercises the PIL decode path long enough to
    observe loop-blocking behavior. We use a real photographic image
    (random pixels) rather than a flat color so the file actually hits
    ~10-20 MB after JPEG compression. Smaller files decode too fast to
    be useful."""
    import os

    # 4000x4000 RGB random pixels → JPEG ~15-25 MB depending on quality
    rgb = os.urandom(4000 * 4000 * 3)
    img = Image.frombytes("RGB", (4000, 4000), rgb)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _build_test_app(
    *, use_async_decode: bool = True, force_blocking_ms: int = 0
) -> web.Application:
    """Mount minimal /upload and /events that exercise the real
    `detect_image_mime_async` + a real SSE loop.

    `use_async_decode=False` swaps the upload handler to call the SYNC
    `detect_image_mime` directly on the event loop — that's the
    pre-P0-2 behavior. Whether this actually blocks the loop measurably
    depends on Pillow's `verify()` being slow enough for the workload.

    `force_blocking_ms > 0` injects a `time.sleep` into the upload
    handler for the methodology-control test below. It's a guaranteed
    loop-blocker independent of Pillow speed, so the test fixture can
    demonstrate it CAN detect blocking when blocking occurs."""
    from astrbot_plugin_webchat_gateway.core.image_util import (
        detect_image_mime,
        detect_image_mime_async,
    )

    async def upload(request: web.Request) -> web.Response:
        body = await request.read()
        if force_blocking_ms > 0:
            # Synthetic blocker: prove the test fixture can detect a
            # blocked loop. Independent of Pillow internals.
            time.sleep(force_blocking_ms / 1000.0)
            mime = detect_image_mime(body)  # fast verify after sleep
        elif use_async_decode:
            # Production code path: handlers/files.py:253.
            mime = await detect_image_mime_async(body)
        else:
            # Pre-P0-2 behavior: sync call on loop thread.
            mime = detect_image_mime(body)
        if mime is None:
            return web.json_response({"error": "invalid_image"}, status=400)
        return web.json_response({"mime": mime, "size": len(body)})

    async def events(request: web.Request) -> web.StreamResponse:
        """SSE-style heartbeat: emits `:hb\n\n` on a fixed cadence
        until the client disconnects. Models the production /events
        long-poll's heartbeat pattern."""
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)
        try:
            for _ in range(200):  # bounded so a runaway test still exits
                await resp.write(b":hb\n\n")
                await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    app = web.Application(client_max_size=64 * 1024 * 1024)  # 64 MB cap
    app.router.add_post("/upload", upload)
    app.router.add_get("/events", events)
    return app


async def _run_upload_with_concurrent_pollers(
    *, use_async_decode: bool, force_blocking_ms: int = 0
) -> tuple[float, dict[int, list[float]]]:
    """Drive the integration scenario and return (upload_elapsed,
    per_client_heartbeat_timestamps)."""
    app = _build_test_app(
        use_async_decode=use_async_decode,
        force_blocking_ms=force_blocking_ms,
    )
    server = TestServer(app)
    await server.start_server()
    try:
        client = TestClient(server)
        await client.start_server()
        try:
            jpeg = _twenty_mb_jpeg()
            heartbeats: dict[int, list[float]] = {i: [] for i in range(10)}
            stop_clients = asyncio.Event()

            async def long_poll_client(i: int) -> None:
                try:
                    async with client.get(
                        "/events", timeout=ClientTimeout(total=30)
                    ) as resp:
                        assert resp.status == 200
                        async for line in resp.content:
                            if stop_clients.is_set():
                                break
                            if line.strip().startswith(b":hb"):
                                heartbeats[i].append(time.monotonic())
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                except Exception as e:
                    print(f"  client {i} error: {e!r}")

            poll_tasks = [
                asyncio.create_task(long_poll_client(i)) for i in range(10)
            ]
            # Establishment: every long-poller registers and receives
            # several heartbeats before the upload kicks in.
            await asyncio.sleep(0.05)

            t_upload_start = time.monotonic()
            upload_resp = await client.post(
                "/upload",
                data=jpeg,
                headers={"Content-Type": "image/jpeg"},
                timeout=ClientTimeout(total=60),
            )
            t_upload_end = time.monotonic()
            upload_payload = await upload_resp.json()
            assert upload_resp.status == 200, (
                upload_resp.status,
                upload_payload,
            )
            assert upload_payload["mime"] == "image/jpeg"
            upload_elapsed = t_upload_end - t_upload_start

            # Let any latent heartbeats drain before we cut the cord.
            await asyncio.sleep(0.05)
            stop_clients.set()
            for t in poll_tasks:
                t.cancel()
            await asyncio.gather(*poll_tasks, return_exceptions=True)
            return upload_elapsed, heartbeats
        finally:
            await client.close()
    finally:
        await server.close()


def _max_inter_arrival_gap(timestamps: list[float]) -> float:
    """Largest gap between consecutive heartbeat arrivals for one
    client, in seconds. A blocked event loop manifests here: while the
    loop is stuck, no heartbeat arrives, so the gap on either side of
    the blocking call grows to the blocking duration."""
    if len(timestamps) < 2:
        return 0.0
    return max(b - a for a, b in zip(timestamps, timestamps[1:]))


@pytest.mark.asyncio
async def test_upload_does_not_starve_concurrent_long_polls():
    """P0-2 main assertion: with the async wrapper, every /events
    client sees heartbeats arriving on cadence throughout the upload —
    no gap larger than a small multiple of the 10 ms heartbeat
    interval, even while a ~20 MB JPEG is being decoded."""
    elapsed, heartbeats = await _run_upload_with_concurrent_pollers(
        use_async_decode=True
    )
    gaps = {i: _max_inter_arrival_gap(ts) for i, ts in heartbeats.items()}
    counts = {i: len(ts) for i, ts in heartbeats.items()}
    print(f"\n  ASYNC upload_elapsed={elapsed * 1000:.0f}ms")
    print(f"  per_client_count={list(counts.values())}")
    print(
        f"  per_client_max_gap_ms="
        f"{[f'{g * 1000:.1f}' for g in gaps.values()]}"
    )

    # Every client received heartbeats (sanity).
    assert all(c >= 3 for c in counts.values()), (
        f"Expected >=3 heartbeats per client; got {counts}"
    )

    # The strong invariant: no inter-heartbeat gap on any client exceeds
    # 50 ms (5× cadence). A blocked loop would produce a gap >= upload
    # decode time (often 30-200 ms) — well above 50 ms. The 5× headroom
    # absorbs normal scheduler jitter without admitting the
    # "loop was blocked" hypothesis.
    max_gap_overall_ms = max(gaps.values()) * 1000
    threshold_ms = 50.0
    assert max_gap_overall_ms <= threshold_ms, (
        f"Loop appears to have stalled: max inter-heartbeat gap was "
        f"{max_gap_overall_ms:.1f} ms (threshold {threshold_ms:.0f} ms). "
        f"upload_elapsed={elapsed * 1000:.0f}ms, per_client_gap_ms="
        f"{[f'{g * 1000:.1f}' for g in gaps.values()]}"
    )


@pytest.mark.asyncio
async def test_methodology_control_blocking_sleep_starves_long_polls():
    """Methodology control: an explicit `time.sleep(0.15)` inside
    /upload provably blocks the loop. Each client's max
    inter-heartbeat gap should be >= ~150 ms, demonstrating that the
    test fixture CAN distinguish blocked vs. unblocked loops. If this
    control ever passes the async test's threshold, the test fixture
    is broken, not the production code."""
    elapsed, heartbeats = await _run_upload_with_concurrent_pollers(
        use_async_decode=False, force_blocking_ms=150
    )
    gaps = {i: _max_inter_arrival_gap(ts) for i, ts in heartbeats.items()}
    counts = {i: len(ts) for i, ts in heartbeats.items()}
    print(f"\n  BLOCKING_SLEEP upload_elapsed={elapsed * 1000:.0f}ms")
    print(f"  per_client_count={list(counts.values())}")
    print(
        f"  per_client_max_gap_ms="
        f"{[f'{g * 1000:.1f}' for g in gaps.values()]}"
    )

    # Every client should have a gap close to 150 ms (the sleep
    # duration). We assert >= 100 ms to allow for some scheduling
    # jitter at the boundaries, while still safely above the 50 ms
    # threshold the main P0-2 assertion uses.
    max_gap_overall_ms = max(gaps.values()) * 1000
    assert max_gap_overall_ms >= 100.0, (
        f"Methodology control failed: a 150 ms blocking sleep produced "
        f"a max inter-heartbeat gap of only {max_gap_overall_ms:.1f} ms. "
        f"The test fixture cannot distinguish blocked vs. unblocked "
        f"loops — the main P0-2 assertion is meaningless until this is "
        f"fixed."
    )
