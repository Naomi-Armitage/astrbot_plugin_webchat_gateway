"""OpenAI-compatible image generation bridge.

The chat path's text flow goes through ``LlmBridge`` and AstrBot's
provider abstraction; image generation is a separate request shape
(POST {endpoint}/images/generations) without an equivalent provider
abstraction in AstrBot today. Rather than couple to a future AstrBot
image-provider API that may or may not land, the bridge here calls
the OpenAI v1/images endpoint directly via aiohttp.ClientSession.

Trigger surface: the chat handlers detect a ``/image`` / ``/draw`` /
``/img`` prefix in the user message and route to ``generate`` instead
of ``LlmBridge.generate_reply_stream``. The composer in the chat
client has a 生成图片 button that prepends the prefix so end-users
don't need to memorise the syntax.

Wire contract: returns ``ImageResult(bytes, mime)``. The caller is
responsible for persisting the bytes through ``FileStore.save`` and
inserting the ``webchat_files`` row.

Failure surface: every transport / auth / API failure raises
``ImageBridgeError(code: str)`` whose ``code`` matches the canonical
``image_*`` audit events (``image_failed`` /
``image_timeout`` / ``image_disabled``). The chat handler catches and
maps to the SSE error frame so the wire taxonomy stays consistent
with the existing ``llm_*`` codes.
"""

from __future__ import annotations

import asyncio
import base64
import re
import secrets
from dataclasses import dataclass
from typing import Any

import aiohttp

from astrbot.api import logger

from .image_util import detect_image_size


_PREFIX_PATTERN = re.compile(r"^\s*/(?:image|img|draw)\b\s*", re.IGNORECASE)

# Cap on the prompt sent to the image API. The text models cap their
# context at `max_message_length`, but image gen costs are per-call,
# not per-token, so we keep the same limit for UX consistency rather
# than a tighter one. OpenAI itself caps DALL-E 3 at 4000 chars.
_DEFAULT_PROMPT_CAP = 4000


# Per-model-family output size allow-lists + aspect→size maps. The
# `_is_gpt_image_model` predicate selects the family; everything else
# uses the DALL-E set. `resolve_size` validates a per-request size/aspect
# against these so an unsupported value can never reach the upstream API
# (which would 400). `auto` is gpt-image-only.
_GPT_IMAGE_SIZES = ("auto", "1024x1024", "1536x1024", "1024x1536")
_DALLE_SIZES = ("1024x1024", "1792x1024", "1024x1792")
_GPT_IMAGE_ASPECT = {
    "1:1": "1024x1024",
    "3:2": "1536x1024", "16:9": "1536x1024", "landscape": "1536x1024",
    "2:3": "1024x1536", "9:16": "1024x1536", "portrait": "1024x1536",
}
_DALLE_ASPECT = {
    "1:1": "1024x1024",
    "16:9": "1792x1024", "3:2": "1792x1024", "landscape": "1792x1024",
    "9:16": "1024x1792", "2:3": "1024x1792", "portrait": "1024x1792",
}

# OpenAI's gpt-image /images/edits accepts an array of up to 16 input
# images; dall-e-2 takes exactly one. `max_reference_images` exposes this
# ceiling per model family so the client + server can cap img2img uploads.
_GPT_IMAGE_MAX_REFS = 16


def is_image_command(message: str) -> bool:
    """Return True if `message` opens with a recognised image-gen prefix."""
    return bool(_PREFIX_PATTERN.match(message or ""))


def strip_image_prefix(message: str) -> str:
    """Remove the leading /image|/img|/draw token + whitespace.

    Returns the bare prompt. If the message doesn't match the prefix,
    returns the message unchanged. Callers should pair this with
    ``is_image_command`` so an empty result is treated as "user typed
    only the trigger with no prompt" rather than "trigger absent".
    """
    return _PREFIX_PATTERN.sub("", message or "", count=1).strip()


