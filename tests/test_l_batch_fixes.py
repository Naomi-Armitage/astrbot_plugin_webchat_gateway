"""Regression tests for the Low-severity audit batch (L-fixes).

Each test pins the post-fix invariant of one L-finding:

  * L1 — `storage.write_audit` truncates `detail` at AUDIT_DETAIL_MAX
    even when the caller bypasses AuditLogger. (Belt-and-braces:
    AuditLogger already caps at the same number, but the storage
    layer is the safety net against future callers that forget.)
  * L3 — `R2FileStore.read` distinguishes NoSuchKey (returns None →
    404 from the handler) from transport / auth errors (raises
    `FileStoreUnavailable` → 503 from the handler).
  * L8 — `Image.MAX_IMAGE_PIXELS` is no longer set at module import
    time. The override is scoped to the `_scoped_max_pixels` context
    manager inside `detect_image_mime`, so a co-resident plugin's
    PIL configuration is restored after our call.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest


# ---------------------------------------------------------------------
# L1: write_audit caps detail at AUDIT_DETAIL_MAX
# ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("tmp_data_dir")
class TestAuditDetailCap:
    async def test_write_audit_truncates_oversize_detail(self, tmp_path: Path):
        from astrbot_plugin_webchat_gateway.storage.base import (
            AUDIT_DETAIL_MAX,
        )
        from astrbot_plugin_webchat_gateway.storage.sqlite_backend import (
            SqliteStorage,
        )

        s = SqliteStorage(str(tmp_path / "cap.db"))
        await s.initialize()
        try:
            # Caller bypasses AuditLogger (which would also truncate)
            # and sends 4× the cap directly. Storage layer must still
            # store at most AUDIT_DETAIL_MAX bytes.
            payload = "x" * (AUDIT_DETAIL_MAX * 4)
            await s.write_audit(
                ts=1_000_000,
                name="alice",
                ip="1.1.1.1",
                event="probe",
                detail=payload,
            )
            rows, total = await s.list_audit(limit=1, offset=0)
            assert total == 1
            assert len(rows[0].detail) == AUDIT_DETAIL_MAX
        finally:
            await s.close()

    async def test_write_audit_passes_small_detail_through(self, tmp_path: Path):
        from astrbot_plugin_webchat_gateway.storage.sqlite_backend import (
            SqliteStorage,
        )

        s = SqliteStorage(str(tmp_path / "cap.db"))
        await s.initialize()
        try:
            await s.write_audit(
                ts=1_000_000,
                name="alice",
                ip="1.1.1.1",
                event="probe",
                detail="short",
            )
            rows, _ = await s.list_audit(limit=1, offset=0)
            assert rows[0].detail == "short"
        finally:
            await s.close()


# ---------------------------------------------------------------------
# L3: FileStoreUnavailable contract
# ---------------------------------------------------------------------


class TestFileStoreUnavailableExport:
    """The exception type must be importable from the public surface so
    handler code can `except FileStoreUnavailable:` without reaching
    into private modules."""

    def test_exported(self):
        from astrbot_plugin_webchat_gateway.core.file_store import (
            FileStoreUnavailable,
        )

        # Must be a class derived from a runtime exception type so
        # `except Exception:` still catches it, but the more specific
        # `except FileStoreUnavailable:` can distinguish.
        assert isinstance(FileStoreUnavailable("x"), Exception)
        assert issubclass(FileStoreUnavailable, RuntimeError)


# ---------------------------------------------------------------------
# L8: PIL MAX_IMAGE_PIXELS scoping
# ---------------------------------------------------------------------


class TestPilMaxPixelsScoping:
    def test_module_import_does_not_set_global(self):
        """Pre-fix, importing core.image_util set Image.MAX_IMAGE_PIXELS
        as a side effect at module load. Restart the test with a known-
        sentinel value to prove the import doesn't clobber it."""
        from PIL import Image

        # Pin a sentinel BEFORE importing the module fresh. If the
        # module-level assignment is still present, this would get
        # overwritten by 50_000_000.
        Image.MAX_IMAGE_PIXELS = 12345

        # Force a fresh import path (sys.modules may have cached it).
        # If the module is already imported, this is a no-op — but the
        # sentinel below would already have been overwritten on the
        # first import, which is the bug we're guarding against.
        import importlib
        import astrbot_plugin_webchat_gateway.core.image_util as _iu

        importlib.reload(_iu)
        assert Image.MAX_IMAGE_PIXELS == 12345, (
            "core.image_util must not set Image.MAX_IMAGE_PIXELS at "
            "module load time — it pollutes every other PIL user in "
            "the process. The override now lives inside a context "
            "manager scoped to each detect_image_mime call."
        )

    def test_detect_image_mime_restores_max_pixels(self):
        """Call detect_image_mime with a known-sentinel value pinned;
        the value must be restored on exit (success path)."""
        from PIL import Image
        from astrbot_plugin_webchat_gateway.core.image_util import (
            detect_image_mime,
            PIL_MAX_PIXELS,
        )

        # Generate a tiny valid PNG so the function reaches the
        # "set MAX_IMAGE_PIXELS → verify" path on the success branch.
        img = Image.new("RGB", (2, 2), (255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        sentinel = 7777
        Image.MAX_IMAGE_PIXELS = sentinel
        mime = detect_image_mime(png_bytes)
        assert mime == "image/png"
        assert Image.MAX_IMAGE_PIXELS == sentinel, (
            "detect_image_mime success path must restore the prior "
            "Image.MAX_IMAGE_PIXELS — co-resident plugins keep their "
            "own configuration"
        )

    def test_detect_image_mime_restores_max_pixels_on_failure(self):
        """Same restore contract on the failure branch (invalid bytes)."""
        from PIL import Image
        from astrbot_plugin_webchat_gateway.core.image_util import (
            detect_image_mime,
        )

        sentinel = 8888
        Image.MAX_IMAGE_PIXELS = sentinel
        assert detect_image_mime(b"not a real image") is None
        assert Image.MAX_IMAGE_PIXELS == sentinel
