"""LlmBridge unit tests.

Covers non-streaming + streaming reply generation after the P0 refactor:
both paths call `provider.text_chat` / `text_chat_stream` with the current
message as `prompt`, the prior turns as structured `contexts` (read RAW
from CM `get_conversation().history`), and the persona via the
`system_prompt` channel — NOT flattened into the prompt text.

The stubbed `_StubContext` mirrors only the AstrBot surface the bridge
touches:

  * `get_current_chat_provider_id(umo=...)`
  * `get_provider_by_id(provider_id)` → object with
    `text_chat(**kwargs)` and `text_chat_stream(**kwargs)`
  * `persona_manager.get_persona(persona_id)` → object with
    `system_prompt` attribute
  * `conversation_manager.{get_curr_conversation_id, new_conversation,
    get_conversation}` where `get_conversation(...).history` is the
    JSON the bridge parses into `contexts`.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest


# --- Stub helpers ----------------------------------------------------------


class _StubResp:
    """Mimic AstrBot's LLMResponse — only `completion_text` is read."""

    def __init__(self, text: str, *, is_chunk: bool = False) -> None:
        self.completion_text = text
        self.is_chunk = is_chunk


class _StubPersona:
    def __init__(self, system_prompt: str) -> None:
        self.system_prompt = system_prompt


class _StubProvider:
    """Programmable provider. Records every text_chat / text_chat_stream
    call so tests can assert which kwargs were passed."""

    def __init__(
        self,
        *,
        text_chat_reply: str = "stub-reply",
        stream_chunks: list[str] | None = None,
    ) -> None:
        self.text_chat_reply = text_chat_reply
        self.stream_chunks = stream_chunks or []
        self.text_chat_calls: list[dict[str, Any]] = []
        self.text_chat_stream_calls: list[dict[str, Any]] = []

    async def text_chat(self, **kwargs: Any) -> _StubResp:
        self.text_chat_calls.append(dict(kwargs))
        return _StubResp(self.text_chat_reply)

    async def text_chat_stream(self, **kwargs: Any):
        self.text_chat_stream_calls.append(dict(kwargs))
        for text in self.stream_chunks:
            yield _StubResp(text, is_chunk=True)


class _StubConversation:
    """Mimic AstrBot's Conversation — the bridge reads `.history`, a JSON
    string of provider-native records (the same shape `add_message_pair`
    persists on the write path)."""

    def __init__(self, history_entries: list[dict] | None) -> None:
        self.history = json.dumps(history_entries or [])


class _StubConversationManager:
    """In-memory CM stub. The bridge calls `get_curr_conversation_id`,
    `new_conversation`, and `get_conversation(...).history`.

    `history_entries` is the RAW provider-native list the bridge windows
    into `contexts`; set on construction so individual tests can simulate
    long-history / trailing-user / multimodal scenarios."""

    def __init__(self, history_entries: list[dict] | None = None) -> None:
        self.curr_cid: str | None = None
        self.new_conv_calls: list[dict[str, Any]] = []
        self._history_entries = history_entries or []

    async def get_curr_conversation_id(self, umo: str) -> str | None:
        return self.curr_cid

    async def new_conversation(self, umo: str, **kwargs: Any) -> str:
        self.new_conv_calls.append({"umo": umo, **kwargs})
        self.curr_cid = "cid-stub"
        return self.curr_cid

    async def get_conversation(self, umo: str, cid: str) -> _StubConversation:
        return _StubConversation(self._history_entries)


class _StubPersonaManager:
    def __init__(self, persona: _StubPersona | None) -> None:
        self._persona = persona

    async def get_persona(self, persona_id: str) -> _StubPersona | None:
        return self._persona


class _StubContext:
    """Mimic the AstrBot Context surface LlmBridge touches."""

    def __init__(
        self,
        *,
        provider_id: str | None = "prov-stub",
        provider: _StubProvider | None = None,
        persona: _StubPersona | None = None,
        history_entries: list[dict] | None = None,
    ) -> None:
        self._provider_id = provider_id
        self.provider = provider or _StubProvider()
        self.persona_manager = _StubPersonaManager(persona)
        self.conversation_manager = _StubConversationManager(
            history_entries=history_entries
        )
        # Spy on provider lookups.
        self.get_provider_by_id_calls: list[str] = []

    async def get_current_chat_provider_id(self, umo: str) -> str | None:
        return self._provider_id

    def get_provider_by_id(self, provider_id: str) -> _StubProvider | None:
        self.get_provider_by_id_calls.append(provider_id)
        return self.provider


# --- Test classes ----------------------------------------------------------


