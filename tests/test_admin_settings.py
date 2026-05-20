"""Tests for the /admin/settings whitelist surface.

Two layers:

  * Unit-level coverage of ``core.settings_schema`` — FIELDS shape,
    BLACKLIST membership, ``field_for_key``, ``read_value``, and the
    validate-then-write contract of ``apply_update``. These run against
    plain dict-of-dicts so the test doesn't need an AstrBotConfig.
  * Integration-level coverage of the aiohttp GET / PATCH handlers from
    ``handlers.admin_settings``. We follow the TestServer + TestClient
    pattern from ``test_h1_files_ip_guard.py`` and hand-roll stubs for
    audit / ip_guard / config, mirroring ``_SpyAudit`` /``_StubIpGuard``
    elsewhere in the suite.
"""

from __future__ import annotations

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


# ---------------------------------------------------------------------
# Unit tests: core.settings_schema
# ---------------------------------------------------------------------


class TestFieldsAndBlacklist:
    def test_fields_non_empty_and_no_overlap_with_blacklist(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            BLACKLIST,
            FIELDS,
        )

        assert len(FIELDS) >= 25, (
            f"whitelist should expose at least 25 fields per spec; got {len(FIELDS)}"
        )
        for f in FIELDS:
            assert f.key not in BLACKLIST, (
                f"defensive: whitelisted key {f.key!r} must not also appear in BLACKLIST"
            )

    def test_blacklist_contains_canonical_keys(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import BLACKLIST

        # Explicit per-key assertions so a missing member surfaces with a
        # readable failure message (vs a single set-diff dump). The
        # canonical blacklist shrank in the "move everything possible
        # into the web UI" pass — only true boot-time essentials remain.
        assert "host" in BLACKLIST
        assert "port" in BLACKLIST
        assert "master_admin_key" in BLACKLIST
        assert "endpoint_prefix" in BLACKLIST
        assert "admin_ui_path" in BLACKLIST
        assert "storage.driver" in BLACKLIST
        assert "storage.sqlite_path" in BLACKLIST
        assert "storage.mysql_dsn" in BLACKLIST
        assert "storage.mysql_pool_max" in BLACKLIST

    def test_promoted_keys_are_no_longer_blacklisted(self):
        """The following used to live in BLACKLIST and are now in the
        whitelist (admin-editable, restart-to-apply). Pin the move so
        a future refactor doesn't quietly re-blacklist them."""
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            BLACKLIST,
            field_for_key,
        )

        promoted = [
            "persona_id",
            "streaming.redis_dsn",
            "uploads.storage_driver",
            "uploads.local_path",
            "uploads.r2.account_id",
            "uploads.r2.access_key_id",
            "uploads.r2.secret_access_key",
            "uploads.r2.bucket",
            "uploads.r2.endpoint",
            "site_icon_url",
        ]
        for key in promoted:
            assert key not in BLACKLIST, (
                f"{key!r} is supposed to be editable via /admin/settings now"
            )
            assert field_for_key(key) is not None, (
                f"{key!r} promoted out of BLACKLIST but missing from FIELDS"
            )

    def test_secret_fields_marked(self):
        """R2 credentials must carry secret=True so the UI renders them
        as password inputs (and a future audit-log scrubber knows to
        treat them as sensitive)."""
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            field_for_key,
        )

        assert field_for_key("uploads.r2.access_key_id").secret is True
        assert field_for_key("uploads.r2.secret_access_key").secret is True
        # Non-secret strings stay secret=False (default).
        assert field_for_key("site_name").secret is False

    def test_every_field_has_chinese_label(self):
        """Spec says all field labels go Chinese — a missing or
        ASCII-only label means a stale field was added without
        following the convention."""
        import re

        from astrbot_plugin_webchat_gateway.core.settings_schema import FIELDS

        for f in FIELDS:
            assert f.label, f"{f.key!r} missing label"
            assert re.search(r"[一-鿿]", f.label), (
                f"{f.key!r} label {f.label!r} has no Chinese characters"
            )


