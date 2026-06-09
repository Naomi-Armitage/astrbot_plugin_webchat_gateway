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

    def post(self, url, *, json=None, data=None, headers=None):
        self.calls.append(
            {"url": url, "json": json, "data": data, "headers": headers}
        )
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _SeqStubSession:
    """ClientSession stub returning a SEQUENCE of responses across
    successive .post() calls (later calls clamp to the last entry).
    Exercises the upstream-5xx retry path."""

    def __init__(self, responses):
        self._responses = responses
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, *, json=None, data=None, headers=None):
        r = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        if isinstance(r, Exception):
            raise r
        return r


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

        session = patch_aiohttp(_StubResponse(status=503, json_body={
            "error": {"message": "service unavailable"},
        }))
        bridge = self._make_bridge()
        bridge._RETRY_BACKOFF_SECONDS = 0  # no real sleep in tests
        with pytest.raises(ImageBridgeError) as exc:
            await bridge.generate("a cat")
        assert exc.value.code == "image_call_failed"
        # 5xx is retried: initial attempt + 2 retries = 3 upstream POSTs.
        assert len(session.calls) == 3

    async def test_upstream_5xx_retried_then_succeeds(self, monkeypatch):
        from astrbot_plugin_webchat_gateway.core import image_bridge

        b64 = base64.b64encode(b"\x89PNG").decode()
        seq = _SeqStubSession([
            _StubResponse(status=500, json_body={"error": {"message": "no available channel"}}),
            _StubResponse(status=200, json_body={"data": [{"b64_json": b64}]}),
        ])
        monkeypatch.setattr(
            image_bridge.aiohttp, "ClientSession", lambda *a, **k: seq
        )
        bridge = self._make_bridge()
        bridge._RETRY_BACKOFF_SECONDS = 0
        result = await bridge.generate("a cat")
        assert result.content  # succeeded on the retry
        assert seq.calls == 2  # one 500 + one 200

    async def test_upstream_4xx_not_retried(self, monkeypatch):
        from astrbot_plugin_webchat_gateway.core import image_bridge
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            ImageBridgeError,
        )

        seq = _SeqStubSession([
            _StubResponse(status=400, json_body={"error": {"message": "bad params"}}),
        ])
        monkeypatch.setattr(
            image_bridge.aiohttp, "ClientSession", lambda *a, **k: seq
        )
        bridge = self._make_bridge()
        bridge._RETRY_BACKOFF_SECONDS = 0
        with pytest.raises(ImageBridgeError) as exc:
            await bridge.generate("a cat")
        assert exc.value.code == "image_call_failed"
        assert seq.calls == 1  # 4xx is NOT retried

    async def test_empty_data_array_raises_empty_reply(self, patch_aiohttp):
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            ImageBridgeError,
        )

        patch_aiohttp(_StubResponse(status=200, json_body={"data": []}))
        bridge = self._make_bridge()
        with pytest.raises(ImageBridgeError) as exc:
            await bridge.generate("a cat")
        assert exc.value.code == "empty_image_reply"

    async def test_missing_b64_and_url_raises_empty_reply(self, patch_aiohttp):
        """When upstream returns a data entry without `b64_json` AND
        without a download `url`, treat as empty — the response is
        structurally well-formed but carries nothing renderable."""
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            ImageBridgeError,
        )

        patch_aiohttp(_StubResponse(status=200, json_body={
            "data": [{"revised_prompt": "a cat sitting"}],  # no image
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

    async def test_gpt_image_model_omits_response_format(self, patch_aiohttp):
        """GPT-image-* models reject `response_format=b64_json` with
        400 Unknown parameter — they only ever return b64 anyway. The
        bridge must skip the field for that family. Verified by
        inspecting the captured request body, not just by happy-path
        success: an upstream that accepts the field but ignores it
        would also make the test pass for the wrong reason."""
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
        b64 = base64.b64encode(png).decode()
        session = patch_aiohttp(_StubResponse(status=200, json_body={
            "data": [{"b64_json": b64}],
        }))
        bridge = self._make_bridge(model="gpt-image-1")
        await bridge.generate("a cat")
        call_body = session.calls[0]["json"]
        assert "response_format" not in call_body, (
            "gpt-image-* models reject response_format; bridge must skip it"
        )
        # And the rest of the payload still looks right.
        assert call_body["model"] == "gpt-image-1"
        assert call_body["prompt"] == "a cat"

    async def test_dall_e_model_still_sends_response_format(self, patch_aiohttp):
        png = b"\x89PNG"
        b64 = base64.b64encode(png).decode()
        session = patch_aiohttp(_StubResponse(status=200, json_body={
            "data": [{"b64_json": b64}],
        }))
        bridge = self._make_bridge(model="dall-e-3")
        await bridge.generate("a cat")
        assert session.calls[0]["json"]["response_format"] == "b64_json"

    @pytest.mark.parametrize(
        "model",
        [
            "gpt-image-1",
            "gpt-image-2",
            "gpt-image-pro",
            "GPT-Image-2",  # case-insensitive
            "openai/gpt-image-2",  # gateway-prefixed
        ],
    )
    async def test_gpt_image_family_all_skip_response_format(
        self, patch_aiohttp, model
    ):
        """The detection is `"gpt-image" in model.lower()`, so the
        whole family (current gpt-image-1 / gpt-image-2 / hypothetical
        gpt-image-pro / case variants / gateway prefixes) classifies
        correctly. Parametrise so a future model name in the same
        family doesn't accidentally regress."""
        png = b"\x89PNG"
        b64 = base64.b64encode(png).decode()
        session = patch_aiohttp(_StubResponse(status=200, json_body={
            "data": [{"b64_json": b64}],
        }))
        bridge = self._make_bridge(model=model)
        await bridge.generate("a cat")
        assert "response_format" not in session.calls[0]["json"], (
            f"model={model!r} is a gpt-image variant; should skip response_format"
        )

    async def test_upstream_error_message_propagates(self, patch_aiohttp):
        """The 400 body's `error.message` is the actionable signal for
        operators — bridge must surface it so the chat audit row + the
        UI bubble can show "upstream 400: Unknown parameter" instead
        of an opaque image_call_failed."""
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            ImageBridgeError,
        )

        patch_aiohttp(_StubResponse(status=400, json_body={
            "error": {"message": "Unknown parameter: 'response_format'"},
        }))
        bridge = self._make_bridge(model="gpt-image-1")
        # Use a custom call site that bypasses the response_format
        # skip (the previous test confirms that's working) — we want
        # to verify the error-propagation path itself. Easiest: send
        # the request and check that the raised exception's string
        # carries the upstream message.
        with pytest.raises(ImageBridgeError) as exc:
            await bridge.generate("a cat")
        assert exc.value.code == "image_call_failed"
        assert "Unknown parameter" in str(exc.value)


# ---------------------------------------------------------------------
# ImageBridge.edit (img2img via /images/edits)
# ---------------------------------------------------------------------


@pytest.mark.asyncio
class TestImageBridgeEdit:
    def _make_bridge(self, **overrides):
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            ImageBridge,
        )

        defaults = {
            "enabled": True,
            "endpoint": "https://api.openai.com/v1",
            "api_key": "sk-test-1234",
            "model": "gpt-image-1",
            "size": "1024x1024",
            "timeout_seconds": 30,
            "img2img": True,
        }
        defaults.update(overrides)
        return ImageBridge(**defaults)

    @staticmethod
    def _field_names(form):
        # aiohttp.FormData stores fields as (type_options, headers, value);
        # the field name lives in type_options["name"].
        names = []
        for type_options, _headers, _value in form._fields:
            names.append(type_options.get("name"))
        return names

    async def test_edit_disabled_when_img2img_off(self, patch_aiohttp):
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            ImageBridgeError,
        )

        # Feature on, but img2img opt-in off → edit_enabled False → refuse.
        bridge = self._make_bridge(img2img=False)
        assert bridge.enabled is True
        assert bridge.edit_enabled is False
        with pytest.raises(ImageBridgeError) as exc:
            await bridge.edit("make it watercolor", b"\x89PNG...", "image/png")
        assert exc.value.code == "image_disabled"

    async def test_edit_request_shape(self, patch_aiohttp):
        # 1x1 PNG, base64.
        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4"
            "nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII="
        )
        session = patch_aiohttp(
            _StubResponse(status=200, json_body={"data": [{"b64_json": png_b64}]})
        )
        bridge = self._make_bridge()
        result = await bridge.edit("make it watercolor", b"rawbytes", "image/png")
        assert result.content  # decoded bytes
        call = session.calls[0]
        # Multipart, not JSON, and aimed at the edits endpoint.
        assert call["url"].endswith("/images/edits")
        assert call["json"] is None
        assert call["data"] is not None
        # The reference image rides as the `image` form field.
        assert "image" in self._field_names(call["data"])
        # Authorization carried; Content-Type left to FormData's boundary.
        assert call["headers"].get("Authorization") == "Bearer sk-test-1234"
        assert "Content-Type" not in call["headers"]

    async def test_edit_empty_input_image_raises(self, patch_aiohttp):
        from astrbot_plugin_webchat_gateway.core.image_bridge import (
            ImageBridgeError,
        )

        patch_aiohttp(_StubResponse(status=200, json_body={"data": []}))
        bridge = self._make_bridge()
        with pytest.raises(ImageBridgeError) as exc:
            await bridge.edit("prompt", b"", "image/png")
        assert exc.value.code == "image_call_failed"

    async def test_edit_dalle2_sends_response_format(self, patch_aiohttp):
        # Non-gpt-image edit-capable model (dall-e-2) keeps response_format.
        png_b64 = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4"
            "nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII="
        )
        session = patch_aiohttp(
            _StubResponse(status=200, json_body={"data": [{"b64_json": png_b64}]})
        )
        bridge = self._make_bridge(model="dall-e-2")
        await bridge.edit("prompt", b"rawbytes", "image/png")
        assert "response_format" in self._field_names(session.calls[0]["data"])