class TestGenerateReplyHappyPath:
    @pytest.mark.asyncio
    async def test_text_only_calls_provider_text_chat_without_image_urls(self):
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        ctx = _StubContext()
        bridge = LlmBridge(ctx, history_turns=0, persona_id="", timeout_seconds=10)
        reply = await bridge.generate_reply(
            token_name="alice",
            session_id="s1",
            username="alice",
            message="hello",
        )
        assert reply == "stub-reply"
        assert len(ctx.provider.text_chat_calls) == 1
        call = ctx.provider.text_chat_calls[0]
        assert call["prompt"] == "hello"
        assert "image_urls" not in call, (
            "Text-only call must NOT pass image_urls — older provider "
            "builds would reject the kwarg with TypeError"
        )
        # history_turns=0 → empty structured context, still passed explicitly.
        assert call["contexts"] == []

    @pytest.mark.asyncio
    async def test_with_image_calls_provider_text_chat_with_image_urls(self):
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        ctx = _StubContext()
        bridge = LlmBridge(ctx, history_turns=0, persona_id="", timeout_seconds=10)
        reply = await bridge.generate_reply(
            token_name="alice",
            session_id="s1",
            username="alice",
            message="describe this",
            image_urls=["/tmp/img.png"],
        )
        assert reply == "stub-reply"
        call = ctx.provider.text_chat_calls[0]
        assert call.get("image_urls") == ["/tmp/img.png"]
        assert call["prompt"] == "describe this"


class TestPersonaResolution:
    @pytest.mark.asyncio
    async def test_resolved_system_prompt_passed_via_channel(self):
        """Persona's system_prompt rides the real `system_prompt` kwarg,
        NOT an inline `[System Prompt]` block in the prompt text."""
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        ctx = _StubContext(persona=_StubPersona("be a poet"))
        bridge = LlmBridge(
            ctx, history_turns=0, persona_id="poet-1", timeout_seconds=10
        )
        await bridge.generate_reply(
            token_name="alice",
            session_id="s1",
            username="alice",
            message="hello",
        )
        call = ctx.provider.text_chat_calls[0]
        assert call.get("system_prompt") == "be a poet"
        assert call["prompt"] == "hello"
        assert "[System Prompt]" not in call["prompt"]
        assert "be a poet" not in call["prompt"]

    @pytest.mark.asyncio
    async def test_no_persona_omits_system_prompt(self):
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        ctx = _StubContext(persona=None)
        bridge = LlmBridge(ctx, history_turns=0, persona_id="", timeout_seconds=10)
        await bridge.generate_reply(
            token_name="alice",
            session_id="s1",
            username="alice",
            message="hello",
        )
        call = ctx.provider.text_chat_calls[0]
        assert not call.get("system_prompt"), (
            f"No persona configured but system_prompt={call.get('system_prompt')!r}"
        )


@pytest.mark.asyncio
class TestStructuredContexts:
    """P0 core invariant. History is fed back as structured `contexts`
    (raw provider-native records read from CM), never flattened into the
    prompt text."""

    async def test_history_passed_as_structured_contexts_not_flattened(self):
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        entries = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello there"},
        ]
        ctx = _StubContext(history_entries=entries)
        bridge = LlmBridge(ctx, history_turns=8, persona_id="", timeout_seconds=10)
        await bridge.generate_reply(
            token_name="alice",
            session_id="s1",
            username="alice",
            message="continue?",
        )
        call = ctx.provider.text_chat_calls[0]
        # Structured, role-preserving, passed verbatim.
        assert call["contexts"] == entries
        # Current message is the prompt; history text is NOT in the prompt.
        assert call["prompt"] == "continue?"
        assert "hi" not in call["prompt"]
        assert "hello there" not in call["prompt"]

    async def test_trailing_user_turn_dropped_from_contexts(self):
        """Regenerate rewrites CM history ending at the user turn, then
        calls with the same user text as `message`. The trailing user
        entry must be dropped from `contexts` so it isn't duplicated with
        the prompt."""
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        entries = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q1-again"},
        ]
        ctx = _StubContext(history_entries=entries)
        bridge = LlmBridge(ctx, history_turns=8, persona_id="", timeout_seconds=10)
        await bridge.generate_reply(
            token_name="alice",
            session_id="s1",
            username="alice",
            message="q1-again",
        )
        call = ctx.provider.text_chat_calls[0]
        assert call["contexts"] == [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ], "Trailing user turn must be dropped to avoid duplicating the prompt"


