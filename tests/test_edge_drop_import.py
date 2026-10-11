"""Edge Drop text imports become scoped Drop rows and one sync refresh event."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request


class _MemoryDropStorage:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.events: list[dict] = []
        self.fail_at: int | None = None

    async def append_drop_message(self, **kwargs) -> int:
        if self.fail_at == len(self.rows) + 1:
            raise RuntimeError("injected write failure")
        self.rows.append(kwargs)
        return len(self.rows)

    async def append_updates(self, *, token_name: str, events: list, now: int):
        self.events.append({"token_name": token_name, "events": events, "now": now})
        return [len(self.events)]


def _routes(monkeypatch, *, enabled=True, gate_response=None, storage=None):
    from astrbot_plugin_webchat_gateway.core.ratelimit import PerTokenUploadGate
    from astrbot_plugin_webchat_gateway.handlers import drop
    from astrbot_plugin_webchat_gateway.handlers.common import GatePass

    if storage is None:
        storage = _MemoryDropStorage()
    audit = SimpleNamespace(write=AsyncMock())
    bus = SimpleNamespace(notify=AsyncMock())
    gate = gate_response or GatePass(
        token=SimpleNamespace(name="alice"), ip="127.0.0.1", origin=None,
        allowed={"*"}, same_host="localhost",
    )
    monkeypatch.setattr(drop, "gate_request", AsyncMock(return_value=gate))
    deps = SimpleNamespace(
        storage=storage, audit=audit, event_bus=bus,
        upload_gate=PerTokenUploadGate(), allowed_origins={"*"},
        max_file_size_mb=20, per_token_storage_mb=100,
        enabled=enabled, trust_referer_as_origin=False,
    )
    return drop.make_drop_handlers(deps), storage, audit, bus


def _request(messages, *, format="edge-drop-text-v1", content_length=None):
    headers = {"Content-Length": str(content_length)} if content_length is not None else {}
    body = json.dumps({"format": format, "messages": messages}).encode()
    return make_mocked_request(
        "POST", "/api/webchat/drop/import", headers=headers,
        payload=SimpleNamespace(read=AsyncMock(side_effect=[body, b""])),
    )


@pytest.mark.asyncio
async def test_import_preserves_text_time_and_token_scope(monkeypatch):
    from astrbot_plugin_webchat_gateway.handlers import drop
    from astrbot_plugin_webchat_gateway.handlers.common import GatePass

    routes, storage, audit, bus = _routes(monkeypatch)
    messages = [
        {"text": "你好", "created_at": 1751869164},
        {"text": "第一行\n第二行", "created_at": 1751979845},
    ]
    response = await routes["import"](_request(messages))
    assert response.status == 200
    assert json.loads(response.body) == {"imported": 2, "failed": 0}
    assert [row["text"] for row in storage.rows] == ["你好", "第一行\n第二行"]
    assert [row["now"] for row in storage.rows] == [1751869164, 1751979845]
    assert all(row["token_name"] == "alice" and row["kind"] == "text" for row in storage.rows)
    assert all(row["device_id"] == "edge-drop-import" and row["file_id"] is None for row in storage.rows)
    assert len(storage.events) == 1
    event = storage.events[0]["events"][0]
    assert event.event_type == "drop_history_imported"
    assert event.session_id == "drop"
    assert json.loads(event.payload) == {"count": 2}
    bus.notify.assert_awaited_once_with("alice")
    assert audit.write.await_args.kwargs["detail"] == {"imported": 2, "failed": 0}

    drop.gate_request.return_value = GatePass(
        token=SimpleNamespace(name="bob"), ip="127.0.0.2", origin=None,
        allowed={"*"}, same_host="localhost",
    )
    await routes["import"](_request([messages[0]]))
    assert storage.rows[-1]["token_name"] == "bob"
    assert storage.events[-1]["token_name"] == "bob"


@pytest.mark.asyncio
async def test_import_and_list_interleave_with_persisted_local_messages(monkeypatch, tmp_path):
    from astrbot_plugin_webchat_gateway.storage.sqlite_backend import SqliteStorage

    storage = SqliteStorage(str(tmp_path / "drop_import.db"))
    await storage.initialize()
    try:
        routes, _, _, _ = _routes(monkeypatch, storage=storage)
        for text, when in [("local old", 1751869000), ("local new", 1751869300)]:
            await storage.append_drop_message(
                token_name="alice", device_id="device-local", device_name="PC",
                kind="text", text=text, file_id=None, filename="",
                mime="", size_bytes=0, now=when,
            )
        response = await routes["import"](_request([
            {"text": "imported", "created_at": 1751869164},
        ]))
        assert response.status == 200
        first = await routes["list"](make_mocked_request(
            "GET", "/api/webchat/drop/messages?limit=2",
        ))
        body = json.loads(first.body)
        assert [row["text"] for row in body["messages"]] == ["local new", "imported"]
        assert body["has_more"] is True
        second = await routes["list"](make_mocked_request(
            "GET", f"/api/webchat/drop/messages?limit=2&before={body['before']}",
        ))
        body = json.loads(second.body)
        assert [row["text"] for row in body["messages"]] == ["local old"]
        assert body["has_more"] is False
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("messages,fmt", [
    ([], "edge-drop-text-v1"),
    ([{"text": "", "created_at": 1751869164}], "edge-drop-text-v1"),
    ([{"text": "你好", "created_at": True}], "edge-drop-text-v1"),
    ([{"text": "你好", "created_at": 1751869164, "file_id": "x"}], "edge-drop-text-v1"),
    ([{"text": "你好", "created_at": 1751869164}], "other"),
    ([{"text": "x", "created_at": 1751869164}] * 1001, "edge-drop-text-v1"),
])
async def test_import_rejects_invalid_backup_before_writing(monkeypatch, messages, fmt):
    routes, storage, _, _ = _routes(monkeypatch)
    response = await routes["import"](_request(messages, format=fmt))
    assert response.status == 400
    assert json.loads(response.body)["error"] == "invalid_drop_import"
    assert storage.rows == []


@pytest.mark.asyncio
async def test_import_rejects_oversized_body(monkeypatch):
    from astrbot_plugin_webchat_gateway.handlers.edge_drop_import import MAX_EDGE_DROP_IMPORT_BYTES

    routes, storage, _, _ = _routes(monkeypatch)
    response = await routes["import"](_request([], content_length=MAX_EDGE_DROP_IMPORT_BYTES + 1))
    assert response.status == 413
    assert storage.rows == []


@pytest.mark.asyncio
async def test_import_bounds_chunked_body_without_content_length(monkeypatch):
    from astrbot_plugin_webchat_gateway.handlers.edge_drop_import import MAX_EDGE_DROP_IMPORT_BYTES

    routes, storage, _, _ = _routes(monkeypatch)
    request = _request([])
    request.content.read.side_effect = [b"x" * (MAX_EDGE_DROP_IMPORT_BYTES + 1)]
    response = await routes["import"](request)
    assert response.status == 413
    request.content.read.assert_awaited_once_with(MAX_EDGE_DROP_IMPORT_BYTES + 1)
    assert storage.rows == []


@pytest.mark.asyncio
async def test_import_reads_all_chunks_before_parsing(monkeypatch):
    routes, storage, _, _ = _routes(monkeypatch)
    request = _request([{"text": "split", "created_at": 1751869164}])
    body = json.dumps({
        "format": "edge-drop-text-v1",
        "messages": [{"text": "split", "created_at": 1751869164}],
    }).encode()
    request.content.read.side_effect = [body[:12], body[12:], b""]
    response = await routes["import"](request)
    assert response.status == 200
    assert [row["text"] for row in storage.rows] == ["split"]


@pytest.mark.asyncio
async def test_import_reports_partial_write_without_duplicate_events(monkeypatch):
    routes, storage, _, _ = _routes(monkeypatch)
    storage.fail_at = 2
    messages = [
        {"text": "one", "created_at": 1751869164},
        {"text": "two", "created_at": 1751869165},
        {"text": "three", "created_at": 1751869166},
    ]
    response = await routes["import"](_request(messages))
    assert response.status == 200
    assert json.loads(response.body) == {"imported": 1, "failed": 2}
    assert len(storage.rows) == 1
    assert len(storage.events) == 1
    assert json.loads(storage.events[0]["events"][0].payload) == {"count": 1}


@pytest.mark.asyncio
async def test_import_reports_when_no_rows_were_written(monkeypatch):
    routes, storage, audit, _ = _routes(monkeypatch)
    storage.fail_at = 1
    response = await routes["import"](_request([
        {"text": "one", "created_at": 1751869164},
        {"text": "two", "created_at": 1751869165},
    ]))
    assert response.status == 500
    assert json.loads(response.body) == {"imported": 0, "failed": 2}
    assert storage.rows == []
    assert storage.events == []
    assert audit.write.await_args.kwargs["detail"] == {"imported": 0, "failed": 2}


@pytest.mark.asyncio
async def test_import_respects_auth_and_drop_toggle(monkeypatch):
    denial = web.Response(status=401)
    routes, storage, _, _ = _routes(monkeypatch, gate_response=denial)
    message = [{"text": "hi", "created_at": 1751869164}]
    assert await routes["import"](_request(message)) is denial
    assert storage.rows == []

    routes, storage, _, _ = _routes(monkeypatch, enabled=False)
    response = await routes["import"](_request(message))
    assert response.status == 403
    assert json.loads(response.body)["error"] == "drop_disabled"
    assert storage.rows == []
