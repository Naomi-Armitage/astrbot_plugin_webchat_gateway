"""LLM call bridge — ported from astrbot_plugin_webchat.

Reuses persona, conversation_manager history, and llm_generate exactly like
the original plugin so behavior matches the simple version.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Sequence

from astrbot.api import logger

try:
    from astrbot.core.agent.message import (
        AssistantMessageSegment,
        TextPart,
        UserMessageSegment,
    )
except ImportError as _e:
    raise ImportError(
        "[WebChatGateway] Cannot import AssistantMessageSegment/TextPart/UserMessageSegment "
        "from astrbot.core.agent.message. This plugin requires AstrBot >= 3.4. "
        f"Original error: {_e}"
    ) from _e


class LlmBridge:
    """Wraps AstrBot LLM/persona/conversation calls for the WebChat pipeline."""

    def __init__(
        self,
        context,
        *,
        history_turns: int,
        persona_id: str,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._context = context
        self._history_turns = max(0, history_turns)
        self._persona_id_cfg = (persona_id or "").strip()
        self._timeout = float(timeout_seconds) if timeout_seconds else None
        self._persona_cache: tuple[str | None, str | None] | None = None

    async def _resolve_persona(self) -> tuple[str | None, str | None]:
        if self._persona_cache is not None:
            return self._persona_cache
        if not self._persona_id_cfg:
            self._persona_cache = (None, None)
            return self._persona_cache
        try:
            persona = await self._context.persona_manager.get_persona(
                self._persona_id_cfg
            )
            system_prompt = (getattr(persona, "system_prompt", "") or "").strip()
            self._persona_cache = (self._persona_id_cfg, system_prompt or None)
            return self._persona_cache
        except Exception:
            logger.warning(
                "[WebChatGateway] persona_id does not exist: %s",
                self._persona_id_cfg,
            )
            return None, None

    async def _history_text(self, unified_origin: str, conversation_id: str) -> str:
        if self._history_turns <= 0:
            return ""
        lines, _ = await self._context.conversation_manager.get_human_readable_context(
            unified_msg_origin=unified_origin,
            conversation_id=conversation_id,
            page=1,
            page_size=self._history_turns * 2,
        )
        if not lines:
            return ""
        ordered = list(reversed(lines))
        return "\n".join(ordered)

    @staticmethod
    def _build_prompt(
        *,
        message: str,
        system_prompt: str | None,
        history: str,
    ) -> str:
        blocks: list[str] = []
        if system_prompt:
            blocks.append(f"[System Prompt]\n{system_prompt}")
        if history:
            blocks.append(f"[Recent Conversation Context]\n{history}")
        blocks.append(f"[Current User Message]\n{message}")
        return "\n\n".join(blocks)

    async def generate_reply(
        self,
        *,
        token_name: str,
        session_id: str,
        username: str,
        message: str,
    ) -> str:
        # Wrap the entire flow in a single timeout. Without this, slow
        # provider lookup / persona resolution / conversation_manager calls
        # could pin the per-token concurrency lock indefinitely while only
        # the inner llm_generate had its own deadline.
        try:
            return await asyncio.wait_for(
                self._generate_reply_inner(
                    token_name=token_name,
                    session_id=session_id,
                    username=username,
                    message=message,
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError("llm_timeout") from exc

    async def _generate_reply_inner(
        self,
        *,
        token_name: str,
        session_id: str,
        username: str,
        message: str,
    ) -> str:
        # Namespace by token so two callers passing the same sessionId never
        # share conversation history across tokens.
        unified_origin = f"webchat_gateway:{token_name}:{session_id}"
        provider_id = await self._context.get_current_chat_provider_id(
            umo=unified_origin
        )
        if not provider_id:
            raise RuntimeError("chat_provider_not_configured")

        persona_id, system_prompt = await self._resolve_persona()

        cm = self._context.conversation_manager
        cid = await cm.get_curr_conversation_id(unified_origin)
        if not cid:
            cid = await cm.new_conversation(
                unified_origin,
                platform_id="webchat_gateway",
                title=username,
                persona_id=persona_id,
            )

        history = await self._history_text(unified_origin, cid)
        prompt = self._build_prompt(
            message=message, system_prompt=system_prompt, history=history
        )

        resp = await self._context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            persona_id=persona_id,
        )
        reply = (resp.completion_text or "").strip()
        if not reply:
            raise RuntimeError("empty_reply")

        try:
            await cm.add_message_pair(
                cid=cid,
                user_message=UserMessageSegment(content=[TextPart(text=message)]),
                assistant_message=AssistantMessageSegment(
                    content=[TextPart(text=reply)]
                ),
            )
        except Exception:
            logger.exception("[WebChatGateway] persist conversation failed")

        return reply

    async def generate_reply_stream(
        self,
        *,
        token_name: str,
        session_id: str,
        username: str,
        message: str,
    ) -> AsyncIterator[str]:
        # Wrap the whole stream in a single wall-clock deadline so a stalled
        # provider can't pin the per-token concurrency lock indefinitely. Per-
        # chunk timeouts are intentionally not enforced — that's the heartbeat's
        # job at the HTTP layer.
        async def _runner() -> AsyncIterator[str]:
            unified_origin = f"webchat_gateway:{token_name}:{session_id}"
            provider_id = await self._context.get_current_chat_provider_id(
                umo=unified_origin
            )
            if not provider_id:
                raise RuntimeError("chat_provider_not_configured")
            provider = self._context.get_provider_by_id(provider_id)
            if provider is None:
                raise RuntimeError("chat_provider_not_configured")

            persona_id, system_prompt = await self._resolve_persona()

            cm = self._context.conversation_manager
            cid = await cm.get_curr_conversation_id(unified_origin)
            if not cid:
                cid = await cm.new_conversation(
                    unified_origin,
                    platform_id="webchat_gateway",
                    title=username,
                    persona_id=persona_id,
                )

            history = await self._history_text(unified_origin, cid)
            prompt = self._build_prompt(
                message=message, system_prompt=system_prompt, history=history
            )

            collected: list[str] = []
            async for chunk in provider.text_chat_stream(prompt=prompt):
                # AstrBot providers yield two kinds of LLMResponse:
                #   is_chunk=True  → per-token delta; chunk.completion_text is
                #                    the new text since the last yield (Anthropic
                #                    sets it directly; OpenAI/Gemini set it via
                #                    result_chain — the property reads through
                #                    transparently).
                #   is_chunk=False → final assembled response; emitted once at
                #                    the end. Skip it (we already accumulated).
                if not getattr(chunk, "is_chunk", False):
                    continue
                text = chunk.completion_text or ""
                if not text:
                    continue
                collected.append(text)
                yield text

            full = "".join(collected).strip()
            if not full:
                raise RuntimeError("empty_reply")

            try:
                await cm.add_message_pair(
                    cid=cid,
                    user_message=UserMessageSegment(content=[TextPart(text=message)]),
                    assistant_message=AssistantMessageSegment(
                        content=[TextPart(text=full)]
                    ),
                )
            except Exception:
                logger.exception("[WebChatGateway] persist conversation failed")

        # Per-chunk idle timeout, NOT a total wall-clock budget. Streaming
        # responses are explicitly unbounded in length (a search-augmented
        # reply with 30 chunks of 5s each is fine, even though the total
        # time exceeds `llm_timeout_seconds`); the only failure mode worth
        # firing on is "no progress in too long". `self._timeout` is the
        # max gap between consecutive chunks (and the max time before the
        # first chunk). Non-streaming `generate_reply` retains its total
        # wall-clock semantics — see asyncio.wait_for in that method.
        agen = _runner()
        try:
            while True:
                try:
                    if self._timeout:
                        try:
                            text = await asyncio.wait_for(
                                agen.__anext__(), timeout=self._timeout
                            )
                        except asyncio.TimeoutError as exc:
                            raise RuntimeError("llm_timeout") from exc
                    else:
                        text = await agen.__anext__()
                except StopAsyncIteration:
                    return
                yield text
        finally:
            await agen.aclose()

    # ----- Title generation -----

    _TITLE_SYSTEM_PROMPT = (
        "你是会话标题生成器。根据下面的对话内容，用 6-12 个简体中文字符总结一个简短标题。\n"
        "只输出标题文本，不要标点，不要解释，不要引号，不要 emoji。"
    )

    @staticmethod
    def _format_conversation(turns: Sequence[dict]) -> str:
        lines: list[str] = []
        for turn in turns:
            role = "user" if str(turn.get("role", "")).lower() == "user" else "bot"
            text = str(turn.get("text") or "").strip()
            if not text:
                continue
            # Cap each turn so a single huge message can't blow the prompt up.
            if len(text) > 500:
                text = text[:500] + "…"
            lines.append(f"[{role}]: {text}")
        return "\n".join(lines)

    @staticmethod
    def _post_process_title(raw: str, fallback: str) -> str:
        text = (raw or "").strip()
        # Take first line only.
        if text:
            text = text.split("\n", 1)[0].strip()
        # Strip surrounding quotes (ASCII + full-width).
        for _ in range(2):
            if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'`“”‘’「」『』《》":
                text = text[1:-1].strip()
            else:
                break
        if len(text) > 30:
            text = text[:30]
        if text:
            return text
        return (fallback or "").strip()[:25]

    async def generate_title(
        self,
        *,
        token_name: str,
        session_id: str,
        conversation: Sequence[dict],
    ) -> str:
        # Match the chat path's umo namespacing so provider routing matches;
        # persona is intentionally NOT applied (titles are neutral).
        unified_origin = f"webchat_gateway:{token_name}:{session_id}"
        body = self._format_conversation(conversation)
        if not body:
            raise RuntimeError("empty_conversation")
        first_user = next(
            (
                str(t.get("text") or "")
                for t in conversation
                if str(t.get("role", "")).lower() == "user"
            ),
            "",
        )
        try:
            provider_id = await self._context.get_current_chat_provider_id(
                umo=unified_origin
            )
            if not provider_id:
                raise RuntimeError("chat_provider_not_configured")
            resp = await asyncio.wait_for(
                self._context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=body,
                    system_prompt=self._TITLE_SYSTEM_PROMPT,
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError("llm_timeout") from exc
        return self._post_process_title(resp.completion_text or "", first_user)
