"""H5: direct coverage for safety-critical primitives.

These tests fill the coverage gaps the H5 audit called out:

  * `TokenService.regenerate` (admin_tokens.py) — rotates `token_hash`,
    which is the same value that `core/file_cookie.sign` folds into
    the HMAC. End-to-end: regenerate → old bearer no longer matches
    by hash → old cookies fail HMAC verify against the new hash.
  * `IpGuard` (core/ip_guard.py) — max_fails / block_seconds /
    is_blocked edge behaviour. Decrement-on-success is covered by
    `test_h3_ip_guard_decrement.py`; this file pins the rest.
  * `core/file_cookie` HMAC roundtrip — self-contained pure-function
    paths (sign / verify / expiry / token-hash-rotation).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------
# file_cookie HMAC roundtrip
# ---------------------------------------------------------------------


class TestFileCookieHmac:
    def test_sign_then_verify_roundtrip(self):
        from astrbot_plugin_webchat_gateway.core import file_cookie

        secret = b"\x01" * 32
        exp = int(time.time()) + 3600
        cookie = file_cookie.sign(
            secret,
            token_name="alice",
            token_hash="HASH_AAA",
            exp_ts=exp,
        )
        assert file_cookie.verify(
            secret, cookie, current_token_hash="HASH_AAA"
        ) == ("alice", exp)

    def test_verify_rejects_after_token_hash_rotation(self):
        """The whole point of folding `token_hash` into the HMAC: an
        admin `regenerate_token` rotates the hash, and every outstanding
        cookie's sig stops matching. Verify returns None."""
        from astrbot_plugin_webchat_gateway.core import file_cookie

        secret = b"\x01" * 32
        exp = int(time.time()) + 3600
        cookie = file_cookie.sign(
            secret,
            token_name="alice",
            token_hash="HASH_OLD",
            exp_ts=exp,
        )
        # Same secret, same cookie, but the live token_hash changed.
        assert (
            file_cookie.verify(secret, cookie, current_token_hash="HASH_NEW")
            is None
        )

    def test_verify_rejects_expired(self):
        from astrbot_plugin_webchat_gateway.core import file_cookie

        secret = b"\x01" * 32
        # Past exp_ts → verify must return None.
        cookie = file_cookie.sign(
            secret, token_name="alice", token_hash="H", exp_ts=int(time.time()) - 1
        )
        assert (
            file_cookie.verify(secret, cookie, current_token_hash="H") is None
        )

    def test_verify_rejects_wrong_secret(self):
        from astrbot_plugin_webchat_gateway.core import file_cookie

        secret_a = b"\xAA" * 32
        secret_b = b"\xBB" * 32
        cookie = file_cookie.sign(
            secret_a,
            token_name="alice",
            token_hash="H",
            exp_ts=int(time.time()) + 3600,
        )
        assert (
            file_cookie.verify(secret_b, cookie, current_token_hash="H")
            is None
        )

    def test_verify_rejects_malformed_inputs(self):
        from astrbot_plugin_webchat_gateway.core import file_cookie

        secret = b"\x01" * 32
        for bad in [
            "",                # empty
            "alice.notatime.sig",  # exp not int
            "alice.999.",         # empty sig
            ".999.sig",           # empty name
            "alice.999.!!badbase64!!",  # invalid base64
            "alice-no-dots",    # rsplit gives <3 parts
        ]:
            assert file_cookie.verify(secret, bad, current_token_hash="H") is None

    def test_token_name_with_dots_roundtrips(self):
        """Admin name charset includes `.`, so the rsplit-on-`.` parse
        must NOT confuse the name with the rest."""
        from astrbot_plugin_webchat_gateway.core import file_cookie

        secret = b"\x01" * 32
        exp = int(time.time()) + 3600
        # Two dots in the name — would break a plain `split('.', 2)`.
        cookie = file_cookie.sign(
            secret,
            token_name="alice.bot.v1",
            token_hash="H",
            exp_ts=exp,
        )
        assert file_cookie.verify(
            secret, cookie, current_token_hash="H"
        ) == ("alice.bot.v1", exp)


