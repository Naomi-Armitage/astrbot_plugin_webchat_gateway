"""LlmBridge unit tests.

Covers the production code paths for non-streaming + streaming reply
generation, plus the persona resolution that feeds the prompt builder.
The stubbed `_StubContext` mirrors only the AstrBot context surface
the bridge actually touches:

  * `get_current_chat_provider_id(umo=...)`
  * `llm_generate(chat_provider_id=, prompt=, persona_id=, image_urls=?)`
  * `get_provider_by_id(provider_id)` → object with
    `text_chat(prompt=, image_urls=, system_prompt=?)` and
    `text_chat_stream(prompt=, image_urls=?)`
  * `persona_manager.get_persona(persona_id)` → object with
    `system_prompt` attribute
  * `conversation_manager.{get_curr_conversation_id, new_conversation,
    get_human_readable_context}`

P1-1 (LLM bridge persona fallback): the TypeError fallback path calls
`provider.text_chat(prompt=..., image_urls=...)` WITHOUT passing the
resolved `system_prompt`, so multimodal replies under a configured
persona silently drop the persona system prompt. The
`test_generate_reply_image_typeerror_fallback_carries_system_prompt`
case below pins the post-fix invariant.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

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


class _StubConversationManager:
    """In-memory CM stub. The bridge calls
    `get_curr_conversation_id`, `new_conversation`, and
    `get_human_readable_context` — fake the minimum surface."""

    def __init__(self) -> None:
        self.curr_cid: str | None = None
        self.new_conv_calls: list[dict[str, Any]] = []

    async def get_curr_conversation_id(self, umo: str) -> str | None:
        return self.curr_cid

    async def new_conversation(self, umo: str, **kwargs: Any) -> str:
        self.new_conv_calls.append({"umo": umo, **kwargs})
        self.curr_cid = "cid-stub"
        return self.curr_cid

    async def get_human_readable_context(
        self, *, unified_msg_origin: str, conversation_id: str, page: int, page_size: int
    ) -> tuple[list[str], int]:
        # Bridge requests page=1 size=history_turns*2. We return empty so
        # the prompt builder skips the history block entirely.
        return [], 0


class _StubPersonaManager:
    def __init__(self, persona: _StubPersona | None) -> None:
        self._persona = persona

    async def get_persona(self, persona_id: str) -> _StubPersona | None:
        return self._persona


class _StubContext:
    """Mimic the AstrBot Context surface LlmBridge touches.

    `llm_generate` is an AsyncMock so tests can swap its behavior
    per-test (return _StubResp, raise TypeError, etc.) without
    reimplementing the class.
    """

    def __init__(
        self,
        *,
        provider_id: str | None = "prov-stub",
        provider: _StubProvider | None = None,
        persona: _StubPersona | None = None,
    ) -> None:
        self._provider_id = provider_id
        self.provider = provider or _StubProvider()
        self.persona_manager = _StubPersonaManager(persona)
        self.conversation_manager = _StubConversationManager()
        # AsyncMock with return_value, override per-test via .side_effect
        # or by reassigning to make it raise.
        self.llm_generate = AsyncMock(
            return_value=_StubResp("stub-non-stream-reply")
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
    async def test_text_only_calls_llm_generate_without_image_urls(self):
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        ctx = _StubContext()
        bridge = LlmBridge(ctx, history_turns=0, persona_id="", timeout_seconds=10)
        reply = await bridge.generate_reply(
            token_name="alice",
            session_id="s1",
            username="alice",
            message="hello",
        )
        assert reply == "stub-non-stream-reply"
        assert ctx.llm_generate.call_count == 1
        kwargs = ctx.llm_generate.call_args.kwargs
        assert "image_urls" not in kwargs, (
            "Text-only call must NOT pass image_urls — older AstrBot "
            "builds would reject the kwarg with TypeError on every call"
        )
        assert kwargs["prompt"]
        assert kwargs["chat_provider_id"] == "prov-stub"

    @pytest.mark.asyncio
    async def test_with_image_calls_llm_generate_with_image_urls(self):
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
        assert reply == "stub-non-stream-reply"
        kwargs = ctx.llm_generate.call_args.kwargs
        assert kwargs.get("image_urls") == ["/tmp/img.png"]


class TestPersonaResolution:
    @pytest.mark.asyncio
    async def test_resolved_system_prompt_appears_in_prompt(self):
        """Sanity check: configured persona's system_prompt is built
        into the prompt the provider sees."""
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
        prompt = ctx.llm_generate.call_args.kwargs["prompt"]
        assert "be a poet" in prompt
        assert "[System Prompt]" in prompt
        assert ctx.llm_generate.call_args.kwargs["persona_id"] == "poet-1"

    @pytest.mark.asyncio
    async def test_no_persona_id_skips_system_prompt(self):
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        ctx = _StubContext(persona=None)
        bridge = LlmBridge(ctx, history_turns=0, persona_id="", timeout_seconds=10)
        await bridge.generate_reply(
            token_name="alice",
            session_id="s1",
            username="alice",
            message="hello",
        )
        prompt = ctx.llm_generate.call_args.kwargs["prompt"]
        assert "[System Prompt]" not in prompt


@pytest.mark.asyncio
class TestImageTypeErrorFallback:
    """P1-1 fix region. AstrBot builds before image_urls support raise
    TypeError on `llm_generate(image_urls=...)`. The bridge catches
    that and falls back to `provider.text_chat(...)`. Pre-fix the
    fallback dropped the resolved system_prompt; post-fix it includes
    it."""

    async def _setup(
        self,
        *,
        persona_prompt: str | None = "you are a helpful assistant",
    ):
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        provider = _StubProvider(text_chat_reply="fallback-reply")
        persona = _StubPersona(persona_prompt) if persona_prompt else None
        ctx = _StubContext(provider=provider, persona=persona)
        # llm_generate accepts image_urls path raises TypeError on every
        # call so the fallback fires.
        ctx.llm_generate.side_effect = TypeError(
            "got an unexpected keyword argument 'image_urls'"
        )
        bridge = LlmBridge(
            ctx,
            history_turns=0,
            persona_id="p-stub" if persona_prompt else "",
            timeout_seconds=10,
        )
        return bridge, ctx, provider

    async def test_fallback_invokes_provider_text_chat_with_image_urls(self):
        bridge, _ctx, provider = await self._setup()
        reply = await bridge.generate_reply(
            token_name="alice",
            session_id="s1",
            username="alice",
            message="describe this",
            image_urls=["/tmp/img.png"],
        )
        assert reply == "fallback-reply"
        assert len(provider.text_chat_calls) == 1
        call = provider.text_chat_calls[0]
        assert call["prompt"]
        assert call["image_urls"] == ["/tmp/img.png"]

    async def test_fallback_carries_system_prompt_into_provider_call(self):
        """**P1-1 invariant.** Multimodal request under a persona must
        carry the persona's system_prompt into the fallback provider
        invocation. Pre-fix this assertion fails because the fallback
        call had no `system_prompt` kwarg. Post-fix the system_prompt
        appears."""
        bridge, _ctx, provider = await self._setup(
            persona_prompt="speak only in haiku"
        )
        await bridge.generate_reply(
            token_name="alice",
            session_id="s1",
            username="alice",
            message="describe this image",
            image_urls=["/tmp/img.png"],
        )
        assert len(provider.text_chat_calls) == 1
        call = provider.text_chat_calls[0]
        assert call.get("system_prompt") == "speak only in haiku", (
            "P1-1 regression: TypeError fallback must pass the resolved "
            f"persona system_prompt to provider.text_chat. Got call kwargs: {call!r}"
        )

    async def test_fallback_without_persona_omits_system_prompt(self):
        """No persona configured → no system_prompt to pass. The
        fallback either omits the kwarg OR passes None — both are
        acceptable as long as no spurious system_prompt is injected."""
        bridge, _ctx, provider = await self._setup(persona_prompt=None)
        await bridge.generate_reply(
            token_name="alice",
            session_id="s1",
            username="alice",
            message="hi",
            image_urls=["/tmp/img.png"],
        )
        assert len(provider.text_chat_calls) == 1
        call = provider.text_chat_calls[0]
        sp = call.get("system_prompt")
        assert not sp, (
            f"No persona configured but fallback passed system_prompt={sp!r}"
        )


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
        # Provider invoked exactly once.
        assert len(provider.text_chat_stream_calls) == 1

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
        """Mirrors the non-streaming compatibility shim: don't pass
        image_urls kwarg on text-only calls — older provider builds
        without the kwarg crash with TypeError."""
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

    async def test_empty_completion_text_raises_empty_reply(self):
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        ctx = _StubContext()
        ctx.llm_generate = AsyncMock(return_value=_StubResp(""))
        bridge = LlmBridge(ctx, history_turns=0, persona_id="", timeout_seconds=10)

        with pytest.raises(RuntimeError, match="empty_reply"):
            await bridge.generate_reply(
                token_name="alice",
                session_id="s1",
                username="alice",
                message="hi",
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
