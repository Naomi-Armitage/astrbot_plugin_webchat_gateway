"""Regression tests for the /image slash command + ImageBridge pipeline.

Three layers:

  * ``is_image_command`` / ``strip_image_prefix`` — pure-function
    helpers that decide whether a chat message routes through the
    image-gen path. Both /chat (server) and the chat_client (browser)
    use the same set of triggers, so a slash that round-trips
    correctly is the load-bearing contract.

  * ``ImageBridge.generate`` — wraps the POST to
    ``{endpoint}/v1/images/generations``. Tested via a monkeypatched
    ``aiohttp.ClientSession`` so the matrix of upstream shapes
    (success / 4xx / 5xx / timeout / empty / malformed) doesn't need
    a real OpenAI account.

  * ``persist_generated_image`` — file-store-first / DB-second write
    contract. The exception rollback path is what protects an
    operator's quota when the DB write fails after bytes already
    landed; pin it.

End-to-end /chat handler routing (slash → bridge → audit row) is left
to the existing chat-handler test surface; here we keep the seams
tight so a future provider change doesn't quietly skip validation.
"""

from __future__ import annotations

import asyncio
import base64

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


# ---------------------------------------------------------------------
# Trigger parsing
# ---------------------------------------------------------------------


class TestIsImageCommand:
    @pytest.mark.parametrize(
        "msg",
        [
            "/image a cat",
            "/img sunset",
            "/draw moon",
            "  /image leading whitespace",
            "/IMAGE case insensitive",
            "/Draw mixed",
        ],
    )
    def test_accepts(self, msg):
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            is_image_command,
        )

        assert is_image_command(msg) is True

    @pytest.mark.parametrize(
        "msg",
        [
            "",
            "hello",
            "this contains /image but not at the start",
            "/imageboard not a prefix because no boundary",
            "/something else",
            "//image",  # not a valid command (double slash)
        ],
    )
    def test_rejects(self, msg):
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            is_image_command,
        )

        assert is_image_command(msg) is False

    def test_strip_prefix_returns_prompt(self):
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            strip_image_prefix,
        )

        assert strip_image_prefix("/image  a red apple") == "a red apple"
        assert strip_image_prefix("/img sunset") == "sunset"
        assert strip_image_prefix("/draw  moon ") == "moon"

    def test_strip_prefix_empty_when_only_trigger(self):
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            strip_image_prefix,
        )

        assert strip_image_prefix("/image") == ""
        assert strip_image_prefix("/image   ") == ""

    def test_strip_prefix_passthrough_non_command(self):
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            strip_image_prefix,
        )

        # Not a command — returned unchanged save for whitespace trim.
        assert strip_image_prefix("hello world") == "hello world"


# ---------------------------------------------------------------------
# ImageBridge.generate
# ---------------------------------------------------------------------


class _StubResponse:
    def __init__(self, *, status: int, json_body=None):
        self.status = status
        self._json = json_body

    async def json(self, content_type=None):
        if self._json is None:
            raise ValueError("no body")
        return self._json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _StubSession:
    """Minimal aiohttp.ClientSession stand-in. Records POST args + the
    next response to hand back."""

    def __init__(self, response: _StubResponse | Exception):
        self._response = response
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, *, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


@pytest.fixture
def patch_aiohttp(monkeypatch):
    """Swap aiohttp.ClientSession for the test stub. Returns a closure
    that the test calls with the desired stub response — the closure
    installs the patch and returns the captured _StubSession so the
    test can inspect calls."""
    from astrbot_plugin_webchat_gateway.core import image_bridge

    captured: dict[str, _StubSession] = {}

    def _install(response):
        session = _StubSession(response)
        # ClientSession(timeout=...) is called positionally as
        # ClientSession(timeout=ClientTimeout(...)) in the bridge.
        # Return the same stub session regardless of args.
        def _factory(*a, **kw):
            return session
        monkeypatch.setattr(image_bridge.aiohttp, "ClientSession", _factory)
        captured["session"] = session
        return session

    return _install


