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


_PREFIX_PATTERN = re.compile(r"^\s*/(?:image|img|draw)\b\s*", re.IGNORECASE)

# Cap on the prompt sent to the image API. The text models cap their
# context at `max_message_length`, but image gen costs are per-call,
# not per-token, so we keep the same limit for UX consistency rather
# than a tighter one. OpenAI itself caps DALL-E 3 at 4000 chars.
_DEFAULT_PROMPT_CAP = 4000


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

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


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
    ) -> None:
        self._enabled = bool(enabled)
        self._endpoint = (endpoint or "").rstrip("/")
        self._api_key = api_key or ""
        self._model = model or "dall-e-3"
        self._size = size or "1024x1024"
        self._timeout_seconds = float(timeout_seconds) if timeout_seconds else 60.0

    @property
    def enabled(self) -> bool:
        # An ImageBridge is only "live" when the operator turned the
        # feature on AND wired both endpoint + api_key. Missing either
        # would let a half-configured deployment 500 on the first
        # /image attempt — surface as ``image_disabled`` instead.
        return self._enabled and bool(self._endpoint) and bool(self._api_key)

    @property
    def model(self) -> str:
        return self._model

    @property
    def size(self) -> str:
        return self._size

    async def generate(self, prompt: str) -> ImageResult:
        if not self.enabled:
            raise ImageBridgeError("image_disabled")
        cleaned = (prompt or "").strip()
        if not cleaned:
            raise ImageBridgeError("image_call_failed", "empty prompt")
        if len(cleaned) > _DEFAULT_PROMPT_CAP:
            cleaned = cleaned[:_DEFAULT_PROMPT_CAP]
        url = f"{self._endpoint}/images/generations"
        body: dict[str, Any] = {
            "model": self._model,
            "prompt": cleaned,
            "n": 1,
            "size": self._size,
            # Force b64 so we don't depend on the legacy 60-min URL
            # expiry path. OpenAI's GPT-image models only return b64
            # anyway — asking for `url` on those raises 400 — so the
            # parameter is harmless for either family.
            "response_format": "b64_json",
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=body, headers=headers) as resp:
                    status = resp.status
                    try:
                        payload = await resp.json(content_type=None)
                    except Exception:
                        payload = None
                    if status >= 400:
                        msg = ""
                        if isinstance(payload, dict):
                            err = payload.get("error")
                            if isinstance(err, dict):
                                msg = str(err.get("message") or "")[:200]
                        logger.warning(
                            "[WebChatGateway] image gen upstream %d: %s",
                            status,
                            msg,
                        )
                        raise ImageBridgeError(
                            "image_call_failed",
                            f"upstream {status}: {msg}",
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
                    if not isinstance(b64, str) or not b64:
                        # Legacy DALL-E with response_format=url falls
                        # here. We force b64_json above, so a missing
                        # field means the upstream actually returned
                        # nothing usable — treat as empty.
                        raise ImageBridgeError("empty_image_reply")
                    try:
                        raw = base64.b64decode(b64, validate=False)
                    except (ValueError, TypeError) as exc:
                        raise ImageBridgeError(
                            "image_call_failed", "b64 decode failed"
                        ) from exc
                    if not raw:
                        raise ImageBridgeError("empty_image_reply")
                    # OpenAI image endpoints always return PNG. If a
                    # future-compatible gateway returns a different
                    # MIME, the upload validator will refuse it on save
                    # (the chat client's allowed_mime gate already
                    # whitelists png/jpeg/webp/gif).
                    return ImageResult(content=raw, mime="image/png", prompt=cleaned)
        except asyncio.TimeoutError as exc:
            raise ImageBridgeError("image_timeout") from exc
        except aiohttp.ClientError as exc:
            logger.warning(
                "[WebChatGateway] image gen client error: %s", exc
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
