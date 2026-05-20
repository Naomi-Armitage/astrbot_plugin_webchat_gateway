"""Regression tests for the "previously generated images vanish on
cold-refresh" failure.

Failure shape: an `/image` slash-command reply persists with
`assistant_text=""` (the image IS the reply, no placeholder filler).
On `get_conversation`, the wire payload to the client must include
the assistant attachment so the chat client renders the image bubble
on reload. Two failure modes both contributed to the data
disappearing:

1. `_renderable_entry` (the shared keep-predicate) used to drop any
   empty-text assistant turn, treating it as "stale noise". The
   `/image` reply has exactly that shape, so `_normalize_history`
   removed the turn entirely before the events-overlay could attach
   the file_ids. Net effect: the row never reached the wire format.

2. `_renderable_entry` and `get_conversation`'s decoration loop both
   restricted attachment extraction to `role == "user"`, so even if
   #1 hadn't fired, the assistant attachments would have been
   stripped on the way out.

The tests pin BOTH failure paths so a future refactor that
re-introduces either gate will fail loudly here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest


# ---------------------------------------------------------------------
# _renderable_entry — pure function, no stubs needed
# ---------------------------------------------------------------------


class TestRenderableEntry:
    def test_keeps_empty_text_assistant_turn(self):
        """An empty-text assistant turn must NOT be dropped at normalize
        time — image-only `/image` replies look exactly like this in CM
        before the events overlay attaches the file_ids. Pre-fix this
        returned None and the turn vanished from history."""
        from astrbot_plugin_webchat_gateway.handlers.conversations_overlay import (
            _renderable_entry,
        )

        result = _renderable_entry(
            {"role": "assistant", "content": [{"type": "text", "text": ""}]}
        )
        assert result is not None
        role, text, file_ids = result
        assert role == "assistant"
        assert text == ""
        assert file_ids == []

    def test_extracts_assistant_file_ids_from_image_url_parts(self):
        """`_cm_persist_pair` now writes ImageURLPart segments under
        the assistant content too. _renderable_entry must surface them
        for both roles — gating extraction to user-only used to drop
        every generated image stored in CM."""
        from astrbot_plugin_webchat_gateway.handlers.conversations_overlay import (
            _renderable_entry,
        )

        content = [
            {"type": "text", "text": ""},
            {
                "type": "image_url",
                "image_url": {"url": "file:///tmp/gen.png", "id": "gen-1"},
            },
        ]
        result = _renderable_entry({"role": "assistant", "content": content})
        assert result is not None
        _, _, file_ids = result
        assert file_ids == ["gen-1"]

    def test_extracts_user_file_ids_unchanged(self):
        """User-side extraction continues to work — the change widened
        the gate from user-only to both, it didn't replace it."""
        from astrbot_plugin_webchat_gateway.handlers.conversations_overlay import (
            _renderable_entry,
        )

        content = [
            {"type": "text", "text": "look"},
            {
                "type": "image_url",
                "image_url": {"url": "file:///tmp/u.png", "id": "u-1"},
            },
        ]
        result = _renderable_entry({"role": "user", "content": content})
        assert result is not None
        role, text, file_ids = result
        assert role == "user"
        assert text == "look"
        assert file_ids == ["u-1"]

    def test_drops_non_user_assistant_roles(self):
        """The "role must be user|assistant" check stayed — system
        / tool messages still don't render."""
        from astrbot_plugin_webchat_gateway.handlers.conversations_overlay import (
            _renderable_entry,
        )

        assert (
            _renderable_entry(
                {"role": "system", "content": [{"type": "text", "text": "x"}]}
            )
            is None
        )
        assert (
            _renderable_entry(
                {"role": "tool", "content": [{"type": "text", "text": "x"}]}
            )
            is None
        )


# ---------------------------------------------------------------------
# _normalize_history — keeps the empty-text assistant row through the
# normalize pass
# ---------------------------------------------------------------------


class TestNormalizeHistoryKeepsImageReply:
    def test_image_only_assistant_turn_survives_normalize(self):
        """The full normalize pass over a CM history that ends in an
        image-only `/image` reply must keep the empty-text assistant
        row so the overlay layer can attach its file_ids. Pre-fix the
        row was dropped and the rendered list ended at the user turn."""
        from astrbot_plugin_webchat_gateway.handlers.conversations_overlay import (
            _normalize_history,
        )

        raw = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "/image a cat"}],
            },
            {
                "role": "assistant",
                # Post-placeholder-removal shape: empty text, no
                # ImageURLPart yet (CM doesn't have it for entries
                # written before the _cm_persist_pair update).
                "content": [{"type": "text", "text": ""}],
            },
        ]
        out = _normalize_history(raw)
        assert len(out) == 2
        assert out[0]["role"] == "user"
        assert out[1]["role"] == "assistant"
        assert out[1]["content"] == ""

    def test_image_url_part_on_assistant_surfaces_attachments(self):
        """Forward-looking: once `_cm_persist_pair` writes ImageURLPart
        under the assistant, the normalize pass must surface the
        file_ids in the `attachments` field on the rendered row."""
        from astrbot_plugin_webchat_gateway.handlers.conversations_overlay import (
            _normalize_history,
        )

        raw = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "/image a cat"}],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": ""},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "file:///tmp/cat.png",
                            "id": "f-cat-1",
                        },
                    },
                ],
            },
        ]
        out = _normalize_history(raw)
        assert len(out) == 2
        assert out[1]["attachments"] == [{"file_id": "f-cat-1"}]