@pytest.mark.asyncio
class TestImageBridgeGenerate:
    def _make_bridge(self, **overrides):
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            ImageBridge,
        )

        defaults = {
            "enabled": True,
            "endpoint": "https://api.openai.com/v1",
            "api_key": "sk-test-1234",
            "model": "dall-e-3",
            "size": "1024x1024",
            "timeout_seconds": 30,
        }
        defaults.update(overrides)
        return ImageBridge(**defaults)

    async def test_disabled_when_feature_off(self, patch_aiohttp):
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            ImageBridgeError,
        )

        bridge = self._make_bridge(enabled=False)
        with pytest.raises(ImageBridgeError) as exc:
            await bridge.generate("a cat")
        assert exc.value.code == "image_disabled"

    async def test_disabled_when_api_key_missing(self):
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            ImageBridgeError,
        )

        bridge = self._make_bridge(api_key="")
        with pytest.raises(ImageBridgeError) as exc:
            await bridge.generate("a cat")
        assert exc.value.code == "image_disabled"

    async def test_disabled_when_endpoint_missing(self):
        """Half-configured deployments shouldn't 500 — surface
        ``image_disabled`` so the operator gets a clear nudge to
        finish the config in the admin panel."""
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            ImageBridgeError,
        )

        bridge = self._make_bridge(endpoint="")
        with pytest.raises(ImageBridgeError) as exc:
            await bridge.generate("a cat")
        assert exc.value.code == "image_disabled"

    async def test_success_returns_decoded_bytes(self, patch_aiohttp):
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
        b64 = base64.b64encode(png_bytes).decode()
        patch_aiohttp(_StubResponse(status=200, json_body={
            "data": [{"b64_json": b64}],
        }))
        bridge = self._make_bridge()
        result = await bridge.generate("a red apple")
        assert result.content == png_bytes
        assert result.mime == "image/png"
        assert result.prompt == "a red apple"

    async def test_success_request_shape(self, patch_aiohttp):
        b64 = base64.b64encode(b"\x89PNG").decode()
        session = patch_aiohttp(_StubResponse(status=200, json_body={
            "data": [{"b64_json": b64}],
        }))
        bridge = self._make_bridge(model="dall-e-3", size="1792x1024")
        await bridge.generate("a forest")
        assert len(session.calls) == 1
        call = session.calls[0]
        assert call["url"] == "https://api.openai.com/v1/images/generations"
        assert call["json"]["model"] == "dall-e-3"
        assert call["json"]["size"] == "1792x1024"
        assert call["json"]["prompt"] == "a forest"
        assert call["json"]["response_format"] == "b64_json"
        # n=1 is hard-coded — a chat turn produces a chat turn, not N.
        assert call["json"]["n"] == 1
        # Authorization header carries the bearer.
        assert call["headers"]["Authorization"] == "Bearer sk-test-1234"

    async def test_upstream_4xx_raises_call_failed(self, patch_aiohttp):
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            ImageBridgeError,
        )

        patch_aiohttp(_StubResponse(status=400, json_body={
            "error": {"message": "prompt rejected by safety classifier"},
        }))
        bridge = self._make_bridge()
        with pytest.raises(ImageBridgeError) as exc:
            await bridge.generate("a cat")
        assert exc.value.code == "image_call_failed"

    async def test_upstream_5xx_raises_call_failed(self, patch_aiohttp):
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            ImageBridgeError,
        )

        patch_aiohttp(_StubResponse(status=503, json_body={
            "error": {"message": "service unavailable"},
        }))
        bridge = self._make_bridge()
        with pytest.raises(ImageBridgeError) as exc:
            await bridge.generate("a cat")
        assert exc.value.code == "image_call_failed"

    async def test_empty_data_array_raises_empty_reply(self, patch_aiohttp):
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            ImageBridgeError,
        )

        patch_aiohttp(_StubResponse(status=200, json_body={"data": []}))
        bridge = self._make_bridge()
        with pytest.raises(ImageBridgeError) as exc:
            await bridge.generate("a cat")
        assert exc.value.code == "empty_image_reply"

    async def test_missing_b64_raises_empty_reply(self, patch_aiohttp):
        """Legacy DALL-E with response_format=url would hit this — we
        force b64_json so a missing field means truly nothing usable
        came back."""
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            ImageBridgeError,
        )

        patch_aiohttp(_StubResponse(status=200, json_body={
            "data": [{"url": "https://example.com/x.png"}],
        }))
        bridge = self._make_bridge()
        with pytest.raises(ImageBridgeError) as exc:
            await bridge.generate("a cat")
        assert exc.value.code == "empty_image_reply"

    async def test_timeout_raises_image_timeout(self, patch_aiohttp):
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            ImageBridgeError,
        )

        patch_aiohttp(asyncio.TimeoutError())
        bridge = self._make_bridge()
        with pytest.raises(ImageBridgeError) as exc:
            await bridge.generate("a cat")
        assert exc.value.code == "image_timeout"

    async def test_empty_prompt_raises_call_failed(self):
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            ImageBridgeError,
        )

        bridge = self._make_bridge()
        with pytest.raises(ImageBridgeError) as exc:
            await bridge.generate("   ")
        assert exc.value.code == "image_call_failed"


