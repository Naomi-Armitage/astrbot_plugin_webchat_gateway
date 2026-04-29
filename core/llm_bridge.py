"""LLM call bridge — ported from astrbot_plugin_webchat.

Reuses persona, conversation_manager history, and llm_generate exactly like
the original plugin so behavior matches the simple version.
"""

from __future__ import annotations

import asyncio

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

        try:
            resp = await asyncio.wait_for(
                self._context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    persona_id=persona_id,
                ),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError("llm_timeout") from exc
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
