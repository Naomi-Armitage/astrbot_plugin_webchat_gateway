"""ConversationService.regenerate_assistant_message_stream service-level
tests. Drives the async generator directly with stubbed CM + storage +
LLM bridge; verifies the chunk / done event shape, persistence side
effects (CM.update_conversation calls), and event emission
(storage.append_updates payloads).

The HTTP wrapper that sits on top of this generator is tested
separately via the existing P0-2 aiohttp integration pattern; here we
focus on the service-layer invariants the wrapper relies on.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest


# --- Stubs -----------------------------------------------------------------


class _StubAssistantSegment:
    """Stand-in for AstrBot's AssistantMessageSegment.model_dump shape."""

    def __init__(self, *, content: list[Any]) -> None:
        self.content = content

    def model_dump(self) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": [
                # Mirror what the production code consumes via
                # `_extract_text` — list of {type, text} dicts.
                {"type": "text", "text": p.text if hasattr(p, "text") else str(p)}
                for p in self.content
            ],
        }


class _StubTextPart:
    def __init__(self, *, text: str) -> None:
        self.text = text


class _StubLlmBridge:
    """Streams pre-canned chunks. `make_failing` flips the bridge into
    raising for the empty-stream / error-mid-stream cases."""

    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.calls: list[dict[str, Any]] = []
        self.exc: Exception | None = None

    async def generate_reply_stream(self, **kwargs: Any):
        self.calls.append(dict(kwargs))
        for c in self.chunks:
            yield c
        if self.exc is not None:
            raise self.exc


class _StubConversation:
    def __init__(self, history: list[Any]) -> None:
        self.history = history


class _StubCM:
    """In-memory CM that tracks the latest history per conversation."""

    def __init__(self, initial_history: list[Any]) -> None:
        self._history = list(initial_history)
        self.cid = "cid-1"
        self.update_calls: list[list[Any]] = []

    async def get_curr_conversation_id(self, umo: str) -> str:
        return self.cid

    async def get_conversation(self, umo: str, cid: str) -> _StubConversation:
        return _StubConversation(self._history)

    async def update_conversation(
        self, *, unified_msg_origin: str, conversation_id: str, history: list[Any]
    ) -> None:
        self.update_calls.append(list(history))
        self._history = list(history)


class _StubStorage:
    """Minimal AbstractStorage surface used by regenerate."""

    def __init__(self, *, today_usage: int = 0, files: dict | None = None) -> None:
        self.today_usage = today_usage
        self.files = files or {}
        self.usage_increments = 0
        self.session_meta_upserts: list[dict[str, Any]] = []
        self.events: list[Any] = []

    async def get_today_usage(self, token_name: str, *, day) -> int:
        return self.today_usage

    async def get_file(self, file_id: str):
        return self.files.get(file_id)

    async def list_files_for_session(self, *, token_name, session_id, **kwargs):
        # Empty — no cross-message attachment reuse to track.
        return []

    async def increment_daily_usage(self, token_name: str, *, day) -> int:
        self.usage_increments += 1
        return self.today_usage + self.usage_increments

    async def upsert_session_meta(self, **kwargs: Any) -> None:
        self.session_meta_upserts.append(dict(kwargs))

    async def append_updates(self, *, token_name: str, events, now: int) -> None:
        self.events.extend(events)


class _StubAuditLogger:
    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    async def write(self, event: str, **kwargs: Any) -> None:
        self.writes.append({"event": event, **kwargs})


class _StubEventBus:
    def __init__(self) -> None:
        self.notify_calls: list[str] = []

    async def notify(self, token_name: str) -> None:
        self.notify_calls.append(token_name)