# ---------------------------------------------------------------------
# Full pipeline — get_conversation should surface assistant attachments
# in the wire payload via the events-overlay backfill
# ---------------------------------------------------------------------


class _StubConversation:
    def __init__(self, history: list[Any]) -> None:
        self.history = history


class _StubCM:
    def __init__(self, history: list[Any]) -> None:
        self._history = history
        self.cid = "cid-1"

    async def get_curr_conversation_id(self, umo: str) -> str:
        return self.cid

    async def get_conversation(self, umo: str, cid: str) -> _StubConversation:
        return _StubConversation(self._history)


class _StubMeta:
    def __init__(self) -> None:
        self.title = "test"
        self.title_manual = False
        self.pinned_at = None
        self.updated_at = 1_700_000_000


class _StubUpdateRow:
    """Mirror UpdateRow with just the fields _build_history_overlay reads."""

    def __init__(
        self, *, pts: int, session_id: str, event_type: str, payload: str
    ) -> None:
        self.pts = pts
        self.session_id = session_id
        self.event_type = event_type
        self.payload = payload


class _StubFileRow:
    def __init__(self, file_id: str, mime: str) -> None:
        self.file_id = file_id
        self.mime = mime


class _StubStorage:
    """Minimal AbstractStorage surface for `get_conversation`."""

    def __init__(
        self,
        *,
        meta: _StubMeta | None,
        update_rows: list[_StubUpdateRow],
        files: dict[str, _StubFileRow] | None = None,
    ) -> None:
        self._meta = meta
        self._update_rows = update_rows
        self._files = files or {}

    async def get_session_meta(self, *, token_name, session_id):
        return self._meta

    async def upsert_session_meta(self, **kwargs):
        # Not exercised on the happy-path test below.
        return _StubMeta()

    async def get_updates(self, *, token_name, since_pts, limit):
        # Single-page return.
        if since_pts >= len(self._update_rows):
            return []
        return self._update_rows[since_pts:]

    async def get_files_by_ids(self, file_ids):
        return [self._files[fid] for fid in file_ids if fid in self._files]


def _user_evt(pts: int, session_id: str, text: str) -> _StubUpdateRow:
    import json

    return _StubUpdateRow(
        pts=pts,
        session_id=session_id,
        event_type="message_added",
        payload=json.dumps({"role": "user", "content": text}),
    )


def _assistant_evt_with_attachment(
    pts: int, session_id: str, file_id: str, mime: str = "image/png"
) -> _StubUpdateRow:
    import json

    return _StubUpdateRow(
        pts=pts,
        session_id=session_id,
        event_type="message_added",
        payload=json.dumps(
            {
                "role": "assistant",
                "content": "",
                "attachments": [{"file_id": file_id, "mime": mime}],
            }
        ),
    )


@pytest.mark.asyncio
class TestGetConversationSurfacesAssistantAttachments:
    async def test_image_reply_attachment_arrives_in_wire_payload(self):
        """End-to-end: the failure the user reported ("以前生成的图片
        重进对话看不到了"). CM has an empty-text assistant turn (no
        ImageURLPart — this is the legacy data shape from before the
        `_cm_persist_pair` update), and `webchat_updates` has the
        corresponding `message_added` event with the assistant
        attachment. The wire payload from `get_conversation` MUST
        carry that attachment forward to the client."""
        from astrbot_plugin_webchat_gateway.handlers.conversations import (
            ConversationService,
        )

        sid = "sess-1"
        # CM history: user prompt + empty-text assistant reply. This
        # is what the pre-fix `_cm_persist_pair` left behind for
        # `/image` calls after we dropped the "[已生成 1 张图片]"
        # placeholder filler.
        cm_history = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "/image a cat"}],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": ""}],
            },
        ]
        # Events overlay carries the assistant attachment.
        update_rows = [
            _user_evt(pts=1, session_id=sid, text="/image a cat"),
            _assistant_evt_with_attachment(
                pts=2, session_id=sid, file_id="gen-cat-1"
            ),
        ]
        service = ConversationService(
            storage=_StubStorage(  # type: ignore[arg-type]
                meta=_StubMeta(),
                update_rows=update_rows,
                files={"gen-cat-1": _StubFileRow("gen-cat-1", "image/png")},
            ),
            audit=AsyncMock(),
            event_bus=AsyncMock(),
            cm=_StubCM(cm_history),
            file_store=None,  # type: ignore[arg-type]
            concurrency=None,
            llm_bridge=None,  # type: ignore[arg-type]
        )

        detail = await service.get_conversation(
            token_name="t1", session_id=sid
        )

        # Assistant turn must be present and carry its attachment.
        assert len(detail.messages) == 2
        assistant_msg = detail.messages[1]
        assert assistant_msg["role"] == "assistant"
        assert assistant_msg["content"] == ""
        attachments = assistant_msg.get("attachments")
        assert attachments, "assistant attachment missing from wire payload"
        assert attachments[0]["file_id"] == "gen-cat-1"
