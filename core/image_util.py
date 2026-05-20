"""Image validation utilities for WebChat uploads.

Server-side defense in depth for the upload pipeline. The handler can
NOT trust the client-supplied `Content-Type` of a multipart part — a
malicious uploader trivially lies about MIME to bypass a string check.
Instead we run two passes of Pillow over the raw bytes:

1. `Image.verify()` — walks the file looking for structural corruption
   without fully decoding pixels. Catches truncated files, header
   forgery, and (because we set `Image.MAX_IMAGE_PIXELS` first)
   decompression bombs that would explode in RAM on a full decode.
2. `Image.open(...).format` re-read — `verify()` consumes the stream
   pointer and forbids further reads on the same handle, so we rebuild
   a fresh `BytesIO` and inspect `.format` to learn the canonical
   container (JPEG/PNG/WEBP/GIF). The string returned here, not the
   client's Content-Type, is what we persist and serve back later.

`detect_image_mime` swallows ALL exceptions — Pillow's error taxonomy is
varied (`UnidentifiedImageError`, `DecompressionBombError`, generic
`OSError` for truncation, `SyntaxError` for some malformed PNGs, etc.)
and the handler only cares about the binary pass/fail signal. Returning
`None` rather than raising keeps the upload path linear: one branch on
`None` covers every failure mode.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from io import BytesIO

from PIL import Image


# MIME → file extension for the four formats we accept. The whitelist is
# tight on purpose: SVG (XSS via embedded JS), AVIF/HEIC (decoder gaps
# across platforms) and TIFF/BMP (rarely uploaded, decoder surface area)
# are all out. The extension is only used for on-disk filenames so the
# files render with a sensible suffix when an operator browses the
# upload root — the authoritative MIME lives in the DB row.
ALLOWED_MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png":  ".png",
    "image/webp": ".webp",
    "image/gif":  ".gif",
}


# Pillow's `MAX_IMAGE_PIXELS` is a class-level guard: any decode whose
# pixel count exceeds this number raises `DecompressionBombError` before
# allocating the pixel buffer. 50M pixels is comfortably above any
# legitimate chat upload (8192×8192 RGBA = ~67M, so we cap below that
# but well over the 2048-long-edge resize the frontend performs) while
# still refusing the classic "100 KB PNG that decodes to 1 GB" payload.
PIL_MAX_PIXELS = 50_000_000


@contextmanager
def _scoped_max_pixels(target: int):
    """Temporarily set `Image.MAX_IMAGE_PIXELS` for one validation call.

    The previous module-level assignment polluted every other AstrBot
    plugin loaded in the same process: anyone using PIL.Image inherited
    OUR threshold whether they wanted it or not. Scoping the override
    to the actual call window restores the prior value on exit so
    other plugins keep whatever policy they configured.

    Not thread-safe in the strict sense — concurrent calls from
    `detect_image_mime_async` (running on the default thread pool)
    will all read/write the same class attribute. In practice every
    one of our concurrent calls sets the SAME value, so the race is
    benign for us; a co-resident plugin running PIL concurrently
    would see our value during our window. Acceptable trade-off
    vs. permanent global pollution.
    """
    saved = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = target
    try:
        yield
    finally:
        Image.MAX_IMAGE_PIXELS = saved


# Pillow's `.format` strings → canonical MIME the spec accepts.
# The inverse of ALLOWED_MIME_TO_EXT, keyed by what `.format` actually
# returns (uppercase letters, no slash) so we don't double-map.
_FORMAT_TO_MIME: dict[str, str] = {
    "JPEG": "image/jpeg",
    "PNG":  "image/png",
    "WEBP": "image/webp",
    "GIF":  "image/gif",
}


def detect_image_mime(content: bytes) -> str | None:
    """Verify `content` is a coherent raster image and return its MIME.

    Returns one of the four values in `ALLOWED_MIME_TO_EXT` on success,
    `None` for any failure (unidentified format, decompression bomb,
    structural corruption, format outside the whitelist).

    Pillow's `verify()` enforces structural integrity AND triggers the
    `MAX_IMAGE_PIXELS` check before any large allocation, so a 100 KB
    PNG that would decode to a 1 GB pixel buffer is refused here at
    constant memory cost.
    """
    if not content:
        return None
    with _scoped_max_pixels(PIL_MAX_PIXELS):
        try:
            # First pass: verify integrity without decoding pixels. After
            # verify() the file handle is consumed; Pillow forbids any
            # further read on the same Image instance.
            probe = Image.open(BytesIO(content))
            probe.verify()
        except Exception:
            return None
        try:
            # Second pass: re-open on a fresh BytesIO to inspect .format
            # (verify() left the previous handle unusable). We don't call
            # .load() — .format is populated lazily from the header and
            # doesn't need a full decode.
            reopened = Image.open(BytesIO(content))
            fmt = (reopened.format or "").upper()
        except Exception:
            return None
    return _FORMAT_TO_MIME.get(fmt)


async def detect_image_mime_async(content: bytes) -> str | None:
    """Off-thread wrapper around `detect_image_mime`.

    PIL's `verify()` + format re-read can spend hundreds of ms (small
    files) to several seconds (a 20 MB JPEG near the size cap) doing
    CPU-bound work. Running it directly on the event loop stalls every
    other in-flight request — SSE heartbeats, long-poll waiters, the
    LLM streaming pump — for that entire duration. `asyncio.to_thread`
    moves the call to the default thread pool so the loop stays
    responsive while the decode runs.

    The sync `detect_image_mime` is kept for tests and any future
    caller that's already on a worker thread.
    """
    return await asyncio.to_thread(detect_image_mime, content)


def ext_for_mime(mime: str) -> str | None:
    """Return the on-disk extension for a validated MIME, or None.

    Convenience wrapper around the `ALLOWED_MIME_TO_EXT` table so call
    sites don't have to import the dict directly. `None` for any MIME
    outside the whitelist — callers should already have validated via
    `detect_image_mime`, but the defensive lookup keeps the contract
    explicit.
    """
    return ALLOWED_MIME_TO_EXT.get(mime)


__all__ = [
    "ALLOWED_MIME_TO_EXT",
    "PIL_MAX_PIXELS",
    "detect_image_mime",
    "detect_image_mime_async",
    "ext_for_mime",
]