class ImageBridgeError(RuntimeError):
    """Wire-stable error for the image generation bridge.

    ``code`` matches the audit event taxonomy:
      * ``image_disabled``   — operator turned the feature off
      * ``image_timeout``    — request exceeded ``timeout_seconds``
      * ``image_call_failed`` — upstream returned non-2xx OR raised
      * ``empty_image_reply`` — upstream returned 2xx but no usable bytes
    Caller (chat handler) maps these to user-visible error codes and
    audit rows verbatim.
    """

    def __init__(
        self, code: str, message: str = "", *, status: int | None = None
    ) -> None:
        super().__init__(message or code)
        self.code = code
        # Upstream HTTP status when the failure came from the image API
        # (None for local / transport failures). Used to decide whether a
        # transient 5xx is worth retrying.
        self.status = status


@dataclass(frozen=True)
class ImageResult:
    """Single generated image. The bridge only ever returns one — the
    OpenAI API accepts an ``n`` parameter but we deliberately don't
    expose it: a chat turn produces a chat turn, and N attachments per
    assistant message would have to fight CM history for ordering.
    """

    content: bytes
    mime: str  # always "image/png" for OpenAI image models today
    prompt: str  # echoed back so the audit event records what was asked
    size: str = ""  # effective size sent upstream (per-request resolved)


