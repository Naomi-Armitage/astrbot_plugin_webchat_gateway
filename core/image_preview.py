"""Small, cached message previews; original attachments stay untouched."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from io import BytesIO

from PIL import Image, ImageOps

from .file_store import FileStore
from .image_util import PIL_MAX_PIXELS


PREVIEW_MAX_EDGE = 640
PREVIEW_MIME = "image/webp"


def make_image_preview(content: bytes) -> bytes:
    """Decode one frame off-thread, respecting orientation and transparency."""
    with Image.open(BytesIO(content)) as source:
        if source.width * source.height > PIL_MAX_PIXELS:
            raise ValueError("Image exceeds preview pixel limit")
        # JPEG can downsample during decoding, saving memory for camera photos.
        source.draft("RGB", (PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE))
        source.thumbnail((PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE), Image.Resampling.LANCZOS)
        ImageOps.exif_transpose(source, in_place=True)
        mode = "RGBA" if "A" in source.getbands() or "transparency" in source.info else "RGB"
        with source.convert(mode) as preview:
            # Do not carry camera metadata into the generated preview.
            preview.info.clear()
            output = BytesIO()
            preview.save(output, format="WEBP", quality=80, method=4)
            return output.getvalue()


class ImagePreviewCache:
    """Per-handler byte-bounded cache, consulted only after file authorization.

    Limit concurrent reads/decodes too: a page of large photos must not put
    every full-size source in memory at once. Nothing is written to storage,
    so previews need no extra quota accounting or deletion lifecycle.
    """

    def __init__(self, max_bytes: int = 16 * 1024 * 1024) -> None:
        self._max_bytes = max_bytes
        self._bytes = 0
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._gate = asyncio.Semaphore(2)

    async def read(self, store: FileStore, *, storage_key: str) -> bytes | None:
        async with self._gate:
            cached = self._cache.get(storage_key)
            if cached is not None:
                self._cache.move_to_end(storage_key)
                return cached
            content = await store.read(storage_key=storage_key)
            if content is None:
                return None
            preview = await asyncio.to_thread(make_image_preview, content)
            if len(preview) <= self._max_bytes:
                # Another request may have finished the same preview meanwhile.
                previous = self._cache.pop(storage_key, None)
                if previous is not None:
                    self._bytes -= len(previous)
                while self._cache and self._bytes + len(preview) > self._max_bytes:
                    _, evicted = self._cache.popitem(last=False)
                    self._bytes -= len(evicted)
                self._cache[storage_key] = preview
                self._bytes += len(preview)
            return preview
