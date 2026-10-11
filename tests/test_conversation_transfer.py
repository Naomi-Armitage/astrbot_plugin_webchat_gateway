"""Conversation JSON backups survive a server round trip without token leaks."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from astrbot_plugin_webchat_gateway.storage.base import SessionMetaRow, UpdateRow


class _MemoryStorage:
    """Keep the service test independent of aiosqlite's worker thread."""

    def __init__(self) -> None:
        self.meta: dict[tuple[str, str], SessionMetaRow] = {}
        self.updates: dict[str, list[UpdateRow]] = {}
        self.fail_title: str | None = None

    async def upsert_session_meta(
        self, *, token_name: str, session_id: str, title: str,
        title_manual: bool, pinned_at: int | None, message_count: int,
        preview: str, now: int,
    ) -> SessionMetaRow:
        if title == self.fail_title:
            raise RuntimeError("injected metadata write failure")
        row = SessionMetaRow(
            token_name=token_name, session_id=session_id, title=title,
            title_manual=title_manual, pinned_at=pinned_at, deleted_at=None,
            updated_at=now, message_count=message_count, preview=preview,
        )
        self.meta[(token_name, session_id)] = row
        return row

    async def get_session_meta(self, *, token_name: str, session_id: str):
        return self.meta.get((token_name, session_id))

    async def list_session_meta(self, *, token_name: str, include_deleted: bool = False):
        return [row for (owner, _), row in self.meta.items()
                if owner == token_name and (include_deleted or row.deleted_at is None)]

    async def append_updates(self, *, token_name: str, events: list, now: int):
        rows = self.updates.setdefault(token_name, [])
        for event in events:
            rows.append(UpdateRow(
                token_name=token_name, pts=len(rows) + 1, ts=now,
                event_type=event.event_type, session_id=event.session_id,
                payload=event.payload,
            ))
        return [row.pts for row in rows[-len(events):]] if events else []

    async def get_updates(self, *, token_name: str, since_pts: int, limit: int):
        return [row for row in self.updates.get(token_name, [])
                if row.pts > since_pts][:limit]

    async def get_max_pts(self, *, token_name: str) -> int:
        return len(self.updates.get(token_name, []))

    async def get_file(self, file_id: str):
        return None


class _MemoryCM:
    def __init__(self) -> None:
        self.histories: dict[str, list[dict]] = {}

    async def new_conversation(self, umo: str, **_kwargs) -> str:
        self.histories[umo] = []
        return umo

    async def update_conversation(
        self, *, unified_msg_origin: str, conversation_id: str, history: list[dict]
    ) -> None:
        assert unified_msg_origin == conversation_id
        self.histories[unified_msg_origin] = history

    async def get_curr_conversation_id(self, umo: str) -> str | None:
        return umo if umo in self.histories else None

    async def get_conversation(self, umo: str, cid: str):
        return SimpleNamespace(history=json.dumps(self.histories[cid]))


class _Audit:
    def __init__(self) -> None:
        self.writes: list[dict] = []

    async def write(self, event: str, **kwargs) -> None:
        self.writes.append({"event": event, **kwargs})


class _Bus:
    def __init__(self) -> None:
        self.notified: list[str] = []

    async def notify(self, token_name: str) -> None:
        self.notified.append(token_name)