class TestFieldForKey:
    def test_audit_retention_days_is_hot_reloaded(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            field_for_key,
        )

        spec = field_for_key("audit_retention_days")
        assert spec is not None
        assert spec.restart_required is False

    def test_site_name_requires_restart(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            field_for_key,
        )

        spec = field_for_key("site_name")
        assert spec is not None
        assert spec.restart_required is True

    def test_blacklisted_key_returns_none(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            field_for_key,
        )

        # `host` is blacklisted; field_for_key must NOT leak its existence —
        # blacklisted and truly-unknown deliberately collapse.
        assert field_for_key("host") is None

    def test_unknown_key_returns_none(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            field_for_key,
        )

        assert field_for_key("nonexistent_field_xyz") is None


class TestReadValue:
    def test_top_level_round_trip(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            read_value,
        )

        config = {"site_name": "My Gateway"}
        assert read_value(config, "site_name") == "My Gateway"

    def test_dotted_nested_two_levels(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            read_value,
        )

        config = {"uploads": {"max_file_size_mb": 50}}
        assert read_value(config, "uploads.max_file_size_mb") == 50

    def test_dotted_nested_three_levels(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            read_value,
        )

        config = {"uploads": {"r2": {"serving_mode": "direct"}}}
        assert read_value(config, "uploads.r2.serving_mode") == "direct"

    def test_missing_intermediate_returns_none(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            read_value,
        )

        # The intermediate section isn't even present in config; read_value
        # should walk safely and report None rather than KeyError.
        assert read_value({}, "missing_section.field") is None


class TestApplyUpdate:
    def test_top_level_int_writes_through(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            apply_update,
        )

        config: dict = {}
        apply_update(config, "audit_retention_days", 14)
        assert config["audit_retention_days"] == 14

    def test_nested_int_writes_through(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            apply_update,
        )

        config: dict = {}
        apply_update(config, "uploads.max_file_size_mb", 50)
        assert config["uploads"]["max_file_size_mb"] == 50

    def test_negative_int_rejected_out_of_range(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            SettingsError,
            apply_update,
        )

        config: dict = {}
        with pytest.raises(SettingsError) as exc:
            apply_update(config, "audit_retention_days", -5)
        assert exc.value.code == "out_of_range"

    def test_huge_int_rejected_out_of_range(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            SettingsError,
            apply_update,
        )

        config: dict = {}
        with pytest.raises(SettingsError) as exc:
            apply_update(config, "audit_retention_days", 99999)
        assert exc.value.code == "out_of_range"

    def test_non_int_rejected_invalid_type(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            SettingsError,
            apply_update,
        )

        config: dict = {}
        with pytest.raises(SettingsError) as exc:
            apply_update(config, "audit_retention_days", "not an int")
        assert exc.value.code == "invalid_type"

    def test_blacklisted_key_reads_as_unknown(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            SettingsError,
            apply_update,
        )

        config: dict = {}
        with pytest.raises(SettingsError) as exc:
            apply_update(config, "host", "0.0.0.0")
        # Spec: blacklist must collapse to "unknown" so the wire doesn't
        # disclose which keys exist.
        assert exc.value.code == "unknown_field"

    def test_truly_unknown_key_rejected(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            SettingsError,
            apply_update,
        )

        config: dict = {}
        with pytest.raises(SettingsError) as exc:
            apply_update(config, "nonexistent", "x")
        assert exc.value.code == "unknown_field"

    def test_valid_option_succeeds(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            apply_update,
        )

        config: dict = {}
        apply_update(config, "theme_family", "classic")
        assert config["theme_family"] == "classic"

    def test_invalid_option_rejected(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            SettingsError,
            apply_update,
        )

        config: dict = {}
        with pytest.raises(SettingsError) as exc:
            apply_update(config, "theme_family", "totally_bogus")
        assert exc.value.code == "invalid_option"

    def test_bool_true_succeeds(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            apply_update,
        )

        config: dict = {}
        apply_update(config, "auto_title_enabled", True)
        assert config["auto_title_enabled"] is True

    def test_bool_string_coercions(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            apply_update,
        )

        config: dict = {}
        apply_update(config, "auto_title_enabled", "yes")
        assert config["auto_title_enabled"] is True
        apply_update(config, "auto_title_enabled", "false")
        assert config["auto_title_enabled"] is False

    def test_bool_unrecognised_string_rejected(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            SettingsError,
            apply_update,
        )

        config: dict = {}
        with pytest.raises(SettingsError) as exc:
            apply_update(config, "auto_title_enabled", "maybe")
        # The schema's _coerce_bool only accepts the canonical truthy /
        # falsy strings; anything else raises invalid_type so a typo'd
        # checkbox value doesn't silently flip a security toggle.
        assert exc.value.code == "invalid_type"

    def test_csv_field_stores_normalised_string(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            apply_update,
        )

        config: dict = {}
        apply_update(
            config, "allowed_origins", "https://a.com, https://b.com"
        )
        # NOTE: impl chose to persist CSV fields as a comma-joined string
        # (no spaces, individual entries stripped) so the on-disk form
        # round-trips through ConfigView.from_raw's own CSV split.
        assert config["allowed_origins"] == "https://a.com,https://b.com"

    def test_failed_apply_leaves_config_unchanged(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            SettingsError,
            apply_update,
        )

        # Pre-seed a value and assert it stays the same after a failed apply.
        config: dict = {"audit_retention_days": 7}
        snapshot = dict(config)
        with pytest.raises(SettingsError):
            apply_update(config, "audit_retention_days", -1)
        assert config == snapshot


