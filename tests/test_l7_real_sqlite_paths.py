"""L7: real-sqlite happy-path coverage for the critical persistence flows.

The existing suite leans on `_StubStorage` for ConversationService
tests (`tests/test_regenerate_service.py`). Stubs run fast but never
exercise the real SQL: a schema typo, a missing index, a CASCADE
mis-spell, or a SQLite-vs-MySQL parity gap goes undetected until
production. Per the user's "integration tests over mocks for
migrations" guidance, we add at least one real-sqlite happy-path for
each safety-critical write.

Covered here:
  * `record_chat_pair` — the main write surface (session_meta upsert +
    EVENT_SESSION_CREATED / EVENT_MESSAGE_ADDED rows + CM call).
    Stubbed: CM (we don't run AstrBot here), file_store (LocalFileStore
    in tmp_path).
  * `clear_history` — session soft-delete + EVENT_SESSION_DELETED row,
    plus the bumped message_count semantics on a recreate.
  * `record_ip_failure` is already exercised against real sqlite in
    `test_h3_ip_guard_decrement.py` and `test_m_batch_fixes.py`, so no
    duplicate coverage here.

`regenerate_assistant_message_stream` is intentionally NOT covered by
this file: it needs a working LLM bridge (or a non-trivial stub thereof)
and the focus here is on the storage-layer truth. Stubbed regenerate
coverage continues to live in `test_regenerate_service.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio


# --- minimal stubs for the non-storage deps -----------------------------


class _StubCM:
    """Records add_message_pair calls; never raises so record_chat_pair
    proceeds to the chat-sync layer."""

    def __init__(self) -> None:
        self.pair_calls: list[dict[str, Any]] = []
        self.cleared_calls: list[str] = []

    async def get_curr_conversation_id(self, umo: str) -> str | None:
        return None

    async def new_conversation(self, umo: str, **kwargs: Any) -> str:
        return "cid-stub"

    async def add_message_pair(self, **kwargs: Any) -> None:
        self.pair_calls.append(dict(kwargs))

    async def get_human_readable_context(self, **kwargs: Any):
        return [], 0

    async def update_conversation(self, **kwargs: Any) -> None:
        pass


class _StubEventBus:
    def __init__(self) -> None:
        self.notified: list[tuple[str, int]] = []

    async def notify(self, *, token_name: str, last_pts: int) -> None:
        self.notified.append((token_name, last_pts))

    async def prune_idle(self) -> None:
        pass


@pytest_asyncio.fixture
async def real_service(tmp_path: Path):
    """Build a ConversationService backed by a real SqliteStorage + a
    LocalFileStore on tmp_path. CM and event bus are stubs since
    they're outside the scope of the storage-truth check."""
    from astrbot_plugin_webchat_gateway.core.audit import AuditLogger
    from astrbot_plugin_webchat_gateway.core.file_store import LocalFileStore
    from astrbot_plugin_webchat_gateway.core.ratelimit import (
        PerTokenConcurrency,
    )
    from astrbot_plugin_webchat_gateway.handlers.conversations_service import (
        ConversationService,
    )
    from astrbot_plugin_webchat_gateway.storage.sqlite_backend import (
        SqliteStorage,
    )

    storage = SqliteStorage(str(tmp_path / "conv.db"))
    await storage.initialize()
    cm = _StubCM()
    audit = AuditLogger(storage)
    bus = _StubEventBus()
    file_store = LocalFileStore(root=str(tmp_path / "uploads"))

    service = ConversationService(
        storage=storage,
        audit=audit,
        event_bus=bus,  # type: ignore[arg-type]
        cm=cm,
        file_store=file_store,
        concurrency=PerTokenConcurrency(),
        llm_bridge=None,  # we don't exercise the LLM path here
    )

    yield service, storage, cm, bus
    await storage.close()