@pytest.mark.asyncio
async def test_import_export_round_trip_and_token_scope():
    from astrbot_plugin_webchat_gateway.handlers.conversations_service import (
        ConversationService,
    )
    from astrbot_plugin_webchat_gateway.handlers.conversations_transfer import (
        ATTACHMENT_PLACEHOLDER,
        TRANSFER_FORMAT,
        TRANSFER_VERSION,
        validate_import_document,
    )
    storage = _MemoryStorage()
    cm = _MemoryCM()
    audit = _Audit()
    bus = _Bus()
    service = ConversationService(
        storage=storage, audit=audit, event_bus=bus, cm=cm, file_store=object()
    )
    backup = {
        "format": TRANSFER_FORMAT,
        "version": TRANSFER_VERSION,
        "conversations": [
            {
                "title": "旅行计划",
                "title_manual": True,
                "pinned": True,
                "messages": [
                    {"role": "user", "content": "去哪里？"},
                    {"role": "assistant", "content": "去海边。"},
                ],
            },
            {"title": "空会话", "title_manual": False, "pinned": False, "messages": []},
        ],
    }
    result = await service.import_conversations(
        token_name="alice",
        conversations=validate_import_document(backup),
        ip="127.0.0.1",
    )
    assert result["imported"] == 2
    assert result["failed"] == 0
    assert len(set(result["session_ids"])) == 2
    assert bus.notified == ["alice"]
    assert audit.writes[-1]["detail"] == {"imported": 2, "failed": 0}
    created_events = storage.updates["alice"]
    assert [event.event_type for event in created_events] == ["session_created"] * 2
    first_event = json.loads(created_events[0].payload)
    assert first_event == {"title": "旅行计划", "title_manual": True, "pinned": True}

    exported = await service.export_conversations(token_name="alice")
    assert exported["format"] == TRANSFER_FORMAT
    assert exported["version"] == TRANSFER_VERSION
    assert exported["attachments_omitted"] == 0
    by_title = {row["title"]: row for row in exported["conversations"]}
    assert by_title["旅行计划"]["messages"] == backup["conversations"][0]["messages"]
    assert by_title["旅行计划"]["pinned"] is True
    assert by_title["空会话"]["messages"] == []

    # A rendered image without a portable file is marked in the text
    # transcript, not serialized as a broken file_id in the backup.
    sid = result["session_ids"][0]
    umo = f"webchat_gateway:alice:{sid}"
    cm.histories[umo].append(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": ""},
                {"type": "image_url", "image_url": {"id": "private-file-id", "url": "file:///private.png"}},
            ],
        }
    )
    with_image = await service.export_conversations(token_name="alice")
    assert with_image["attachments_omitted"] == 1
    assert with_image["conversations"][0]["messages"][-1]["content"] == ATTACHMENT_PLACEHOLDER
    assert "private-file-id" not in json.dumps(with_image)

    copied = await service.import_conversations(
        token_name="bob", conversations=validate_import_document(exported)
    )
    assert copied["imported"] == 2
    assert set(copied["session_ids"]).isdisjoint(result["session_ids"])
    assert len((await service.export_conversations(token_name="bob"))["conversations"]) == 2
    assert (await service.export_conversations(token_name="charlie"))["conversations"] == []


@pytest.mark.asyncio
async def test_empty_backup_and_partial_failure():
    from astrbot_plugin_webchat_gateway.handlers.conversations_service import ConversationService
    from astrbot_plugin_webchat_gateway.handlers.conversations_transfer import (
        TRANSFER_FORMAT, TRANSFER_VERSION, validate_import_document,
    )

    storage = _MemoryStorage()
    storage.fail_title = "失败"
    audit = _Audit()
    service = ConversationService(
        storage=storage, audit=audit, event_bus=_Bus(), cm=_MemoryCM(), file_store=object()
    )
    empty = {"format": TRANSFER_FORMAT, "version": TRANSFER_VERSION, "conversations": []}
    assert validate_import_document(empty) == []
    assert (await service.export_conversations(token_name="alice"))["conversations"] == []
    assert await service.import_conversations(
        token_name="alice", conversations=validate_import_document(empty)
    ) == {"imported": 0, "failed": 0, "session_ids": []}

    backup = {**empty, "conversations": [
        {"title": "成功", "title_manual": False, "pinned": False, "messages": []},
        {"title": "失败", "title_manual": False, "pinned": False, "messages": []},
    ]}
    result = await service.import_conversations(
        token_name="alice", conversations=validate_import_document(backup)
    )
    assert result["imported"] == 1
    assert result["failed"] == 1
    assert len((await service.export_conversations(token_name="alice"))["conversations"]) == 1
    assert audit.writes[-1]["detail"] == {"imported": 1, "failed": 1}