class ImageBridge:
    """Thin async client for the OpenAI ``/v1/images/generations`` API.

    Operator-configured (via the admin panel's image gen section):
      * ``endpoint`` — base URL, e.g. ``https://api.openai.com/v1``
      * ``api_key`` — Bearer token
      * ``model`` — ``dall-e-3`` / ``gpt-image-1`` / etc.
      * ``size`` — ``1024x1024`` etc.
      * ``timeout_seconds`` — total request timeout
    """

    def __init__(
        self,
        *,
        enabled: bool,
        endpoint: str,
        api_key: str,
        model: str,
        size: str,
        timeout_seconds: float,
        img2img: bool = False,
    ) -> None:
        self._enabled = bool(enabled)
        self._endpoint = (endpoint or "").rstrip("/")
        self._api_key = api_key or ""
        self._model = model or "dall-e-3"
        self._size = size or "1024x1024"
        self._timeout_seconds = float(timeout_seconds) if timeout_seconds else 60.0
        self._img2img = bool(img2img)

    @property
    def enabled(self) -> bool:
        # An ImageBridge is only "live" when the operator turned the
        # feature on AND wired both endpoint + api_key. Missing either
        # would let a half-configured deployment 500 on the first
        # /image attempt — surface as ``image_disabled`` instead.
        return self._enabled and bool(self._endpoint) and bool(self._api_key)

    @property
    def img2img(self) -> bool:
        return self._img2img

    @property
    def edit_enabled(self) -> bool:
        # img2img is a strict subset of enabled: the operator must have
        # turned image-gen on, wired endpoint+key, AND opted into the
        # edits path (which requires an edits-capable model).
        return self.enabled and self._img2img

    @property
    def max_reference_images(self) -> int:
        """Upper bound on reference images per img2img request — surfaced
        via /site so the client caps uploads. 0 when img2img is off; 1 for
        non-gpt-image edit models (dall-e-2 /images/edits takes a single
        image); the OpenAI gpt-image array ceiling otherwise. The server
        and client further clamp this to the per-message attachment cap."""
        if not self.edit_enabled:
            return 0
        return _GPT_IMAGE_MAX_REFS if self._is_gpt_image_model else 1

    @property
    def model(self) -> str:
        return self._model

    @property
    def size(self) -> str:
        return self._size

    @property
    def _is_gpt_image_model(self) -> bool:
        # Normalise separators + case so shorthand gateway names
        # (`gpt-img2`, `gpt-img-2`, `gpt_image_2`, `GPT-Image`) classify
        # with the canonical `gpt-image-1/2`. A plain `"gpt-image" in
        # name` substring misses `gpt-img2` → the model gets treated as
        # DALL-E, offered DALL-E-only sizes via /site, and wrongly sent
        # `response_format` upstream (400). Stays negative for `dall-e-*`
        # and unrelated providers such as `gemini-image`.
        norm = re.sub(r"[^a-z0-9]", "", (self._model or "").lower())
        return "gptimage" in norm or "gptimg" in norm

    def _allowed_sizes(self) -> tuple[str, ...]:
        return _GPT_IMAGE_SIZES if self._is_gpt_image_model else _DALLE_SIZES

    def _aspect_map(self) -> dict[str, str]:
        return _GPT_IMAGE_ASPECT if self._is_gpt_image_model else _DALLE_ASPECT

    def allowed_sizes(self) -> list[str]:
        """Public: the concrete sizes (+ ``auto`` for gpt-image) the
        current model accepts. Surfaced via /site so the UI ratio
        selector only offers valid choices for the configured model."""
        return list(self._allowed_sizes())

    def _default_size(self) -> str:
        """Operator default, normalised + validated for the current
        model. Falls back to 1024x1024 if the configured size isn't
        valid for this model family (e.g. a DALL-E size left in config
        after switching to gpt-image)."""
        s = (self._size or "").strip().lower()
        if s in self._allowed_sizes():
            return s
        return "1024x1024"

    def resolve_size(self, requested: str | None) -> str:
        """Resolve a per-request size/aspect to a concrete, model-valid
        size string. NEVER raises — any unsupported / malformed value
        falls back to the operator default (then 1024x1024). Accepts a
        concrete size (``1536x1024``), an aspect token (``16:9`` /
        ``portrait`` / ``1:1``), or ``auto`` (gpt-image only)."""
        req = (requested or "").strip().lower()
        if not req:
            return self._default_size()
        if req in self._allowed_sizes():
            return req
        mapped = self._aspect_map().get(req)
        if mapped:
            return mapped
        return self._default_size()

    def resolve_size_for_reference(self, image_bytes: bytes) -> str | None:
        """Map a reference image's aspect ratio to the closest supported
        output size (img2img: keep the original proportions). Returns
        None if the dimensions can't be read, so the caller can fall
        back to ``resolve_size``."""
        dims = detect_image_size(image_bytes)
        if not dims:
            return None
        width, height = dims
        if height <= 0:
            return None
        target = width / height
        best: str | None = None
        best_diff: float | None = None
        for s in self._allowed_sizes():
            if s == "auto":
                continue
            try:
                sw, sh = (int(p) for p in s.split("x"))
            except (ValueError, AttributeError):
                continue
            if sh <= 0:
                continue
            diff = abs((sw / sh) - target)
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best = s
        return best

    def _clean_prompt(self, prompt: str) -> str:
        cleaned = (prompt or "").strip()
        if not cleaned:
            raise ImageBridgeError("image_call_failed", "empty prompt")
        if len(cleaned) > _DEFAULT_PROMPT_CAP:
            cleaned = cleaned[:_DEFAULT_PROMPT_CAP]
        return cleaned

    # Transient upstream 5xx — e.g. a gateway's "no available channel"
    # when it round-robins onto a momentarily-dead channel — usually
    # succeeds on a retry that lands on a healthy channel. Retry ONLY
    # 5xx: 4xx (bad params), timeouts (slow; retrying compounds latency)
    # and empty replies are not retried. A 5xx returns fast, so a couple
    # of retries add seconds, not minutes. Failed attempts charge no
    # quota (the chat handler only counts a successful generation).
    _MAX_UPSTREAM_RETRIES = 2
    _RETRY_BACKOFF_SECONDS = 0.8

    async def _post_with_retry(self, attempt) -> "ImageResult":
        """Run one upstream attempt, retrying transient 5xx failures.

        ``attempt`` is a zero-arg coroutine function performing a single
        POST + response parse that returns an ImageResult or raises
        ImageBridgeError. Only ImageBridgeError with ``status >= 500`` is
        retried; everything else propagates immediately.
        """
        last: ImageBridgeError | None = None
        for i in range(self._MAX_UPSTREAM_RETRIES + 1):
            try:
                return await attempt()
            except ImageBridgeError as exc:
                if (
                    exc.status is not None
                    and exc.status >= 500
                    and i < self._MAX_UPSTREAM_RETRIES
                ):
                    last = exc
                    logger.warning(
                        "[WebChatGateway] image upstream %s; retry %d/%d",
                        exc.status, i + 1, self._MAX_UPSTREAM_RETRIES,
                    )
                    await asyncio.sleep(self._RETRY_BACKOFF_SECONDS * (i + 1))
                    continue
                raise
        assert last is not None  # loop exits only via return or raise
        raise last

    async def _read_image_response(
        self,
        session: aiohttp.ClientSession,
        resp: aiohttp.ClientResponse,
        prompt: str,
        size: str,
    ) -> ImageResult:
        # Shared response handler for both /images/generations (generate)
        # and /images/edits (edit): identical envelope (data[0].b64_json or
        # .url) and identical error taxonomy. `session` is needed for the
        # URL-download fallback. Must be awaited inside the open session.
        status = resp.status
        try:
            payload = await resp.json(content_type=None)
        except Exception:
            payload = None
        if status >= 400:
            # Surface upstream's error message into the exception so the
            # chat audit row captures what actually went wrong (auth, bad
            # params, rate limit, etc.) instead of a generic
            # ``image_call_failed``. Operators reading the audit log can
            # grep for the upstream code.
            msg = ""
            if isinstance(payload, dict):
                err = payload.get("error")
                if isinstance(err, dict):
                    msg = str(err.get("message") or "")[:200]
                elif isinstance(err, str):
                    msg = err[:200]
            logger.warning(
                "[WebChatGateway] image upstream %d model=%s: %s",
                status,
                self._model,
                msg or "(no error message)",
            )
            raise ImageBridgeError(
                "image_call_failed",
                f"upstream {status}: {msg or self._model}",
                status=status,
            )
        if not isinstance(payload, dict):
            raise ImageBridgeError(
                "image_call_failed", "malformed upstream response"
            )
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise ImageBridgeError("empty_image_reply")
        item = data[0] if isinstance(data[0], dict) else None
        if not item:
            raise ImageBridgeError("empty_image_reply")
        b64 = item.get("b64_json")
        if isinstance(b64, str) and b64:
            try:
                raw = base64.b64decode(b64, validate=False)
            except (ValueError, TypeError) as exc:
                raise ImageBridgeError(
                    "image_call_failed", "b64 decode failed"
                ) from exc
            if not raw:
                raise ImageBridgeError("empty_image_reply")
            return ImageResult(content=raw, mime="image/png", prompt=prompt, size=size)
        # Some OpenAI-compatible gateways (and DALL-E without
        # `response_format=b64_json`) only return a URL pointing at the
        # rendered image. Fetch the bytes ourselves so the rest of the
        # pipeline (FileStore save + webchat_files insert) doesn't need to
        # know about that variant.
        download_url = item.get("url")
        if isinstance(download_url, str) and download_url:
            try:
                async with session.get(download_url) as dl:
                    if dl.status >= 400:
                        raise ImageBridgeError(
                            "image_call_failed",
                            f"image download {dl.status}",
                        )
                    raw = await dl.read()
                    if not raw:
                        raise ImageBridgeError("empty_image_reply")
                    # Trust upstream Content-Type if it's a sensible
                    # image/* string; fall back to png otherwise (DALL-E
                    # URLs are PNG in practice).
                    ct = dl.headers.get("Content-Type", "").split(
                        ";", 1
                    )[0].strip().lower()
                    mime = ct if ct.startswith("image/") else "image/png"
                    return ImageResult(content=raw, mime=mime, prompt=prompt, size=size)
            except aiohttp.ClientError as exc:
                raise ImageBridgeError(
                    "image_call_failed",
                    f"image download client error: {exc}",
                ) from exc
        raise ImageBridgeError("empty_image_reply")

    async def generate(self, prompt: str, size: str | None = None) -> ImageResult:
        if not self.enabled:
            raise ImageBridgeError("image_disabled")
        cleaned = self._clean_prompt(prompt)
        eff_size = self.resolve_size(size)
        url = f"{self._endpoint}/images/generations"
        # `response_format` is NOT accepted by GPT-image-* models —
        # OpenAI explicitly returns 400 "Unknown parameter" for those.
        # Only DALL-E 2/3 accept the field, and they default to URL,
        # so we force `b64_json` only when we know it's safe. Family
        # detection lives in `_is_gpt_image_model`, which normalises the
        # name so `dall-e-3`, `gpt-image-1`, shorthand `gpt-img2`, and
        # unrelated `gemini-image` all classify correctly. It is a
        # superset of the reference plugin's plain-substring check
        # (Railgun19457/astrbot_plugin_image_generation), additionally
        # tolerating the `gpt-img*` shorthands some gateways expose.
        is_gpt_image = self._is_gpt_image_model
        body: dict[str, Any] = {
            "model": self._model,
            "prompt": cleaned,
            "n": 1,
            "size": eff_size,
        }
        if not is_gpt_image:
            body["response_format"] = "b64_json"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)

        async def _attempt() -> ImageResult:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=body, headers=headers) as resp:
                    return await self._read_image_response(
                        session, resp, cleaned, eff_size
                    )

        try:
            return await self._post_with_retry(_attempt)
        except asyncio.TimeoutError as exc:
            raise ImageBridgeError("image_timeout") from exc
        except aiohttp.ClientError as exc:
            logger.warning(
                "[WebChatGateway] image gen client error: %s", exc
            )
            raise ImageBridgeError(
                "image_call_failed", f"client error: {exc}"
            ) from exc

    async def edit(
        self,
        prompt: str,
        images: list[tuple[bytes, str]],
        size: str | None = None,
    ) -> ImageResult:
        """Image-to-image via POST {endpoint}/images/edits (multipart).

        Mirrors ``generate`` but uploads the user's reference image(s) as
        the edit base. Requires an edits-capable model (gpt-image-1/2,
        dall-e-2; NOT dall-e-3) — gated by ``edit_enabled`` / the
        ``img2img`` config. ``images`` is a list of ``(bytes, mime)``
        pairs: the gpt-image family accepts several (sent as repeated
        ``image[]`` parts), while dall-e-2 takes a single image, so a
        non-gpt-image model uses only the first.
        """
        if not self.edit_enabled:
            raise ImageBridgeError("image_disabled")
        cleaned = self._clean_prompt(prompt)
        # Drop empties defensively; the caller already filters unreadable
        # references, but a stray empty blob would 400 upstream.
        refs = [(b, m) for (b, m) in (images or []) if b]
        if not refs:
            raise ImageBridgeError("image_call_failed", "empty input image")
        is_gpt_image = self._is_gpt_image_model
        # dall-e-2 /images/edits takes exactly one image; only the gpt-image
        # family accepts an array. Cap non-gpt-image to the first reference
        # so an extra attachment can't 400 the request.
        if not is_gpt_image:
            refs = refs[:1]
        # img2img: keep the (first) reference image's proportions. Map its
        # aspect ratio onto the closest model-supported size; fall back to
        # the per-request / operator default only if dimensions can't be read.
        eff_size = self.resolve_size_for_reference(refs[0][0]) or self.resolve_size(size)
        url = f"{self._endpoint}/images/edits"
        # Multipart form: each input image rides as a file part with a
        # filename whose extension matches its mime so upstream content
        # sniffing accepts it (OpenAI rejects unknown/extensionless parts).
        # A single image uses the scalar `image` field (works for both
        # families and keeps the proven path byte-identical); multiple
        # images use repeated `image[]` parts, which only gpt-image accepts.
        form = aiohttp.FormData()
        form.add_field("model", self._model)
        form.add_field("prompt", cleaned)
        form.add_field("n", "1")
        form.add_field("size", eff_size)
        if not is_gpt_image:
            form.add_field("response_format", "b64_json")
        single = len(refs) == 1
        field_name = "image" if single else "image[]"
        for i, (img_bytes, img_mime) in enumerate(refs):
            ext = _MIME_TO_EXT.get((img_mime or "").lower(), ".png")
            filename = f"image{ext}" if single else f"image{i}{ext}"
            form.add_field(
                field_name,
                img_bytes,
                filename=filename,
                content_type=(img_mime or "image/png"),
            )
        # No explicit Content-Type — aiohttp.FormData sets
        # multipart/form-data with the right boundary. Setting it manually
        # would break the boundary and the upload would fail.
        headers = {"Authorization": f"Bearer {self._api_key}"}
        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)

        async def _attempt() -> ImageResult:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, data=form, headers=headers) as resp:
                    return await self._read_image_response(
                        session, resp, cleaned, eff_size
                    )

        try:
            return await self._post_with_retry(_attempt)
        except asyncio.TimeoutError as exc:
            raise ImageBridgeError("image_timeout") from exc
        except aiohttp.ClientError as exc:
            logger.warning(
                "[WebChatGateway] image edit client error: %s", exc
            )
            raise ImageBridgeError(
                "image_call_failed", f"client error: {exc}"
            ) from exc