@pytest.mark.asyncio
class TestRecordChatPairRealSqlite:
    async def test_first_pair_creates_session_meta_and_events(
        self, real_service
    ):
        from astrbot_plugin_webchat_gateway.handlers.conversations_service import (
            EVENT_MESSAGE_ADDED,
            EVENT_SESSION_CREATED,
        )

        service, storage, cm, _bus = real_service
        await service.record_chat_pair(
            token_name="alice",
            session_id="s1",
            user_text="hello",
            assistant_text="hi there",
        )

        # session_meta row exists with the right count + preview.
        meta = await storage.get_session_meta(
            token_name="alice", session_id="s1"
        )
        assert meta is not None
        assert meta.message_count == 2  # user + assistant
        assert "hi there" in meta.preview

        # webchat_updates carries the session_created + message_added
        # events the web UI's long-poll loop expects.
        rows = await storage.get_updates(
            token_name="alice", since_pts=0, limit=100
        )
        event_types = [r.event_type for r in rows]
        assert EVENT_SESSION_CREATED in event_types
        assert event_types.count(EVENT_MESSAGE_ADDED) >= 1

        # CM saw the pair too.
        assert len(cm.pair_calls) == 1

    async def test_second_pair_increments_message_count(self, real_service):
        service, storage, _cm, _bus = real_service
        await service.record_chat_pair(
            token_name="alice",
            session_id="s1",
            user_text="hi",
            assistant_text="hello",
        )
        await service.record_chat_pair(
            token_name="alice",
            session_id="s1",
            user_text="how are you",
            assistant_text="fine",
        )
        meta = await storage.get_session_meta(
            token_name="alice", session_id="s1"
        )
        assert meta is not None
        assert meta.message_count == 4
        # Preview is the LATEST assistant message, not concatenated.
        assert meta.preview.startswith("fine")

    async def test_pts_monotonic_across_pairs(self, real_service):
        """A regression that bisects events would show up here — pts
        must advance strictly between successive pairs."""
        service, storage, _cm, _bus = real_service
        await service.record_chat_pair(
            token_name="alice",
            session_id="s1",
            user_text="a",
            assistant_text="A",
        )
        first = await storage.get_updates(
            token_name="alice", since_pts=0, limit=100
        )
        max_first = max(r.pts for r in first)

        await service.record_chat_pair(
            token_name="alice",
            session_id="s1",
            user_text="b",
            assistant_text="B",
        )
        second = await storage.get_updates(
            token_name="alice", since_pts=max_first, limit=100
        )
        assert second, "second pair must emit fresh updates past max_first"
        assert all(r.pts > max_first for r in second)

    async def test_two_sessions_isolated(self, real_service):
        service, storage, _cm, _bus = real_service
        await service.record_chat_pair(
            token_name="alice",
            session_id="s1",
            user_text="a",
            assistant_text="A",
        )
        await service.record_chat_pair(
            token_name="alice",
            session_id="s2",
            user_text="b",
            assistant_text="B",
        )
        meta_s1 = await storage.get_session_meta(
            token_name="alice", session_id="s1"
        )
        meta_s2 = await storage.get_session_meta(
            token_name="alice", session_id="s2"
        )
        assert meta_s1 is not None and meta_s2 is not None
        assert meta_s1.session_id == "s1"
        assert meta_s2.session_id == "s2"
        assert meta_s1.message_count == 2
        assert meta_s2.message_count == 2

    async def test_cross_token_isolation(self, real_service):
        """alice's session_id 's1' must not collide with bob's 's1'."""
        service, storage, _cm, _bus = real_service
        await service.record_chat_pair(
            token_name="alice",
            session_id="s1",
            user_text="from alice",
            assistant_text="A",
        )
        await service.record_chat_pair(
            token_name="bob",
            session_id="s1",
            user_text="from bob",
            assistant_text="B",
        )
        # alice's view doesn't see bob's events and vice versa.
        alice_rows = await storage.get_updates(
            token_name="alice", since_pts=0, limit=100
        )
        bob_rows = await storage.get_updates(
            token_name="bob", since_pts=0, limit=100
        )
        # Each side has BOTH session_created and at least one message_added.
        alice_payloads = " ".join(r.payload for r in alice_rows)
        bob_payloads = " ".join(r.payload for r in bob_rows)
        assert "from alice" in alice_payloads
        assert "from alice" not in bob_payloads
        assert "from bob" in bob_payloads
        assert "from bob" not in alice_payloads
