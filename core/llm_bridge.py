"""LLM call bridge for the WebChat pipeline.

Chat (non-streaming + streaming) feeds prior turns to the provider as
structured `contexts` and the persona via the `system_prompt` channel,
calling `provider.text_chat` / `text_chat_stream` directly so both paths
behave identically. Title generation is a self-contained one-off and
still uses `llm_generate`.
"""

from __future__ import annotations

import asyncio
import json
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
        chat_provider_id: str = "",
        chat_fallback_provider_id: str = "",
    ) -> None:
        self._context = context
        self._history_turns = max(0, history_turns)
        self._persona_id_cfg = (persona_id or "").strip()
        # Operator-pinned chat provider override. When non-empty,
        # ``_resolve_provider_id`` returns this verbatim (subject to the
        # existence check below) instead of asking AstrBot which
        # provider is "current" — useful for deployments where the
        # WebChat plugin needs to target a specific model independent
        # of the bot's global default (e.g. cheaper model for chat,
        # GPT-Image-1 still set as the global). Empty string keeps the
        # legacy fallback behaviour.
        self._chat_provider_override = (chat_provider_id or "").strip()
        # Mid-tier safety net: when the operator's pinned provider has
        # been removed / disabled / renamed in AstrBot, we'd otherwise
        # hard-fail every chat with `chat_provider_not_configured`.
        # Configuring a fallback here lets the bridge step down ONE
        # level before falling all the way back to the bot's global
        # default. Empty string disables the middle tier (legacy
        # two-step chain: pinned → global).
        self._chat_provider_fallback = (chat_fallback_provider_id or "").strip()
        # Process-lifetime memo of "provider X (in role Y) was missing
        # at lookup time". `_resolve_provider_id` runs per chat call;
        # without this, every call after the configured provider
        # vanishes would emit a fresh WARNING line — eventually the
        # operator stops noticing. First miss per (id, role) logs at
        # WARNING; subsequent misses are silent so the log stays
        # readable.
        self._missing_provider_warned: set[tuple[str, str]] = set()
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

    async def _resolve_provider_id(self, *, umo: str) -> str | None:
        """Return the chat provider id to use for ``umo``.

        Three-tier resolution chain — first viable wins:

          1. ``chat_provider_id`` (operator-pinned). Used if set AND
             the id still resolves to a live provider.
          2. ``chat_fallback_provider_id`` (operator-pinned mid-tier
             safety net). Used if set AND the primary either wasn't
             set or has disappeared AND the fallback id resolves to
             a live provider.
          3. AstrBot's ``get_current_chat_provider_id`` (bot global).
             Always tried last so a deployment whose primary +
             fallback have both vanished still serves /chat instead
             of hard-failing every request with
             ``chat_provider_not_configured``.

        Each downward step emits ONE warning per (provider_id, role)
        per process lifetime so operators see the configuration drift
        without the log flooding on every chat call. Returning ``None``
        means even the global default isn't wired — callers raise
        ``chat_provider_not_configured``.
        """
        if self._chat_provider_override:
            if self._context.get_provider_by_id(
                self._chat_provider_override
            ) is not None:
                return self._chat_provider_override
            self._warn_provider_missing(
                self._chat_provider_override, role="primary"
            )
        if self._chat_provider_fallback:
            if self._context.get_provider_by_id(
                self._chat_provider_fallback
            ) is not None:
                return self._chat_provider_fallback
            self._warn_provider_missing(
                self._chat_provider_fallback, role="fallback"
            )
        return await self._context.get_current_chat_provider_id(umo=umo)

    def _warn_provider_missing(self, provider_id: str, *, role: str) -> None:
        """Log once-per-process that a configured provider can't be
        resolved. Per-(id, role) memoisation: the bridge runs the
        chain on every chat call, so without dedup a missing provider
        spams a WARNING line per request and operators stop noticing.
        First occurrence logs at WARNING with both fields so an alert
        rule can pin them; subsequent occurrences are silent.
        """
        key = (provider_id, role)
        if key in self._missing_provider_warned:
            return
        self._missing_provider_warned.add(key)
        logger.warning(
            "[WebChatGateway] configured %s chat provider not found, "
            "falling back: provider_id=%s",
            role,
            provider_id,
        )

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

    # Coarse cap on the history slice fed back to the model, measured
    # in CHARS of rendered text (not tokens — a blunt guard; real
    # token budgeting is deferred). History is already turn-windowed by
    # `history_turns` (last turns*2 messages), but a single turn can be
    # megabytes if the user pasted a log dump; that one giant entry
    # would dominate the context and squeeze out the system prompt +
    # the current question, or push the request past the provider's
    # context window into a mid-turn 4xx. Applied per-ENTRY: keep whole
    # messages newest-first until the budget is exhausted, then drop
    # the remaining older entries WHOLE — never bisecting a single
    # message (a half-message is worse context than one fewer turn).
    _MAX_HISTORY_CHARS = 8000

    @staticmethod
    def _entry_text_len(content) -> int:
        """Approximate the rendered text length of a CM message's
        `content`. Mirrors the two shapes AstrBot stores — a bare
        string, or a list of segment dicts where text lives under
        `{"text": ...}` (image / other parts contribute no text). Used
        only for the coarse history budget, so non-text parts are
        intentionally counted as 0."""
        if isinstance(content, str):
            return len(content)
        if isinstance(content, list):
            total = 0
            for seg in content:
                if isinstance(seg, dict):
                    t = seg.get("text")
                    if isinstance(t, str):
                        total += len(t)
            return total
        return 0

    async def _history_contexts(
        self, unified_origin: str, conversation_id: str
    ) -> list[dict]:
        """Structured conversation history to feed back as `contexts`.

        Reads the RAW provider-native records straight from AstrBot CM
        (`get_conversation().history` — the same JSON the write path
        persists via `add_message_pair`) instead of rendering them to
        text. Passing these as `contexts=` preserves user/assistant
        role boundaries and any multimodal `ImageURLPart` segments, so
        the model sees a real multi-turn conversation rather than a
        flattened transcript.

        Windowing:
          * empty when `history_turns <= 0`;
          * keep the last `history_turns * 2` messages (≈ N exchanges);
          * drop a trailing user message — the current turn's user text
            is passed separately as `prompt=`, so a trailing user entry
            would duplicate it. Normal /chat never hits this (the
            current user turn isn't persisted until after the reply);
            the regenerate path does (it rewrites CM history ending at
            the user turn before calling), so this de-dupes it;
          * apply the coarse `_MAX_HISTORY_CHARS` budget newest-first,
            dropping older entries whole.
        """
        if self._history_turns <= 0:
            return []
        try:
            conv = await self._context.conversation_manager.get_conversation(
                unified_origin, conversation_id
            )
        except Exception:
            logger.exception("[WebChatGateway] CM.get_conversation failed")
            return []
        if not conv:
            return []
        history_raw = getattr(conv, "history", None)
        if isinstance(history_raw, str):
            try:
                parsed = json.loads(history_raw or "[]")
            except (TypeError, ValueError):
                parsed = []
        else:
            parsed = history_raw or []
        if not isinstance(parsed, list):
            return []
        entries = [e for e in parsed if isinstance(e, dict)]
        if not entries:
            return []
        entries = entries[-(self._history_turns * 2):]
        if entries and str(entries[-1].get("role") or "").strip().lower() == "user":
            entries = entries[:-1]
        if not entries:
            return []
        budget = self._MAX_HISTORY_CHARS
        kept_reversed: list[dict] = []
        for entry in reversed(entries):
            cost = self._entry_text_len(entry.get("content"))
            if kept_reversed and cost > budget:
                break
            budget -= cost
            kept_reversed.append(entry)
        kept_reversed.reverse()
        return kept_reversed

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
        # the inner provider.text_chat had its own deadline.
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
        provider_id = await self._resolve_provider_id(umo=unified_origin)
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

        contexts = await self._history_contexts(unified_origin, cid)

        # Unified low-level call (mirrors generate_reply_stream): the
        # CURRENT user message goes as `prompt`, prior turns as
        # structured `contexts`, and the persona via the real
        # `system_prompt` channel — NOT inlined into the prompt text.
        # `image_urls` is only included when present so older provider
        # builds without the kwarg still serve text-only chats.
        kwargs: dict[str, object] = {"prompt": message, "contexts": contexts}
        if system_prompt:
            kwargs["system_prompt"] = system_prompt
        if image_urls:
            kwargs["image_urls"] = image_urls
        try:
            resp = await provider.text_chat(**kwargs)
        except EmptyModelOutputError as exc:
            raise RuntimeError("empty_reply") from exc
        # `provider.text_chat` may raise EmptyModelOutputError directly (some
        # provider paths surface it that way); the existing `if not reply`
        # guard below catches the case where it returns a response object with
        # empty text. Both collapse into the same code so the handler can
        # render them uniformly.
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
            provider_id = await self._resolve_provider_id(umo=unified_origin)
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

            contexts = await self._history_contexts(unified_origin, cid)

            collected: list[str] = []
            # Same shape as the non-streaming path: current message as
            # `prompt`, prior turns as structured `contexts`, persona via
            # the real `system_prompt` channel. The system_prompt /
            # image_urls kwargs are only included when set so older
            # provider builds without them still serve text-only /
            # persona-less chats.
            stream_kwargs: dict[str, object] = {
                "prompt": message,
                "contexts": contexts,
            }
            if system_prompt:
                stream_kwargs["system_prompt"] = system_prompt
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
            provider_id = await self._resolve_provider_id(umo=unified_origin)
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