# ---------------------------------------------------------------------
# resolve_size / resolve_size_for_reference (per-request aspect ratio)
# ---------------------------------------------------------------------


def _png_bytes(width: int, height: int) -> bytes:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _bridge_for(model: str, size: str = "1024x1024", img2img: bool = False):
    from astrbot_plugin_webchat_gateway.core.image_bridge import ImageBridge

    return ImageBridge(
        enabled=True,
        endpoint="https://api.openai.com/v1",
        api_key="sk-test",
        model=model,
        size=size,
        timeout_seconds=30,
        img2img=img2img,
    )


class TestResolveSize:
    def test_gpt_image_aspect_tokens(self):
        b = _bridge_for("gpt-image-1")
        assert b.resolve_size("1:1") == "1024x1024"
        assert b.resolve_size("16:9") == "1536x1024"
        assert b.resolve_size("9:16") == "1024x1536"
        assert b.resolve_size("landscape") == "1536x1024"
        assert b.resolve_size("portrait") == "1024x1536"

    def test_gpt_image_auto_allowed(self):
        assert _bridge_for("gpt-image-1").resolve_size("auto") == "auto"

    def test_dalle_aspect_tokens(self):
        b = _bridge_for("dall-e-3")
        assert b.resolve_size("16:9") == "1792x1024"
        assert b.resolve_size("9:16") == "1024x1792"
        assert b.resolve_size("1:1") == "1024x1024"

    def test_dalle_rejects_auto_falls_back(self):
        # auto is gpt-image-only; dall-e falls back to the default.
        assert _bridge_for("dall-e-3").resolve_size("auto") == "1024x1024"

    def test_concrete_size_passthrough(self):
        assert _bridge_for("dall-e-3").resolve_size("1792x1024") == "1792x1024"

    def test_garbage_and_none_fall_back_to_default(self):
        b = _bridge_for("gpt-image-1", size="1536x1024")
        assert b.resolve_size("garbage") == "1536x1024"
        assert b.resolve_size(None) == "1536x1024"
        assert b.resolve_size("") == "1536x1024"

    def test_default_invalid_for_model_falls_back_1024(self):
        # DALL-E size left in config but model is gpt-image → 1024x1024.
        b = _bridge_for("gpt-image-1", size="1792x1024")
        assert b.resolve_size(None) == "1024x1024"
        assert b.resolve_size("nonsense") == "1024x1024"

    def test_never_raises(self):
        b = _bridge_for("gpt-image-1")
        for v in (None, "", "  ", "abc", "1x", "x1", "999x999", "16:9", "auto"):
            out = b.resolve_size(v)
            assert isinstance(out, str) and out


