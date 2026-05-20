"""LLM call bridge — ported from astrbot_plugin_webchat.

Reuses persona, conversation_manager history, and llm_generate exactly like
the original plugin so behavior matches the simple version.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Sequence

from astrbot.api import logger

# AstrBot raises this from openai_source / similar provider sources when an
# upstream LLM responds with finish_reason=stop AND zero completion tokens
# AND empty content. The user-visible outcome is identical to the bridge's
# own `RuntimeError("empty_reply")`, so we normalise both into a single
# error code rather than letting `EmptyModelOutputError` leak as a generic
# `internal_error` 500. Defensive import: older AstrBot builds without the
# exception still load the plugin (the except below becomes a no-op).
try:
    from astrbot.core.exceptions import EmptyModelOutputError
except ImportError:  # pragma: no cover
    class EmptyModelOutputError(Exception):  # type: ignore[no-redef]
        """Fallback shim when AstrBot doesn't export the real class."""


def map_llm_error(exc: BaseException) -> tuple[str, int, str]:
    """Map an LLM call exception to `(code, http_status, audit_event)`.

    Centralised so /chat (non-stream), /chat/stream, and regenerate
    all produce the same wire-level error taxonomy without re-spelling
    the ladder. The internal exception text is NOT returned — callers
    log it themselves; the caller-supplied audit detail is what
    surfaces to operators.

    * `RuntimeError("llm_timeout")` → `("llm_timeout", 504, "llm_timeout")`.
    * `RuntimeError("empty_reply")` → `("empty_reply", 502, "chat_empty_reply")`.
    * anything else → `("llm_call_failed", 500, "chat_error")`.
    """
    if isinstance(exc, RuntimeError):
        code = str(exc)
        if code == "llm_timeout":
            return ("llm_timeout", 504, "llm_timeout")
        if code == "empty_reply":
            return ("empty_reply", 502, "chat_empty_reply")
    return ("llm_call_failed", 500, "chat_error")


