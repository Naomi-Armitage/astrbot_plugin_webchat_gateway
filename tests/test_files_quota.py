"""Regression tests for the image upload storage quota.

The quota is shared with Drop and must include rows that have not yet been
attached to a message. These tests drive the production handler through
aiohttp with a real SQLite backend so the committed=0 state is exercised.
"""

from __future__ import annotations

import io
from pathlib import Path

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer

import pytest


class _StubIpGuard:
    async def is_blocked(self, ip: str, *, now: int = 0) -> tuple[bool, int]:
        return False, 0

    async def record_failure(self, ip: str) -> None:
        pass

    async def reset(self, ip: str) -> None:
        pass


def _png_bytes() -> bytes:
    # Minimal valid 2x2 PNG generated with Pillow in the test body to keep
    # import-time plugin collection independent of optional image deps.
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (2, 2), color=(20, 80, 160)).save(buf, format="PNG")
    return buf.getvalue()


def _upload_form(content: bytes) -> FormData:
    form = FormData()
    form.add_field("file", content, filename="pixel.png", content_type="image/png")
    form.add_field("session_id", "session-1")
    return form


async def _make_client(tmp_path: Path, *, quota_mb: int = 1):
    from astrbot_plugin_webchat_gateway.core.audit import AuditLogger
    from astrbot_plugin_webchat_gateway.core.auth import generate_token, hash_token
    from astrbot_plugin_webchat_gateway.core.file_store import LocalFileStore
    from astrbot_plugin_webchat_gateway.core.ratelimit import PerTokenUploadGate
    from astrbot_plugin_webchat_gateway.handlers.files import (
        UploadDeps,
        make_upload_handler,
    )
    from astrbot_plugin_webchat_gateway.storage.sqlite_backend import SqliteStorage

    storage = SqliteStorage(str(tmp_path / "files-quota.db"))
    await storage.initialize()
    token_value = generate_token()
    token_name = "alice"
    await storage.create_token(
        name=token_name,
        token_hash=hash_token(token_value),
        daily_quota=10,
        note="",
        now=1,
    )
    deps = UploadDeps(
        storage=storage,
        audit=AuditLogger(storage),
        ip_guard=_StubIpGuard(),  # type: ignore[arg-type]
        file_store=LocalFileStore(root=str(tmp_path / "uploads")),
        upload_gate=PerTokenUploadGate(),
        allowed_origins={"*"},
        max_file_size_mb=5,
        per_token_storage_mb=quota_mb,
        allowed_mime=("image/png",),
        storage_driver="local",
        r2_serving_mode="proxy",
        r2_direct_link_ttl_seconds=300,
        files_serve_prefix="/api/webchat/files/",
        trust_forwarded_for=False,
        allow_missing_origin=True,
    )
    app = web.Application(client_max_size=10 * 1024 * 1024)
    app.router.add_post("/api/webchat/upload", make_upload_handler(deps))
    server = TestServer(app)
    await server.start_server()
    client = TestClient(server)
    await client.start_server()
    return client, server, storage, token_name, {
        "Authorization": f"Bearer {token_value}"
    }


@pytest.mark.asyncio
async def test_upload_quota_counts_uncommitted_files(tmp_path: Path):
    client, server, storage, token_name, headers = await _make_client(tmp_path)
    try:
        quota = 1 * 1024 * 1024
        await storage.insert_file(
            file_id="u" * 16,
            token_name=token_name,
            session_id="session-abandoned",
            mime="image/png",
            size_bytes=quota,
            storage_key=f"{token_name}/u.png",
            now=100,
        )

        response = await client.post(
            "/api/webchat/upload",
            headers=headers,
            data=_upload_form(_png_bytes()),
        )
        assert response.status == 429
        assert (await response.json())["error"] == "storage_quota_exceeded"
        assert await storage.total_size_for_token(token_name) == quota
    finally:
        await client.close()
        await server.close()
        await storage.close()


@pytest.mark.asyncio
async def test_upload_quota_query_failure_returns_503(tmp_path: Path):
    client, server, storage, token_name, headers = await _make_client(tmp_path)

    async def fail_total(_token_name: str) -> int:
        raise RuntimeError("database unavailable")

    storage.total_size_for_token = fail_total  # type: ignore[method-assign]
    try:
        response = await client.post(
            "/api/webchat/upload",
            headers=headers,
            data=_upload_form(_png_bytes()),
        )
        assert response.status == 503
        assert (await response.json())["error"] == "storage_unavailable"
        assert response.headers["Retry-After"] == "5"
        assert await storage.list_files_for_session(
            token_name=token_name, session_id="session-1"
        ) == []
    finally:
        await client.close()
        await server.close()
        await storage.close()