@pytest.mark.asyncio
class TestStreamNonStreamParity:
    """The streaming and non-streaming paths must feed the model the same
    way — both send system_prompt via the channel (the pre-P0 stream path
    sent neither system_prompt nor contexts)."""

    async def test_both_paths_send_system_prompt_and_contexts(self):
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        provider = _StubProvider(text_chat_reply="ns-reply", stream_chunks=["x"])
        ctx = _StubContext(
            provider=provider,
            persona=_StubPersona("speak plainly"),
            history_entries=[{"role": "user", "content": "earlier"}],
        )
        bridge = LlmBridge(
            ctx, history_turns=8, persona_id="p1", timeout_seconds=10
        )

        await bridge.generate_reply(
            token_name="alice", session_id="s1", username="alice", message="hi"
        )
        async for _ in bridge.generate_reply_stream(
            token_name="alice", session_id="s1", username="alice", message="hi"
        ):
            pass

        ns = provider.text_chat_calls[0]
        st = provider.text_chat_stream_calls[0]
        for label, call in (("non-stream", ns), ("stream", st)):
            assert call.get("system_prompt") == "speak plainly", (
                f"{label} path dropped system_prompt: {call!r}"
            )
            assert call["prompt"] == "hi", f"{label} inlined something into prompt"
            assert "contexts" in call, f"{label} path sent no contexts"


@pytest.mark.asyncio
class TestGenerateReplyStream:
    async def test_yields_chunks_in_order(self):
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        provider = _StubProvider(stream_chunks=["hello", " ", "world"])
        ctx = _StubContext(provider=provider)
        bridge = LlmBridge(ctx, history_turns=0, persona_id="", timeout_seconds=10)

        collected: list[str] = []
        async for chunk in bridge.generate_reply_stream(
            token_name="alice",
            session_id="s1",
            username="alice",
            message="hi",
        ):
            collected.append(chunk)
        assert collected == ["hello", " ", "world"]
        assert len(provider.text_chat_stream_calls) == 1
        call = provider.text_chat_stream_calls[0]
        assert call["prompt"] == "hi"
        assert "contexts" in call

    async def test_empty_stream_raises_empty_reply(self):
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        provider = _StubProvider(stream_chunks=[])  # nothing yielded
        ctx = _StubContext(provider=provider)
        bridge = LlmBridge(ctx, history_turns=0, persona_id="", timeout_seconds=10)

        with pytest.raises(RuntimeError, match="empty_reply"):
            async for _ in bridge.generate_reply_stream(
                token_name="alice",
                session_id="s1",
                username="alice",
                message="hi",
            ):
                pass

    async def test_image_urls_forwarded_to_text_chat_stream(self):
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        provider = _StubProvider(stream_chunks=["x"])
        ctx = _StubContext(provider=provider)
        bridge = LlmBridge(ctx, history_turns=0, persona_id="", timeout_seconds=10)

        async for _ in bridge.generate_reply_stream(
            token_name="alice",
            session_id="s1",
            username="alice",
            message="hi",
            image_urls=["/tmp/img.png"],
        ):
            pass
        assert provider.text_chat_stream_calls[0].get("image_urls") == [
            "/tmp/img.png"
        ]

    async def test_text_only_stream_omits_image_urls(self):
        """Don't pass image_urls on text-only calls — older provider
        builds without the kwarg crash with TypeError."""
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        provider = _StubProvider(stream_chunks=["x"])
        ctx = _StubContext(provider=provider)
        bridge = LlmBridge(ctx, history_turns=0, persona_id="", timeout_seconds=10)

        async for _ in bridge.generate_reply_stream(
            token_name="alice",
            session_id="s1",
            username="alice",
            message="hi",
        ):
            pass
        call = provider.text_chat_stream_calls[0]
        assert "image_urls" not in call
        assert "contexts" in call


@pytest.mark.asyncio
class TestImagePathStructuredContexts:
    """Successor to the old TypeError-fallback region. The image path now
    goes straight through `provider.text_chat` and must carry the persona
    system_prompt, the image_urls, AND the structured contexts."""

    async def test_image_path_uses_text_chat_with_contexts_and_system_prompt(self):
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        provider = _StubProvider(text_chat_reply="img-reply")
        entries = [
            {"role": "user", "content": "prev"},
            {"role": "assistant", "content": "resp"},
        ]
        ctx = _StubContext(
            provider=provider,
            persona=_StubPersona("haiku only"),
            history_entries=entries,
        )
        bridge = LlmBridge(
            ctx, history_turns=8, persona_id="p1", timeout_seconds=10
        )
        reply = await bridge.generate_reply(
            token_name="alice",
            session_id="s1",
            username="alice",
            message="describe this image",
            image_urls=["/tmp/img.png"],
        )
        assert reply == "img-reply"
        call = provider.text_chat_calls[0]
        assert call.get("system_prompt") == "haiku only", (
            "P1-1 invariant: multimodal request under a persona must carry "
            f"the persona system_prompt. Got: {call!r}"
        )
        assert call["image_urls"] == ["/tmp/img.png"]
        assert call["contexts"] == entries
        assert call["prompt"] == "describe this image"