class LlmBridge:
    """Wraps AstrBot LLM/persona/conversation calls for the WebChat pipeline."""

    def __init__(
        self,
        context,
        *,
        history_turns: int,
        persona_id: str,
        timeout_seconds: float = 60.0,
        total_stream_timeout_seconds: float | None = None,
    ) -> None:
        self._context = context
        self._history_turns = max(0, history_turns)
        self._persona_id_cfg = (persona_id or "").strip()
        self._timeout = float(timeout_seconds) if timeout_seconds else None
        # Total wall-clock budget for streaming responses. Without this,
        # a misbehaving provider that emits one byte every (per-chunk
        # timeout - ε) seconds can hold the per-token concurrency lock
        # for hours: the per-chunk idle timeout would reset on every
        # crumb of progress. Default: 8× per-chunk timeout, capped at
        # 600s and floored at 2× so it always exceeds at least one idle
        # window. None / 0 disables the total budget (legacy behaviour).
        if total_stream_timeout_seconds is None:
            if self._timeout:
                self._total_stream_timeout: float | None = min(
                    600.0, max(self._timeout * 2, self._timeout * 8)
                )
            else:
                self._total_stream_timeout = None
        elif total_stream_timeout_seconds <= 0:
            self._total_stream_timeout = None
        else:
            self._total_stream_timeout = float(total_stream_timeout_seconds)
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

    # Hard cap on the rendered history slice (in CHAR count, not
    # tokens — but at typical model packing of 2-4 chars/token, 8K
    # chars maps to 2-4K tokens of *just* history before the prompt
    # frame and the current user message are even appended). Without
    # this guard, a token-side runaway (verbose model that keeps
    # generating long replies, or pathological pasted content) could
    # let the rendered history overflow the model's context window
    # and trigger a provider 4xx mid-turn. The history slice is
    # already CM-windowed by `history_turns` (page_size = turns*2)
    # but a single turn can be megabytes if the user pasted a log
    # dump — that one giant entry would dominate the prompt and
    # squeeze out the system prompt + the current question.
    _MAX_HISTORY_CHARS = 8000

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
        joined = "\n".join(ordered)
        if len(joined) > self._MAX_HISTORY_CHARS:
            # Tail-keep: a conversation that ran past the cap loses the
            # oldest turns, not the most recent ones. Continuity with
            # what the user is asking right now is more useful than
            # preserving a partial early turn. The cut may bisect a
            # line; leave it raw rather than realigning to a newline
            # boundary — provider tokenizers treat a half-line as a
            # complete prefix and the model handles the implicit start
            # fine (cleaner to occasionally render one truncated line
            # than to lose another entire turn to alignment).
            joined = joined[-self._MAX_HISTORY_CHARS:]
        return joined

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
        image_urls: list[str] | None = None,
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
                    image_urls=image_urls,
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
        image_urls: list[str] | None = None,
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

        # AstrBot's `llm_generate` accepts image_urls on newer builds.
        # Older builds reject the kwarg with TypeError — in that case
        # fall back to grabbing the provider directly and calling
        # `provider.text_chat(image_urls=...)`, which is the surface the
        # streaming path uses (provider.text_chat_stream). Splitting on
        # TypeError keeps real LLM-side failures (provider config, auth
        # errors) on the original exception path.
        try:
            if image_urls:
                try:
                    resp = await self._context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=prompt,
                        persona_id=persona_id,
                        image_urls=image_urls,
                    )
                except TypeError:
                    provider = self._context.get_provider_by_id(provider_id)
                    if provider is None:
                        raise RuntimeError("chat_provider_not_configured")
                    # Pass the resolved system_prompt explicitly. Without
                    # it, AstrBot's provider.text_chat would receive the
                    # prompt body alone — the inline `[System Prompt]\n…`
                    # block in `prompt` is documentation for the model,
                    # NOT a substitute for the provider's actual system
                    # prompt channel (which some backends route through a
                    # separate SDK field). Skipping system_prompt here
                    # silently dropped persona context on the
                    # image+persona path — `llm_generate` would have
                    # resolved persona_id internally, but the fallback
                    # had no equivalent. The variable is already in
                    # scope from `_resolve_persona()`.
                    resp = await provider.text_chat(
                        prompt=prompt,
                        image_urls=image_urls,
                        system_prompt=system_prompt,
                    )
            else:
                resp = await self._context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    persona_id=persona_id,
                )
        except EmptyModelOutputError as exc:
            raise RuntimeError("empty_reply") from exc
        # `llm_generate` may raise EmptyModelOutputError directly (some provider
        # paths surface it that way); the existing `if not reply` guard below
        # catches the case where it returns a response object with empty text.
        # Both collapse into the same code so the handler can render them
        # uniformly.
        reply = (resp.completion_text or "").strip()
        if not reply:
            raise RuntimeError("empty_reply")

        # CM persistence is owned by ConversationService.record_chat_pair so
        # the streaming/incomplete path can persist partial replies through
        # the same code path.
        return reply

    async def generate_reply_stream(
        self,
        *,
        token_name: str,
        session_id: str,
        username: str,
        message: str,
        image_urls: list[str] | None = None,
    ) -> AsyncIterator[str]:
        # Streaming has TWO independent timeouts:
        #   * Per-chunk idle (`self._timeout`): fires if no new chunk
        #     arrives within N seconds. Catches the "provider just
        #     stopped sending" failure mode.
        #   * Total wall-clock (`self._total_stream_timeout`): caps the
        #     whole response. Catches the "provider drip-feeds one
        #     byte every (idle - ε) seconds" failure mode that the
        #     per-chunk timeout alone can't see — without it a single
        #     misbehaving call holds the per-token concurrency lock
        #     for hours.
        # Both fire as `RuntimeError("llm_timeout")` so the handler
        # layer treats them identically.
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
            # Build the streaming kwargs lazily so older AstrBot builds
            # without `image_urls` on text_chat_stream still work for
            # text-only messages — the kwarg is only included when
            # there's something to send.
            stream_kwargs: dict[str, object] = {"prompt": prompt}
            if image_urls:
                stream_kwargs["image_urls"] = image_urls
            try:
                async for chunk in provider.text_chat_stream(**stream_kwargs):
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
            except EmptyModelOutputError as exc:
                # Upstream returned finish_reason=stop with zero tokens. Same
                # user-visible outcome as our `if not full: empty_reply` guard
                # below — normalise so the handler can route it specifically
                # instead of falling into the generic internal_error branch.
                raise RuntimeError("empty_reply") from exc

            full = "".join(collected).strip()
            if not full:
                raise RuntimeError("empty_reply")

            # CM persistence is owned by ConversationService.record_chat_pair
            # so partial-reply (incomplete) flows persist via the same path.

        # Combined idle + total-wall-clock timeout. Each iteration
        # waits min(per_chunk_idle, remaining_total_budget). If the
        # remaining total budget falls to <= 0 we raise immediately
        # without entering another wait — covers the corner case
        # where a chunk arrives just before the deadline and we'd
        # otherwise loop once more.
        #
        # Persistent-pull pattern: a single in-flight `__anext__()` Task
        # is reused across iterations and shielded from `wait_for`
        # cancellation. Calling `__anext__()` on a fresh task each
        # iteration would leave the previous call running in the
        # background; the next iteration's call would then crash with
        # "asynchronous generator is already running". With shield, a
        # timeout raises but the inner pull keeps running, ready for the
        # next iteration to await it again — until we explicitly cancel
        # it on the timeout path below before raising llm_timeout.
        agen = _runner()
        pull_task: asyncio.Task | None = None
        loop = asyncio.get_running_loop()
        stream_started_at = loop.time()
        total_budget = self._total_stream_timeout
        try:
            while True:
                if pull_task is None:
                    pull_task = asyncio.ensure_future(agen.__anext__())

                # Compute the effective wait for this iteration.
                if total_budget is not None:
                    remaining = total_budget - (loop.time() - stream_started_at)
                    if remaining <= 0:
                        # Already past the total deadline. Cancel the
                        # in-flight pull and surface timeout.
                        pull_task.cancel()
                        try:
                            await pull_task
                        except (
                            asyncio.CancelledError,
                            StopAsyncIteration,
                            Exception,
                        ):
                            pass
                        pull_task = None
                        raise RuntimeError("llm_timeout")
                    iter_timeout = (
                        min(self._timeout, remaining)
                        if self._timeout
                        else remaining
                    )
                else:
                    iter_timeout = self._timeout  # may be None

                try:
                    if iter_timeout:
                        text = await asyncio.wait_for(
                            asyncio.shield(pull_task),
                            timeout=iter_timeout,
                        )
                    else:
                        text = await pull_task
                except asyncio.TimeoutError as exc:
                    pull_task.cancel()
                    try:
                        await pull_task
                    except (
                        asyncio.CancelledError,
                        StopAsyncIteration,
                        Exception,
                    ):
                        pass
                    pull_task = None
                    raise RuntimeError("llm_timeout") from exc
                except StopAsyncIteration:
                    pull_task = None
                    return
                pull_task = None
                yield text
        finally:
            if pull_task is not None and not pull_task.done():
                pull_task.cancel()
                try:
                    await pull_task
                except (
                    asyncio.CancelledError,
                    StopAsyncIteration,
                    Exception,
                ):
                    pass
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
