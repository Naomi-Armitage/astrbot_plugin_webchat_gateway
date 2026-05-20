"""Shared chat helpers — used by chat.py, chat_stream.py, and chat_files_auth.py.

Split out so the three handler modules don't form a circular import
chain (chat.py would otherwise need to import from chat_stream.py to
re-export, while chat_stream.py needs ChatDeps + _parse_chat_body from
chat.py). Pure carry-over from the original handlers/chat.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from aiohttp import web

from astrbot.api import logger

from ..core.audit import AuditLogger
from ..core.cookie_logout import CookieLogoutTracker
from ..core.file_store import FileStore
from ..core.ip_guard import IpGuard
from ..core.llm_bridge import LlmBridge
from ..core.ratelimit import PerTokenConcurrency
from ..core.stream_registry import StreamRegistry
from ..storage.base import AbstractStorage, FileRow, TokenRow
from .common import gate_request, json_response

if TYPE_CHECKING:
    from .conversations import ConversationService


_HEARTBEAT_INTERVAL = 20.0


def _is_expired(token: TokenRow, now: int) -> bool:
    return token.expires_at is not None and token.expires_at <= now


@dataclass
class ChatDeps:
    storage: AbstractStorage
    audit: AuditLogger
    ip_guard: IpGuard
    concurrency: PerTokenConcurrency
    llm_bridge: LlmBridge
    conv_service: ConversationService
    registry: StreamRegistry
    file_store: FileStore
    allowed_origins: set[str]
    max_message_length: int
    max_attachments_per_message: int
    trust_forwarded_for: bool
    # HMAC secret for issuing the /files/{id} access cookie. The chat
    # client renders attachments as plain `<img src>` which can't set
    # Authorization headers — instead we set an HttpOnly + SameSite=Lax
    # cookie on /me responses, scoped to the configured files prefix.
    # See `core/file_cookie.py` for the protocol. Always set by main.py;
    # default factory keeps a bare ChatDeps test-construct usable.
    file_cookie_secret: bytes = b""
    file_cookie_ttl_seconds: int = 86400
    # Path attribute on the Set-Cookie. MUST match the actual /files
    # route prefix (`endpoint_prefix + "/files"`) so the browser scopes
    # the cookie correctly. Hardcoding would break operators who change
    # `endpoint_prefix` from the default.
    file_cookie_path: str = "/api/webchat/files"
    # In-memory tracker for server-side cookie invalidation on logout.
    # `make_logout_handler` records into this; `make_serve_handler` (in
    # handlers/files.py via UploadDeps) reads from a separate copy of
    # the same tracker instance so both endpoints stay consistent.
    # See `core/cookie_logout.py` for the exact semantic.
    cookie_logout_tracker: CookieLogoutTracker | None = None
    trust_referer_as_origin: bool = False
    allow_missing_origin: bool = False


@dataclass
class _ParsedRequest:
    session_id: str
    user_id: str
    username: str
    message: str
    attachments: list[str]  # list of file_ids


class _ParseError(Exception):
    """Raised by `_parse_payload` to short-circuit with a specific code."""

    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def _parse_payload(payload: Any, *, max_attachments: int) -> _ParsedRequest | None:
    if not isinstance(payload, dict):
        return None
    message = str(payload.get("message") or "").strip()
    raw_attachments = payload.get("attachments")
    attachments: list[str] = []
    if raw_attachments is not None:
        if not isinstance(raw_attachments, list):
            raise _ParseError("invalid_payload")
        if len(raw_attachments) > max_attachments:
            raise _ParseError("too_many_attachments")
        seen: set[str] = set()
        for entry in raw_attachments:
            if not isinstance(entry, dict):
                raise _ParseError("invalid_payload")
            fid = entry.get("file_id")
            if not isinstance(fid, str) or not fid:
                raise _ParseError("invalid_payload")
            fid = fid.strip()
            if not fid:
                raise _ParseError("invalid_payload")
            if fid in seen:
                # Silent dedup — repeated file_ids in one message are
                # not a security issue but they'd over-count toward the
                # `mark_files_committed` set; collapse here so the
                # downstream code only deals with unique ids.
                continue
            seen.add(fid)
            attachments.append(fid)
    if not message and not attachments:
        return None
    session_id = str(
        payload.get("sessionId") or payload.get("session_id") or "webchat"
    ).strip() or "webchat"
    user_id = str(payload.get("userId") or payload.get("user_id") or "").strip()
    username = (str(payload.get("username") or "").strip() or "WebUser")[:64]
    return _ParsedRequest(
        session_id=session_id[:128],
        user_id=user_id[:128],
        username=username,
        message=message,
        attachments=attachments,
    )


async def _parse_chat_body(
    request: web.Request,
    max_message_length: int,
    *,
    max_attachments: int,
    origin: str | None,
    allowed: set[str],
    same_host: str,
) -> _ParsedRequest | web.Response:
    """Parse the JSON body for /chat-style requests, applying the same
    error-shape contract both /chat and /chat/stream advertise. Returns
    either the parsed payload or an already-serialized error Response."""
    try:
        payload = await request.json()
    except web.HTTPRequestEntityTooLarge:
        return json_response(
            {"error": "payload_too_large"}, status=413,
            origin=origin, allowed_origins=allowed, same_origin_host=same_host,
        )
    except json.JSONDecodeError:
        return json_response(
            {"error": "invalid_json"}, status=400,
            origin=origin, allowed_origins=allowed, same_origin_host=same_host,
        )
    except Exception:
        logger.exception("[WebChatGateway] unexpected JSON parse error")
        return json_response(
            {"error": "invalid_json"}, status=400,
            origin=origin, allowed_origins=allowed, same_origin_host=same_host,
        )
    try:
        data = _parse_payload(payload, max_attachments=max_attachments)
    except _ParseError as exc:
        return json_response(
            {"error": exc.code}, status=exc.status,
            origin=origin, allowed_origins=allowed, same_origin_host=same_host,
        )
    if data is None:
        return json_response(
            {"error": "invalid_payload"}, status=400,
            origin=origin, allowed_origins=allowed, same_origin_host=same_host,
        )
    if len(data.message) > max_message_length:
        return json_response(
            {"error": "message_too_long", "max_length": max_message_length},
            status=400,
            origin=origin, allowed_origins=allowed, same_origin_host=same_host,
        )
    return data


@dataclass
class PreparedChatRequest:
    """Output of the shared /chat & /chat/stream preamble.

    Bundles the gate result + parsed body + attachment ownership rows so
    both handlers can drop straight into their lock-acquisition step.
    `attachment_rows` mirrors `data.attachments` ordering and is empty
    when the body carries no file_ids.
    """

    token: TokenRow
    ip: str
    origin: str | None
    allowed: set[str]
    same_host: str
    data: _ParsedRequest
    attachment_rows: list[FileRow]


async def prepare_chat_request(
    request: web.Request, deps: ChatDeps
) -> PreparedChatRequest | web.Response:
    """Run the gate → body-parse → attachment-ownership preamble shared
    by /chat and /chat/stream.

    Returns a `PreparedChatRequest` bundle on success, or an already-
    CORS'd error Response on any short-circuit. Mirrors the original
    inline sequence verbatim — ordering matters because each step
    short-circuits before the next can pin a per-token slot, and any
    drift between the two handlers reintroduces the bugs the dedup
    was meant to retire.
    """
    gated = await gate_request(request, deps)
    if isinstance(gated, web.Response):
        return gated

    parsed = await _parse_chat_body(
        request, deps.max_message_length,
        max_attachments=deps.max_attachments_per_message,
        origin=gated.origin, allowed=gated.allowed, same_host=gated.same_host,
    )
    if isinstance(parsed, web.Response):
        return parsed

    # Validate attachment ownership before any per-token lock is taken so
    # a stale or cross-token file_id can't pin a chat / streaming slot
    # during the storage round trip. Each attachment must belong to THIS
    # token AND THIS session — cross-session reuse is rejected to prevent
    # a token from leaking a file_id into another's session via the wire.
    attachment_rows: list[FileRow] = []
    for fid in parsed.attachments:
        try:
            row = await deps.storage.get_file(fid)
        except Exception:
            logger.exception(
                "[WebChatGateway] get_file failed file_id=%s", fid
            )
            return json_response(
                {"error": "internal_error"},
                status=500,
                origin=gated.origin,
                allowed_origins=gated.allowed,
                same_origin_host=gated.same_host,
            )
        if (
            row is None
            or row.token_name != gated.token.name
            or row.session_id != parsed.session_id
        ):
            return json_response(
                {"error": "invalid_attachment"},
                status=400,
                origin=gated.origin,
                allowed_origins=gated.allowed,
                same_origin_host=gated.same_host,
            )
        attachment_rows.append(row)

    return PreparedChatRequest(
        token=gated.token,
        ip=gated.ip,
        origin=gated.origin,
        allowed=gated.allowed,
        same_host=gated.same_host,
        data=parsed,
        attachment_rows=attachment_rows,
    )