# ---------------------------------------------------------------------
# Integration tests: handlers.admin_settings via aiohttp TestServer
# ---------------------------------------------------------------------


class _RecordingAudit:
    """Mirrors `_SpyAudit` in `test_m_batch_fixes.py`. Records every
    write so the test can assert on event names + details verbatim."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, dict]] = []

    async def write(self, event: str, **kwargs):
        self.writes.append((event, dict(kwargs)))


class _StubIpGuard:
    """Same shape as ``test_h1_files_ip_guard.py`` — never blocks."""

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


class _StubConfig(dict):
    """Dict-like config that also records ``save_config`` invocations.

    Inheriting from ``dict`` lets the schema helpers walk + mutate the
    container exactly as they would against AstrBotConfig, while the
    extra ``save_config`` attribute mirrors the production AstrBotConfig
    contract the handler relies on after a successful PATCH.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.save_calls = 0

    def save_config(self) -> None:
        self.save_calls += 1


_ADMIN_KEY = "0123456789abcdef0123456789abcdef"  # 32 chars, mirrors prod-style master keys


def _build_deps(*, config, audit, ip_guard, on_reload=None):
    from astrbot_plugin_webchat_gateway.handlers.admin_settings import (
        AdminSettingsDeps,
    )

    return AdminSettingsDeps(
        config=config,
        audit=audit,
        allowed_origins={"*"},
        master_admin_key=_ADMIN_KEY,
        ip_guard=ip_guard,
        trust_forwarded_for=False,
        trust_referer_as_origin=False,
        # PATCH must NOT allow missing Origin so the test reflects
        # the prod posture for state-changing endpoints. GET handler
        # internally allow_missing=True, so we don't need to send one.
        allow_missing_origin=True,
        on_reload=on_reload,
    )


async def _make_client(deps):
    from astrbot_plugin_webchat_gateway.handlers.admin_settings import (
        make_admin_settings_handlers,
    )

    handlers = make_admin_settings_handlers(deps)
    app = web.Application()
    app.router.add_get("/api/webchat/admin/settings", handlers["get_settings"])
    app.router.add_patch(
        "/api/webchat/admin/settings", handlers["patch_settings"]
    )
    app.router.add_options(
        "/api/webchat/admin/settings", handlers["preflight"]
    )
    server = TestServer(app)
    await server.start_server()
    client = TestClient(server)
    await client.start_server()
    return client, server


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_ADMIN_KEY}"}


# ---------- GET ----------


