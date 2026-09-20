"""Preview pixels, memory bounds, and authenticated HTTP delivery."""

import asyncio
import json
from io import BytesIO
from random import Random
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import make_mocked_request
from PIL import Image

from astrbot_plugin_webchat_gateway.core.image_preview import (
    ImagePreviewCache,
    make_image_preview,
)
from test_drop_handlers import _make_harness


def image_bytes(mode="RGB", size=(1600, 800), format="PNG", **kwargs):
    with Image.new(mode, size) as source:
        output = BytesIO()
        source.save(output, format=format, **kwargs)
        return output.getvalue()


def test_preview_preserves_orientation_and_does_not_upscale():
    exif = Image.Exif()
    exif[274] = 6  # A phone photo stored sideways.
    content = image_bytes(format="JPEG", exif=exif)
    with Image.open(BytesIO(make_image_preview(content))) as preview:
        assert preview.size == (320, 640)
        assert not preview.getexif()
    with Image.open(BytesIO(make_image_preview(image_bytes(size=(32, 16))))) as preview:
        assert preview.size == (32, 16)


@pytest.mark.parametrize("format", ["PNG", "WEBP", "GIF"])
def test_transparent_images_keep_alpha(format):
    content = image_bytes(mode="RGBA", size=(20, 10), format=format)
    with Image.open(BytesIO(make_image_preview(content))) as preview:
        assert preview.getpixel((0, 0))[3] == 0


def test_pixel_limit_checked_before_decoding(monkeypatch):
    from astrbot_plugin_webchat_gateway.core import image_preview

    monkeypatch.setattr(image_preview, "PIL_MAX_PIXELS", 100)
    with pytest.raises(ValueError, match="分辨率超出预览上限"):
        make_image_preview(image_bytes(size=(20, 20)))


@pytest.mark.asyncio
@pytest.mark.parametrize("photo_kind", ["mpo", "high_resolution"])
async def test_phone_jpeg_previews_from_legacy_drop_files(tmp_path, photo_kind):
    from astrbot_plugin_webchat_gateway.core.image_util import detect_image_mime
    from astrbot_plugin_webchat_gateway.handlers.drop import make_drop_serve_handler

    output = BytesIO()
    if photo_kind == "mpo":
        # Phone HDR/depth JPEGs can contain an MPF marker and a second frame.
        # Pillow calls this MPO even though the filename is still .jpeg.
        with Image.new("RGB", (5712, 4248), "blue") as photo, Image.new("RGB", (400, 300), "red") as extra:
            photo.save(output, format="MPO", save_all=True, append_images=[extra])
        content = output.getvalue()
        with Image.open(BytesIO(content)) as image:
            assert image.format == "MPO"
        assert detect_image_mime(content) == "image/jpeg"
    else:
        # This photo is under 1 MB on disk, but just over 50 million pixels.
        with Image.new("RGB", (8192, 6144), "blue") as photo:
            photo.save(output, format="JPEG", quality=90)
        content = output.getvalue()

    storage, _audit, _bus, _guard, store, deps, headers, name = await _make_harness(tmp_path)
    file_id = "c" * 16
    await store.save(storage_key="phone.bin", content=content, mime="application/octet-stream")
    await storage.insert_file(
        file_id=file_id, token_name=name, session_id="drop", mime="application/octet-stream",
        size_bytes=len(content), storage_key="phone.bin", now=1, filename="IMG_5918.jpeg",
    )
    handler = make_drop_serve_handler(deps)
    try:
        response = await handler(make_mocked_request(
            "GET", f"/api/webchat/drop/files/{file_id}?preview=1", headers=headers,
            match_info={"file_id": file_id},
        ))
        assert response.status == 200, response.text
        with Image.open(BytesIO(response.body)) as preview:
            assert preview.size == ((640, 476) if photo_kind == "mpo" else (640, 480))
            red, _, blue = preview.getpixel((320, 240))[:3]
            assert blue > 200 and red < 30  # Main photo, not the auxiliary frame.
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_cache_reuses_previews_and_evicts_by_bytes():
    content = image_bytes(size=(64, 32))
    store = AsyncMock()
    store.read.return_value = content
    cache = ImagePreviewCache(max_bytes=len(make_image_preview(content)) * 2)
    for key in ("one", "two", "one", "three", "one", "two"):
        assert await cache.read(store, storage_key=key)
    assert [call.kwargs["storage_key"] for call in store.read.call_args_list] == [
        "one", "two", "three", "two",
    ]


@pytest.mark.asyncio
async def test_missing_sources_are_not_cached():
    store = AsyncMock()
    store.read.side_effect = [None, image_bytes(size=(32, 16))]
    cache = ImagePreviewCache()
    assert await cache.read(store, storage_key="missing") is None
    assert await cache.read(store, storage_key="missing")


