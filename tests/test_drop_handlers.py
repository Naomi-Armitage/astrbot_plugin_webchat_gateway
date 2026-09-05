"""Drop HTTP handlers on a real SqliteStorage + LocalFileStore.

Exercises the five endpoints + the Drop serve endpoint end-to-end
through aiohttp's TestServer with real bearer auth (the same pattern
as test_m_batch_fixes' `_build_deps_and_token`): gate → body parse →
ownership check → commit → persist → event push. Also pins the
reserved-session guards (conversations refusing the "drop" session id)
so a regression can't reintroduce CM pollution via the reserved
namespace.
"""

from __future__ import annotations

import json
from pathlib import Path

from aiohttp import FormData

import pytest
from aiohttp.test_utils import TestClient, TestServer


class _StubEventBus:
    def __init__(self) -> None:
        self.notified: list[str] = []

    async def notify(self, token_name: str) -> None:
        self.notified.append(token_name)

    async def prune_idle(self) -> None:
        pass


class _StubIpGuard:
    def __init__(self) -> None:
        self.failures = 0

    async def is_blocked(self, ip: str, *, now: int = 0) -> tuple[bool, int]:
        return (False, 0)

    async def record_failure(self, ip: str) -> None:
        self.failures += 1

    async def reset(self, ip: str) -> None:
        pass


def _upload_form(content: bytes, filename: str, mime: str = "text/plain") -> FormData:
    """Multipart body exactly as the browser's FormData would send it.

    A plain dict with a mixed tuple+str value set gets sent as
    application/x-www-form-urlencoded by aiohttp 3.14's TestClient,
    which the upload handler correctly refuses — always wrap in an
    explicit FormData.
    """
    form = FormData()
    form.add_field("file", content, filename="blob.bin", content_type=mime)
    form.add_field("filename", filename)
    return form


async def _make_harness(tmp_path: Path, *, enabled: bool = True):
    from astrbot_plugin_webchat_gateway.core.audit import AuditLogger
    from astrbot_plugin_webchat_gateway.core.auth import (
        generate_token,
        hash_token,
    )
    from astrbot_plugin_webchat_gateway.core.file_store import LocalFileStore
    from astrbot_plugin_webchat_gateway.core.ratelimit import (
        PerTokenUploadGate,
    )
    from astrbot_plugin_webchat_gateway.handlers.drop import DropDeps
    from astrbot_plugin_webchat_gateway.storage.sqlite_backend import (
        SqliteStorage,
    )

    storage = SqliteStorage(str(tmp_path / "drop_http.db"))
    await storage.initialize()
    audit = AuditLogger(storage)
    bus = _StubEventBus()
    guard = _StubIpGuard()
    file_store = LocalFileStore(root=str(tmp_path / "uploads"))

    plaintext = generate_token()
    name = "alice"
    await storage.create_token(
        name=name,
        token_hash=hash_token(plaintext),
        daily_quota=100,
        note="",
        now=1,
    )
    headers = {"Authorization": f"Bearer {plaintext}"}

    deps = DropDeps(
        storage=storage,
        audit=audit,
        event_bus=bus,  # type: ignore[arg-type]
        file_store=file_store,
        upload_gate=PerTokenUploadGate(),
        ip_guard=guard,  # type: ignore[arg-type]
        allowed_origins={"*"},
        per_token_storage_mb=10,
        max_file_size_mb=5,
        trust_forwarded_for=False,
        enabled=enabled,
        allow_missing_origin=True,
    )
    return storage, audit, bus, guard, file_store, deps, headers, name