# ---------------------------------------------------------------------
# persist_generated_image
# ---------------------------------------------------------------------


class _StubFileStore:
    def __init__(self, *, save_raises: Exception | None = None,
                 delete_raises: Exception | None = None):
        self.saves: list[dict] = []
        self.deletes: list[str] = []
        self._save_raises = save_raises
        self._delete_raises = delete_raises

    async def save(self, *, storage_key, content, mime):
        if self._save_raises:
            raise self._save_raises
        self.saves.append({"storage_key": storage_key, "size": len(content), "mime": mime})

    async def delete(self, *, storage_key):
        if self._delete_raises:
            raise self._delete_raises
        self.deletes.append(storage_key)


class _StubStorage:
    def __init__(self, *, insert_raises: Exception | None = None,
                 commit_raises: Exception | None = None):
        self.inserted: list[dict] = []
        self.committed: list[list[str]] = []
        self._insert_raises = insert_raises
        self._commit_raises = commit_raises

    async def insert_file(self, **kwargs):
        if self._insert_raises:
            raise self._insert_raises
        self.inserted.append(kwargs)

    async def mark_files_committed(self, ids, *, now):
        if self._commit_raises:
            raise self._commit_raises
        self.committed.append(list(ids))


@pytest.mark.asyncio
class TestPersistGeneratedImage:
    def _make_result(self):
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            ImageResult,
        )

        return ImageResult(
            content=b"\x89PNG" + b"\x00" * 16,
            mime="image/png",
            prompt="a forest",
        )

    async def test_happy_path_inserts_and_commits(self):
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            persist_generated_image,
        )

        store = _StubFileStore()
        storage = _StubStorage()
        attachment = await persist_generated_image(
            storage=storage,
            file_store=store,
            token_name="alice",
            result=self._make_result(),
            now=1_700_000_000,
        )
        assert set(attachment.keys()) == {"file_id", "mime"}
        assert attachment["mime"] == "image/png"
        assert len(store.saves) == 1
        # file_store.save → storage.insert_file → mark_files_committed
        assert len(storage.inserted) == 1
        assert storage.inserted[0]["file_id"] == attachment["file_id"]
        assert storage.committed == [[attachment["file_id"]]]
        # storage_key follows the {token_name}/{file_id}{ext} convention
        assert store.saves[0]["storage_key"].startswith("alice/")
        assert store.saves[0]["storage_key"].endswith(".png")

    async def test_storage_failure_rolls_back_file_store(self):
        """If the DB write fails AFTER bytes landed on disk, the
        helper deletes the storage object so we don't leak the
        user's per-token quota on a row that won't exist for orphan
        GC to find."""
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            persist_generated_image,
        )

        store = _StubFileStore()
        storage = _StubStorage(insert_raises=RuntimeError("db down"))
        with pytest.raises(RuntimeError):
            await persist_generated_image(
                storage=storage,
                file_store=store,
                token_name="alice",
                result=self._make_result(),
                now=1_700_000_000,
            )
        # save landed, then the rollback deleted the same key.
        assert len(store.saves) == 1
        assert store.deletes == [store.saves[0]["storage_key"]]

    async def test_commit_failure_also_rolls_back(self):
        """mark_files_committed failing after insert means the row
        exists but is committed=0. The rollback delete still wipes
        the storage bytes so the orphan GC's sweep catches the row
        on the next iteration."""
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            persist_generated_image,
        )

        store = _StubFileStore()
        storage = _StubStorage(commit_raises=RuntimeError("commit failed"))
        with pytest.raises(RuntimeError):
            await persist_generated_image(
                storage=storage,
                file_store=store,
                token_name="alice",
                result=self._make_result(),
                now=1_700_000_000,
            )
        assert len(store.saves) == 1
        assert store.deletes == [store.saves[0]["storage_key"]]


# ---------------------------------------------------------------------
# ConfigView + schema integration
# ---------------------------------------------------------------------


