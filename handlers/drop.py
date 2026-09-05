"""Drop — multi-device self-to-self file/note transfer (no LLM).

A fixed per-token session ("drop") in the sidebar where one user's
devices push text notes and files to each other, mirroring the late
MS Edge Drop feature. Design invariants that distinguish it from the
chat pipeline:

* **LLM never participates.** Messages live in `webchat_drop_messages`
  (v6), NOT in AstrBot CM — record_chat_pair's user/assistant pair
  semantics and the ImageURLPart context pollution TECH_DEBT §1 warns
  about are both avoided. Push-to-peers rides the existing
  `webchat_updates` long-poll channel via dedicated `drop_*` event
  types (the overlay ignores unknown event types, and get_conversation
  never reads this table, so the regular chat path is untouched).
* **No quota.** The daily LLM quota is not charged. Abuse is bounded
  instead by the per-token storage cap (shared with image uploads) and
  a per-message text length cap.
* **Device attribution.** Each row carries `device_id` (client-persisted
  UUID) + `device_name` (UA-derived label). The origin device renders
  its own messages right-aligned; peer devices render them
  assistant-style on the left with the device name. `device_id` is
  stored server-side, so the client dedups its own echo by id equality
  rather than by bubble side.

Endpoints (all registered in handlers/server.py under cfg.drop_*):

  POST {prefix}/drop/send          JSON {text, attachments, device_id, device_name}
  POST {prefix}/drop/upload        multipart file upload (any type, NOT the
                                   image-only /upload — Drop accepts arbitrary
                                   files and enforces its own MIME policy)
  GET  {prefix}/drop/messages?before=&limit=   newest-first pagination
  DELETE {prefix}/drop/messages/{message_id}   soft-delete one message
  POST {prefix}/drop/clear         hard-clear own history + release files
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from astrbot.api import logger

from ..core.event_bus import EventBus
from ..core.file_lifecycle import (
    commit_attachments_or_release,
    release_files_safely,
)
from ..core.file_store import FileStore, FileStoreUnavailable
from ..core.image_util import detect_image_mime_async, ext_for_mime
from ..core.ip_guard import IpGuard
from ..core.ratelimit import PerTokenUploadGate
from ..core.audit import AuditLogger
from ..storage.base import AbstractStorage, FileRow, NewEvent
from .admin_tokens import ServiceError
from .common import (
    build_cors_headers,
    error_response,
    extract_origin,
    gate_request,
    json_response,
    preflight_response,
)
from .conversations_overlay import (
    EVENT_DROP_HISTORY_CLEARED,
    EVENT_DROP_MESSAGE_ADDED,
    EVENT_DROP_MESSAGE_DELETED,
)

# Fixed pseudo-session that owns Drop file rows in webchat_files. The
# literal is part of the storage contract (`list_drop_files` filters
# session_id = 'drop'); it deliberately fails _SESSION_ID_PATTERN for
# the CHAT path only — the chat handlers reject it as a normal session
# so regular conversations can't collide with the reserved namespace.
# The Drop endpoints accept it directly (their own constant, not the
# chat-side pattern).
DROP_SESSION_ID = "drop"

# Drop text cap. Independent of max_message_length (an LLM-context
# knob) — notes are for the user only, but an unbounded TEXT column is
# still an abuse channel on a quota-free endpoint. 16k chars ≈ 8-10
# screens of notes; larger transfers should be files.
_MAX_DROP_TEXT_CHARS = 16_000

# Keep this in lock-step with uploads.max_attachments_per_message's
# configured upper bound. The client receives the effective upload cap from
# /site; accepting the full bound here prevents a valid 9--16 file selection
# from uploading successfully only to be rejected by /drop/send.
_MAX_DROP_ATTACHMENTS = 16

# Client-supplied device identifiers.
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_\-.:]{8,64}$")
_MAX_DEVICE_NAME_CHARS = 64
_MAX_FILENAME_BYTES = 255

# Drop accepts arbitrary file types (that's the point) but refuses the
# MIMEs a browser may execute in-document. SVG is the classic stored-XSS
# vector (script inside an <img>-served document); HTML/XHTML would run
# script on direct navigation. Everything else renders as a download —
# the serve handler forces `attachment` disposition for non-images, so
# even a mislabeled binary can't run in the gateway's origin. When a
# more exotic active content type shows up, it lands here too.
_BLOCKED_DROP_MIME = frozenset(
    {
        "image/svg+xml",
        "text/html",
        "application/xhtml+xml",
        "application/javascript",
        "text/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "text/ecmascript",
        "application/xml",
        "text/xml",
    }
)

# Only these raster formats are safe to render inline. The upload path
# rejects SVG/HTML, but an explicit allow-list here also protects legacy rows
# and operator-imported data from being interpreted as active content.
_INLINE_DROP_MIME = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif"}
)

# Extensions that would let an attacker bypass the MIME check by
# renaming. The upload handler derives MIME from CONTENT (PIL for
# images) or the client-declared type for non-images; a "notes.html"
# whose bytes sniff as text/plain would still serve as attachment, but
# blocking executable extensions keeps the storage listing honest too.
_BLOCKED_DROP_EXTENSIONS = frozenset(
    {".html", ".htm", ".xhtml", ".xht", ".svg", ".shtml"}
)

# Cursor pagination bounds. 100 per page matches _GET_EVENTS_LIMIT so a
# full Drop history cold-load has the same wire profile as the event log.
_DEFAULT_DROP_PAGE_SIZE = 50
_MAX_DROP_PAGE_SIZE = 200

# Session-id pattern for the Drop upload's session_id form field.
# Constant-valued in practice (the client always sends "drop"), kept as
# a pattern so a future per-device Drop namespace doesn't need a second
# validation path.
_DROP_SESSION_RE = re.compile(r"^drop$")


@dataclass
class DropDeps:
    """Wiring surface for the Drop endpoints.

    Shares storage / audit / ip_guard / file_store with the chat layer
    (same instances from main.py) but has its own origin/auth policy
    fields so it satisfies `gate_request`'s `_GateDeps` Protocol
    without dragging ChatDeps in.
    """

    storage: AbstractStorage
    audit: AuditLogger
    event_bus: EventBus
    file_store: FileStore
    upload_gate: PerTokenUploadGate
    ip_guard: IpGuard
    allowed_origins: set[str]
    # Same knobs as UploadDeps — the per-token storage cap is SHARED
    # with image uploads (one budget, both features draw on it), so
    # Drop can't double a user's storage footprint.
    per_token_storage_mb: int
    max_file_size_mb: int
    trust_forwarded_for: bool
    # Operator toggle. False keeps the routes installed (the frontend
    # hides the session based on /site) but every write endpoint
    # rejects with 403 drop_disabled. The serve route ignores this —
    # files already in Drop history stay downloadable.
    enabled: bool = True
    # HMAC secret + logout tracker for the wcg_file cookie — the Drop
    # serve endpoint accepts the same cookie the image /files/{id}
    # endpoint issued (single handshake for both surfaces). Empty
    # secret = cookie auth disabled (bearer-only).
    file_cookie_secret: bytes = b""
    cookie_logout_tracker: Any | None = None
    trust_referer_as_origin: bool = False
    allow_missing_origin: bool = False


def _sanitize_filename(raw: Any) -> str:
    """Reduce a client-supplied filename to a safe display string.

    Keeps the basename only (strip any path components — Windows and
    POSIX separators both), drops control characters, caps the byte
    length. Never used for storage paths (the storage_key is derived
    from the server-generated file_id), only for Content-Disposition
    and client display — RFC 5987/6266 encoding happens at the serve
    site where the header value is built.
    """
    if not isinstance(raw, str):
        return ""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    # Control chars (newlines could smuggle header folding; the rest is
    # display noise).
    name = "".join(ch for ch in name if ord(ch) >= 32)
    # Cap by UTF-8 bytes, respecting a multi-byte boundary.
    encoded = name.encode("utf-8", "ignore")
    while len(encoded) > _MAX_FILENAME_BYTES:
        name = name[:-1]
        encoded = name.encode("utf-8", "ignore")
    return name.strip()


def _sanitize_device_name(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    name = raw.strip()
    name = "".join(ch for ch in name if ord(ch) >= 32)[:_MAX_DEVICE_NAME_CHARS]
    return name


def _drop_message_payload(row: Any) -> dict:
    """Wire format of one Drop message — shared by the send response,
    the list endpoint, and the `drop_message_added` event payload so
    all three consumers agree by construction."""
    out: dict[str, Any] = {
        "id": row.id,
        "device_id": row.device_id,
        "device_name": row.device_name,
        "kind": row.kind,
        "text": row.text,
        "created_at": row.created_at,
    }
    if row.kind == "file":
        out["file_id"] = row.file_id
        out["filename"] = row.filename
        out["mime"] = row.mime
        out["size"] = row.size_bytes
    return out


def _is_blocked_drop_extension(filename: str) -> bool:
    dot = filename.rfind(".")
    if dot < 0:
        return False
    return filename[dot:].lower() in _BLOCKED_DROP_EXTENSIONS


def _guess_drop_mime(content: bytes, declared: str) -> str:
    """Authoritative MIME for a NON-image Drop upload.

    Image declarations never reach here — `_resolve_drop_mime` routes
    them through the PIL sniff. Non-images trust the declared type only
    when it's not one of the executable set; anything undeclared or
    suspicious becomes application/octet-stream (which always
    downloads, never renders).
    """
    # Multipart Content-Type may carry parameters (for example
    # text/html; charset=utf-8). Compare the normalized media type, not
    # the raw header, so parameters cannot bypass the active-content block.
    declared_norm = (declared or "").split(";", 1)[0].strip().lower()
    if declared_norm in _BLOCKED_DROP_MIME:
        return "application/octet-stream"
    if declared_norm and "/" in declared_norm and len(declared_norm) <= 100:
        # Basic shape validation only; permissiveness here is safe
        # because the serve handler pins non-image responses to
        # `attachment` disposition — the browser never renders them
        # in-document.
        return declared_norm
    return "application/octet-stream"


async def _resolve_drop_mime(content: bytes, declared: str) -> str:
    """Async MIME resolution — off-thread PIL for images, sync policy
    for everything else. Returns the authoritative stored MIME."""
    declared_norm = (declared or "").split(";", 1)[0].strip().lower()
    if declared_norm.startswith("image/"):
        # Content-sniff: a mislabeled non-image must not land in an
        # <img>-renderable slot. Returns the canonical image MIME or
        # None (→ caller 415s / falls back to octet-stream).
        try:
            detected = await detect_image_mime_async(content)
        except Exception:
            logger.exception("[WebChatGateway] drop image sniff failed")
            detected = None
        if detected is not None:
            return detected
        # Claims to be an image but isn't a coherent raster — refuse
        # rather than storing something the chat client would try to
        # render as an <img>.
        return "application/octet-stream"
    return _guess_drop_mime(content, declared_norm)


def _ext_for_drop_mime(mime: str) -> str:
    # Image mimes reuse the canonical map; other types keep a short
    # allowlist so operator-side storage browsing stays readable.
    known = ext_for_mime(mime)
    if known:
        return known
    return {
        "application/pdf": ".pdf",
        "application/zip": ".zip",
        "text/plain": ".txt",
        "text/csv": ".csv",
        "application/json": ".json",
        "audio/mpeg": ".mp3",
        "audio/wav": ".wav",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "application/octet-stream": ".bin",
    }.get(mime, ".bin")


def make_drop_handlers(deps: DropDeps):
    """Build the five Drop endpoints. Returns a dict of handlers —
    same shape as make_conversation_handlers so server.py mounts them
    uniformly."""

    allowed = deps.allowed_origins
    max_size_bytes = deps.max_file_size_mb * 1024 * 1024
    per_token_quota_bytes = deps.per_token_storage_mb * 1024 * 1024

    async def _drop_reference_count(row: FileRow) -> int | None:
        """Return Drop references, or None when the backend cannot answer.

        Unknown is deliberately treated as unsafe by destructive callers.
        The fallback scan keeps older third-party storage implementations
        source-compatible while current SQLite/MySQL backends use a COUNT.
        """
        if not row.file_id:
            return 0
        counter = getattr(deps.storage, "count_drop_file_references", None)
        try:
            if callable(counter):
                return int(
                    await counter(
                        token_name=row.token_name, file_id=row.file_id
                    )
                )
            # Never perform a compatibility full-table scan here. A legacy
            # backend without the COUNT API cannot answer safely at scale:
            # truncation can under-count references and release an in-use file.
            # Unknown is handled as unsafe by destructive callers.
            logger.warning(
                "[WebChatGateway] storage backend lacks count_drop_file_references; "
                "refusing fallback full-table scan for file=%s",
                row.file_id,
            )
            return None
        except Exception:
            logger.exception(
                "[WebChatGateway] drop reference count failed file=%s",
                row.file_id,
            )
            return None

    def _disabled(request: web.Request, origin: str | None) -> web.Response:
        # audit 命名沿用 auth_fail 的 detail.reason 风格——写 drop_rejected
        # 会稀释域词汇表；disabled 是配置态不是攻击信号，log 即可。
        logger.info(
            "[WebChatGateway] drop request rejected: feature disabled (%s)",
            request.path,
        )
        return json_response(
            {"error": "drop_disabled"}, status=403,
            origin=origin, allowed_origins=allowed,
            same_origin_host=request.host,
        )

    def _err(
        request: web.Request, origin: str | None, exc: ServiceError
    ) -> web.Response:
        return error_response(request, origin=origin, allowed=allowed, exc=exc)

    async def _push_events(
        token_name: str, events: list[NewEvent], now: int
    ) -> None:
        """Append to the shared event log + wake long-polls. Never
        raises — a failed push means peers fall back to their next
        list refresh (same degradation contract as record_chat_pair)."""
        try:
            await deps.storage.append_updates(
                token_name=token_name, events=events, now=now
            )
            await deps.event_bus.notify(token_name)
        except Exception:
            logger.exception(
                "[WebChatGateway] drop event push failed token=%s", token_name
            )

    # ----- POST {prefix}/drop/send -----

    async def send_drop(request: web.Request) -> web.Response:
        gated = await gate_request(request, deps)
        if isinstance(gated, web.Response):
            return gated
        token = gated.token
        ip = gated.ip
        origin = gated.origin
        if not deps.enabled:
            return _disabled(request, origin)

        try:
            payload = await request.json()
        except web.HTTPRequestEntityTooLarge:
            return json_response(
                {"error": "payload_too_large"}, status=413,
                origin=origin, allowed_origins=allowed,
                same_origin_host=gated.same_host,
            )
        except Exception:
            payload = None
        if not isinstance(payload, dict):
            return json_response(
                {"error": "invalid_payload"}, status=400,
                origin=origin, allowed_origins=allowed,
                same_origin_host=gated.same_host,
            )

        text = payload.get("text")
        if not isinstance(text, str):
            text = ""
        text = text.strip()
        if len(text) > _MAX_DROP_TEXT_CHARS:
            return json_response(
                {"error": "text_too_long", "max_length": _MAX_DROP_TEXT_CHARS},
                status=400,
                origin=origin, allowed_origins=allowed,
                same_origin_host=gated.same_host,
            )

        device_id = payload.get("device_id")
        if not isinstance(device_id, str) or not _DEVICE_ID_RE.match(device_id):
            return json_response(
                {"error": "invalid_device_id"}, status=400,
                origin=origin, allowed_origins=allowed,
                same_origin_host=gated.same_host,
            )
        device_name = _sanitize_device_name(payload.get("device_name"))

        raw_attachments = payload.get("attachments")
        attachments: list[str] = []
        if raw_attachments is not None:
            if not isinstance(raw_attachments, list):
                return json_response(
                    {"error": "invalid_payload"}, status=400,
                    origin=origin, allowed_origins=allowed,
                    same_origin_host=gated.same_host,
                )
            if len(raw_attachments) > _MAX_DROP_ATTACHMENTS:
                return json_response(
                    {"error": "too_many_attachments"},
                    status=400,
                    origin=origin, allowed_origins=allowed,
                    same_origin_host=gated.same_host,
                )
            seen: set[str] = set()
            for entry in raw_attachments:
                if not isinstance(entry, dict):
                    return json_response(
                        {"error": "invalid_payload"}, status=400,
                        origin=origin, allowed_origins=allowed,
                        same_origin_host=gated.same_host,
                    )
                fid = entry.get("file_id")
                if not isinstance(fid, str) or not fid.strip():
                    return json_response(
                        {"error": "invalid_payload"}, status=400,
                        origin=origin, allowed_origins=allowed,
                        same_origin_host=gated.same_host,
                    )
                fid = fid.strip()
                if fid in seen:
                    continue
                seen.add(fid)
                attachments.append(fid)

        if not text and not attachments:
            return json_response(
                {"error": "invalid_payload"}, status=400,
                origin=origin, allowed_origins=allowed,
                same_origin_host=gated.same_host,
            )

        async with deps.upload_gate.acquire(token.name):
            # Attachment ownership: each file must belong to THIS token and
            # be parked in the Drop namespace. Cross-session reuse of a
            # chat-upload file_id is rejected — those live in their own
            # conversation's lifecycle and a Drop commit would yank them
            # out of the orphan-GC window.
            attachment_rows: list[FileRow] = []
            for fid in attachments:
                try:
                    row = await deps.storage.get_file(fid)
                except Exception:
                    logger.exception("[WebChatGateway] drop get_file failed")
                    return json_response(
                        {"error": "internal_error"}, status=500,
                        origin=origin, allowed_origins=allowed,
                        same_origin_host=gated.same_host,
                    )
                if (
                    row is None
                    or row.token_name != token.name
                    or row.session_id != DROP_SESSION_ID
                ):
                    return json_response(
                        {"error": "invalid_attachment"}, status=400,
                        origin=origin, allowed_origins=allowed,
                        same_origin_host=gated.same_host,
                    )
                attachment_rows.append(row)

            # Commit files first (same envelope as /chat): on failure the
            # release is best-effort and we 500 so the client can retry;
            # nothing is visible to peers yet.
            if attachment_rows and not await commit_attachments_or_release(
                storage=deps.storage,
                file_store=deps.file_store,
                rows=attachment_rows,
                log_label="drop_send",
                audit=deps.audit,
            ):
                return json_response(
                    {"error": "internal_error"}, status=500,
                    origin=origin, allowed_origins=allowed,
                    same_origin_host=gated.same_host,
                )

            now = int(time.time())
            created: list[dict] = []
            created_ids: list[int] = []
            events: list[NewEvent] = []
            try:
                if text:
                    mid = await deps.storage.append_drop_message(
                        token_name=token.name,
                        device_id=device_id,
                        device_name=device_name,
                        kind="text",
                        text=text,
                        file_id=None,
                        filename="",
                        mime="",
                        size_bytes=0,
                        now=now,
                    )
                    created_ids.append(mid)
                    row = await deps.storage.get_drop_message(
                        token_name=token.name, message_id=mid
                    )
                    if row is not None:
                        created.append(_drop_message_payload(row))
                        events.append(
                            NewEvent(
                                event_type=EVENT_DROP_MESSAGE_ADDED,
                                session_id=DROP_SESSION_ID,
                                payload=json.dumps(
                                    _drop_message_payload(row), ensure_ascii=False
                                ),
                            )
                        )
                for row in attachment_rows:
                    # One drop message per file — mirrors how the chat UI
                    # renders one bubble per attachment and keeps delete-
                    # one-file semantics simple.
                    mid = await deps.storage.append_drop_message(
                        token_name=token.name,
                        device_id=device_id,
                        device_name=device_name,
                        kind="file",
                        text="",
                        file_id=row.file_id,
                        filename=row.filename or "",
                        mime=row.mime,
                        size_bytes=row.size_bytes,
                        now=now,
                    )
                    created_ids.append(mid)
                    full = await deps.storage.get_drop_message(
                        token_name=token.name, message_id=mid
                    )
                    if full is not None:
                        created.append(_drop_message_payload(full))
                        events.append(
                            NewEvent(
                                event_type=EVENT_DROP_MESSAGE_ADDED,
                                session_id=DROP_SESSION_ID,
                                payload=json.dumps(
                                    _drop_message_payload(full), ensure_ascii=False
                                ),
                            )
                        )
            except Exception:
                logger.exception("[WebChatGateway] drop send persist failed")
                # Each append_drop_message is committed independently by the
                # storage backends. Remove rows already inserted by this request
                # so a later attachment failure cannot leave a partial send.
                for mid in created_ids:
                    try:
                        await deps.storage.hard_delete_drop_message(
                            token_name=token.name, message_id=mid
                        )
                    except Exception:
                        logger.exception(
                            "[WebChatGateway] drop send row rollback failed id=%s",
                            mid,
                        )
                # Release only files that no remaining Drop row references.
                # Re-check the reference count inside the lock to prevent TOCTOU
                # race where another send_drop appends a message referencing the
                # same file between our count check and release call.
                releasable: list[FileRow] = []
                for file_row in attachment_rows:
                    try:
                        ref_count = await _drop_reference_count(file_row)
                        if ref_count == 0:
                            releasable.append(file_row)
                    except Exception:
                        logger.exception(
                            "[WebChatGateway] drop ref_count check failed file=%s",
                            file_row.file_id,
                        )
                        # Unknown count: treat as unsafe, do not release
                if releasable:
                    try:
                        await release_files_safely(
                            storage=deps.storage,
                            file_store=deps.file_store,
                            rows=releasable,
                            log_label="drop_send_rollback",
                        )
                    except Exception:
                        logger.exception(
                            "[WebChatGateway] drop send rollback raised"
                        )
                # Do NOT push events after rollback — the rows no longer exist.
                # Clearing the events list ensures the success path's push at L623
                # does not send phantom messages to peers.
                events.clear()
                return json_response(
                    {"error": "internal_error"}, status=500,
                    origin=origin, allowed_origins=allowed,
                    same_origin_host=gated.same_host,
                )

        # Push events only after successful persist. The rollback path above
        # clears the events list to prevent phantom messages.
        await _push_events(token.name, events, now)
        await deps.audit.write(
            "drop_sent",
            name=token.name,
            ip=ip,
            detail={
                "text_len": len(text),
                "files": len(attachment_rows),
                "device_id": device_id,
            },
        )
        return json_response(
            {"messages": created},
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=gated.same_host,
        )

    # ----- POST {prefix}/drop/upload -----

    async def upload_drop(request: web.Request) -> web.Response:
        gated = await gate_request(request, deps)
        if isinstance(gated, web.Response):
            return gated
        token = gated.token
        ip = gated.ip
        origin = gated.origin
        if not deps.enabled:
            return _disabled(request, origin)

        if not (request.content_type or "").startswith("multipart/"):
            return json_response(
                {"error": "invalid_payload"}, status=400,
                origin=origin, allowed_origins=allowed,
                same_origin_host=gated.same_host,
            )
        try:
            reader = await request.multipart()
        except (ValueError, AssertionError):
            return json_response(
                {"error": "invalid_payload"}, status=400,
                origin=origin, allowed_origins=allowed,
                same_origin_host=gated.same_host,
            )
        except web.HTTPRequestEntityTooLarge:
            return json_response(
                {"error": "payload_too_large"}, status=413,
                origin=origin, allowed_origins=allowed,
                same_origin_host=gated.same_host,
            )

        file_content: bytes | None = None
        client_filename = ""
        declared_mime = ""
        try:
            while True:
                try:
                    part = await reader.next()
                except web.HTTPRequestEntityTooLarge:
                    return json_response(
                        {"error": "payload_too_large"}, status=413,
                        origin=origin, allowed_origins=allowed,
                        same_origin_host=gated.same_host,
                    )
                if part is None:
                    break
                name = (part.name or "").strip()
                if name == "file" and file_content is None:
                    buf = bytearray()
                    while True:
                        try:
                            chunk = await part.read_chunk(size=64 * 1024)
                        except web.HTTPRequestEntityTooLarge:
                            return json_response(
                                {"error": "payload_too_large"}, status=413,
                                origin=origin, allowed_origins=allowed,
                                same_origin_host=gated.same_host,
                            )
                        if not chunk:
                            break
                        buf.extend(chunk)
                        if len(buf) > max_size_bytes:
                            return json_response(
                                {"error": "payload_too_large"}, status=413,
                                origin=origin, allowed_origins=allowed,
                                same_origin_host=gated.same_host,
                            )
                    file_content = bytes(buf)
                    # Content-Type as declared by the client for THIS
                    # part; only trusted for non-images (see
                    # _resolve_drop_mime).
                    declared_mime = part.headers.get("Content-Type", "")
                elif name == "filename" and not client_filename:
                    try:
                        raw = await part.text()
                    except Exception:
                        raw = ""
                    client_filename = _sanitize_filename(raw)
        except (ConnectionResetError, ConnectionError):
            logger.debug(
                "[WebChatGateway] drop upload aborted: client disconnected"
            )
            return json_response(
                {"error": "client_disconnected"}, status=400,
                origin=origin, allowed_origins=allowed,
                same_origin_host=gated.same_host,
            )
        except Exception:
            logger.exception("[WebChatGateway] drop upload parse failed")
            return json_response(
                {"error": "invalid_payload"}, status=400,
                origin=origin, allowed_origins=allowed,
                same_origin_host=gated.same_host,
            )

        if not file_content:
            return json_response(
                {"error": "invalid_payload"}, status=400,
                origin=origin, allowed_origins=allowed,
                same_origin_host=gated.same_host,
            )
        if _is_blocked_drop_extension(client_filename):
            await deps.audit.write(
                "drop_upload_rejected",
                name=token.name,
                ip=ip,
                detail={
                    "reason": "blocked_extension",
                    "filename": client_filename,
                    "size": len(file_content),
                },
            )
            return json_response(
                {"error": "unsupported_type"}, status=415,
                origin=origin, allowed_origins=allowed,
                same_origin_host=gated.same_host,
            )

        mime = await _resolve_drop_mime(file_content, declared_mime)
        # Belt-and-braces: even if PIL returned an empty type or the
        # declared MIME was rewritten to octet-stream, the ORIGINAL
        # declared type can still be in the executable set (the user
        # claimed text/html but PIL said no → reject). This catches
        # the `evil.svg` rename-to-`blob.bin` attack where the
        # declared Content-Type comes through as application/octet-
        # stream but the filename advertises the real intent.
        declared_norm = (declared_mime or "").split(";", 1)[0].strip().lower()
        if mime in _BLOCKED_DROP_MIME or declared_norm in _BLOCKED_DROP_MIME:
            await deps.audit.write(
                "drop_upload_rejected",
                name=token.name,
                ip=ip,
                detail={
                    "reason": "blocked_mime",
                    "mime": mime,
                    "declared_mime": declared_norm,
                    "size": len(file_content),
                },
            )
            return json_response(
                {"error": "unsupported_type"}, status=415,
                origin=origin, allowed_origins=allowed,
                same_origin_host=gated.same_host,
            )

        file_id = secrets.token_urlsafe(12)
        ext = _ext_for_drop_mime(mime)
        storage_key = f"{token.name}/{file_id}{ext}"

        # Shared per-token storage budget with the image upload path —
        # same blocking gate, same total-size accounting, so Drop can't
        # double the user's storage footprint.
        async with deps.upload_gate.acquire(token.name):
            try:
                committed_total = (
                    await deps.storage.total_committed_size_for_token(token.name)
                )
            except Exception:
                logger.exception(
                    "[WebChatGateway] drop total_committed_size failed"
                )
                return json_response(
                    {"error": "storage_unavailable"}, status=503,
                    origin=origin, allowed_origins=allowed,
                    same_origin_host=gated.same_host,
                    extra_headers={"Retry-After": "5"},
                )
            # Check quota against committed files only. Uncommitted temporary
            # files are cleaned by orphan GC; counting them would incorrectly
            # reject uploads when the user has orphaned temporaries.
            if committed_total + len(file_content) > per_token_quota_bytes:
                await deps.audit.write(
                    "drop_upload_rejected",
                    name=token.name,
                    ip=ip,
                    detail={
                        "reason": "storage_quota_exceeded",
                        "size": len(file_content),
                    },
                )
                return json_response(
                    {"error": "storage_quota_exceeded"}, status=429,
                    origin=origin, allowed_origins=allowed,
                    same_origin_host=gated.same_host,
                )
            now = int(time.time())
            try:
                await deps.storage.insert_file(
                    file_id=file_id,
                    token_name=token.name,
                    session_id=DROP_SESSION_ID,
                    mime=mime,
                    size_bytes=len(file_content),
                    storage_key=storage_key,
                    now=now,
                    filename=client_filename,
                )
            except Exception:
                logger.exception("[WebChatGateway] drop insert_file failed")
                return json_response(
                    {"error": "internal_error"}, status=500,
                    origin=origin, allowed_origins=allowed,
                    same_origin_host=gated.same_host,
                )

            try:
                await deps.file_store.save(
                    storage_key=storage_key, content=file_content, mime=mime
                )
            except Exception:
                logger.exception("[WebChatGateway] drop file_store.save failed")
                try:
                    await deps.storage.delete_files_by_ids([file_id])
                except Exception:
                    logger.exception(
                        "[WebChatGateway] drop insert_file rollback failed"
                    )
                return json_response(
                    {"error": "internal_error"}, status=500,
                    origin=origin, allowed_origins=allowed,
                    same_origin_host=gated.same_host,
                )


        # Reservation and object write are complete before releasing
        # the gate, so other mutations cannot observe a partial upload.
        await deps.audit.write(
            "drop_upload_ok",
            name=token.name,
            ip=ip,
            detail={
                "file_id": file_id,
                "size": len(file_content),
                "mime": mime,
                "has_filename": bool(client_filename),
            },
        )
        return json_response(
            {
                "file_id": file_id,
                "mime": mime,
                "size": len(file_content),
                "filename": client_filename,
            },
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=gated.same_host,
        )

    # ----- GET {prefix}/drop/messages -----

    async def list_drop(request: web.Request) -> web.Response:
        gated = await gate_request(request, deps)
        if isinstance(gated, web.Response):
            return gated
        token = gated.token
        origin = gated.origin
        if not deps.enabled:
            return _disabled(request, origin)

        def _parse_int(raw: str | None, default: int, lo: int, hi: int) -> int:
            try:
                v = int(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, v))

        limit = _parse_int(
            request.query.get("limit"),
            _DEFAULT_DROP_PAGE_SIZE,
            1,
            _MAX_DROP_PAGE_SIZE,
        )
        before_raw = (request.query.get("before") or "").strip()
        before_id: int | None = None
        if before_raw:
            try:
                before_id = int(before_raw)
            except ValueError:
                return json_response(
                    {"error": "invalid_cursor"}, status=400,
                    origin=origin, allowed_origins=allowed,
                    same_origin_host=gated.same_host,
                )
            if before_id < 0:
                return json_response(
                    {"error": "invalid_cursor"}, status=400,
                    origin=origin, allowed_origins=allowed,
                    same_origin_host=gated.same_host,
                )
        try:
            # Peek+1: fetch one row beyond what we'll return so we can
            # set `has_more` exactly. Returning a full page is NOT
            # proof of more rows — a boundary-aligned limit (= total
            # count) would lie about has_more. Cheaper than a separate
            # COUNT query and avoids the empty-next-page UX.
            rows = await deps.storage.list_drop_messages(
                token_name=token.name,
                limit=limit + 1,
                before_id=before_id,
                include_deleted=False,
            )
        except ServiceError:
            raise
        except Exception:
            logger.exception("[WebChatGateway] drop list failed")
            return json_response(
                {"error": "internal_error"}, status=500,
                origin=origin, allowed_origins=allowed,
                same_origin_host=gated.same_host,
            )
        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]
        return json_response(
            {
                "messages": [_drop_message_payload(r) for r in rows],
                "has_more": has_more,
            },
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=gated.same_host,
        )

    # ----- DELETE {prefix}/drop/messages/{message_id} -----

    async def delete_drop(request: web.Request) -> web.Response:
        gated = await gate_request(request, deps)
        if isinstance(gated, web.Response):
            return gated
        token = gated.token
        ip = gated.ip
        origin = gated.origin
        if not deps.enabled:
            return _disabled(request, origin)

        raw = (request.match_info.get("message_id") or "").strip()
        try:
            message_id = int(raw)
        except ValueError:
            return json_response(
                {"error": "invalid_payload"}, status=400,
                origin=origin, allowed_origins=allowed,
                same_origin_host=gated.same_host,
            )
        if message_id <= 0:
            return json_response(
                {"error": "invalid_payload"}, status=400,
                origin=origin, allowed_origins=allowed,
                same_origin_host=gated.same_host,
            )

        async with deps.upload_gate.acquire(token.name):
            row = await deps.storage.get_drop_message(
                token_name=token.name, message_id=message_id
            )
            # Uniform 404 for missing + soft-deleted (idempotent retries
            # after the first successful delete see the same response).
            if row is None or row.deleted_at is not None:
                return json_response(
                    {"error": "not_found"}, status=404,
                    origin=origin, allowed_origins=allowed,
                    same_origin_host=gated.same_host,
                )
            now = int(time.time())
            try:
                changed = await deps.storage.soft_delete_drop_message(
                    token_name=token.name, message_id=message_id, now=now
                )
            except Exception:
                logger.exception("[WebChatGateway] drop delete failed")
                return json_response(
                    {"error": "internal_error"}, status=500,
                    origin=origin, allowed_origins=allowed,
                    same_origin_host=gated.same_host,
                )
        if changed:
            await _push_events(
                token.name,
                [
                    NewEvent(
                        event_type=EVENT_DROP_MESSAGE_DELETED,
                        session_id=DROP_SESSION_ID,
                        payload=json.dumps({"id": message_id}),
                    )
                ],
                now,
            )
            await deps.audit.write(
                "drop_message_deleted",
                name=token.name,
                ip=ip,
                detail={"message_id": message_id, "kind": row.kind},
            )
        return json_response(
            {"ok": True, "id": message_id},
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=gated.same_host,
        )

    # ----- POST {prefix}/drop/clear -----

    async def clear_drop(request: web.Request) -> web.Response:
        gated = await gate_request(request, deps)
        if isinstance(gated, web.Response):
            return gated
        token = gated.token
        ip = gated.ip
        origin = gated.origin
        if not deps.enabled:
            return _disabled(request, origin)

        async with deps.upload_gate.acquire(token.name):
            # Collect file rows BEFORE the wipe so the release has the
            # storage_keys (same pattern as _clear_history_inner).
            try:
                file_rows = await deps.storage.list_drop_files(
                    token_name=token.name
                )
            except Exception:
                logger.exception("[WebChatGateway] drop clear list files failed")
                # Do not delete the message rows when we cannot enumerate their
                # files. Proceeding would make the files unreachable to both
                # clear and orphan pruning, permanently consuming quota.
                return json_response(
                    {"error": "storage_unavailable"}, status=503,
                    origin=origin, allowed_origins=allowed,
                    same_origin_host=gated.same_host,
                    extra_headers={"Retry-After": "5"},
                )
            # Release storage objects before deleting their message/file rows.
            # A partial release leaves the rows reachable for a retry.
            if file_rows:
                try:
                    released_count = await release_files_safely(
                        storage=deps.storage,
                        file_store=deps.file_store,
                        rows=file_rows,
                        log_label="drop_clear",
                        delete_db=False,
                    )
                except Exception:
                    logger.exception("[WebChatGateway] drop clear release raised")
                    released_count = 0
                if released_count != len(file_rows):
                    return json_response(
                        {"error": "storage_unavailable"}, status=503,
                        origin=origin, allowed_origins=allowed,
                        same_origin_host=gated.same_host,
                        extra_headers={"Retry-After": "5"},
                    )
            if file_rows:
                try:
                    db_removed = await deps.storage.delete_files_by_ids(
                        [row.file_id for row in file_rows]
                    )
                except Exception:
                    logger.exception("[WebChatGateway] drop clear file-row delete failed")
                    return json_response(
                        {"error": "storage_unavailable"}, status=503,
                        origin=origin, allowed_origins=allowed,
                        same_origin_host=gated.same_host,
                        extra_headers={"Retry-After": "5"},
                    )
                if db_removed is not None and int(db_removed) != len(file_rows):
                    return json_response(
                        {"error": "storage_unavailable"}, status=503,
                        origin=origin, allowed_origins=allowed,
                        same_origin_host=gated.same_host,
                        extra_headers={"Retry-After": "5"},
                    )
            now = int(time.time())
            try:
                removed = await deps.storage.clear_drop_history(
                    token_name=token.name, now=now
                )
            except Exception:
                logger.exception("[WebChatGateway] drop clear failed")
                return json_response(
                    {"error": "internal_error"}, status=500,
                    origin=origin, allowed_origins=allowed,
                    same_origin_host=gated.same_host,
                )
        # File rows were removed before the message clear above.
        await _push_events(
            token.name,
            [
                NewEvent(
                    event_type=EVENT_DROP_HISTORY_CLEARED,
                    session_id=DROP_SESSION_ID,
                    payload="{}",
                )
            ],
            now,
        )
        await deps.audit.write(
            "drop_cleared",
            name=token.name,
            ip=ip,
            detail={"removed": removed, "files": len(file_rows)},
        )
        return json_response(
            {"ok": True, "removed": removed},
            origin=origin,
            allowed_origins=allowed,
            same_origin_host=gated.same_host,
        )

    async def preflight(request: web.Request) -> web.Response:
        return preflight_response(
            origin=extract_origin(
                request, trust_referer_as_origin=deps.trust_referer_as_origin
            ),
            allowed=allowed,
            same_origin_host=request.host,
        )

    return {
        "send": send_drop,
        "upload": upload_drop,
        "list": list_drop,
        "delete": delete_drop,
        "clear": clear_drop,
        "preflight": preflight,
    }


def make_drop_serve_handler(deps: DropDeps):
    """GET {prefix}/drop/files/{file_id} — serve one Drop attachment.

    Separate from the image serve endpoint because the disposition
    policy is inverted: images stay `inline` (the client renders
    thumbnails), everything else is forced `attachment` with the
    original filename — the browser downloads instead of rendering,
    which is the load-bearing defense against stored active content
    (the MIME whitelist rejects the worst offenders at upload, but
    disposition is the backstop if a permissive operator loosens the
    list later). Auth reuses the SAME bearer + wcg_file cookie pair as
    /files/{id} so the chat client's existing cookie flow works
    without a second handshake.
    """
    from ..core.auth import extract_bearer
    from ..core.file_cookie import (
        FILE_AUTH_COOKIE_NAME,
        verify as verify_file_cookie,
    )

    async def handle(request: web.Request) -> web.StreamResponse:
        origin = extract_origin(
            request, trust_referer_as_origin=deps.trust_referer_as_origin
        )
        allowed = deps.allowed_origins
        same_host = request.host

        from .common import is_origin_allowed, client_ip

        if not is_origin_allowed(
            origin,
            allowed,
            same_origin_host=same_host,
            allow_missing=deps.allow_missing_origin,
        ):
            return json_response(
                {"error": "forbidden_origin"}, status=403,
                origin=origin, allowed_origins=allowed,
                same_origin_host=same_host,
            )
        ip = client_ip(request, trust_forwarded_for=deps.trust_forwarded_for)
        blocked, retry_after = await deps.ip_guard.is_blocked(ip)
        if blocked:
            return json_response(
                {"error": "ip_blocked", "retry_after": retry_after},
                status=429,
                origin=origin, allowed_origins=allowed,
                same_origin_host=same_host,
                extra_headers={"Retry-After": str(retry_after)},
            )

        # Same dual credential path as the image serve handler — bearer
        # first (JS fetches), then the path-scoped cookie (<a download>
        # can't set headers).
        token = None
        presented = extract_bearer(request)
        cookie_value: str | None = None
        if presented:
            gated = await gate_request(request, deps)
            if isinstance(gated, web.Response):
                return gated
            token = gated.token
        else:
            cookie_value = request.cookies.get(FILE_AUTH_COOKIE_NAME)
            if cookie_value and deps.file_cookie_secret:
                peek_parts = cookie_value.rsplit(".", 2)
                peek_name = peek_parts[0] if len(peek_parts) == 3 else ""
                token_row = (
                    await deps.storage.get_token_by_name(peek_name)
                    if peek_name
                    else None
                )
                if token_row is not None:
                    verified = verify_file_cookie(
                        deps.file_cookie_secret,
                        cookie_value,
                        current_token_hash=token_row.token_hash,
                    )
                    if verified is not None:
                        token_name_via_cookie, _exp = verified
                        invalidated = (
                            deps.cookie_logout_tracker is not None
                            and deps.cookie_logout_tracker.is_invalidated(
                                token_name_via_cookie, exp_ts=_exp
                            )
                        )
                        if (
                            not invalidated
                            and token_row.revoked_at is None
                            and not (
                                token_row.expires_at is not None
                                and token_row.expires_at <= int(time.time())
                            )
                        ):
                            token = token_row
        if token is None:
            no_credential_presented = (not presented) and (not cookie_value)
            if no_credential_presented:
                try:
                    await deps.ip_guard.record_failure(ip)
                except Exception:
                    logger.exception(
                        "[WebChatGateway] drop serve record_failure failed"
                    )
            try:
                await deps.audit.write(
                    "auth_fail",
                    ip=ip,
                    detail={
                        "reason": (
                            "no_token"
                            if no_credential_presented
                            else "bad_cookie"
                        ),
                        "endpoint": "drop_files",
                    },
                )
            except Exception:
                pass
            return json_response(
                {"error": "unauthorized"}, status=401,
                origin=origin, allowed_origins=allowed,
                same_origin_host=same_host,
            )
        await deps.ip_guard.reset(ip)

        file_id = (request.match_info.get("file_id") or "").strip()
        if not file_id or len(file_id) != 16 or not file_id.isalnum():
            # file_id is token_urlsafe(12) — [A-Za-z0-9_-]{16}. isalnum()
            # would reject '-'/'_', so validate with an explicit charset
            # check instead.
            if not re.fullmatch(r"[A-Za-z0-9_-]{16}", file_id):
                return json_response(
                    {"error": "invalid_file_id"}, status=400,
                    origin=origin, allowed_origins=allowed,
                    same_origin_host=same_host,
                )

        try:
            row = await deps.storage.get_file(file_id)
        except Exception:
            logger.exception("[WebChatGateway] drop serve get_file failed")
            return json_response(
                {"error": "internal_error"}, status=500,
                origin=origin, allowed_origins=allowed,
                same_origin_host=same_host,
            )
        # Ownership + namespace: the file must belong to this token AND
        # sit in the Drop namespace. A chat-upload file_id is NOT
        # servable here — its lifecycle belongs to its conversation.
        if (
            row is None
            or row.token_name != token.name
            or row.session_id != DROP_SESSION_ID
        ):
            if row is not None:
                try:
                    await deps.audit.write(
                        "drop_serve_blocked",
                        name=token.name,
                        ip=ip,
                        detail={"file_id": file_id, "reason": "cross_token"},
                    )
                except Exception:
                    pass
            return json_response(
                {"error": "not_found"}, status=404,
                origin=origin, allowed_origins=allowed,
                same_origin_host=same_host,
            )

        try:
            payload = await deps.file_store.read(storage_key=row.storage_key)
        except FileStoreUnavailable:
            logger.exception(
                "[WebChatGateway] drop file_store.read backend unavailable"
            )
            return json_response(
                {"error": "file_store_unavailable"}, status=503,
                origin=origin, allowed_origins=allowed,
                same_origin_host=same_host,
                extra_headers={"Retry-After": "5"},
            )
        except Exception:
            logger.exception("[WebChatGateway] drop file_store.read failed")
            return json_response(
                {"error": "internal_error"}, status=500,
                origin=origin, allowed_origins=allowed,
                same_origin_host=same_host,
            )
        if payload is None:
            return json_response(
                {"error": "not_found"}, status=404,
                origin=origin, allowed_origins=allowed,
                same_origin_host=same_host,
            )

        normalized_mime = (row.mime or "").split(";", 1)[0].strip().lower()
        is_image = normalized_mime in _INLINE_DROP_MIME
        cors = build_cors_headers(origin, allowed, same_origin_host=same_host)
        if is_image:
            disposition = 'inline'
            display_name = file_id
        else:
            disposition = "attachment"
            display_name = row.filename or f"drop-{file_id}"
        # RFC 6266: quoted-string for the filename; strip quotes and
        # backslashes so a crafted name can't break out of the quoted
        # form. Non-ASCII names use the filename*= parameter with proper
        # percent-encoding per RFC 5987/6266.
        safe_name = display_name.replace("\\", "_").replace('"', "_")
        has_non_ascii = any(ord(c) > 127 for c in safe_name)
        if has_non_ascii:
            from urllib.parse import quote
            # RFC 5987: percent-encode the filename*= value to prevent
            # special characters (semicolons, quotes, etc.) from breaking
            # the header syntax.
            disposition_value = (
                f'{disposition}; filename="download"; '
                f"filename*=UTF-8''{quote(safe_name, safe='')}"
            )
        else:
            disposition_value = f'{disposition}; filename="{safe_name}"'

        return web.Response(
            body=payload,
            status=200,
            headers={
                **cors,
                "Content-Type": normalized_mime or "application/octet-stream",
                "Cache-Control": "private, max-age=86400",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": disposition_value,
                "Referrer-Policy": "no-referrer",
            },
        )

    return handle


def make_drop_files_preflight(deps: DropDeps):
    async def handle(request: web.Request) -> web.Response:
        return preflight_response(
            origin=extract_origin(
                request, trust_referer_as_origin=deps.trust_referer_as_origin
            ),
            allowed=deps.allowed_origins,
            same_origin_host=request.host,
        )

    return handle


__all__ = [
    "DROP_SESSION_ID",
    "DropDeps",
    "make_drop_handlers",
    "make_drop_serve_handler",
    "make_drop_files_preflight",
]