@pytest.mark.asyncio
async def test_get_returns_full_field_list_with_required_keys():
    audit = _RecordingAudit()
    guard = _StubIpGuard()
    config = _StubConfig(
        site_name="Demo",
        audit_retention_days=7,
        uploads={"max_file_size_mb": 20, "r2": {"serving_mode": "proxy"}},
    )
    deps = _build_deps(config=config, audit=audit, ip_guard=guard)
    client, server = await _make_client(deps)
    try:
        resp = await client.get(
            "/api/webchat/admin/settings", headers=_auth_headers()
        )
        assert resp.status == 200
        body = await resp.json()
    finally:
        await client.close()
        await server.close()
    assert "fields" in body
    fields = body["fields"]
    assert isinstance(fields, list) and len(fields) >= 25
    # Spec-mandated common keys on every entry.
    common = {"key", "section", "type", "value", "restart_required", "hint"}
    for entry in fields:
        missing = common - set(entry)
        assert not missing, f"entry {entry.get('key')!r} missing {missing}"
    # audit_retention_days specifically must be present + hot-reloaded.
    by_key = {f["key"]: f for f in fields}
    assert "audit_retention_days" in by_key
    assert by_key["audit_retention_days"]["restart_required"] is False
    # Int fields carry min/max; options fields carry options list.
    assert "min" in by_key["audit_retention_days"]
    assert "max" in by_key["audit_retention_days"]
    assert "options" in by_key["theme_family"]


@pytest.mark.asyncio
async def test_get_without_auth_returns_401():
    audit = _RecordingAudit()
    guard = _StubIpGuard()
    deps = _build_deps(
        config=_StubConfig(), audit=audit, ip_guard=guard
    )
    client, server = await _make_client(deps)
    try:
        resp = await client.get("/api/webchat/admin/settings")
        assert resp.status == 401
    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_get_with_wrong_admin_key_returns_401():
    audit = _RecordingAudit()
    guard = _StubIpGuard()
    deps = _build_deps(
        config=_StubConfig(), audit=audit, ip_guard=guard
    )
    client, server = await _make_client(deps)
    try:
        resp = await client.get(
            "/api/webchat/admin/settings",
            headers={"Authorization": "Bearer wrong-key-short"},
        )
        assert resp.status == 401
    finally:
        await client.close()
        await server.close()


# ---------- PATCH happy paths ----------


@pytest.mark.asyncio
async def test_patch_hot_reloaded_field_saves_and_reloads():
    audit = _RecordingAudit()
    guard = _StubIpGuard()
    config = _StubConfig(audit_retention_days=7)
    reload_calls = []

    async def _spy_reload():
        reload_calls.append(1)

    deps = _build_deps(
        config=config, audit=audit, ip_guard=guard, on_reload=_spy_reload
    )
    client, server = await _make_client(deps)
    try:
        resp = await client.patch(
            "/api/webchat/admin/settings",
            headers=_auth_headers(),
            json={"updates": {"audit_retention_days": 14}},
        )
        assert resp.status == 200
        body = await resp.json()
    finally:
        await client.close()
        await server.close()
    assert body["saved"] == ["audit_retention_days"]
    assert body["hot_reloaded"] == ["audit_retention_days"]
    assert body["restart_required"] == []
    assert config["audit_retention_days"] == 14
    assert config.save_calls == 1
    assert len(reload_calls) == 1


@pytest.mark.asyncio
async def test_patch_restart_required_field_classified_correctly():
    audit = _RecordingAudit()
    guard = _StubIpGuard()
    config = _StubConfig(site_name="Old")
    deps = _build_deps(config=config, audit=audit, ip_guard=guard)
    client, server = await _make_client(deps)
    try:
        resp = await client.patch(
            "/api/webchat/admin/settings",
            headers=_auth_headers(),
            json={"updates": {"site_name": "New Name"}},
        )
        assert resp.status == 200
        body = await resp.json()
    finally:
        await client.close()
        await server.close()
    assert body["saved"] == ["site_name"]
    assert body["restart_required"] == ["site_name"]
    assert body["hot_reloaded"] == []
    assert config["site_name"] == "New Name"
    assert config.save_calls == 1


# ---------- PATCH validation failures ----------