# ---------------------------------------------------------------------
# IpGuard edge behaviour (decrement covered separately in H3 tests)
# ---------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("tmp_data_dir")
class TestIpGuard:
    async def _guard(self, tmp_path: Path, *, max_fails=3, block=60):
        from astrbot_plugin_webchat_gateway.core.ip_guard import IpGuard
        from astrbot_plugin_webchat_gateway.storage.sqlite_backend import (
            SqliteStorage,
        )

        storage = SqliteStorage(str(tmp_path / "guard.db"))
        await storage.initialize()
        guard = IpGuard(storage, max_fails=max_fails, block_seconds=block)
        return guard, storage

    async def test_disabled_when_max_fails_zero(self, tmp_path: Path):
        from astrbot_plugin_webchat_gateway.core.ip_guard import IpGuard
        from astrbot_plugin_webchat_gateway.storage.sqlite_backend import (
            SqliteStorage,
        )

        storage = SqliteStorage(str(tmp_path / "guard.db"))
        await storage.initialize()
        try:
            guard = IpGuard(storage, max_fails=0, block_seconds=60)
            assert not guard.enabled
            # Disabled guards must not block AND must not write rows.
            for _ in range(100):
                assert (await guard.record_failure("1.2.3.4")) == 0
            assert (await guard.is_blocked("1.2.3.4")) == (False, 0)
        finally:
            await storage.close()

    async def test_blocks_after_threshold(self, tmp_path: Path):
        guard, storage = await self._guard(tmp_path, max_fails=3)
        try:
            # 2 failures: still allowed.
            await guard.record_failure("1.1.1.1")
            await guard.record_failure("1.1.1.1")
            assert (await guard.is_blocked("1.1.1.1"))[0] is False
            # 3rd failure crosses threshold.
            await guard.record_failure("1.1.1.1")
            blocked, retry_after = await guard.is_blocked("1.1.1.1")
            assert blocked is True
            assert retry_after > 0
        finally:
            await storage.close()

    async def test_unknown_ip_normalised_to_stable_bucket(
        self, tmp_path: Path
    ):
        """`ip == "unknown"` (the value `client_ip()` returns when
        `request.remote` is None and XFF is untrusted) must fold into
        ONE stable bucket so floods from clients-with-no-IP still trip
        the guard."""
        guard, storage = await self._guard(tmp_path, max_fails=2)
        try:
            await guard.record_failure("unknown")
            await guard.record_failure("unknown")
            blocked, _ = await guard.is_blocked("unknown")
            assert blocked, (
                "two failures from 'unknown' must use the same row "
                "and trip the threshold"
            )
        finally:
            await storage.close()

    async def test_empty_ip_is_skipped(self, tmp_path: Path):
        """`_normalize_ip("")` returns None — record/reset are no-ops
        (we'd rather under-account than mis-attribute to an empty bucket
        that conflicts with the 'unknown' fallback)."""
        guard, storage = await self._guard(tmp_path, max_fails=2)
        try:
            assert (await guard.record_failure("")) == 0
            blocked, _ = await guard.is_blocked("")
            assert not blocked
        finally:
            await storage.close()


# ---------------------------------------------------------------------
# TokenService end-to-end coverage
# ---------------------------------------------------------------------


class _CollectingAudit:
    def __init__(self) -> None:
        self.writes: list[tuple[str, dict]] = []

    async def write(self, event: str, **kwargs):
        self.writes.append((event, dict(kwargs)))


