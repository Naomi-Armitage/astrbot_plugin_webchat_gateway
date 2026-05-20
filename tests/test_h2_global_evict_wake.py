"""Regression: H2 — InMemoryBuffer global-cap eviction must wake subscribers.

The per-token eviction branch in `InMemoryBuffer.create()` routed the
victim through `_evict_locked`, which sets `entry.terminal` and
`entry.new_chunk` so any parked `iter_subscribe` wakes and discovers
the entry is gone. The global-cap branch instead did a bare
`_entries.pop(...)` — same row drop, no wake — so a subscriber racing
between snapshot (entry present, terminal=False) and the cap eviction
sleeps on a dead `Event` until outer cancellation.

The fix routes both branches through `_evict_locked`. This test pins
the post-fix invariant directly: after global-cap eviction, the
evicted entry's events are set even though the entry is no longer in
the map.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_global_cap_eviction_sets_wake_events():
    from astrbot_plugin_webchat_gateway.core.stream_buffer import InMemoryBuffer

    # max_per_token high enough to never fire — only global cap matters.
    buf = InMemoryBuffer(grace_seconds=60, max_per_token=10, max_global=2)

    # Seed A as a closed (terminal) entry so the cap loop is willing to
    # evict it. close() sets the events as part of its own contract; we
    # clear them again so the assertion below reflects what global-cap
    # eviction did, not what close() did earlier.
    await buf.create(stream_id="sid-A", token_name="alice", session_id="s1")
    await buf.append_chunk("sid-A", seq=1, text="x")
    await buf.close("sid-A", state="closed_ok", final={})

    entry_a = buf._entries["sid-A"]  # type: ignore[attr-defined]
    entry_a.terminal = asyncio.Event()
    entry_a.new_chunk = asyncio.Event()

    # Fill to global cap (B is the second slot) then create C — that
    # triggers the global-cap eviction branch for A.
    await buf.create(stream_id="sid-B", token_name="bob", session_id="s2")
    await buf.create(stream_id="sid-C", token_name="carol", session_id="s3")

    assert "sid-A" not in buf._entries  # type: ignore[attr-defined]
    # Pre-fix these would still be unset — that's the wake-loss bug.
    assert entry_a.terminal.is_set(), (
        "global-cap eviction must set terminal so iter_subscribe wakes"
    )
    assert entry_a.new_chunk.is_set(), (
        "global-cap eviction must set new_chunk too (mirrors _evict_locked)"
    )