def _user_entry(text: str) -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _bot_entry(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


# --- Fixture builder ------------------------------------------------------


def _build_service(
    *,
    history: list[Any] | None = None,
    chunks: list[str] | None = None,
    today_usage: int = 0,
    llm_exc: Exception | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
):
    """Construct a ConversationService with all-stub deps and return
    `(service, cm, storage, audit, event_bus, llm)` so each test can
    assert on the relevant slots."""
    from astrbot_plugin_webchat_gateway.handlers import (
        conversations_service as conv_mod,
    )
    from astrbot_plugin_webchat_gateway.handlers.conversations import (
        ConversationService,
    )

    cm = _StubCM(history or [])
    storage = _StubStorage(today_usage=today_usage)
    audit = _StubAuditLogger()
    bus = _StubEventBus()
    llm = _StubLlmBridge(chunks or [])
    if llm_exc is not None:
        llm.exc = llm_exc

    # Substitute the AssistantMessageSegment / TextPart names the
    # production code uses to build the new assistant entry on stream
    # completion. These come from AstrBot core which we don't import
    # in tests; the stubs above produce a model_dump compatible with
    # `_extract_text`.
    if monkeypatch is not None:
        monkeypatch.setattr(
            conv_mod, "AssistantMessageSegment", _StubAssistantSegment
        )
        monkeypatch.setattr(conv_mod, "TextPart", _StubTextPart)

    service = ConversationService(
        storage=storage,  # type: ignore[arg-type]
        audit=audit,  # type: ignore[arg-type]
        event_bus=bus,  # type: ignore[arg-type]
        cm=cm,
        file_store=None,  # type: ignore[arg-type]  # text-only flows don't touch file_store
        concurrency=None,  # tests bypass per-token lock
        llm_bridge=llm,  # type: ignore[arg-type]
    )
    return service, cm, storage, audit, bus, llm


async def _drive(generator):
    """Drain an async generator into (chunks, terminal). `terminal` is
    the final yielded dict (typically `{"type":"done",...}`) or None
    if the generator raised."""
    chunks: list[str] = []
    terminal: dict[str, Any] | None = None
    async for evt in generator:
        if evt.get("type") == "chunk":
            chunks.append(evt["delta"])
        else:
            terminal = evt
    return chunks, terminal


# --- Tests ----------------------------------------------------------------


@pytest.mark.asyncio
class TestRegenerateStreamHappyPath:
    async def test_streams_chunks_then_emits_done(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        history = [_user_entry("hello?"), _bot_entry("OLD reply")]
        service, cm, storage, audit, bus, llm = _build_service(
            history=history,
            chunks=["new ", "answer"],
            today_usage=0,
            monkeypatch=monkeypatch,
        )
        chunks, terminal = await _drive(
            service.regenerate_assistant_message_stream(
                token_name="alice",
                session_id="s1",
                message_index=1,
                token_daily_quota=100,
                ip="127.0.0.1",
            )
        )
        assert chunks == ["new ", "answer"]
        assert terminal is not None
        assert terminal["type"] == "done"
        assert terminal["reply"] == "new answer"
        assert terminal["daily_quota"] == 100
        # Usage was incremented exactly once.
        assert storage.usage_increments == 1
        assert terminal["remaining"] == 99

    async def test_persists_truncate_then_final_history(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        history = [
            _user_entry("hello"),
            _bot_entry("OLD"),
        ]
        service, cm, storage, *_ = _build_service(
            history=history,
            chunks=["NEW reply"],
            monkeypatch=monkeypatch,
        )
        await _drive(
            service.regenerate_assistant_message_stream(
                token_name="alice",
                session_id="s1",
                message_index=1,
                token_daily_quota=100,
            )
        )
        # Two CM.update_conversation calls: 1st truncate to [user],
        # 2nd write final = [user, new_assistant].
        assert len(cm.update_calls) == 2
        truncated = cm.update_calls[0]
        final = cm.update_calls[1]
        assert len(truncated) == 1
        assert truncated[0]["role"] == "user"
        assert len(final) == 2
        assert final[0]["role"] == "user"
        assert final[1]["role"] == "assistant"
        # New assistant entry carries the streamed reply.
        new_content = final[1]["content"]
        assert any(
            isinstance(p, dict) and p.get("text") == "NEW reply"
            for p in new_content
        )

    async def test_emits_delete_and_added_events(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        history = [_user_entry("hi"), _bot_entry("OLD")]
        service, _cm, storage, _audit, bus, _llm = _build_service(
            history=history,
            chunks=["new"],
            monkeypatch=monkeypatch,
        )
        await _drive(
            service.regenerate_assistant_message_stream(
                token_name="alice",
                session_id="s1",
                message_index=1,
                token_daily_quota=100,
            )
        )
        # message_deleted (idx=1, assistant) + message_added (assistant, "new").
        events = storage.events
        assert len(events) == 2
        types = [e.event_type for e in events]
        assert "message_deleted" in types
        assert "message_added" in types
        # Bus notified the token once after persistence.
        assert bus.notify_calls == ["alice"]

    async def test_records_audit_on_success(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        history = [_user_entry("hi"), _bot_entry("OLD")]
        service, *rest = _build_service(
            history=history,
            chunks=["x"],
            monkeypatch=monkeypatch,
        )
        audit = rest[2]
        await _drive(
            service.regenerate_assistant_message_stream(
                token_name="alice",
                session_id="s1",
                message_index=1,
                token_daily_quota=100,
            )
        )
        events = [w["event"] for w in audit.writes]
        assert "conv_message_regenerated" in events


@pytest.mark.asyncio
class TestRegenerateStreamGuards:
    async def test_quota_exceeded_raises_before_side_effects(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from astrbot_plugin_webchat_gateway.handlers.conversations import (
            ServiceError,
        )

        history = [_user_entry("hi"), _bot_entry("OLD")]
        service, cm, storage, audit, _bus, _llm = _build_service(
            history=history,
            chunks=["new"],
            today_usage=100,
            monkeypatch=monkeypatch,
        )
        with pytest.raises(ServiceError) as ei:
            await _drive(
                service.regenerate_assistant_message_stream(
                    token_name="alice",
                    session_id="s1",
                    message_index=1,
                    token_daily_quota=100,
                )
            )
        assert ei.value.code == "quota_exceeded"
        # No CM mutation, no events, no usage increment.
        assert cm.update_calls == []
        assert storage.events == []
        assert storage.usage_increments == 0
        # Quota audit fired.
        assert any(w["event"] == "quota_exceeded" for w in audit.writes)

    async def test_empty_stream_raises_empty_reply(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from astrbot_plugin_webchat_gateway.handlers.conversations import (
            ServiceError,
        )

        history = [_user_entry("hi"), _bot_entry("OLD")]
        service, _cm, storage, audit, _bus, _llm = _build_service(
            history=history,
            chunks=[],  # provider yielded zero chunks
            monkeypatch=monkeypatch,
        )
        with pytest.raises(ServiceError) as ei:
            await _drive(
                service.regenerate_assistant_message_stream(
                    token_name="alice",
                    session_id="s1",
                    message_index=1,
                    token_daily_quota=100,
                )
            )
        assert ei.value.code == "empty_reply"
        assert ei.value.status == 502
        # Truncate happened (pre-LLM side effect) but no final write.
        # That's the documented behavior — failed regen leaves the
        # session in the truncated state.
        # No quota burned.
        assert storage.usage_increments == 0

    async def test_non_assistant_target_raises_message_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from astrbot_plugin_webchat_gateway.handlers.conversations import (
            ServiceError,
        )

        history = [_user_entry("hi"), _bot_entry("ack")]
        service, cm, storage, *_ = _build_service(
            history=history,
            chunks=["x"],
            monkeypatch=monkeypatch,
        )
        with pytest.raises(ServiceError) as ei:
            await _drive(
                service.regenerate_assistant_message_stream(
                    token_name="alice",
                    session_id="s1",
                    message_index=0,  # user, not assistant
                    token_daily_quota=100,
                )
            )
        assert ei.value.code == "message_not_found"
        assert ei.value.status == 404
        assert cm.update_calls == []
        assert storage.events == []

    async def test_no_history_raises_session_not_found(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from astrbot_plugin_webchat_gateway.handlers.conversations import (
            ServiceError,
        )

        service, *_ = _build_service(
            history=[],  # no conversation history
            chunks=["x"],
            monkeypatch=monkeypatch,
        )
        with pytest.raises(ServiceError) as ei:
            await _drive(
                service.regenerate_assistant_message_stream(
                    token_name="alice",
                    session_id="s1",
                    message_index=0,
                    token_daily_quota=100,
                )
            )
        assert ei.value.code == "session_not_found"


# Suppress unused-import warning for AsyncMock — kept for future
# expansion of failing-bridge tests.
_ = AsyncMock