@pytest.mark.asyncio
@pytest.mark.usefixtures("tmp_data_dir")
class TestTokenService:
    async def _service(self, tmp_path: Path):
        from astrbot_plugin_webchat_gateway.handlers.admin_tokens import (
            TokenService,
        )
        from astrbot_plugin_webchat_gateway.storage.sqlite_backend import (
            SqliteStorage,
        )

        storage = SqliteStorage(str(tmp_path / "tokens.db"))
        await storage.initialize()
        audit = _CollectingAudit()
        svc = TokenService(storage, audit, default_daily_quota=200)
        return svc, storage, audit

    async def test_issue_persists_and_audits(self, tmp_path: Path):
        from astrbot_plugin_webchat_gateway.core.auth import hash_token

        svc, storage, audit = await self._service(tmp_path)
        try:
            result = await svc.issue(name="alice", ip="10.0.0.1")
            row = await storage.get_token_by_name("alice")
            assert row is not None
            # Plaintext was returned exactly once; storage stores hash.
            assert row.token_hash == hash_token(result.token)
            assert result.daily_quota == 200
            # Audit captured the issuance.
            assert any(ev == "issue" for ev, _ in audit.writes)
        finally:
            await storage.close()

    async def test_issue_rejects_duplicate_name(self, tmp_path: Path):
        from astrbot_plugin_webchat_gateway.handlers.admin_tokens import (
            ServiceError,
        )

        svc, storage, _audit = await self._service(tmp_path)
        try:
            await svc.issue(name="alice")
            with pytest.raises(ServiceError) as exc:
                await svc.issue(name="alice")
            assert exc.value.code == "name_exists"
        finally:
            await storage.close()

    async def test_regenerate_rotates_hash_and_invalidates_old_bearer(
        self, tmp_path: Path
    ):
        """The critical safety property: after `regenerate`, the OLD
        plaintext bearer no longer resolves to a row via
        `get_token_by_hash`, and the NEW plaintext does."""
        from astrbot_plugin_webchat_gateway.core.auth import hash_token

        svc, storage, _audit = await self._service(tmp_path)
        try:
            initial = await svc.issue(name="alice")
            old_plain = initial.token
            old_row = await storage.get_token_by_hash(hash_token(old_plain))
            assert old_row is not None and old_row.name == "alice"

            regen = await svc.regenerate(name="alice")
            new_plain = regen.token

            # Old bearer no longer maps to the row.
            assert (
                await storage.get_token_by_hash(hash_token(old_plain))
            ) is None
            # New bearer does.
            new_row = await storage.get_token_by_hash(hash_token(new_plain))
            assert new_row is not None and new_row.name == "alice"
            # token_hash actually rotated (not just plaintext).
            assert old_row.token_hash != new_row.token_hash
        finally:
            await storage.close()

    async def test_regenerate_propagates_to_file_cookie_hmac(
        self, tmp_path: Path
    ):
        """End-to-end: a cookie signed under the OLD token_hash stops
        verifying once `regenerate` rotates the hash. This is the
        contract that the /files cookie path depends on for instant
        revocation."""
        from astrbot_plugin_webchat_gateway.core import file_cookie

        svc, storage, _audit = await self._service(tmp_path)
        try:
            await svc.issue(name="alice")
            old_hash = (await storage.get_token_by_name("alice")).token_hash

            secret = b"\x33" * 32
            exp = int(time.time()) + 3600
            cookie = file_cookie.sign(
                secret,
                token_name="alice",
                token_hash=old_hash,
                exp_ts=exp,
            )
            # Pre-regenerate: cookie verifies.
            assert file_cookie.verify(
                secret, cookie, current_token_hash=old_hash
            ) == ("alice", exp)

            await svc.regenerate(name="alice")
            new_hash = (await storage.get_token_by_name("alice")).token_hash
            assert new_hash != old_hash

            # Post-regenerate: same cookie, verify against the live
            # hash → None. This is exactly what /files does on every
            # serve request, so all outstanding cookies are killed in
            # one step.
            assert (
                file_cookie.verify(secret, cookie, current_token_hash=new_hash)
                is None
            )
        finally:
            await storage.close()

    async def test_regenerate_custom_token_uses_caller_value(
        self, tmp_path: Path
    ):
        from astrbot_plugin_webchat_gateway.core.auth import hash_token

        svc, storage, _audit = await self._service(tmp_path)
        try:
            await svc.issue(name="alice")
            custom = "x" * 40
            regen = await svc.regenerate(name="alice", custom_token=custom)
            assert regen.token == custom
            row = await storage.get_token_by_name("alice")
            assert row.token_hash == hash_token(custom)
        finally:
            await storage.close()

    async def test_regenerate_unknown_name_raises(self, tmp_path: Path):
        from astrbot_plugin_webchat_gateway.handlers.admin_tokens import (
            ServiceError,
        )

        svc, storage, audit = await self._service(tmp_path)
        try:
            with pytest.raises(ServiceError) as exc:
                await svc.regenerate(name="ghost")
            assert exc.value.code == "not_found"
            # Miss audited for operator visibility.
            assert any(ev == "regenerate_miss" for ev, _ in audit.writes)
        finally:
            await storage.close()

    async def test_revoke_and_audit(self, tmp_path: Path):
        svc, storage, audit = await self._service(tmp_path)
        try:
            await svc.issue(name="alice")
            assert (await svc.revoke(name="alice")) is True
            row = await storage.get_token_by_name("alice")
            assert row.revoked_at is not None
            assert any(ev == "revoke" for ev, _ in audit.writes)
            # Second revoke is a miss.
            assert (await svc.revoke(name="alice")) is False
            assert any(ev == "revoke_miss" for ev, _ in audit.writes)
        finally:
            await storage.close()

    async def test_rename_cascades_through_storage(self, tmp_path: Path):
        svc, storage, _audit = await self._service(tmp_path)
        try:
            await svc.issue(name="alice")
            await svc.rename(old_name="alice", new_name="bob")
            assert (await storage.get_token_by_name("alice")) is None
            assert (await storage.get_token_by_name("bob")) is not None
        finally:
            await storage.close()