@pytest.mark.asyncio
async def test_patch_blacklisted_key_returns_400_unknown_field():
    audit = _RecordingAudit()
    guard = _StubIpGuard()
    # Pre-seed a host value so we can prove it wasn't overwritten.
    config = _StubConfig(host="127.0.0.1")
    reload_calls = []

    async def _spy_reload():
        reload_calls.append(1)

    deps = _build_deps(
        config=config, audit=audit, ip_guard=guard, on_reload=_spy_reload
    )
    client, server = await _make_client(deps)
    try:
        resp = await client.patch(
            "/api/webchat/admin/settings",
            headers=_auth_headers(),
            json={"updates": {"host": "evil.com"}},
        )
        assert resp.status == 400
        body = await resp.json()
    finally:
        await client.close()
        await server.close()
    assert body["error"] == "unknown_field"
    assert config["host"] == "127.0.0.1"
    assert config.save_calls == 0
    assert len(reload_calls) == 0


@pytest.mark.asyncio
async def test_patch_out_of_range_int_returns_400_no_audit():
    audit = _RecordingAudit()
    guard = _StubIpGuard()
    config = _StubConfig(audit_retention_days=7)
    deps = _build_deps(config=config, audit=audit, ip_guard=guard)
    client, server = await _make_client(deps)
    try:
        resp = await client.patch(
            "/api/webchat/admin/settings",
            headers=_auth_headers(),
            json={"updates": {"audit_retention_days": 99999}},
        )
        assert resp.status == 400
        body = await resp.json()
    finally:
        await client.close()
        await server.close()
    assert body["error"] == "out_of_range"
    assert config.save_calls == 0
    # Audit must NOT carry an admin_settings_update event on a failed
    # validation — only successful patches log the keys.
    events = [ev for ev, _ in audit.writes]
    assert "admin_settings_update" not in events


@pytest.mark.asyncio
async def test_patch_empty_updates_returns_400_invalid_payload():
    audit = _RecordingAudit()
    guard = _StubIpGuard()
    config = _StubConfig()
    deps = _build_deps(config=config, audit=audit, ip_guard=guard)
    client, server = await _make_client(deps)
    try:
        resp = await client.patch(
            "/api/webchat/admin/settings",
            headers=_auth_headers(),
            json={"updates": {}},
        )
        assert resp.status == 400
        body = await resp.json()
    finally:
        await client.close()
        await server.close()
    assert body["error"] == "invalid_payload"


@pytest.mark.asyncio
async def test_patch_non_dict_body_returns_400():
    audit = _RecordingAudit()
    guard = _StubIpGuard()
    config = _StubConfig()
    deps = _build_deps(config=config, audit=audit, ip_guard=guard)
    client, server = await _make_client(deps)
    try:
        resp = await client.patch(
            "/api/webchat/admin/settings",
            headers=_auth_headers(),
            json=["not", "a", "dict"],
        )
        assert resp.status == 400
    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_patch_mixed_valid_and_invalid_is_atomic():
    """One bad key rejects the whole batch — no partial writes."""
    audit = _RecordingAudit()
    guard = _StubIpGuard()
    config = _StubConfig(audit_retention_days=7, site_name="Original")
    deps = _build_deps(config=config, audit=audit, ip_guard=guard)
    client, server = await _make_client(deps)
    try:
        resp = await client.patch(
            "/api/webchat/admin/settings",
            headers=_auth_headers(),
            # One good, one out-of-range; spec says the whole batch
            # fails so the valid one must NOT be written either.
            json={
                "updates": {
                    "site_name": "New Name",
                    "audit_retention_days": 999999,
                }
            },
        )
        assert resp.status == 400
    finally:
        await client.close()
        await server.close()
    assert config["audit_retention_days"] == 7
    assert config["site_name"] == "Original"
    assert config.save_calls == 0


# ---------- PATCH audit + auth ----------