@pytest.mark.asyncio
async def test_concurrent_source_reads_are_bounded():
    pending = active = peak = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def read(**kwargs):
        nonlocal pending, active, peak
        active += 1
        pending += 1
        peak = max(peak, active)
        if pending == 2:
            entered.set()
        await release.wait()
        active -= 1
        return None

    store = AsyncMock()
    store.read.side_effect = read
    cache = ImagePreviewCache()
    tasks = [asyncio.create_task(cache.read(store, storage_key=str(i))) for i in range(8)]
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert pending == 2
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert peak == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["drop", "drop_legacy", "chat", "r2_direct"])
async def test_large_jpeg_preview_and_original_delivery(tmp_path, route):
    from astrbot_plugin_webchat_gateway.core.auth import generate_token, hash_token
    from astrbot_plugin_webchat_gateway.handlers.drop import make_drop_serve_handler
    from astrbot_plugin_webchat_gateway.handlers.files import UploadDeps, make_serve_handler

    storage, audit, _bus, guard, store, deps, headers, name = await _make_harness(tmp_path)
    # Real, noisy JPEG over 5 MB, matching the original Drop failure.
    with Image.frombytes("RGB", (2600, 1800), Random(0).randbytes(2600 * 1800 * 3)) as source:
        output = BytesIO()
        source.save(output, format="JPEG", quality=98)
        original = output.getvalue()
    assert len(original) > 5 * 1024 * 1024
    is_drop = route.startswith("drop")
    file_id = "a" * 16
    key = f"{name}/{file_id}.jpg"
    await store.save(storage_key=key, content=original, mime="image/jpeg")
    await storage.insert_file(
        file_id=file_id, token_name=name, session_id="drop" if is_drop else "chat",
        mime="application/octet-stream" if route == "drop_legacy" else "image/jpeg",
        size_bytes=len(original), storage_key=key,
        now=1, filename="photo.jpeg",
    )
    store.signed_url = AsyncMock(return_value="https://example.test/original.jpg")
    if is_drop:
        handler = make_drop_serve_handler(deps)
        url = f"/api/webchat/drop/files/{file_id}"
    else:
        handler = make_serve_handler(UploadDeps(
            storage=storage, audit=audit, ip_guard=guard, file_store=store,
            upload_gate=deps.upload_gate, allowed_origins={"*"}, max_file_size_mb=20,
            per_token_storage_mb=100, allowed_mime=("image/jpeg",),
            storage_driver="r2" if route == "r2_direct" else "local",
            r2_serving_mode="direct", r2_direct_link_ttl_seconds=300,
            files_serve_prefix="/api/webchat/files/", trust_forwarded_for=False,
            allow_missing_origin=True,
        ))
        url = f"/api/webchat/files/{file_id}"
    async def get(query="", auth=headers):
        return await handler(make_mocked_request(
            "GET", url + query, headers=auth, match_info={"file_id": file_id},
        ))

    try:
        response = await get("?preview=1")
        assert response.status == 200
        assert response.headers["Content-Type"] == "image/webp"
        assert response.headers["Content-Disposition"].startswith("inline")
        assert response.headers["Cache-Control"].startswith("private")
        preview = response.body
        assert len(preview) < len(original) // 10
        with Image.open(BytesIO(preview)) as image:
            image.load()
            assert max(image.size) == 640
        store.signed_url.assert_not_called()

        # Cached bytes must still require authorization on every request.
        assert (await get("?preview=1", auth={})).status == 401
        other = generate_token()
        await storage.create_token(name="bob", token_hash=hash_token(other), daily_quota=10, note="", now=1)
        assert (await get("?preview=1", auth={"Authorization": f"Bearer {other}"})).status == 404

        response = await get()
        if route == "r2_direct":
            assert response.status == 302
            store.signed_url.assert_awaited_once()
        else:
            assert response.headers["Content-Type"] == "image/jpeg"
            assert response.body == original
        if is_drop:
            response = await get("?preview=1&download=1")
            assert response.headers["Content-Disposition"].startswith("attachment")
            assert "photo.jpeg" in response.headers["Content-Disposition"]
            assert response.body == original
    finally:
        await storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", ["", "application/octet-stream", "image/jpg", "text/plain"])
@pytest.mark.parametrize("format,mime", [
    ("JPEG", "image/jpeg"), ("PNG", "image/png"),
    ("GIF", "image/gif"), ("WEBP", "image/webp"),
])
async def test_drop_upload_recognizes_image_bytes_without_browser_mime(declared, format, mime):
    from astrbot_plugin_webchat_gateway.handlers.drop import _resolve_drop_mime

    assert await _resolve_drop_mime(image_bytes(format=format), declared) == mime


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [b"<html>not an image</html>", b"\xff\xd8\xffbroken jpeg"])
async def test_non_images_with_jpeg_filenames_stay_downloadable(tmp_path, content):
    from astrbot_plugin_webchat_gateway.handlers.drop import (
        _resolve_drop_mime, make_drop_serve_handler,
    )

    assert await _resolve_drop_mime(content, "application/octet-stream") == "application/octet-stream"
    storage, _audit, _bus, _guard, store, deps, headers, name = await _make_harness(tmp_path)
    file_id = "b" * 16
    await store.save(storage_key="fake.jpg", content=content, mime="application/octet-stream")
    await storage.insert_file(
        file_id=file_id, token_name=name, session_id="drop", mime="application/octet-stream",
        size_bytes=len(content), storage_key="fake.jpg", now=1, filename="fake.jpeg",
    )
    handler = make_drop_serve_handler(deps)

    async def get(query):
        return await handler(make_mocked_request(
            "GET", f"/api/webchat/drop/files/{file_id}{query}",
            headers=headers, match_info={"file_id": file_id},
        ))

    try:
        response = await get("?preview=1")
        assert response.status == 415
        error = json.loads(response.text)
        assert error["reason"] == "decode_failed"
        assert error["detail"] == "图片数据不完整或无法解码"
        for query in ("", "?download=1", "?preview=1&download=1"):
            response = await get(query)
            assert response.status == 200
            assert response.headers["Content-Disposition"].startswith("attachment")
            assert response.body == content
    finally:
        await storage.close()
