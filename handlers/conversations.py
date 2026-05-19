"""HTTP handlers + ConversationDeps for the chat-sync layer.

`ConversationService` (the single owner of writes to `webchat_updates`
and CM history mutations) lives in `conversations_service.py`; the
pure helpers (`_normalize_history`, `_extract_*`, event-type constants)
in `conversations_overlay.py`. This file keeps only the handler-side
surface: `ConversationDeps`, payload formatters, query parsers, the
patch-body validator, and `make_conversation_handlers`.

Re-exports `ConversationService` and its result dataclasses at module
top so external `from .conversations import ConversationService` /
`from .conversations import ConversationDetail` callers keep working
through the split.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from astrbot.api import logger

from ..core.audit import AuditLogger
from ..core.event_bus import EventBus
from ..core.file_store import FileStore
from ..core.ip_guard import IpGuard
from ..core.ratelimit import PerTokenConcurrency
from ..storage.base import AbstractStorage, SessionMetaRow
from .admin_tokens import ServiceError
from .common import (
    build_cors_headers,
    error_response,
    extract_origin,
    gate_request,
    json_response,
    preflight_response,
)
# Backward-compat re-exports: the standalone helpers + event-type
# constants moved out to conversations_overlay; the service + its
# result types moved to conversations_service. External
# `from .conversations import X` keeps working.
from .conversations_overlay import (  # noqa: F401
    EVENT_HISTORY_CLEARED,
    EVENT_MESSAGE_ADDED,
    EVENT_MESSAGE_DELETED,
    EVENT_SESSION_CREATED,
    EVENT_SESSION_META_UPDATED,
    EVENT_STREAM_ENDED,
    EVENT_STREAM_STARTED,
    _extract_attachment_file_ids,
    _extract_text,
    _normalize_history,
    _renderable_entry,
)
from .conversations_service import (  # noqa: F401
    ConversationDetail,
    ConversationListItem,
    ConversationListResult,
    ConversationService,
    EventsResult,
    RegenerateResult,
    _PREVIEW_CHARS,
    _umo,
)
# `_MAX_EVENT_TIMEOUT` is the service-side hard cap on long-poll hold
# time; the handler also clamps the user-supplied `timeout` query
# parameter to it so the validation and the eventual enforcement
# agree on the bound.
from .conversations_service import _MAX_EVENT_TIMEOUT


# Handler-side constants. `_DEFAULT_EVENT_TIMEOUT` is the long-poll
# default when the client doesn't supply one. `_TITLE_MAX` is the
# UTF-8 char cap on `PATCH /conversations/{sid}` title rewrites.
_DEFAULT_EVENT_TIMEOUT = 25.0
_TITLE_MAX = 255


@dataclass
class ConversationDeps:
    storage: AbstractStorage
    audit: AuditLogger
    event_bus: EventBus
    cm: Any  # AstrBot ConversationManager — loose-typed to avoid the import
    file_store: FileStore  # used by ConversationService to resolve attachments
    allowed_origins: set[str]
    trust_forwarded_for: bool
    trust_referer_as_origin: bool = False
    allow_missing_origin: bool = False
    ip_guard: IpGuard | None = None  # required by gate_request
    # Optional — when supplied, `clear_history` takes the per-token
    # concurrency lock so it cannot run while a `/chat/stream` is in
    # flight for the same token. Without this, a clear can race the
    # stream's record_chat_pair: it lists+deletes attachments, then
    # the stream writes a CM ImageURLPart pointing at the now-gone
    # file_id, and the next history fetch renders broken thumbnails.
    concurrency: PerTokenConcurrency | None = None


def _strict_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ServiceError("invalid_payload", status=400)
    return value



# ----- HTTP handlers -----


def _meta_payload(row: SessionMetaRow) -> dict:
    return {
        "session_id": row.session_id,
        "title": row.title,
        "title_manual": row.title_manual,
        "pinned": row.pinned_at is not None,
        "deleted": row.deleted_at is not None,
        "updated_at": row.updated_at,
    }


def _list_item_payload(item: ConversationListItem) -> dict:
    return {
        "session_id": item.session_id,
        "title": item.title,
        "title_manual": item.title_manual,
        "pinned": item.pinned,
        "updated_at": item.updated_at,
        "message_count": item.message_count,
        "preview": item.preview,
    }


def _events_payload(result: EventsResult) -> dict:
    if result.too_far:
        return {
            "tooFar": True,
            "last_pts": result.last_pts,
            "events": [],
        }
    out_events: list[dict] = []
    for ev in result.events:
        try:
            payload_obj = json.loads(ev.payload) if ev.payload else {}
        except (TypeError, ValueError):
            payload_obj = {}
        out_events.append(
            {
                "pts": ev.pts,
                "ts": ev.ts,
                "event_type": ev.event_type,
                "session_id": ev.session_id,
                "payload": payload_obj,
            }
        )
    return {
        "events": out_events,
        "last_pts": result.last_pts,
        "has_more": result.has_more,
    }


def _validate_patch_body(body: object) -> dict:
    """Pull the four allowed fields out of a PATCH body, with strict types."""
    if not isinstance(body, dict):
        raise ServiceError("invalid_payload", status=400)
    out: dict[str, Any] = {}
    if "title" in body:
        raw = body.get("title")
        if not isinstance(raw, str):
            raise ServiceError("invalid_payload", status=400)
        title = raw.strip()
        if len(title) > _TITLE_MAX:
            raise ServiceError("invalid_payload", status=400)
        out["title"] = title
    if "title_manual" in body:
        out["title_manual"] = _strict_bool(body.get("title_manual"))
    if "pinned" in body:
        out["pinned"] = _strict_bool(body.get("pinned"))
    if "deleted" in body:
        out["deleted"] = _strict_bool(body.get("deleted"))
    if not out:
        raise ServiceError("invalid_payload", status=400)
    return out


def _parse_int_query(raw: Any, *, default: int, lo: int, hi: int) -> int:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _parse_float_query(
    raw: Any, *, default: float, lo: float, hi: float
) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def make_conversation_handlers(
    deps: ConversationDeps, service: ConversationService
):
    allowed = deps.allowed_origins
    trust_referer = deps.trust_referer_as_origin

    def _err(request: web.Request, origin, exc: ServiceError) -> web.Response:
        return error_response(request, origin=origin, allowed=allowed, exc=exc)

    async def list_conversations(request: web.Request) -> web.Response:
        gated = await gate_request(request, deps)
        if isinstance(gated, web.Response):
            return gated
        try:
            result = await service.list_conversations(
                token_name=gated.token.name
            )
        except ServiceError as exc:
            return _err(request, gated.origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] list_conversations failed")
            return _err(
                request, gated.origin, ServiceError("internal_error", status=500)
            )
        return json_response(
            {
                "last_pts": result.last_pts,
                "conversations": [
                    _list_item_payload(it) for it in result.conversations
                ],
            },
            origin=gated.origin,
            allowed_origins=gated.allowed,
            same_origin_host=gated.same_host,
        )

    async def get_conversation(request: web.Request) -> web.Response:
        gated = await gate_request(request, deps)
        if isinstance(gated, web.Response):
            return gated
        session_id = (request.match_info.get("session_id") or "").strip()[:128]
        if not session_id:
            return _err(
                request, gated.origin, ServiceError("invalid_payload", status=400)
            )
        try:
            detail = await service.get_conversation(
                token_name=gated.token.name, session_id=session_id
            )
        except ServiceError as exc:
            return _err(request, gated.origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] get_conversation failed")
            return _err(
                request, gated.origin, ServiceError("internal_error", status=500)
            )
        return json_response(
            {
                "session_id": detail.session_id,
                "title": detail.title,
                "title_manual": detail.title_manual,
                "pinned": detail.pinned,
                "updated_at": detail.updated_at,
                "messages": detail.messages,
            },
            origin=gated.origin,
            allowed_origins=gated.allowed,
            same_origin_host=gated.same_host,
        )

    async def patch_conversation(request: web.Request) -> web.Response:
        gated = await gate_request(request, deps)
        if isinstance(gated, web.Response):
            return gated
        session_id = (request.match_info.get("session_id") or "").strip()[:128]
        if not session_id:
            return _err(
                request, gated.origin, ServiceError("invalid_payload", status=400)
            )
        try:
            body = await request.json()
        except web.HTTPRequestEntityTooLarge:
            return _err(
                request, gated.origin, ServiceError("payload_too_large", status=413)
            )
        except (json.JSONDecodeError, ValueError):
            return _err(
                request, gated.origin, ServiceError("invalid_json", status=400)
            )
        except Exception:
            logger.exception("[WebChatGateway] patch_conversation parse failed")
            return _err(
                request, gated.origin, ServiceError("invalid_json", status=400)
            )
        try:
            fields = _validate_patch_body(body)
            row = await service.update_metadata(
                token_name=gated.token.name,
                session_id=session_id,
                title=fields.get("title"),
                title_manual=fields.get("title_manual"),
                pinned=fields.get("pinned"),
                deleted=fields.get("deleted"),
                ip=gated.ip,
            )
        except ServiceError as exc:
            return _err(request, gated.origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] patch_conversation failed")
            return _err(
                request, gated.origin, ServiceError("internal_error", status=500)
            )
        return json_response(
            _meta_payload(row),
            origin=gated.origin,
            allowed_origins=gated.allowed,
            same_origin_host=gated.same_host,
        )

    async def clear_conversation(request: web.Request) -> web.Response:
        gated = await gate_request(request, deps)
        if isinstance(gated, web.Response):
            return gated
        session_id = (request.match_info.get("session_id") or "").strip()[:128]
        if not session_id:
            return _err(
                request, gated.origin, ServiceError("invalid_payload", status=400)
            )
        try:
            row = await service.clear_history(
                token_name=gated.token.name,
                session_id=session_id,
                ip=gated.ip,
            )
        except ServiceError as exc:
            return _err(request, gated.origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] clear_conversation failed")
            return _err(
                request, gated.origin, ServiceError("internal_error", status=500)
            )
        return json_response(
            _meta_payload(row),
            origin=gated.origin,
            allowed_origins=gated.allowed,
            same_origin_host=gated.same_host,
        )

    async def get_events(request: web.Request) -> web.Response:
        gated = await gate_request(request, deps)
        if isinstance(gated, web.Response):
            return gated
        since_raw = request.query.get("since")
        if since_raw is None or since_raw == "":
            since = 0
        else:
            try:
                since = int(since_raw)
            except (TypeError, ValueError):
                return _err(
                    request,
                    gated.origin,
                    ServiceError("invalid_since", status=400),
                )
            if since < 0:
                return _err(
                    request,
                    gated.origin,
                    ServiceError("invalid_since", status=400),
                )
        timeout = _parse_float_query(
            request.query.get("timeout"),
            default=_DEFAULT_EVENT_TIMEOUT,
            lo=0.0,
            hi=_MAX_EVENT_TIMEOUT,
        )
        try:
            result = await service.get_events(
                token_name=gated.token.name,
                since_pts=since,
                timeout=timeout,
            )
        except ServiceError as exc:
            return _err(request, gated.origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] get_events failed")
            return _err(
                request, gated.origin, ServiceError("internal_error", status=500)
            )
        return json_response(
            _events_payload(result),
            origin=gated.origin,
            allowed_origins=gated.allowed,
            same_origin_host=gated.same_host,
            extra_headers={"Cache-Control": "no-store"},
        )

    async def delete_message(request: web.Request) -> web.Response:
        """DELETE {prefix}/conversations/{session_id}/messages/{message_index}

        Splices a single message at `message_index` (0-based into the
        rendered history) out of CM history. Releases attachment files no
        longer referenced by the surviving messages. Emits a
        `message_deleted` event for peers.

        Responses:
          200 `{ok: true, session_id, title, ..., message_count, preview}`
              — deleted; payload includes the refreshed session meta so
              the client can update its sidebar entry inline.
          400 `{error: "invalid_payload"}` — bad session_id or index.
          401 — auth gate.
          404 `{error: "session_not_found"|"message_not_found"}`.
          429 `{error: "concurrent_request"}` — stream in-flight.
          500 `{error: "internal_error"|"delete_failed"}`.
        """
        gated = await gate_request(request, deps)
        if isinstance(gated, web.Response):
            return gated
        session_id = (request.match_info.get("session_id") or "").strip()[:128]
        if not session_id:
            return _err(
                request, gated.origin, ServiceError("invalid_payload", status=400)
            )
        raw_index = request.match_info.get("message_index") or ""
        try:
            message_index = int(raw_index)
        except (TypeError, ValueError):
            return _err(
                request, gated.origin, ServiceError("invalid_payload", status=400)
            )
        try:
            row = await service.delete_message_by_index(
                token_name=gated.token.name,
                session_id=session_id,
                message_index=message_index,
                ip=gated.ip,
            )
        except ServiceError as exc:
            return _err(request, gated.origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] delete_message failed")
            return _err(
                request, gated.origin, ServiceError("internal_error", status=500)
            )
        return json_response(
            {"ok": True, **_meta_payload(row)},
            origin=gated.origin,
            allowed_origins=gated.allowed,
            same_origin_host=gated.same_host,
        )

    async def regenerate_message(request: web.Request) -> web.Response:
        """POST {prefix}/conversations/{session_id}/regenerate

        Body: `{"message_index": int}` — index of the assistant message
        to regenerate. The user message preceding it stays. The endpoint
        truncates CM history to `[0, message_index)`, runs the
        **streaming** LLM call, appends the new assistant reply, and
        emits `message_deleted` + `message_added` events for peers.

        Returns `text/event-stream`. Each SSE data frame is JSON. Event
        shapes:
          * `{"type": "chunk", "delta": str}` — incremental text from
            the LLM
          * `{"type": "done", "reply": str, "remaining": int,
            "daily_quota": int}` — final state once persistence + event
            emission complete (the LLM reply has finished + been
            persisted; the SSE will close right after)
          * `{"type": "error", "code": str}` — terminal failure. Codes
            mirror the JSON error codes the previous non-streaming
            handler returned: `invalid_payload` / `invalid_json` /
            `session_not_found` / `message_not_found` /
            `concurrent_request` / `quota_exceeded` / `empty_reply` /
            `llm_timeout` / `llm_call_failed` / `regenerate_failed` /
            `internal_error`.

        Before SSE handshake (auth / parse errors) the response is a
        plain JSON error (same as the previous handler). Once `prepare`
        has flushed the 200 + event-stream headers, ALL further errors
        come back as SSE `{"type": "error", ...}` frames.
        """
        gated = await gate_request(request, deps)
        if isinstance(gated, web.Response):
            return gated
        session_id = (request.match_info.get("session_id") or "").strip()[:128]
        if not session_id:
            return _err(
                request, gated.origin, ServiceError("invalid_payload", status=400)
            )
        try:
            body = await request.json()
        except web.HTTPRequestEntityTooLarge:
            return _err(
                request, gated.origin, ServiceError("payload_too_large", status=413)
            )
        except (json.JSONDecodeError, ValueError):
            return _err(
                request, gated.origin, ServiceError("invalid_json", status=400)
            )
        except Exception:
            logger.exception("[WebChatGateway] regenerate parse failed")
            return _err(
                request, gated.origin, ServiceError("invalid_json", status=400)
            )
        if not isinstance(body, dict):
            return _err(
                request, gated.origin, ServiceError("invalid_payload", status=400)
            )
        raw_index = body.get("message_index")
        if not isinstance(raw_index, int) or isinstance(raw_index, bool):
            # Reject bool (json would parse `true` as a number-like value
            # in some loose checks) AND non-int. Negative indices are
            # caught downstream as message_not_found.
            return _err(
                request, gated.origin, ServiceError("invalid_payload", status=400)
            )

        cors = build_cors_headers(
            gated.origin, gated.allowed, same_origin_host=gated.same_host
        )
        response = web.StreamResponse(
            status=200,
            headers={
                **cors,
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-store",
                # nginx: disable response buffering. Apache mod_proxy
                # obeys this same header. Without it, intermediate
                # proxies hold chunks until a buffer fills, defeating
                # streaming.
                "X-Accel-Buffering": "no",
            },
        )

        async def write_frame(payload: dict) -> bool:
            """Serialise + write one SSE data frame. Returns False on
            disconnect/write failure so the caller can stop iterating
            (and skip further side-effect-free chunk emits). The done /
            error frames still go through the same path; if the write
            fails on done, the server state is already consistent
            (events persisted, audit written) — the client just won't
            see the final acknowledgement, which is recoverable via
            list_conversations on the next refresh."""
            try:
                data = json.dumps(payload, ensure_ascii=False)
                await response.write(
                    ("data: " + data + "\n\n").encode("utf-8")
                )
                return True
            except (ConnectionResetError, asyncio.CancelledError):
                raise
            except Exception:
                # Write errors past handshake are usually disconnects.
                # Don't log.exception per-chunk (could be one entry per
                # token in a long reply); log once at caller scope.
                return False

        # SSE handshake. Failure here means the client closed before we
        # could send the 200 + event-stream headers. Return a plain
        # response (already started, so just bail) and let the upstream
        # task drain.
        try:
            await response.prepare(request)
            await response.write(b": ready\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            return response
        except Exception:
            logger.exception(
                "[WebChatGateway] regenerate SSE handshake failed"
            )
            return response

        client_connected = True
        try:
            async for evt in service.regenerate_assistant_message_stream(
                token_name=gated.token.name,
                session_id=session_id,
                message_index=raw_index,
                token_daily_quota=gated.token.daily_quota,
                ip=gated.ip,
            ):
                if not client_connected:
                    # Client gone — keep draining the generator so the
                    # CM write + event emission still happen, but stop
                    # bothering with the (failing) SSE writes.
                    continue
                ok = await write_frame(evt)
                if not ok:
                    client_connected = False
        except ServiceError as exc:
            if client_connected:
                await write_frame({"type": "error", "code": exc.code})
        except (ConnectionResetError, asyncio.CancelledError):
            # Client disconnect during chunk write. Generator may have
            # raised this too if it was awaiting a write at the time.
            # Either way, just bail — service-side persistence either
            # already happened or will be rolled back by exception
            # propagation.
            return response
        except Exception:
            logger.exception(
                "[WebChatGateway] regenerate SSE stream failed"
            )
            if client_connected:
                await write_frame({"type": "error", "code": "internal_error"})

        try:
            await response.write_eof()
        except Exception:
            pass
        return response

    async def preflight(request: web.Request) -> web.Response:
        return preflight_response(
            origin=extract_origin(request, trust_referer_as_origin=trust_referer),
            allowed=allowed,
            same_origin_host=request.host,
        )

    return {
        "list": list_conversations,
        "get": get_conversation,
        "patch": patch_conversation,
        "clear": clear_conversation,
        "delete_message": delete_message,
        "regenerate_message": regenerate_message,
        "events": get_events,
        "preflight": preflight,
    }


__all__ = [
    "ConversationDeps",
    "ConversationService",
    "make_conversation_handlers",
]