@pytest.mark.asyncio
async def test_patch_writes_audit_with_keys_not_values():
    audit = _RecordingAudit()
    guard = _StubIpGuard()
    config = _StubConfig(site_name="Old", audit_retention_days=7)
    deps = _build_deps(config=config, audit=audit, ip_guard=guard)
    client, server = await _make_client(deps)
    try:
        resp = await client.patch(
            "/api/webchat/admin/settings",
            headers=_auth_headers(),
            json={
                "updates": {
                    "site_name": "SecretCompanyName",
                    "audit_retention_days": 14,
                }
            },
        )
        assert resp.status == 200
    finally:
        await client.close()
        await server.close()
    matching = [w for w in audit.writes if w[0] == "admin_settings_update"]
    assert len(matching) == 1, audit.writes
    _ev, kwargs = matching[0]
    detail = kwargs.get("detail")
    # `detail` may be the raw dict (audit logger serializes on write).
    if isinstance(detail, str):
        detail = json.loads(detail)
    assert isinstance(detail, dict)
    assert set(detail.get("keys", [])) == {
        "site_name",
        "audit_retention_days",
    }
    # Spec: values are deliberately omitted from the audit row.
    serialised = json.dumps(detail)
    assert "SecretCompanyName" not in serialised
    assert "14" not in detail.get("keys", [])


@pytest.mark.asyncio
async def test_patch_without_auth_returns_401():
    audit = _RecordingAudit()
    guard = _StubIpGuard()
    config = _StubConfig(audit_retention_days=7)
    deps = _build_deps(config=config, audit=audit, ip_guard=guard)
    client, server = await _make_client(deps)
    try:
        resp = await client.patch(
            "/api/webchat/admin/settings",
            json={"updates": {"audit_retention_days": 14}},
        )
        assert resp.status == 401
    finally:
        await client.close()
        await server.close()
    # Unauthorized PATCH must not touch the config or write the
    # admin_settings_update audit event.
    assert config.save_calls == 0
    assert config["audit_retention_days"] == 7
    events = [ev for ev, _ in audit.writes]
    assert "admin_settings_update" not in events


# ---------------------------------------------------------------------
# Restart endpoint
# ---------------------------------------------------------------------


async def _make_restart_client(deps):
    """Same shape as `_make_client` but also wires the restart route so
    the integration test can POST /admin/restart."""
    from astrbot_plugin_webchat_gateway.handlers.admin_settings import (
        make_admin_settings_handlers,
    )

    handlers = make_admin_settings_handlers(deps)
    app = web.Application()
    app.router.add_get("/api/webchat/admin/settings", handlers["get_settings"])
    app.router.add_patch(
        "/api/webchat/admin/settings", handlers["patch_settings"]
    )
    app.router.add_options(
        "/api/webchat/admin/settings", handlers["preflight"]
    )
    app.router.add_post(
        "/api/webchat/admin/restart", handlers["post_restart"]
    )
    app.router.add_options(
        "/api/webchat/admin/restart", handlers["preflight"]
    )
    server = TestServer(app)
    await server.start_server()
    client = TestClient(server)
    await client.start_server()
    return client, server


def _build_deps_with_restart(
    *, config, audit, ip_guard, on_restart=None, on_reload=None
):
    from astrbot_plugin_webchat_gateway.handlers.admin_settings import (
        AdminSettingsDeps,
    )

    return AdminSettingsDeps(
        config=config,
        audit=audit,
        allowed_origins={"*"},
        master_admin_key=_ADMIN_KEY,
        ip_guard=ip_guard,
        trust_forwarded_for=False,
        trust_referer_as_origin=False,
        allow_missing_origin=True,
        on_reload=on_reload,
        on_restart=on_restart,
    )