@pytest.mark.usefixtures("tmp_data_dir")
class TestImageGenConfig:
    def test_defaults(self):
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        cfg = ConfigView.from_raw({})
        # Off by default — operator must opt in via the admin panel.
        assert cfg.image_gen.enabled is False
        assert cfg.image_gen.endpoint == "https://api.openai.com/v1"
        assert cfg.image_gen.api_key == ""
        assert cfg.image_gen.model == "dall-e-3"
        assert cfg.image_gen.size == "1024x1024"
        assert cfg.image_gen.timeout_seconds == 60

    def test_custom_config(self):
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        cfg = ConfigView.from_raw({
            "image_gen": {
                "enabled": True,
                "endpoint": "https://gateway.example.com/v1/",  # trailing slash stripped
                "api_key": "sk-xxx",
                "model": "gpt-image-1",
                "size": "1792x1024",
                "timeout_seconds": 30,
            },
        })
        assert cfg.image_gen.enabled is True
        assert cfg.image_gen.endpoint == "https://gateway.example.com/v1"
        assert cfg.image_gen.api_key == "sk-xxx"
        assert cfg.image_gen.model == "gpt-image-1"
        assert cfg.image_gen.size == "1792x1024"
        assert cfg.image_gen.timeout_seconds == 30

    def test_timeout_clamped(self):
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        cfg = ConfigView.from_raw({
            "image_gen": {"timeout_seconds": 99999},
        })
        assert cfg.image_gen.timeout_seconds == 600
        cfg = ConfigView.from_raw({
            "image_gen": {"timeout_seconds": 0},
        })
        assert cfg.image_gen.timeout_seconds == 5

    def test_schema_lists_image_gen_fields(self):
        """All six image_gen.* fields show up in the admin settings
        whitelist with the correct labels + secret flags."""
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            field_for_key,
        )

        for key in (
            "image_gen.enabled",
            "image_gen.endpoint",
            "image_gen.api_key",
            "image_gen.model",
            "image_gen.size",
            "image_gen.timeout_seconds",
        ):
            spec = field_for_key(key)
            assert spec is not None, f"{key!r} missing from whitelist"
            assert spec.section == "生图"
        # api_key is the only secret in the group.
        assert field_for_key("image_gen.api_key").secret is True
        assert field_for_key("image_gen.endpoint").secret is False

    def test_image_gen_fields_hot_reload(self):
        """All six image_gen.* fields must be restart_required=False
        — the bug they fix was 'operator enabled the feature, saved,
        but the chat kept returning image_disabled' because
        ImageBridge captured the boot-time empty api_key in closure.
        main.py's _reload_cfg now rebuilds the bridge from the live
        ConfigView and swaps it into ChatDeps, so a save should be
        immediately observable in /chat behaviour."""
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            field_for_key,
        )

        for key in (
            "image_gen.enabled",
            "image_gen.endpoint",
            "image_gen.api_key",
            "image_gen.model",
            "image_gen.size",
            "image_gen.timeout_seconds",
        ):
            spec = field_for_key(key)
            assert spec is not None
            assert spec.restart_required is False, (
                f"{key!r} is supposed to be hot-reloadable now"
            )


# ---------------------------------------------------------------------
# chat_provider_id override
# ---------------------------------------------------------------------


@pytest.mark.usefixtures("tmp_data_dir")
class TestChatProviderId:
    def test_default_empty(self):
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        cfg = ConfigView.from_raw({})
        assert cfg.chat_provider_id == ""

    def test_strips_whitespace(self):
        from astrbot_plugin_webchat_gateway.core.config import ConfigView

        cfg = ConfigView.from_raw({"chat_provider_id": "  my-provider  "})
        assert cfg.chat_provider_id == "my-provider"

    def test_in_settings_whitelist(self):
        from astrbot_plugin_webchat_gateway.core.settings_schema import (
            field_for_key,
        )

        spec = field_for_key("chat_provider_id")
        assert spec is not None
        assert spec.section == "对话行为"
        assert spec.type == "string"