@pytest.mark.asyncio
class TestProviderConfigErrors:
    async def test_missing_provider_id_raises_runtime_error(self):
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        ctx = _StubContext(provider_id=None)
        bridge = LlmBridge(ctx, history_turns=0, persona_id="", timeout_seconds=10)

        with pytest.raises(RuntimeError, match="chat_provider_not_configured"):
            await bridge.generate_reply(
                token_name="alice",
                session_id="s1",
                username="alice",
                message="hi",
            )

    async def test_empty_reply_raises_empty_reply(self):
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        provider = _StubProvider(text_chat_reply="")  # provider returns empty
        ctx = _StubContext(provider=provider)
        bridge = LlmBridge(ctx, history_turns=0, persona_id="", timeout_seconds=10)

        with pytest.raises(RuntimeError, match="empty_reply"):
            await bridge.generate_reply(
                token_name="alice",
                session_id="s1",
                username="alice",
                message="hi",
            )


@pytest.mark.asyncio
class TestHistoryBudget:
    """P1-5 successor. `_history_contexts` windows to `history_turns*2`
    messages and applies a coarse `_MAX_HISTORY_CHARS` budget newest-first
    — dropping older entries WHOLE, never bisecting a single message."""

    async def test_short_history_passes_through_unmodified(self):
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        entries = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "how are you"},
            {"role": "assistant", "content": "good"},  # trailing assistant
        ]
        ctx = _StubContext(history_entries=entries)
        bridge = LlmBridge(ctx, history_turns=8, persona_id="", timeout_seconds=10)
        await bridge.generate_reply(
            token_name="alice",
            session_id="s1",
            username="alice",
            message="continue?",
        )
        call = ctx.provider.text_chat_calls[0]
        # Well under budget → every entry survives, in order, intact.
        assert call["contexts"] == entries

    async def test_long_history_drops_oldest_whole_entries(self):
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        # 20 entries × ~600 chars = ~12000 chars > 8000 budget. Distinct
        # markers at both ends; trailing entry is assistant so the
        # trailing-user de-dup rule doesn't interfere. history_turns large
        # enough that the char budget (not the turn window) is the binding
        # constraint.
        oldest = {"role": "user", "content": "OLDEST_" + "x" * 600}
        newest = {"role": "assistant", "content": "NEWEST_" + "x" * 600}
        middle = [
            {"role": ("user" if i % 2 == 0 else "assistant"),
             "content": f"mid{i}_" + "x" * 600}
            for i in range(18)
        ]
        entries = [oldest] + middle + [newest]
        ctx = _StubContext(history_entries=entries)
        bridge = LlmBridge(
            ctx, history_turns=20, persona_id="", timeout_seconds=10
        )
        await bridge.generate_reply(
            token_name="alice",
            session_id="s1",
            username="alice",
            message="continue?",
        )
        contexts = ctx.provider.text_chat_calls[0]["contexts"]

        total_chars = sum(len(e["content"]) for e in contexts)
        assert total_chars <= 8000, (
            f"history budget exceeded: {total_chars} chars"
        )
        # Newest kept, oldest dropped.
        assert any("NEWEST_" in e["content"] for e in contexts)
        assert not any("OLDEST_" in e["content"] for e in contexts)
        # No single entry was bisected — every surviving content is the
        # full original string (length 600 + prefix), never a slice.
        originals = {e["content"] for e in entries}
        for e in contexts:
            assert e["content"] in originals, (
                "a surviving entry was truncated mid-message"
            )


def test_map_llm_error_routes_known_codes():
    """Sanity: the centralised error mapper still routes the codes
    chat / stream / regenerate all depend on."""
    from astrbot_plugin_webchat_gateway.core.llm_bridge import map_llm_error

    assert map_llm_error(RuntimeError("llm_timeout")) == (
        "llm_timeout",
        504,
        "llm_timeout",
    )
    assert map_llm_error(RuntimeError("empty_reply")) == (
        "empty_reply",
        502,
        "chat_empty_reply",
    )
    assert map_llm_error(ValueError("anything else")) == (
        "llm_call_failed",
        500,
        "chat_error",
    )


# Suppress unused-import warning for MagicMock — kept for future
# expansion (e.g. inspecting persona_manager call shape).
_ = MagicMock
