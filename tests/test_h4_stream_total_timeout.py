"""Regression: H4 — streaming LLM must enforce a TOTAL wall-clock budget.

`generate_reply_stream` previously had only a per-chunk idle timeout.
A misbehaving provider that emitted one byte every `(idle - ε)` seconds
would reset the idle clock on every crumb of progress and pin the
per-token concurrency lock for hours.

The fix adds an independent total wall-clock budget
(`total_stream_timeout_seconds`, defaulted to 8× idle capped at 600s)
that fires as `RuntimeError("llm_timeout")` regardless of how active
the provider is. Per-chunk and total timeouts are both enforced; the
effective wait per iteration is `min(idle, remaining_total)`.

This test models the drip-feed scenario:
  - per-chunk idle = 0.5s
  - chunk cadence  = 0.05s   (well inside idle — no idle-timeout fires)
  - total budget   = 0.3s    (forces the total-budget branch)
"""

from __future__ import annotations

import asyncio
import time

import pytest

# We reuse the stub helpers from the existing LlmBridge tests by
# duplicating the minimal subset here — keeps this file self-contained
# and decouples its lifetime from any test_llm_bridge refactor.


class _StubResp:
    def __init__(self, text: str, *, is_chunk: bool = False) -> None:
        self.completion_text = text
        self.is_chunk = is_chunk


class _DripFeedProvider:
    """Yields `is_chunk=True` deltas forever at a fixed cadence. Used
    to drive the bridge through many idle-windows so only the total
    budget can stop it."""

    def __init__(self, *, cadence_seconds: float) -> None:
        self._cadence = cadence_seconds

    async def text_chat_stream(self, **kwargs):
        i = 0
        while True:
            await asyncio.sleep(self._cadence)
            i += 1
            yield _StubResp(f"chunk{i}", is_chunk=True)


class _StubPersonaManager:
    async def get_persona(self, persona_id):
        return None


class _StubConversationManager:
    async def get_curr_conversation_id(self, umo):
        return "cid-stub"

    async def new_conversation(self, umo, **kwargs):
        return "cid-stub"

    async def get_human_readable_context(self, **kwargs):
        return [], 0


class _StubContext:
    def __init__(self, provider):
        self.provider = provider
        self.persona_manager = _StubPersonaManager()
        self.conversation_manager = _StubConversationManager()

    async def get_current_chat_provider_id(self, umo):
        return "prov-stub"

    def get_provider_by_id(self, provider_id):
        return self.provider


@pytest.mark.asyncio
async def test_drip_feed_provider_hits_total_budget():
    """Provider yields a chunk every 50ms forever; per-chunk idle is
    500ms (so idle-timeout never fires); total budget is 300ms. The
    stream must raise `llm_timeout` from the total-budget branch,
    NOT idle-timeout."""
    from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

    ctx = _StubContext(_DripFeedProvider(cadence_seconds=0.05))
    bridge = LlmBridge(
        ctx,
        history_turns=0,
        persona_id="",
        timeout_seconds=0.5,            # per-chunk idle
        total_stream_timeout_seconds=0.3,  # total wall-clock
    )

    collected: list[str] = []
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="llm_timeout"):
        # asyncio.wait_for as a hard backstop — if the fix is missing
        # the loop drip-feeds forever and we'd hang. 5s is generous
        # vs. the expected ~0.3s + small overhead.
        async def _consume():
            async for text in bridge.generate_reply_stream(
                token_name="alice",
                session_id="s1",
                username="alice",
                message="hi",
            ):
                collected.append(text)

        await asyncio.wait_for(_consume(), timeout=5.0)
    elapsed = time.monotonic() - started

    # The provider yielded at least one chunk before the budget tripped
    # (0.05s cadence vs 0.3s budget).
    assert collected, "expected at least one chunk before total-budget timeout"
    # Stream cut off near the total budget, not after the 5s backstop.
    # Allow some slack for scheduler jitter on slow CI.
    assert elapsed < 2.0, (
        f"stream did not honour total wall-clock budget — elapsed={elapsed:.2f}s "
        "(expected near 0.3s). Pre-fix: stream drip-feeds forever and the 5s "
        "backstop fires as TimeoutError, not as RuntimeError('llm_timeout')."
    )


@pytest.mark.asyncio
async def test_default_total_budget_derived_from_idle():
    """When the caller omits `total_stream_timeout_seconds`, LlmBridge
    must compute a safe default rather than disabling the budget.
    The contract: default = min(8 × idle, 600), floor at 2 × idle."""
    from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

    ctx = _StubContext(_DripFeedProvider(cadence_seconds=0.05))

    # idle=10 → default total = min(8×10, 600) = 80
    b1 = LlmBridge(ctx, history_turns=0, persona_id="", timeout_seconds=10)
    assert b1._total_stream_timeout == 80.0

    # idle=100 → default total = min(8×100, 600) = 600 (cap)
    b2 = LlmBridge(ctx, history_turns=0, persona_id="", timeout_seconds=100)
    assert b2._total_stream_timeout == 600.0

    # Explicit override wins.
    b3 = LlmBridge(
        ctx,
        history_turns=0,
        persona_id="",
        timeout_seconds=60,
        total_stream_timeout_seconds=42,
    )
    assert b3._total_stream_timeout == 42.0

    # Negative / zero override disables.
    b4 = LlmBridge(
        ctx,
        history_turns=0,
        persona_id="",
        timeout_seconds=60,
        total_stream_timeout_seconds=-1,
    )
    assert b4._total_stream_timeout is None
