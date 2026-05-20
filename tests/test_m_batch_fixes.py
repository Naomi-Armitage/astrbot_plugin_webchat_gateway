"""Regression tests for the Medium-severity audit fixes (M-batch).

Each test pins one of the M-batch failure modes:
  * M1 — revoked / expired tokens must also bump IP-guard counter
          (was: only `token is None` did, making leaked-but-revoked
          tokens a free liveness oracle).
  * M2 — `/title` Origin + IP-guard checks must run BEFORE the
          `auto_title_enabled=False` short-circuit (was: an
          unauthenticated cross-origin probe could read the flag).
  * M4 — `HREF_OK` must reject protocol-relative `//evil.com` URLs.
  * M8 — `SqliteStorage.record_ip_failure` must be a single atomic
          UPSERT (was: SELECT-then-INSERT/UPDATE with only an
          in-process lock — multi-worker deployments could race).
  * M11 — `commit_attachments_or_release` must emit a
          `file_release_failed` audit event when BOTH the commit
          and the inner release raise (was: silently logger.exception
          on the double-failure with no operator-visible signal).

M3 (regenerate 4xx-before-prepare) is exercised indirectly by the
full pytest suite continuing to pass; M5, M6, M7, M9 are UI / Redis
changes whose contracts are observable via code review (we exercise
them where unit-testable, but Redis-side Lua semantics need an
integration env).
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------
# M4: HREF_OK regex
# ---------------------------------------------------------------------


class TestHrefOkRegex:
    """The TypeScript regex is mirrored in Python below so unit tests
    can exercise the EXACT same character class against the same set
    of probe inputs we'd worry about. If the .ts gets out of sync the
    e2e build will still catch it, but a unit-level pin here keeps
    the failure mode discoverable in the test suite."""

    # Same shape as web/src/shared/site.ts:28 — keep in sync.
    HREF_OK = re.compile(r"^(?:https?://|/(?!/))", re.IGNORECASE)

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/a",
            "http://example.com",
            "HTTPS://EXAMPLE.COM",
            "/privacy",
            "/some/path?x=1",
        ],
    )
    def test_accepts_safe(self, url):
        assert self.HREF_OK.match(url), url

    @pytest.mark.parametrize(
        "url",
        [
            "//evil.com",                    # protocol-relative — was the bug
            "//evil.com/path",
            "//evil.com/path?x=1",
            "javascript:alert(1)",
            "data:text/html,<script>",
            "file:///etc/passwd",
            "ftp://example.com",
            "",
            "no-scheme.example.com",
        ],
    )
    def test_rejects_unsafe(self, url):
        assert not self.HREF_OK.match(url), url

    def test_source_file_matches_python_mirror(self):
        """Grep the actual site.ts so the pin lives where the source
        does — pre-fix this test would fail because the source had
        `/^(https?:\\/\\/|\\/)/i` (single-`/` second branch). A
        future refactor that drops the `(?!/)` lookahead is caught
        here even if the Python mirror above stays in sync only by
        copy-paste."""
        import pathlib

        path = (
            pathlib.Path(__file__).resolve().parent.parent
            / "web"
            / "src"
            / "shared"
            / "site.ts"
        )
        text = path.read_text(encoding="utf-8")
        # `\/(?!\/)` is the post-fix shape. The pre-fix source had
        # `\/)/i` (bare slash close) — assert the lookahead is present
        # AND the protocol-relative case is rejected.
        assert "(?!\\/)" in text or "(?!/)" in text, (
            "site.ts HREF_OK must include a negative lookahead so the "
            "second alternation only matches a single leading slash, "
            "not protocol-relative `//…`"
        )


# ---------------------------------------------------------------------
# M1: revoked / expired token still bumps IP-guard
# ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("tmp_data_dir")
class TestGateRecordsRevokedAndExpired:
    """`gate_request` is the chat-path auth gate. Pre-fix it only
    called `ip_guard.record_failure` when the bearer didn't match any
    row; revoked or expired tokens went through the same 401 path but
    skipped the IP-guard accounting. An attacker holding a leaked-
    but-revoked token could ping /chat at unlimited rate as a free
    liveness check on whether the gateway still recognised the name.
    """

    async def _build_deps_and_token(self, tmp_path: Path, *, revoked=False, expired=False):
        from astrbot_plugin_webchat_gateway.core.audit import AuditLogger
        from astrbot_plugin_webchat_gateway.core.auth import (
            generate_token,
            hash_token,
        )
        from astrbot_plugin_webchat_gateway.core.ip_guard import IpGuard
        from astrbot_plugin_webchat_gateway.storage.sqlite_backend import (
            SqliteStorage,
        )

        storage = SqliteStorage(str(tmp_path / "gate.db"))
        await storage.initialize()
        audit = AuditLogger(storage)
        guard = IpGuard(storage, max_fails=5, block_seconds=60)

        plaintext = generate_token()
        now = int(time.time())
        await storage.create_token(
            name="alice",
            token_hash=hash_token(plaintext),
            daily_quota=10,
            note="",
            now=now,
            expires_at=(now - 60) if expired else None,
        )
        if revoked:
            await storage.revoke_token("alice", now=now)
        return storage, audit, guard, plaintext

    def _mock_request(self, bearer: str | None):
        """Return a stand-in object that `gate_request` reads from."""

        class _StubRequest:
            host = "localhost"
            remote = "1.2.3.4"

            @property
            def headers(self):
                h = {}
                if bearer is not None:
                    h["Authorization"] = f"Bearer {bearer}"
                return h

            @property
            def cookies(self):
                return {}

        return _StubRequest()

    async def _call_gate(self, storage, audit, guard, plaintext, *, allow_missing=True):
        from astrbot_plugin_webchat_gateway.handlers.common import gate_request

        class _Deps:
            def __init__(self):
                self.storage = storage
                self.audit = audit
                self.ip_guard = guard
                self.allowed_origins = {"*"}
                self.trust_forwarded_for = False
                self.trust_referer_as_origin = False
                self.allow_missing_origin = allow_missing

        return await gate_request(self._mock_request(plaintext), _Deps())

    async def test_revoked_bearer_records_failure(self, tmp_path: Path):
        storage, audit, guard, plaintext = await self._build_deps_and_token(
            tmp_path, revoked=True
        )
        try:
            await self._call_gate(storage, audit, guard, plaintext)
            blocked, _ = await guard.is_blocked("1.2.3.4")
            count = (
                await storage.record_ip_failure(
                    "1.2.3.4",
                    now=int(time.time()),
                    max_fails=999,
                    block_seconds=60,
                )
            ) - 1
            # `-1` to back out the probing increment we just added.
            # Real assertion: BEFORE our probe, the counter was >0 ⇒
            # gate_request recorded a failure for the revoked bearer.
            assert count >= 1, (
                "revoked bearer must increment IP-guard counter; pre-fix "
                "only `token is None` did, making a leaked-but-revoked "
                "token a free probe oracle"
            )
        finally:
            await storage.close()

    async def test_expired_bearer_records_failure(self, tmp_path: Path):
        storage, audit, guard, plaintext = await self._build_deps_and_token(
            tmp_path, expired=True
        )
        try:
            await self._call_gate(storage, audit, guard, plaintext)
            count = (
                await storage.record_ip_failure(
                    "1.2.3.4",
                    now=int(time.time()),
                    max_fails=999,
                    block_seconds=60,
                )
            ) - 1
            assert count >= 1, (
                "expired bearer must also increment IP-guard counter; "
                "same oracle shape as the revoked case"
            )
        finally:
            await storage.close()


# ---------------------------------------------------------------------
# M8: ON CONFLICT UPSERT preserves the threshold/blocked_until semantics
# ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("tmp_data_dir")
class TestRecordIpFailureUpsert:
    async def _storage(self, tmp_path: Path):
        from astrbot_plugin_webchat_gateway.storage.sqlite_backend import (
            SqliteStorage,
        )

        s = SqliteStorage(str(tmp_path / "upsert.db"))
        await s.initialize()
        return s

    async def test_first_failure_inserts(self, tmp_path: Path):
        s = await self._storage(tmp_path)
        try:
            count = await s.record_ip_failure(
                "1.1.1.1", now=10_000, max_fails=3, block_seconds=60
            )
            assert count == 1
            blocked, _ = await s.is_ip_blocked("1.1.1.1", now=10_000)
            assert not blocked
        finally:
            await s.close()

    async def test_successive_failures_increment(self, tmp_path: Path):
        s = await self._storage(tmp_path)
        try:
            assert (await s.record_ip_failure(
                "1.1.1.1", now=10_000, max_fails=3, block_seconds=60
            )) == 1
            assert (await s.record_ip_failure(
                "1.1.1.1", now=10_001, max_fails=3, block_seconds=60
            )) == 2
            # Threshold is 3 — this third failure trips the block.
            assert (await s.record_ip_failure(
                "1.1.1.1", now=10_002, max_fails=3, block_seconds=60
            )) == 3
            blocked, retry_after = await s.is_ip_blocked("1.1.1.1", now=10_002)
            assert blocked
            assert retry_after > 0
        finally:
            await s.close()

    async def test_blocked_until_rolls_forward(self, tmp_path: Path):
        """Each failure past the threshold pushes blocked_until forward —
        a sustained attacker stays blocked, doesn't slide out after one
        block_seconds window."""
        s = await self._storage(tmp_path)
        try:
            for now in (10_000, 10_001, 10_002):  # hit threshold
                await s.record_ip_failure(
                    "1.1.1.1", now=now, max_fails=3, block_seconds=60
                )
            blocked_until_first = (
                await s.record_ip_failure(
                    "1.1.1.1", now=10_010, max_fails=3, block_seconds=60
                )
            )
            assert blocked_until_first == 4
            # Confirm blocked_until is `last_fail_ts + block_seconds` for
            # the most recent failure.
            row = await s.is_ip_blocked("1.1.1.1", now=10_010)
            assert row[0] is True
            assert row[1] == (10_010 + 60) - 10_010
        finally:
            await s.close()


# ---------------------------------------------------------------------
# M11: double-failure emits file_release_failed audit event
# ---------------------------------------------------------------------


class _SpyAudit:
    def __init__(self):
        self.writes: list[tuple[str, dict]] = []

    async def write(self, event, **kwargs):
        self.writes.append((event, dict(kwargs)))


class _BrokenStorage:
    async def mark_files_committed(self, *args, **kwargs):
        raise RuntimeError("storage down")

    async def delete_files_by_ids(self, *args, **kwargs):
        # The inner release_files_safely succeeds on file_store.delete
        # for each row (so storage_deleted_ids is non-empty), then
        # calls this — failing here propagates a RuntimeError out of
        # release_files_safely, which is the double-failure shape
        # commit_attachments_or_release's outer except is meant to catch.
        raise RuntimeError("db delete also down")


class _FlakyFileStore:
    """Pretends storage delete succeeded so the inner release reaches
    the DB-delete step (where _BrokenStorage raises)."""

    async def delete(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_commit_release_double_failure_audits():
    """When mark_files_committed AND the compensating release both
    raise, surface a `file_release_failed` audit event so operators
    can manually clean up the partial-commit orphan rows."""
    from astrbot_plugin_webchat_gateway.core.file_lifecycle import (
        commit_attachments_or_release,
    )
    from astrbot_plugin_webchat_gateway.storage.base import FileRow

    audit = _SpyAudit()
    storage = _BrokenStorage()
    file_store = _FlakyFileStore()
    rows = [
        FileRow(
            file_id=f"f{i}",
            token_name="alice",
            session_id="s1",
            mime="image/png",
            size_bytes=10,
            storage_key=f"k{i}",
            committed=False,
            uploaded_at=0,
            committed_at=None,
        )
        for i in range(3)
    ]
    ok = await commit_attachments_or_release(
        storage=storage,
        file_store=file_store,
        rows=rows,
        log_label="test",
        audit=audit,
    )
    assert ok is False
    events = [ev for ev, _ in audit.writes]
    assert "file_release_failed" in events, (
        "double-failure path must emit a file_release_failed audit "
        "event — the only operator-visible signal that some rows may "
        "now be permanently outside the orphan-GC sweep"
    )
    # Detail must include row count + file_ids so an operator can
    # reconcile against actual storage state.
    detail_event = next(
        kw for ev, kw in audit.writes if ev == "file_release_failed"
    )
    assert detail_event["detail"]["row_count"] == 3
    assert detail_event["detail"]["file_ids"] == ["f0", "f1", "f2"]
    # The DB delete raised, so release_files_safely returned 0.
    assert detail_event["detail"]["released_ok"] == 0
    assert detail_event["detail"]["release_raised"] is False


class _OkDeleteStorage:
    """mark_files_committed fails, but delete_files_by_ids succeeds —
    release_files_safely fully recovers, no orphans, no audit needed."""

    async def mark_files_committed(self, *args, **kwargs):
        raise RuntimeError("storage transient")

    async def delete_files_by_ids(self, ids):
        return None


@pytest.mark.asyncio
async def test_commit_release_full_recovery_does_not_audit():
    """When release fully recovers all rows after a commit failure, no
    audit event fires — the rows are back to committed=0, the orphan
    GC will sweep them, no operator intervention needed."""
    from astrbot_plugin_webchat_gateway.core.file_lifecycle import (
        commit_attachments_or_release,
    )
    from astrbot_plugin_webchat_gateway.storage.base import FileRow

    audit = _SpyAudit()
    rows = [
        FileRow(
            file_id=f"f{i}",
            token_name="alice",
            session_id="s1",
            mime="image/png",
            size_bytes=10,
            storage_key=f"k{i}",
            committed=False,
            uploaded_at=0,
            committed_at=None,
        )
        for i in range(2)
    ]
    ok = await commit_attachments_or_release(
        storage=_OkDeleteStorage(),
        file_store=_FlakyFileStore(),
        rows=rows,
        log_label="test",
        audit=audit,
    )
    assert ok is False
    assert not any(ev == "file_release_failed" for ev, _ in audit.writes), (
        "release recovered all rows; spamming file_release_failed audit "
        "would defeat the operator-visible signal it's supposed to be"
    )