__all__ = [
    "ImageBridge",
    "ImageBridgeError",
    "ImageResult",
    "is_image_command",
    "persist_generated_image",
    "strip_image_prefix",
]


_MIME_TO_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


async def persist_generated_image(
    *,
    storage,
    file_store,
    token_name: str,
    result: ImageResult,
    now: int,
) -> dict:
    """Save generated bytes through ``FileStore`` and insert the
    matching ``webchat_files`` row, committed=1.

    Generated images are born committed: they're being attached to the
    assistant message that ``record_chat_pair`` is about to write. The
    orphan-GC two-step (insert committed=0 then commit) that user
    uploads use is overkill here — we control both writes in the same
    request, so we can land the row in its final state directly.

    Returns the attachment payload (``{file_id, mime}``) the chat
    layer embeds into the ``message_added`` event's ``attachments``
    list and the SSE ``image_ready`` frame.
    """
    file_id = secrets.token_urlsafe(12)
    ext = _MIME_TO_EXT.get(result.mime, ".png")
    storage_key = f"{token_name}/{file_id}{ext}"

    # File-store first: a failed insert with the bytes already on disk
    # leaves a recoverable orphan, but a failed save with the DB row
    # present would 404 every future serve attempt.
    await file_store.save(
        storage_key=storage_key, content=result.content, mime=result.mime
    )
    try:
        await storage.insert_file(
            file_id=file_id,
            token_name=token_name,
            session_id="",  # filled when the message_added event lands
            mime=result.mime,
            size_bytes=len(result.content),
            storage_key=storage_key,
            now=now,
        )
        await storage.mark_files_committed([file_id], now=now)
    except Exception:
        # DB write failed. Best-effort delete the storage object so we
        # don't leak quota on a row that won't exist for orphan GC to
        # find.
        try:
            await file_store.delete(storage_key=storage_key)
        except Exception:
            logger.exception(
                "[WebChatGateway] image gen storage-rollback delete failed"
            )
        raise
    return {"file_id": file_id, "mime": result.mime}