class TestResolveSizeForReference:
    def test_landscape_reference_gpt_image(self):
        b = _bridge_for("gpt-image-1", img2img=True)
        assert b.resolve_size_for_reference(_png_bytes(1600, 900)) == "1536x1024"

    def test_portrait_reference_gpt_image(self):
        b = _bridge_for("gpt-image-1", img2img=True)
        assert b.resolve_size_for_reference(_png_bytes(900, 1600)) == "1024x1536"

    def test_square_reference(self):
        b = _bridge_for("gpt-image-1", img2img=True)
        assert b.resolve_size_for_reference(_png_bytes(512, 512)) == "1024x1024"

    def test_landscape_reference_dalle(self):
        b = _bridge_for("dall-e-2", img2img=True)
        assert b.resolve_size_for_reference(_png_bytes(1920, 1080)) == "1792x1024"

    def test_unreadable_returns_none(self):
        b = _bridge_for("gpt-image-1", img2img=True)
        assert b.resolve_size_for_reference(b"not an image") is None
        assert b.resolve_size_for_reference(b"") is None


@pytest.mark.asyncio
class TestPerRequestSize:
    async def test_generate_resolved_size_in_request_and_result(self, patch_aiohttp):
        b64 = base64.b64encode(b"\x89PNG").decode()
        session = patch_aiohttp(
            _StubResponse(status=200, json_body={"data": [{"b64_json": b64}]})
        )
        bridge = _bridge_for("gpt-image-1", size="1024x1024")
        result = await bridge.generate("a cat", size="16:9")
        assert session.calls[0]["json"]["size"] == "1536x1024"
        assert result.size == "1536x1024"

    async def test_generate_default_when_no_size(self, patch_aiohttp):
        b64 = base64.b64encode(b"\x89PNG").decode()
        session = patch_aiohttp(
            _StubResponse(status=200, json_body={"data": [{"b64_json": b64}]})
        )
        bridge = _bridge_for("dall-e-3", size="1792x1024")
        result = await bridge.generate("a forest")  # no size → operator default
        assert session.calls[0]["json"]["size"] == "1792x1024"
        assert result.size == "1792x1024"

    async def test_edit_prefers_reference_aspect_over_request(self, patch_aiohttp):
        b64 = base64.b64encode(b"\x89PNG").decode()
        patch_aiohttp(
            _StubResponse(status=200, json_body={"data": [{"b64_json": b64}]})
        )
        bridge = _bridge_for("gpt-image-1", size="1024x1024", img2img=True)
        # Landscape reference + a square request → reference wins.
        result = await bridge.edit(
            "make it watercolor", _png_bytes(1600, 900), "image/png", size="1:1"
        )
        assert result.size == "1536x1024"


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
        # Default 180s matches the reference plugin and OpenAI's own
        # SDK; the previous 60s default tripped legitimate gpt-image
        # / high-detail generations that legitimately take 100-180s.
        assert cfg.image_gen.timeout_seconds == 180

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
        assert cfg.image_gen.timeout_seconds == 1800
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
        """When `chat_provider_id` is set in config AND the provider
        still exists in the AstrBot context, LlmBridge returns it
        verbatim and skips the global lookup — which is the whole
        point of having the override."""
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        context_calls: list[str] = []

        class _Ctx:
            def get_provider_by_id(self, provider_id):
                # Override resolves to a live provider — return any
                # truthy sentinel since the bridge only checks identity
                # against None.
                return object() if provider_id == "custom-llm" else None

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
            "override should short-circuit before the global lookup"
        )

    async def test_no_override_falls_back_to_context(self):
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        class _Ctx:
            def get_provider_by_id(self, provider_id):
                return None  # nothing pinned, this should not be called

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
            def get_provider_by_id(self, provider_id):
                return None

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


