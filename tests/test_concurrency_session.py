"""Per-(token, session) concurrency lock.

The lock key moved from token to (token, session) so a long image generation
in one session no longer blocks chat in another. These pin both halves of that
contract: different sessions of one token acquire concurrently, while two turns
in the SAME session still single-flight (which keeps history ordering + the
optimistic-echo dedup correct).
"""

from __future__ import annotations

import pytest


def _rl():
    from astrbot_plugin_webchat_gateway.core.ratelimit import (
        PerTokenConcurrency,
        session_key,
    )

    return PerTokenConcurrency, session_key


@pytest.mark.asyncio
async def test_different_sessions_same_token_run_concurrently():
    PerTokenConcurrency, session_key = _rl()
    c = PerTokenConcurrency()
    async with c.acquire(session_key("alice", "s1")) as a1:
        assert a1 is True
        # A second session of the SAME token is NOT blocked.
        async with c.acquire(session_key("alice", "s2")) as a2:
            assert a2 is True


@pytest.mark.asyncio
async def test_same_session_is_single_flight():
    PerTokenConcurrency, session_key = _rl()
    c = PerTokenConcurrency()
    async with c.acquire(session_key("alice", "s1")) as a1:
        assert a1 is True
        # Same (token, session) → second acquire refused (single-flight 429).
        async with c.acquire(session_key("alice", "s1")) as a2:
            assert a2 is False
    # Released on exit → acquirable again.
    async with c.acquire(session_key("alice", "s1")) as a3:
        assert a3 is True


@pytest.mark.asyncio
async def test_acquire_with_id_is_per_session():
    PerTokenConcurrency, session_key = _rl()
    c = PerTokenConcurrency()
    k1 = session_key("bob", "s1")
    k2 = session_key("bob", "s2")
    assert await c.acquire_with_id(k1, "stream-1") is True
    # Same token, different session → concurrent stream allowed.
    assert await c.acquire_with_id(k2, "stream-2") is True
    # Same session → refused while held.
    assert await c.acquire_with_id(k1, "stream-1b") is False
    await c.release(k1, "stream-1")
    await c.release(k2, "stream-2")
    # After release, the session can acquire again.
    assert await c.acquire_with_id(k1, "stream-3") is True
    await c.release(k1, "stream-3")


def test_session_key_is_nul_separated_and_collision_free():
    _PerTokenConcurrency, session_key = _rl()
    assert session_key("a", "b") == "a\x00b"
    # No collision across the token/session boundary.
    assert session_key("a", "b") != session_key("ab", "")
    assert session_key("a", "bc") != session_key("ab", "c")