@pytest.mark.asyncio
class TestDropEndpoints:
    async def _client(self, tmp_path: Path, *, enabled: bool = True):
        from aiohttp import web

        from astrbot_plugin_webchat_gateway.handlers.drop import (
            make_drop_handlers,
            make_drop_serve_handler,
        )

        (
            storage, audit, bus, guard, file_store, deps, headers, name
        ) = await _make_harness(tmp_path, enabled=enabled)

        app = web.Application(client_max_size=10 * 1024 * 1024)
        handlers = make_drop_handlers(deps)
        app.router.add_post("/api/webchat/drop/send", handlers["send"])
        app.router.add_post("/api/webchat/drop/upload", handlers["upload"])
        app.router.add_get("/api/webchat/drop/messages", handlers["list"])
        app.router.add_delete(
            "/api/webchat/drop/messages/{message_id}", handlers["delete"]
        )
        app.router.add_post("/api/webchat/drop/clear", handlers["clear"])
        app.router.add_get(
            "/api/webchat/drop/files/{file_id}",
            make_drop_serve_handler(deps),
        )
        server = TestServer(app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        return (
            client, server, storage, bus, guard, file_store, headers, name
        )

    async def _close(self, client: TestClient, server: TestServer) -> None:
        await client.close()
        await server.close()

    async def test_send_text_note_roundtrip_and_event(
        self, tmp_path: Path
    ):
        client, server, storage, bus, _guard, _fs, headers, name = (
            await self._client(tmp_path)
        )
        try:
            resp = await client.post(
                "/api/webchat/drop/send",
                headers=headers,
                json={
                    "text": "meeting note",
                    "device_id": "device-abcdef1",
                    "device_name": "Work PC",
                },
            )
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            assert len(body["messages"]) == 1
            msg = body["messages"][0]
            assert msg["text"] == "meeting note"
            assert msg["kind"] == "text"
            assert msg["device_id"] == "device-abcdef1"
            assert msg["device_name"] == "Work PC"
            assert "file_id" not in msg

            # Persisted truth.
            rows = await storage.list_drop_messages(
                token_name=name, limit=10, before_id=None,
                include_deleted=False,
            )
            assert len(rows) == 1 and rows[0].id == msg["id"]

            # The long-poll channel got the wake + a drop_message_added
            # event carrying the SAME wire payload.
            assert bus.notified == [name]
            updates = await storage.get_updates(
                token_name=name, since_pts=0, limit=100
            )
            drop_events = [
                u for u in updates if u.event_type == "drop_message_added"
            ]
            assert len(drop_events) == 1
            payload = json.loads(drop_events[0].payload)
            assert payload["id"] == msg["id"]
            assert payload["text"] == "meeting note"
            assert drop_events[0].session_id == "drop"
        finally:
            await self._close(client, server)

    async def test_send_requires_device_id_shape(self, tmp_path: Path):
        client, server, *_rest, headers, _name = await self._client(tmp_path)
        try:
            resp = await client.post(
                "/api/webchat/drop/send",
                headers=headers,
                json={"text": "hi", "device_id": "short"},
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid_device_id"
        finally:
            await self._close(client, server)

    async def test_send_rejects_empty_payload(self, tmp_path: Path):
        client, server, *_rest, headers, _name = await self._client(tmp_path)
        try:
            resp = await client.post(
                "/api/webchat/drop/send",
                headers=headers,
                json={"text": "", "device_id": "device-abcdef1"},
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid_payload"
        finally:
            await self._close(client, server)

    async def test_send_rejects_text_over_cap(self, tmp_path: Path):
        client, server, *_rest, headers, _name = await self._client(tmp_path)
        try:
            resp = await client.post(
                "/api/webchat/drop/send",
                headers=headers,
                json={
                    "text": "x" * 17_000,
                    "device_id": "device-abcdef1",
                },
            )
            assert resp.status == 400
            body = await resp.json()
            assert body["error"] == "text_too_long"
            assert body["max_length"] == 16_000
        finally:
            await self._close(client, server)

    async def test_upload_send_serve_attachment_disposition(
        self, tmp_path: Path
    ):
        client, server, storage, _bus, _guard, _fs, headers, name = (
            await self._client(tmp_path)
        )
        try:
            payload = b"hello drop file"
            resp = await client.post(
                "/api/webchat/drop/upload",
                headers=headers,
                data=_upload_form(payload, "notes.txt"),
            )
            assert resp.status == 200, await resp.text()
            up = await resp.json()
            assert up["filename"] == "notes.txt"
            assert up["mime"] == "text/plain"
            assert up["size"] == len(payload)

            resp = await client.post(
                "/api/webchat/drop/send",
                headers=headers,
                json={
                    "attachments": [{"file_id": up["file_id"]}],
                    "device_id": "device-abcdef1",
                    "device_name": "PC",
                },
            )
            assert resp.status == 200
            msgs = (await resp.json())["messages"]
            assert len(msgs) == 1
            assert msgs[0]["kind"] == "file"
            assert msgs[0]["filename"] == "notes.txt"

            # Serve: text/plain is NOT an image → forced attachment with
            # the original filename (the load-bearing stored-content
            # defense).
            resp = await client.get(
                f"/api/webchat/drop/files/{up['file_id']}",
                headers=headers,
            )
            assert resp.status == 200
            assert await resp.read() == payload
            assert resp.headers["Content-Type"].startswith("text/plain")
            disp = resp.headers["Content-Disposition"]
            assert disp.startswith("attachment")
            assert "notes.txt" in disp

            # A chat-session file must be rejected by the ownership
            # check (Drop files live in the reserved "drop" namespace).
            await storage.insert_file(
                file_id="d" * 16, token_name=name, session_id="s1",
                mime="image/png", size_bytes=4,
                storage_key=f"{name}/chat.png", now=100,
            )
            resp = await client.post(
                "/api/webchat/drop/send",
                headers=headers,
                json={
                    "attachments": [{"file_id": "d" * 16}],
                    "device_id": "device-abcdef1",
                },
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "invalid_attachment"
        finally:
            await self._close(client, server)

    async def test_blocked_extension_and_mime_are_415(self, tmp_path: Path):
        client, server, *_rest, headers, _name = await self._client(tmp_path)
        try:
            resp = await client.post(
                "/api/webchat/drop/upload",
                headers=headers,
                data=_upload_form(b"<svg/>", "evil.svg", "image/svg+xml"),
            )
            assert resp.status == 415
            assert (await resp.json())["error"] == "unsupported_type"

            resp = await client.post(
                "/api/webchat/drop/upload",
                headers=headers,
                data=_upload_form(b"<html></html>", "page.txt", "text/html"),
            )
            assert resp.status == 415
            assert (await resp.json())["error"] == "unsupported_type"
        finally:
            await self._close(client, server)

    async def test_image_upload_sniffs_real_mime_and_serves_inline(
        self, tmp_path: Path
    ):
        import io

        from PIL import Image

        client, server, *_rest, headers, _name = await self._client(tmp_path)
        try:
            buf = io.BytesIO()
            Image.new("RGB", (4, 4), color=(200, 10, 10)).save(
                buf, format="PNG"
            )
            png_bytes = buf.getvalue()
            resp = await client.post(
                "/api/webchat/drop/upload",
                headers=headers,
                data=_upload_form(png_bytes, "photo.png", "image/png"),
            )
            assert resp.status == 200
            up = await resp.json()
            assert up["mime"] == "image/png"
            resp = await client.get(
                f"/api/webchat/drop/files/{up['file_id']}",
                headers=headers,
            )
            assert resp.status == 200
            # Images keep the inline render path (thumbnail grid).
            assert resp.headers["Content-Disposition"].startswith("inline")
        finally:
            await self._close(client, server)

    async def test_pagination_cursor(self, tmp_path: Path):
        client, server, *_rest, headers, _name = await self._client(tmp_path)
        try:
            for i in range(4):
                resp = await client.post(
                    "/api/webchat/drop/send",
                    headers=headers,
                    json={
                        "text": f"m{i}",
                        "device_id": "device-abcdef1",
                    },
                )
                assert resp.status == 200
            resp = await client.get(
                "/api/webchat/drop/messages?limit=2", headers=headers
            )
            page1 = await resp.json()
            assert [m["text"] for m in page1["messages"]] == ["m3", "m2"]
            assert page1["has_more"] is True
            cursor = page1["messages"][-1]["id"]
            resp = await client.get(
                f"/api/webchat/drop/messages?limit=2&before={cursor}",
                headers=headers,
            )
            page2 = await resp.json()
            assert [m["text"] for m in page2["messages"]] == ["m1", "m0"]
            assert page2["has_more"] is False
        finally:
            await self._close(client, server)

    async def test_delete_soft_deletes_broadcasts_and_idempotent_404(
        self, tmp_path: Path
    ):
        client, server, storage, _bus, _guard, _fs, headers, name = (
            await self._client(tmp_path)
        )
        try:
            resp = await client.post(
                "/api/webchat/drop/send",
                headers=headers,
                json={"text": "to delete", "device_id": "device-abcdef1"},
            )
            mid = (await resp.json())["messages"][0]["id"]

            resp = await client.delete(
                f"/api/webchat/drop/messages/{mid}", headers=headers
            )
            assert resp.status == 200
            # Retry sees the uniform 404 (idempotency contract).
            resp = await client.delete(
                f"/api/webchat/drop/messages/{mid}", headers=headers
            )
            assert resp.status == 404

            updates = await storage.get_updates(
                token_name=name, since_pts=0, limit=100
            )
            deleted = [
                u for u in updates if u.event_type == "drop_message_deleted"
            ]
            assert len(deleted) == 1
            assert json.loads(deleted[0].payload)["id"] == mid

            # Live list hides it.
            resp = await client.get(
                "/api/webchat/drop/messages", headers=headers
            )
            assert (await resp.json())["messages"] == []
        finally:
            await self._close(client, server)

    async def test_clear_hard_deletes_and_releases_files(
        self, tmp_path: Path
    ):
        client, server, storage, _bus, _guard, file_store, headers, name = (
            await self._client(tmp_path)
        )
        try:
            payload = b"file bytes"
            resp = await client.post(
                "/api/webchat/drop/upload",
                headers=headers,
                data=_upload_form(payload, "a.txt"),
            )
            up = await resp.json()
            file_id = up["file_id"]
            storage_key = f"{name}/{file_id}.txt"
            resp = await client.post(
                "/api/webchat/drop/send",
                headers=headers,
                json={
                    "attachments": [{"file_id": file_id}],
                    "device_id": "device-abcdef1",
                },
            )
            assert resp.status == 200
            resp = await client.post(
                "/api/webchat/drop/send",
                headers=headers,
                json={"text": "note", "device_id": "device-abcdef1"},
            )
            assert resp.status == 200

            resp = await client.post(
                "/api/webchat/drop/clear", headers=headers
            )
            assert resp.status == 200
            assert (await resp.json())["removed"] == 2

            rows = await storage.list_drop_messages(
                token_name=name, limit=10, before_id=None,
                include_deleted=True,
            )
            assert rows == []
            # File row hard-deleted from webchat_files too.
            assert await storage.get_file(file_id) is None
            # And the storage object is gone.
            assert await file_store.read(storage_key=storage_key) is None
            # Peers got the cleared event.
            updates = await storage.get_updates(
                token_name=name, since_pts=0, limit=100
            )
            assert any(
                u.event_type == "drop_history_cleared" for u in updates
            )
        finally:
            await self._close(client, server)

    async def test_disabled_feature_rejects_writes_but_serves_files(
        self, tmp_path: Path
    ):
        """`drop.enabled=False` → write endpoints 403 with drop_disabled
        (frontend hides the session on /site) while the serve route
        stays live — old links/downloads keep working, mirroring how
        /files/{id} outlives uploads.enabled=False."""
        from aiohttp import web

        from astrbot_plugin_webchat_gateway.handlers.drop import (
            make_drop_handlers,
            make_drop_serve_handler,
        )

        (
            storage, audit, bus, guard, file_store, deps, headers, name
        ) = await _make_harness(tmp_path, enabled=False)
        deps.enabled = False

        app = web.Application(client_max_size=10 * 1024 * 1024)
        handlers = make_drop_handlers(deps)
        app.router.add_post("/api/webchat/drop/send", handlers["send"])
        app.router.add_post("/api/webchat/drop/upload", handlers["upload"])
        app.router.add_get(
            "/api/webchat/drop/files/{file_id}",
            make_drop_serve_handler(deps),
        )
        server = TestServer(app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        try:
            resp = await client.post(
                "/api/webchat/drop/send",
                headers=headers,
                json={"text": "nope", "device_id": "device-abcdef1"},
            )
            assert resp.status == 403
            assert (await resp.json())["error"] == "drop_disabled"

            # Serve stays live while disabled (empty history → 404 from
            # the ownership check, NOT 403 — same uniform-not-found
            # posture as the enabled case).
            resp = await client.get(
                "/api/webchat/drop/files/" + "a" * 16, headers=headers
            )
            assert resp.status == 404
        finally:
            await client.close()
            await server.close()

    async def test_unauthenticated_send_401_and_records_ip_failure(
        self, tmp_path: Path
    ):
        client, server, _storage, _bus, guard, _fs, _headers, _name = (
            await self._client(tmp_path)
        )
        try:
            resp = await client.post(
                "/api/webchat/drop/send",
                json={"text": "anon", "device_id": "device-abcdef1"},
            )
            assert resp.status == 401
            # No credential at all → IP brute-force accounting fires
            # (the same posture as /chat).
            assert guard.failures == 1
        finally:
            await self._close(client, server)


@pytest.mark.asyncio
class TestReservedSessionGuards:
    """The literal "drop" session id is reserved for the Drop feature.
    A conversations PATCH/clear under it would either lazy-create a
    meta row the Drop panel never reads or run the CM-based clear that
    can't see Drop files. These tests pin the rejections."""

    async def _conv_client(self, tmp_path: Path):
        from aiohttp import web

        from astrbot_plugin_webchat_gateway.core.audit import AuditLogger
        from astrbot_plugin_webchat_gateway.core.auth import (
            generate_token,
            hash_token,
        )
        from astrbot_plugin_webchat_gateway.core.event_bus import EventBus
        from astrbot_plugin_webchat_gateway.core.file_store import (
            LocalFileStore,
        )
        from astrbot_plugin_webchat_gateway.core.ratelimit import (
            PerTokenConcurrency,
        )
        from astrbot_plugin_webchat_gateway.handlers.conversations import (
            ConversationDeps,
            make_conversation_handlers,
        )
        from astrbot_plugin_webchat_gateway.handlers.conversations_service import (
            ConversationService,
        )
        from astrbot_plugin_webchat_gateway.storage.sqlite_backend import (
            SqliteStorage,
        )

        storage = SqliteStorage(str(tmp_path / "guard.db"))
        await storage.initialize()
        audit = AuditLogger(storage)
        bus = EventBus()
        file_store = LocalFileStore(root=str(tmp_path / "uploads"))

        class _StubCM:
            async def get_curr_conversation_id(self, umo: str):
                return None

            async def new_conversation(self, umo, **kwargs):
                return "cid"

            async def add_message_pair(self, **kwargs):
                pass

            async def get_human_readable_context(self, **kwargs):
                return [], 0

            async def update_conversation(self, **kwargs):
                pass

            async def get_conversation(self, **kwargs):
                return None

        service = ConversationService(
            storage=storage,
            audit=audit,
            event_bus=bus,
            cm=_StubCM(),
            file_store=file_store,
            concurrency=PerTokenConcurrency(),
            llm_bridge=None,
        )
        deps = ConversationDeps(
            storage=storage,
            audit=audit,
            event_bus=bus,
            cm=_StubCM(),
            file_store=file_store,
            allowed_origins={"*"},
            trust_forwarded_for=False,
            allow_missing_origin=True,
            # gate_request dereferences deps.ip_guard.is_blocked —
            # the conv layer accepts None (field has default factory)
            # but a missing ip_guard here would 500 every request. The
            # real wiring in main.py always passes a real IpGuard;
            # tests must mirror that.
            ip_guard=_StubIpGuard(),
            concurrency=PerTokenConcurrency(),
        )
        handlers = make_conversation_handlers(deps, service)

        plaintext = generate_token()
        await storage.create_token(
            name="alice", token_hash=hash_token(plaintext),
            daily_quota=10, note="", now=1,
        )
        headers = {"Authorization": f"Bearer {plaintext}"}

        app = web.Application()
        app.router.add_get("/api/webchat/conversations", handlers["list"])
        app.router.add_get(
            "/api/webchat/conversations/{session_id}", handlers["get"]
        )
        app.router.add_patch(
            "/api/webchat/conversations/{session_id}", handlers["patch"]
        )
        app.router.add_post(
            "/api/webchat/conversations/{session_id}/clear",
            handlers["clear"],
        )
        server = TestServer(app)
        await server.start_server()
        client = TestClient(server)
        await client.start_server()
        return client, server, storage, headers

    async def test_patch_drop_session_rejected(self, tmp_path: Path):
        client, server, storage, headers = await self._conv_client(tmp_path)
        try:
            resp = await client.patch(
                "/api/webchat/conversations/drop",
                headers=headers,
                json={"title": "mine"},
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "reserved_session"
            # No lazy meta row was created.
            assert (
                await storage.get_session_meta(
                    token_name="alice", session_id="drop"
                )
                is None
            )
        finally:
            await client.close()
            await server.close()

    async def test_clear_drop_session_rejected(self, tmp_path: Path):
        client, server, storage, headers = await self._conv_client(tmp_path)
        try:
            resp = await client.post(
                "/api/webchat/conversations/drop/clear", headers=headers
            )
            assert resp.status == 400
            assert (await resp.json())["error"] == "reserved_session"
        finally:
            await client.close()
            await server.close()

    async def test_get_drop_session_is_404(self, tmp_path: Path):
        client, server, storage, headers = await self._conv_client(tmp_path)
        try:
            resp = await client.get(
                "/api/webchat/conversations/drop", headers=headers
            )
            # No meta + no CM history → the normal not_found path.
            assert resp.status == 404
        finally:
            await client.close()
            await server.close()