@pytest.mark.asyncio
async def test_restart_schedules_callback_and_returns_202():
    """Happy path: a valid POST returns 202 immediately AND schedules
    the restart callback in a background task. The handler must NOT
    block on the callback — otherwise the response can't make it out
    before _stop tears down the aiohttp server."""
    import asyncio as _asyncio

    audit = _RecordingAudit()
    guard = _StubIpGuard()
    restart_done = _asyncio.Event()
    restart_calls = []

    async def _restart():
        restart_calls.append(True)
        restart_done.set()

    deps = _build_deps_with_restart(
        config=_StubConfig(), audit=audit, ip_guard=guard, on_restart=_restart
    )
    client, server = await _make_restart_client(deps)
    try:
        resp = await client.post(
            "/api/webchat/admin/restart", headers=_auth_headers()
        )
        assert resp.status == 202
        body = await resp.json()
        assert body == {"status": "restarting"}
        # At response time the callback hasn't run yet (the handler
        # sleeps 0.25s before invoking it).
        assert restart_calls == []
        # The background task should fire within ~1s.
        await _asyncio.wait_for(restart_done.wait(), timeout=2.0)
        assert restart_calls == [True]
    finally:
        await client.close()
        await server.close()
    # Audit row written BEFORE the lifecycle bounce so the breadcrumb
    # survives even if _stop kills the writer.
    events = [ev for ev, _ in audit.writes]
    assert "admin_restart" in events
    detail = next(kw["detail"] for ev, kw in audit.writes if ev == "admin_restart")
    assert detail == {"phase": "requested"}


@pytest.mark.asyncio
async def test_restart_without_auth_returns_401():
    audit = _RecordingAudit()
    guard = _StubIpGuard()
    fired = []

    async def _restart():
        fired.append(True)

    deps = _build_deps_with_restart(
        config=_StubConfig(), audit=audit, ip_guard=guard, on_restart=_restart
    )
    client, server = await _make_restart_client(deps)
    try:
        resp = await client.post("/api/webchat/admin/restart")
        assert resp.status == 401
    finally:
        await client.close()
        await server.close()
    # Unauthenticated request must NOT schedule the restart callback
    # and must NOT emit an admin_restart audit row.
    assert fired == []
    events = [ev for ev, _ in audit.writes]
    assert "admin_restart" not in events


@pytest.mark.asyncio
async def test_restart_returns_503_when_callback_not_wired():
    """If the host plugin didn't supply ``on_restart`` (e.g. a test
    deployment, or a future variant without lifecycle access), the
    handler must surface a clear 503 instead of silently succeeding."""
    audit = _RecordingAudit()
    guard = _StubIpGuard()
    deps = _build_deps_with_restart(
        config=_StubConfig(), audit=audit, ip_guard=guard, on_restart=None
    )
    client, server = await _make_restart_client(deps)
    try:
        resp = await client.post(
            "/api/webchat/admin/restart", headers=_auth_headers()
        )
        assert resp.status == 503
        body = await resp.json()
        assert body.get("error") == "restart_not_supported"
    finally:
        await client.close()
        await server.close()
    # No admin_restart row when we couldn't actually restart.
    events = [ev for ev, _ in audit.writes]
    assert "admin_restart" not in events


@pytest.mark.asyncio
async def test_restart_response_does_not_block_on_slow_callback():
    """Even a callback that takes seconds (or hangs) must not delay
    the 202. The handler dispatches the callback in a fire-and-forget
    task so the response writes out cleanly."""
    import asyncio as _asyncio
    import time as _time

    audit = _RecordingAudit()
    guard = _StubIpGuard()
    started = _asyncio.Event()

    async def _slow_restart():
        started.set()
        # Long enough that a synchronous-await handler would obviously
        # blow past the response timeout, short enough that the test
        # still finishes promptly.
        await _asyncio.sleep(2.0)

    deps = _build_deps_with_restart(
        config=_StubConfig(),
        audit=audit,
        ip_guard=guard,
        on_restart=_slow_restart,
    )
    client, server = await _make_restart_client(deps)
    try:
        t0 = _time.monotonic()
        resp = await client.post(
            "/api/webchat/admin/restart", headers=_auth_headers()
        )
        elapsed = _time.monotonic() - t0
        assert resp.status == 202
        # Response must arrive well before the 2.0s the callback
        # actually takes (with margin for the 0.25s pre-call sleep
        # the handler does AFTER the response goes out).
        assert elapsed < 1.0, f"handler blocked on slow callback: {elapsed:.2f}s"
    finally:
        await client.close()
        await server.close()
    # And the callback did start (proving fire-and-forget actually
    # scheduled it — not just that we skipped it).
    await _asyncio.wait_for(started.wait(), timeout=2.0)