@pytest.mark.parametrize(
    "change",
    [
        {"format": "other"},
        {"version": 2},
        {"version": True},
        {"conversations": None},
        {"conversations": [{}] * 501},
        {"conversations": [{"title": "x", "title_manual": 1, "pinned": False, "messages": []}]},
        {"conversations": [{"title": "x" * 256, "title_manual": False, "pinned": False, "messages": []}]},
        {"conversations": [{"title": "x", "title_manual": False, "pinned": False,
                            "messages": [{"role": "system", "content": "hidden"}]}]},
        {"conversations": [{"title": "x", "title_manual": False, "pinned": False,
                            "messages": [{"role": "user", "content": "text", "attachments": []}]}]},
        {"conversations": [{"title": "x", "title_manual": False, "pinned": False,
                            "messages": [{"role": "user", "content": "x" * 100_001}]}]},
        {"conversations": [{"title": "x", "title_manual": False, "pinned": False,
                            "messages": [{"role": "user", "content": "x"}] * 20_001}]},
    ],
)
def test_invalid_backup_rejected_before_any_write(change):
    from astrbot_plugin_webchat_gateway.handlers.admin_tokens import ServiceError
    from astrbot_plugin_webchat_gateway.handlers.conversations_transfer import (
        TRANSFER_FORMAT,
        TRANSFER_VERSION,
        validate_import_document,
    )

    valid = {
        "format": TRANSFER_FORMAT,
        "version": TRANSFER_VERSION,
        "conversations": [
            {"title": "x", "title_manual": False, "pinned": False, "messages": []}
        ],
    }
    with pytest.raises(ServiceError):
        validate_import_document({**valid, **change})


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,declared_size,status,error", [
    (b"not json", None, 400, "invalid_json"),
    (b'{"format":"other"}', None, 400, "invalid_import"),
    (b"", 513, 413, "payload_too_large"),
    (b"x" * 513, None, 413, "payload_too_large"),
])
async def test_import_http_rejects_bad_files_before_writing(
    monkeypatch, raw, declared_size, status, error,
):
    from astrbot_plugin_webchat_gateway.handlers import conversations as handlers
    from astrbot_plugin_webchat_gateway.handlers.common import GatePass

    gate = AsyncMock(return_value=GatePass(
        token=SimpleNamespace(name="alice"), ip="127.0.0.1", origin=None,
        allowed={"*"}, same_host="localhost",
    ))
    monkeypatch.setattr(handlers, "gate_request", gate)
    monkeypatch.setattr(handlers, "MAX_TRANSFER_BYTES", 512)
    service = SimpleNamespace(import_conversations=AsyncMock())
    routes = handlers.make_conversation_handlers(
        SimpleNamespace(allowed_origins={"*"}, trust_referer_as_origin=False), service,
    )
    request = make_mocked_request("POST", "/conversations/import", headers=(
        {"Content-Length": str(declared_size)} if declared_size is not None else {}
    ))
    request._read_bytes = raw
    response = await routes["import"](request)
    assert response.status == status
    assert json.loads(response.body)["error"] == error
    service.import_conversations.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["import", "export"])
async def test_transfer_requires_authentication(monkeypatch, operation):
    from astrbot_plugin_webchat_gateway.handlers import conversations as handlers

    denial = web.Response(status=401)
    monkeypatch.setattr(handlers, "gate_request", AsyncMock(return_value=denial))
    service = SimpleNamespace(import_conversations=AsyncMock(), export_conversations=AsyncMock())
    routes = handlers.make_conversation_handlers(
        SimpleNamespace(allowed_origins={"*"}, trust_referer_as_origin=False), service,
    )
    request = make_mocked_request("POST" if operation == "import" else "GET", f"/conversations/{operation}")
    assert await routes[operation](request) is denial
    service.import_conversations.assert_not_awaited()
    service.export_conversations.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("rows,result,status", [
    ([], {"imported": 0, "failed": 0, "session_ids": []}, 200),
    ([{"title": "x", "title_manual": False, "pinned": False, "messages": []}],
     {"imported": 0, "failed": 1, "session_ids": []}, 500),
    ([{"title": "x", "title_manual": False, "pinned": False, "messages": []}],
     {"imported": 1, "failed": 0, "session_ids": ["new-id"]}, 200),
])
async def test_import_http_reports_outcome(monkeypatch, rows, result, status):
    from astrbot_plugin_webchat_gateway.handlers import conversations as handlers
    from astrbot_plugin_webchat_gateway.handlers.common import GatePass
    from astrbot_plugin_webchat_gateway.handlers.conversations_transfer import (
        TRANSFER_FORMAT, TRANSFER_VERSION,
    )

    monkeypatch.setattr(handlers, "gate_request", AsyncMock(return_value=GatePass(
        token=SimpleNamespace(name="alice"), ip="127.0.0.1", origin=None,
        allowed={"*"}, same_host="localhost",
    )))
    service = SimpleNamespace(import_conversations=AsyncMock(return_value=result))
    routes = handlers.make_conversation_handlers(
        SimpleNamespace(allowed_origins={"*"}, trust_referer_as_origin=False), service,
    )
    request = make_mocked_request("POST", "/conversations/import")
    request._read_bytes = json.dumps({
        "format": TRANSFER_FORMAT, "version": TRANSFER_VERSION, "conversations": rows,
    }).encode()
    response = await routes["import"](request)
    assert response.status == status
    assert json.loads(response.body) == result
    service.import_conversations.assert_awaited_once()