@pytest.mark.asyncio
class TestLlmBridgeProviderFallback:
    """Regression: configured primary / fallback / global chain.

    Pinned `chat_provider_id` used to be returned verbatim with no
    existence check, so a provider that got deleted / disabled /
    renamed in AstrBot hard-failed every chat with
    `chat_provider_not_configured` (502). The bridge now validates
    via `context.get_provider_by_id` and steps down through the
    `chat_fallback_provider_id` middle tier before landing on the
    bot's global default. Each downward step warns once per
    (provider_id, role) per process so log noise stays bounded.
    """

    async def test_primary_missing_falls_back_to_secondary(self, caplog):
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        global_calls: list[str] = []

        class _Ctx:
            def get_provider_by_id(self, provider_id):
                # primary gone, fallback present
                return object() if provider_id == "fb-provider" else None

            async def get_current_chat_provider_id(self, *, umo):
                global_calls.append(umo)
                return "astrbot-global"

        bridge = LlmBridge(
            _Ctx(),
            history_turns=4,
            persona_id="",
            chat_provider_id="primary-gone",
            chat_fallback_provider_id="fb-provider",
        )
        with caplog.at_level("WARNING"):
            result = await bridge._resolve_provider_id(
                umo="webchat_gateway:alice:s1"
            )
        assert result == "fb-provider"
        assert global_calls == [], (
            "fallback resolved → must not consult the bot global"
        )
        # First miss for primary logs ONE warning carrying both fields.
        primary_warnings = [
            r for r in caplog.records
            if "primary-gone" in r.getMessage() and "primary" in r.getMessage()
        ]
        assert len(primary_warnings) == 1

    async def test_both_pinned_missing_falls_back_to_global(self, caplog):
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        class _Ctx:
            def get_provider_by_id(self, provider_id):
                return None  # neither pinned provider exists

            async def get_current_chat_provider_id(self, *, umo):
                return "astrbot-global"

        bridge = LlmBridge(
            _Ctx(),
            history_turns=4,
            persona_id="",
            chat_provider_id="primary-gone",
            chat_fallback_provider_id="fallback-gone",
        )
        with caplog.at_level("WARNING"):
            result = await bridge._resolve_provider_id(
                umo="webchat_gateway:alice:s1"
            )
        assert result == "astrbot-global"
        msgs = [r.getMessage() for r in caplog.records]
        assert any("primary-gone" in m and "primary" in m for m in msgs)
        assert any("fallback-gone" in m and "fallback" in m for m in msgs)

    async def test_missing_provider_warning_is_logged_once(self, caplog):
        """A misconfigured override that survives across many chats
        should NOT spam one WARNING per request — `_resolve_provider_id`
        runs per chat call. First miss per (id, role) logs; the rest
        are silent."""
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        class _Ctx:
            def get_provider_by_id(self, provider_id):
                return None

            async def get_current_chat_provider_id(self, *, umo):
                return "astrbot-global"

        bridge = LlmBridge(
            _Ctx(),
            history_turns=4,
            persona_id="",
            chat_provider_id="primary-gone",
        )
        with caplog.at_level("WARNING"):
            for _ in range(5):
                await bridge._resolve_provider_id(
                    umo="webchat_gateway:alice:s1"
                )
        warns = [
            r for r in caplog.records
            if "primary-gone" in r.getMessage()
        ]
        assert len(warns) == 1, (
            "configured-but-missing provider should warn ONCE per process, "
            f"got {len(warns)} warnings across 5 chat calls"
        )

    async def test_all_missing_returns_none(self):
        """When the pinned providers are gone AND the bot has no global
        default wired either, `_resolve_provider_id` returns None and
        the caller raises `chat_provider_not_configured`."""
        from astrbot_plugin_webchat_gateway.core.llm_bridge import LlmBridge

        class _Ctx:
            def get_provider_by_id(self, provider_id):
                return None

            async def get_current_chat_provider_id(self, *, umo):
                return None

        bridge = LlmBridge(
            _Ctx(),
            history_turns=4,
            persona_id="",
            chat_provider_id="primary-gone",
            chat_fallback_provider_id="fallback-gone",
        )
        result = await bridge._resolve_provider_id(umo="webchat_gateway:alice:s1")
        assert result is None


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