@pytest.mark.asyncio
class TestLlmBridgeProviderOverride:
    async def test_override_takes_precedence(self):
        """When `chat_provider_id` is set in config, LlmBridge
        returns it verbatim and skips the AstrBot context lookup —
        which is the whole point of having the override."""
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        context_calls: list[str] = []

        class _Ctx:
            async def get_current_chat_provider_id(self, *, umo):
                context_calls.append(umo)
                return "fallback-provider"

        bridge = LlmBridge(
            _Ctx(),
            history_turns=4,
            persona_id="",
            chat_provider_id="custom-llm",
        )
        result = await bridge._resolve_provider_id(umo="webchat_gateway:alice:s1")
        assert result == "custom-llm"
        assert context_calls == [], (
            "override should short-circuit before the AstrBot lookup"
        )

    async def test_no_override_falls_back_to_context(self):
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        class _Ctx:
            async def get_current_chat_provider_id(self, *, umo):
                return "astrbot-default"

        bridge = LlmBridge(
            _Ctx(),
            history_turns=4,
            persona_id="",
            chat_provider_id="",  # empty
        )
        result = await bridge._resolve_provider_id(umo="webchat_gateway:alice:s1")
        assert result == "astrbot-default"

    async def test_whitespace_only_override_falls_back(self):
        """A config value that's just whitespace should be treated as
        unset, not as a literal provider id (which would never match
        anything and break the chat entirely)."""
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        class _Ctx:
            async def get_current_chat_provider_id(self, *, umo):
                return "astrbot-default"

        bridge = LlmBridge(
            _Ctx(),
            history_turns=4,
            persona_id="",
            chat_provider_id="   ",
        )
        result = await bridge._resolve_provider_id(umo="webchat_gateway:alice:s1")
        assert result == "astrbot-default"


# ---------------------------------------------------------------------
# /site exposes image_gen.enabled (live)
# ---------------------------------------------------------------------


def _make_site_deps(provider):
    from astrbot_plugin_webchat_gateway.handlers.site import SiteDeps

    return SiteDeps(
        site_name="Demo",
        welcome_message="",
        show_github_link=False,
        privacy_url="",
        site_icon_url="",
        theme_family="classic",
        allowed_origins={"*"},
        trust_referer_as_origin=False,
        uploads_enabled=True,
        uploads_max_file_size_mb=20,
        uploads_max_attachments_per_message=4,
        uploads_allowed_mime=("image/png",),
        image_gen_enabled_provider=provider,
    )


async def _site_client(deps):
    from astrbot_plugin_webchat_gateway.handlers.site import make_site_handlers

    handlers = make_site_handlers(deps)
    app = web.Application()
    app.router.add_get("/api/webchat/site", handlers["get_site"])
    server = TestServer(app)
    await server.start_server()
    client = TestClient(server)
    await client.start_server()
    return client, server


@pytest.mark.asyncio
class TestSiteImageGenFlag:
    async def test_image_gen_enabled_reflects_provider_true(self):
        deps = _make_site_deps(lambda: True)
        client, server = await _site_client(deps)
        try:
            resp = await client.get("/api/webchat/site")
            data = await resp.json()
        finally:
            await client.close()
            await server.close()
        assert data["image_gen"]["enabled"] is True

    async def test_image_gen_enabled_reflects_provider_false(self):
        deps = _make_site_deps(lambda: False)
        client, server = await _site_client(deps)
        try:
            resp = await client.get("/api/webchat/site")
            data = await resp.json()
        finally:
            await client.close()
            await server.close()
        assert data["image_gen"]["enabled"] is False

    async def test_provider_called_per_request(self):
        """The chat client polls /site to learn whether to show the
        生图 button; the value MUST be re-resolved per request so a
        hot-reload (operator saves new api_key in admin panel) is
        visible without a chat-page refresh."""
        calls = []

        def provider():
            calls.append(True)
            return len(calls) >= 2  # first call False, second True

        deps = _make_site_deps(provider)
        client, server = await _site_client(deps)
        try:
            r1 = await (await client.get("/api/webchat/site")).json()
            r2 = await (await client.get("/api/webchat/site")).json()
        finally:
            await client.close()
            await server.close()
        assert r1["image_gen"]["enabled"] is False
        assert r2["image_gen"]["enabled"] is True
        assert len(calls) == 2

    async def test_provider_exception_falls_back_to_false(self):
        """A broken provider must NOT 500 the public /site endpoint —
        it should degrade to image_gen.enabled=False so the chat
        client hides the button instead of crashing."""
        def provider():
            raise RuntimeError("bad provider")

        deps = _make_site_deps(provider)
        client, server = await _site_client(deps)
        try:
            resp = await client.get("/api/webchat/site")
            assert resp.status == 200
            data = await resp.json()
        finally:
            await client.close()
            await server.close()
        assert data["image_gen"]["enabled"] is False
