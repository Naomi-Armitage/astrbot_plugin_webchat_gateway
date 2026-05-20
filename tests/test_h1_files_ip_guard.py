"""Regression: H1 — /files/{id} IP-guard must NOT count cookie failures.

The serve handler had a "no bearer AND no/invalid cookie" branch that
unconditionally called `ip_guard.record_failure(ip)`. The accompanying
comment explicitly said failures where a cookie WAS present but didn't
verify should NOT be counted, but the code disagreed — it bucketed
bad-sig / expired / logout-invalidated / token-rotated cookies into the
same `record_failure` path as truly anonymous requests.

The reachable abuse case: an admin calls `regenerate_token` (rotates
`token_hash`, leaves `token_name` intact). Every open browser tab's
`<img src="/files/{id}">` now fails HMAC at once because the cookie's
sig is keyed by the *old* `token_hash`. With the original code, each
fired image silently incremented the user's own IP failure counter
until the IP got locked out — even though the user did nothing wrong.

The fix records a failure ONLY when no credential of any kind was
presented (neither `Authorization` header nor `wcg_file` cookie). This
test pins that contract by spinning up a real aiohttp server with the
production serve handler and asserting:

  1. A request with NO bearer AND NO cookie → record_failure called.
  2. A request with NO bearer but a bad-sig cookie → record_failure NOT
     called (this is the H1 failure mode).
"""

from __future__ import annotations

import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


# ----- Minimal stubs the handler actually touches -----


class _StubAudit:
    def __init__(self) -> None:
        self.writes: list[tuple[str, dict]] = []

    async def write(self, event: str, **kwargs):
        self.writes.append((event, dict(kwargs)))


class _StubIpGuard:
    """Records counter touches; never blocks (max_fails=10 / block=60)."""

    def __init__(self) -> None:
        self.record_failure_calls = 0
        self.reset_calls = 0

    async def is_blocked(self, ip):
        return (False, 0)

    async def record_failure(self, ip):
        self.record_failure_calls += 1
        return self.record_failure_calls

    async def reset(self, ip):
        self.reset_calls += 1


class _StubStorage:
    """Just enough for the bad-cookie path. The handler peeks at a
    token row by name to fetch `token_hash` before HMAC verification."""

    def __init__(self) -> None:
        # alice exists; her current token_hash is "HASH_NEW". The test
        # forges a cookie with "HASH_OLD" so verify() rejects it —
        # exactly the post-regenerate scenario.
        self._row = type(
            "TokenRow",
            (),
            dict(
                name="alice",
                token_hash="HASH_NEW",
                daily_quota=10,
                note="",
                created_at=0,
                revoked_at=None,
                expires_at=None,
            ),
        )

    async def get_token_by_name(self, name: str):
        return self._row if name == "alice" else None

    async def get_token_by_hash(self, h):  # used by gate_request's bearer path
        return None


class _StubFileStore:
    async def signed_url(self, key, *, ttl_seconds):
        return None

    async def read(self, key):
        return b""


def _build_deps(*, storage, audit, ip_guard, secret: bytes):
    """Build a minimal UploadDeps; only the fields read by the serve
    handler in the bad-cookie path matter."""
    from astrbot_plugin_webchat_gateway.handlers.files import UploadDeps

    return UploadDeps(
        storage=storage,
        audit=audit,
        ip_guard=ip_guard,
        file_store=_StubFileStore(),
        upload_gate=None,  # not reached on the bad-cookie path
        allowed_origins={"*"},
        max_file_size_mb=20,
        per_token_storage_mb=500,
        allowed_mime=("image/jpeg",),
        storage_driver="local",
        r2_serving_mode="proxy",
        r2_direct_link_ttl_seconds=300,
        files_serve_prefix="/api/webchat/files/",
        trust_forwarded_for=False,
        file_cookie_secret=secret,
        cookie_logout_tracker=None,
        trust_referer_as_origin=False,
        allow_missing_origin=True,  # so empty/missing Origin doesn't 403 the test
    )


async def _make_client(deps):
    from astrbot_plugin_webchat_gateway.handlers.files import make_serve_handler

    app = web.Application()
    app.router.add_get("/api/webchat/files/{file_id}", make_serve_handler(deps))
    server = TestServer(app)
    await server.start_server()
    client = TestClient(server)
    await client.start_server()
    return client, server


def _forge_bad_sig_cookie(token_name: str = "alice", exp_ts: int | None = None) -> str:
    """Build a `name.exp.sig` cookie value whose sig is keyed by an
    old/rotated token_hash. The handler will look up alice's CURRENT
    hash ('HASH_NEW') and verify() returns None — the exact failure
    mode this regression test cares about."""
    from astrbot_plugin_webchat_gateway.core.file_cookie import sign

    secret = b"\x00" * 32
    exp = exp_ts or int(time.time()) + 3600
    return sign(secret, token_name=token_name, token_hash="HASH_OLD", exp_ts=exp)


@pytest.mark.asyncio
async def test_no_credential_increments_ip_guard():
    """Sanity: a request with NO bearer and NO cookie still counts as a
    brute-force attempt. (The branch we DIDN'T weaken.)"""
    audit = _StubAudit()
    guard = _StubIpGuard()
    deps = _build_deps(
        storage=_StubStorage(),
        audit=audit,
        ip_guard=guard,
        secret=b"\x00" * 32,
    )
    client, server = await _make_client(deps)
    try:
        resp = await client.get("/api/webchat/files/abc123")
        assert resp.status == 401
    finally:
        await client.close()
        await server.close()
    assert guard.record_failure_calls == 1, (
        "no-credential request must still be counted; otherwise /files "
        "becomes a quieter enumeration channel than /chat"
    )
    # Audit should also reflect the no_token reason.
    assert any(
        ev == "auth_fail" and detail.get("detail", {}).get("reason") == "no_token"
        for ev, detail in audit.writes
    ), audit.writes


@pytest.mark.asyncio
async def test_bad_cookie_does_not_increment_ip_guard():
    """H1 contract: a request with a bad-sig cookie (e.g. the user's
    own cookie after admin `regenerate_token` rotated the token_hash)
    returns 401 but does NOT bump the IP-guard counter. Without this,
    a single admin regenerate locks every open tab's IP."""
    audit = _StubAudit()
    guard = _StubIpGuard()
    secret = b"\x00" * 32
    deps = _build_deps(
        storage=_StubStorage(),
        audit=audit,
        ip_guard=guard,
        secret=secret,
    )
    client, server = await _make_client(deps)
    try:
        bad_cookie = _forge_bad_sig_cookie()
        resp = await client.get(
            "/api/webchat/files/abc123",
            cookies={"wcg_file": bad_cookie},
        )
        assert resp.status == 401
    finally:
        await client.close()
        await server.close()
    assert guard.record_failure_calls == 0, (
        "bad-sig cookie (e.g. post-regenerate-token rotation) must NOT "
        "increment the IP-guard counter — comment at handlers/files.py "
        "is now matched by the code"
    )
    # Audit still records the failure with the more-specific reason so
    # operators can see "cookie present but invalid" vs "no credential".
    assert any(
        ev == "auth_fail" and detail.get("detail", {}).get("reason") == "bad_cookie"
        for ev, detail in audit.writes
    ), audit.writes
