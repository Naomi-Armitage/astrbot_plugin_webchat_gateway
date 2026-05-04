"""ConversationService and the five chat-sync HTTP handlers.

Service owns every write to `webchat_updates` so the per-token pts allocation
stays single-flight inside the storage layer. Handlers run through the
shared `gate_request` (origin → IP → bearer → revoked/expired) like /chat,
then delegate. Mutations append events under one storage call so peers see
multi-event changes (e.g. session_created + 2x message_added) atomically.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from aiohttp import web

from astrbot.api import logger

from ..core.audit import AuditLogger
from ..core.event_bus import EventBus
from ..core.ip_guard import IpGuard
from ..storage.base import (
    _UNSET,
    AbstractStorage,
    NewEvent,
    SessionMetaRow,
    UpdateRow,
)
from .admin_tokens import ServiceError
from .common import (
    client_ip,
    extract_origin,
    gate_request,
    json_response,
    preflight_response,
)


_TOO_FAR_THRESHOLD = 1000
_DEFAULT_EVENT_TIMEOUT = 25.0
_MAX_EVENT_TIMEOUT = 30.0
_GET_EVENTS_LIMIT = 100
_PREVIEW_CHARS = 80
_TITLE_MAX = 255
# Per-token concurrent long-poll cap. Above this, a new long-poll is
# silently degraded to short-poll (timeout=0) so a single account opening
# many tabs can't pin one async task + socket per tab indefinitely. The
# client's transport state machine sees consecutive zero-event responses
# and naturally falls back to short-polling at the FE layer.
_MAX_LONG_POLL_PER_TOKEN = 8

EVENT_SESSION_CREATED = "session_created"
EVENT_SESSION_META_UPDATED = "session_meta_updated"
EVENT_MESSAGE_ADDED = "message_added"
EVENT_HISTORY_CLEARED = "history_cleared"


@dataclass
class ConversationDeps:
    storage: AbstractStorage
    audit: AuditLogger
    event_bus: EventBus
    cm: Any  # AstrBot ConversationManager — loose-typed to avoid the import
    allowed_origins: set[str]
    trust_forwarded_for: bool
    trust_referer_as_origin: bool = False
    allow_missing_origin: bool = False
    ip_guard: IpGuard | None = None  # required by gate_request


# ----- service result shapes -----


@dataclass(frozen=True)
class ConversationListItem:
    session_id: str
    title: str
    title_manual: bool
    pinned: bool
    updated_at: int
    message_count: int
    preview: str


@dataclass(frozen=True)
class ConversationListResult:
    conversations: list[ConversationListItem]
    last_pts: int


@dataclass(frozen=True)
class ConversationDetail:
    session_id: str
    title: str
    title_manual: bool
    pinned: bool
    updated_at: int
    messages: list[dict]


@dataclass(frozen=True)
class EventsResult:
    events: list[UpdateRow]
    last_pts: int
    has_more: bool
    too_far: bool = False


# ----- helpers -----


def _umo(token_name: str, session_id: str) -> str:
    """Match the namespacing used by LlmBridge so CM lookups line up."""
    return f"webchat_gateway:{token_name}:{session_id}"


def _extract_text(content: Any) -> str:
    """Pull human-readable text out of a CM message record.

    AstrBot stores messages as either OpenAI-style `{role, content}` with a
    string content, or `{role, content: [{type: "text", text: "..."}, ...]}`
    when message segments are involved (see UserMessageSegment / TextPart in
    llm_bridge.py). Anything else collapses to "".
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for seg in content:
            if isinstance(seg, dict):
                t = seg.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "".join(parts)
    return ""


def _normalize_history(raw: Any) -> list[dict]:
    """CM history is JSON; render it as `[{role, content}, ...]` with
    text-only content. Tool calls, system messages, anything we can't
    flatten to text are dropped — the chat UI doesn't render them."""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in ("user", "assistant"):
            continue
        text = _extract_text(item.get("content"))
        if not text:
            continue
        out.append({"role": role, "content": text})
    return out


def _strict_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ServiceError("invalid_payload", status=400)
    return value


# ----- service -----


class ConversationService:
    """Wraps AstrBot CM + the meta/updates tables. Single owner of
    `webchat_updates` writes."""

    def __init__(
        self,
        *,
        storage: AbstractStorage,
        audit: AuditLogger,
        event_bus: EventBus,
        cm: Any,
    ) -> None:
        self._storage = storage
        self._audit = audit
        self._event_bus = event_bus
        self._cm = cm

    @staticmethod
    def _now() -> int:
        return int(time.time())

    async def _cm_history(
        self, *, token_name: str, session_id: str
    ) -> tuple[list[dict], str | None]:
        """Return (messages, conversation_id). conversation_id is None if
        there is no conversation yet."""
        umo = _umo(token_name, session_id)
        try:
            cid = await self._cm.get_curr_conversation_id(umo)
        except Exception:
            logger.exception(
                "[WebChatGateway] CM.get_curr_conversation_id failed"
            )
            return [], None
        if not cid:
            return [], None
        try:
            conv = await self._cm.get_conversation(umo, cid)
        except Exception:
            logger.exception("[WebChatGateway] CM.get_conversation failed")
            return [], cid
        if not conv:
            return [], cid
        history_raw = getattr(conv, "history", None)
        if isinstance(history_raw, str):
            try:
                parsed = json.loads(history_raw or "[]")
            except (TypeError, ValueError):
                parsed = []
        else:
            parsed = history_raw or []
        return _normalize_history(parsed), cid

    async def list_conversations(
        self, *, token_name: str
    ) -> ConversationListResult:
        # Read-only: meta carries cached `message_count` + `preview` (kept
        # in sync by record_chat_pair / clear_history / lazy backfill in
        # get_conversation), so the sidebar refresh runs as a single SELECT
        # — no per-row CM lookup like the v3 implementation did.
        rows = await self._storage.list_session_meta(
            token_name=token_name, include_deleted=False
        )
        items: list[ConversationListItem] = []
        for meta in rows:
            items.append(
                ConversationListItem(
                    session_id=meta.session_id,
                    title=meta.title,
                    title_manual=meta.title_manual,
                    pinned=meta.pinned_at is not None,
                    updated_at=meta.updated_at,
                    message_count=meta.message_count,
                    preview=meta.preview,
                )
            )
        items.sort(
            key=lambda it: (1 if it.pinned else 0, it.updated_at),
            reverse=True,
        )
        last_pts = await self._storage.get_max_pts(token_name=token_name)
        return ConversationListResult(conversations=items, last_pts=last_pts)

    async def get_conversation(
        self, *, token_name: str, session_id: str
    ) -> ConversationDetail:
        meta = await self._storage.get_session_meta(
            token_name=token_name, session_id=session_id
        )
        messages, _cid = await self._cm_history(
            token_name=token_name, session_id=session_id
        )
        if meta is None and not messages:
            raise ServiceError("not_found", status=404)
        if meta is None:
            # Legacy session: CM has messages but we never recorded meta
            # (pre-v3 install or a record_chat_pair that failed earlier).
            # Lazy-create the meta row with the cache populated from CM,
            # so peer devices learn about the session on their next list
            # refresh. We do NOT emit an event here — keeping GET
            # idempotent enough that retries don't multiply notifications.
            now = self._now()
            preview_src = messages[-1]["content"] if messages else ""
            meta = await self._storage.upsert_session_meta(
                token_name=token_name,
                session_id=session_id,
                title="",
                title_manual=False,
                message_count=len(messages),
                preview=preview_src[:_PREVIEW_CHARS],
                now=now,
            )
        return ConversationDetail(
            session_id=session_id,
            title=meta.title,
            title_manual=meta.title_manual,
            pinned=meta.pinned_at is not None,
            updated_at=meta.updated_at,
            messages=messages,
        )

    async def update_metadata(
        self,
        *,
        token_name: str,
        session_id: str,
        title: str | None = None,
        title_manual: bool | None = None,
        pinned: bool | None = None,
        deleted: bool | None = None,
        ip: str | None = None,
    ) -> SessionMetaRow:
        # PATCH allows lazy-create per spec. Validation has already happened
        # at the handler boundary; this layer trusts its inputs but still
        # records which fields changed for audit purposes.
        changed: dict[str, Any] = {}
        if title is not None:
            changed["title"] = True  # field-changed marker only; never log text
        if title_manual is not None:
            changed["title_manual"] = title_manual
        now = self._now()
        pinned_arg: int | None | object = _UNSET
        if pinned is not None:
            pinned_arg = now if pinned else None
            changed["pinned"] = pinned
        deleted_arg: int | None | object = _UNSET
        if deleted is not None:
            deleted_arg = now if deleted else None
            changed["deleted"] = deleted
        row = await self._storage.upsert_session_meta(
            token_name=token_name,
            session_id=session_id,
            title=title,
            title_manual=title_manual,
            pinned_at=pinned_arg,
            deleted_at=deleted_arg,
            now=now,
        )
        # Build the event payload from caller-visible fields. We deliberately
        # do NOT include the literal title text in the audit detail (security
        # rule) — but the event payload IS the wire format peers consume,
        # so it has to carry the new title.
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if title_manual is not None:
            payload["title_manual"] = title_manual
        if pinned is not None:
            payload["pinned"] = pinned
        if deleted is not None:
            payload["deleted"] = deleted
        if payload:
            await self._storage.append_updates(
                token_name=token_name,
                events=[
                    NewEvent(
                        event_type=EVENT_SESSION_META_UPDATED,
                        session_id=session_id,
                        payload=json.dumps(payload, ensure_ascii=False),
                    )
                ],
                now=now,
            )
            await self._event_bus.notify(token_name)
        # Audit records WHICH fields changed, never the values. `title` here
        # is a bool flag, not the new text; pinned/deleted carry only the
        # new bool which is non-sensitive (binary state).
        await self._audit.write(
            "conv_meta_update",
            name=token_name,
            ip=ip,
            detail={"session_id": session_id, "fields": list(changed.keys())},
        )
        return row

    async def clear_history(
        self,
        *,
        token_name: str,
        session_id: str,
        ip: str | None = None,
    ) -> SessionMetaRow:
        umo = _umo(token_name, session_id)
        try:
            cid = await self._cm.get_curr_conversation_id(umo)
        except Exception:
            logger.exception(
                "[WebChatGateway] CM.get_curr_conversation_id failed"
            )
            cid = None
        if cid:
            try:
                # Passing history=[] zeros the conversation content while
                # leaving the conversation row, persona, and selection state
                # intact — matches "soft" wipe semantics expected by the UI.
                await self._cm.update_conversation(
                    unified_msg_origin=umo,
                    conversation_id=cid,
                    history=[],
                )
            except Exception:
                logger.exception(
                    "[WebChatGateway] CM.update_conversation(clear) failed"
                )
                raise ServiceError("clear_failed", status=500) from None
        now = self._now()
        row = await self._storage.upsert_session_meta(
            token_name=token_name,
            session_id=session_id,
            title="",
            title_manual=False,
            message_count=0,
            preview="",
            now=now,
        )
        await self._storage.append_updates(
            token_name=token_name,
            events=[
                NewEvent(
                    event_type=EVENT_HISTORY_CLEARED,
                    session_id=session_id,
                    payload="{}",
                )
            ],
            now=now,
        )
        await self._event_bus.notify(token_name)
        await self._audit.write(
            "conv_history_cleared",
            name=token_name,
            ip=ip,
            detail={"session_id": session_id},
        )
        return row

    async def record_chat_pair(
        self,
        *,
        token_name: str,
        session_id: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        # Must never raise: the chat reply has already been delivered to the
        # client. A failure here is a sync hiccup, not a chat error. We
        # log + audit instead, and the next list_conversations call on the
        # affected device will catch up the missing state.
        try:
            now = self._now()
            existing = await self._storage.get_session_meta(
                token_name=token_name, session_id=session_id
            )
            events: list[NewEvent] = []
            new_count = (existing.message_count if existing else 0) + 2
            preview = assistant_text[:_PREVIEW_CHARS]
            if existing is None:
                # Emit session_created BEFORE the message_added pair so peers
                # apply them in the natural order. All three land in one
                # append_updates call → one pts block → atomic from the
                # client's perspective.
                events.append(
                    NewEvent(
                        event_type=EVENT_SESSION_CREATED,
                        session_id=session_id,
                        payload=json.dumps({"title": ""}, ensure_ascii=False),
                    )
                )
                await self._storage.upsert_session_meta(
                    token_name=token_name,
                    session_id=session_id,
                    title="",
                    title_manual=False,
                    message_count=new_count,
                    preview=preview,
                    now=now,
                )
            else:
                # Bump updated_at so list_conversations sort order matches
                # "most recent activity"; refresh cached count + preview so
                # the sidebar list endpoint stays single-query (no CM read).
                # No event for the bump itself — the message_added pair
                # already conveys the change.
                #
                # Stale-tab race: another device may have soft-deleted this
                # session while this tab kept its old view. The deleted
                # row is excluded from list_conversations, but the new
                # message_added events would still land — peers would see
                # the chat resurface only as bubbles, with the session
                # itself filtered out (visible-elsewhere mismatch). Clear
                # `deleted_at` to undelete and emit a meta_updated event
                # before the message pair so all peers re-create / reveal
                # the row consistently.
                deleted_arg: int | None | object = _UNSET
                if existing.deleted_at is not None:
                    deleted_arg = None
                    events.append(
                        NewEvent(
                            event_type=EVENT_SESSION_META_UPDATED,
                            session_id=session_id,
                            payload=json.dumps(
                                {"deleted": False}, ensure_ascii=False
                            ),
                        )
                    )
                await self._storage.upsert_session_meta(
                    token_name=token_name,
                    session_id=session_id,
                    deleted_at=deleted_arg,
                    message_count=new_count,
                    preview=preview,
                    now=now,
                )
            events.append(
                NewEvent(
                    event_type=EVENT_MESSAGE_ADDED,
                    session_id=session_id,
                    payload=json.dumps(
                        {"role": "user", "content": user_text},
                        ensure_ascii=False,
                    ),
                )
            )
            events.append(
                NewEvent(
                    event_type=EVENT_MESSAGE_ADDED,
                    session_id=session_id,
                    payload=json.dumps(
                        {"role": "assistant", "content": assistant_text},
                        ensure_ascii=False,
                    ),
                )
            )
            await self._storage.append_updates(
                token_name=token_name, events=events, now=now
            )
            await self._event_bus.notify(token_name)
        except Exception as exc:
            logger.exception(
                "[WebChatGateway] record_chat_pair failed token=%s session=%s",
                token_name,
                session_id,
            )
            try:
                await self._audit.write(
                    "sync_record_failed",
                    name=token_name,
                    detail={
                        "session_id": session_id,
                        "error": str(exc)[:200],
                    },
                )
            except Exception:
                # Audit logging itself failed — already logged via logger
                # above. Swallow so chat reply is never blocked.
                pass

    async def get_events(
        self,
        *,
        token_name: str,
        since_pts: int,
        timeout: float,
    ) -> EventsResult:
        if since_pts < 0:
            raise ServiceError("invalid_since", status=400)
        timeout = max(0.0, min(timeout, _MAX_EVENT_TIMEOUT))

        # Wrap-around / pts-rewind detection. `append_updates` allocates pts
        # via `MAX(pts) + 1`, so a token whose entire event log was wiped by
        # the retention prune restarts numbering from 1. A client that was
        # offline with `since=N` (N ≫ new_max) would otherwise loop forever
        # asking for `pts > N` — get_updates returns nothing, the relative-
        # threshold check `current_max - since > 1000` underflows (negative
        # > 1000 is False), and the client never advances. Force tooFar.
        current_max = await self._storage.get_max_pts(token_name=token_name)
        if since_pts > current_max:
            return EventsResult(
                events=[], last_pts=current_max, has_more=False, too_far=True
            )

        rows = await self._storage.get_updates(
            token_name=token_name, since_pts=since_pts, limit=_GET_EVENTS_LIMIT
        )
        # Retention-aware tooFar: when the first available row is past
        # `since_pts + 1`, retention pruning consumed events the client
        # never saw. Streaming forward from `rows` would silently desync
        # the client; force a cold refetch instead. (Detecting via the
        # gap on `rows[0].pts` avoids an extra MIN(pts) round-trip; if
        # rows is empty, no gap is observable yet — the client is at the
        # head and will pick up new events normally.)
        if (
            since_pts > 0
            and rows
            and rows[0].pts > since_pts + 1
        ):
            return EventsResult(
                events=[], last_pts=current_max, has_more=False, too_far=True
            )
        if not rows and timeout > 0:
            if current_max - since_pts > _TOO_FAR_THRESHOLD:
                # Client is too stale to catch up via the event log — let it
                # know to drop the cache and call list_conversations.
                return EventsResult(
                    events=[], last_pts=current_max, has_more=False, too_far=True
                )
            # Per-token waiter cap. A 9th concurrent long-poll on the same
            # token gets a 429 instead of a held connection so the frontend
            # registers it as a failure, hits its 3-in-5s threshold, and
            # degrades to short-poll naturally. Returning empty 200 here
            # would loop the client with no backoff (no failure → no
            # state-machine transition).
            if (
                await self._event_bus.waiter_count(token_name)
                >= _MAX_LONG_POLL_PER_TOKEN
            ):
                raise ServiceError("too_many_waiters", status=429)
            await self._event_bus.wait(token_name, timeout=timeout)
            rows = await self._storage.get_updates(
                token_name=token_name,
                since_pts=since_pts,
                limit=_GET_EVENTS_LIMIT,
            )
        if rows:
            last_pts = rows[-1].pts
        else:
            # The wait may have changed current_max; re-read so the response
            # reflects post-wait state. Without this, a client polling at
                # `since=N` after a prune would keep seeing the stale max.
            last_pts = await self._storage.get_max_pts(token_name=token_name)
            if since_pts > last_pts:
                return EventsResult(
                    events=[], last_pts=last_pts, has_more=False, too_far=True
                )
        # tooFar applies on the immediate-return path too: if a client
        # short-polls with a wildly stale `since`, the fast read above could
        # still be empty and we would have skipped the threshold check.
        if not rows and last_pts - since_pts > _TOO_FAR_THRESHOLD:
            return EventsResult(
                events=[], last_pts=last_pts, has_more=False, too_far=True
            )
        return EventsResult(
            events=rows,
            last_pts=last_pts,
            has_more=len(rows) == _GET_EVENTS_LIMIT,
            too_far=False,
        )


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
        extra = None
        if exc.code == "ip_blocked" and str(exc):
            extra = {"Retry-After": str(exc)}
        detail = str(exc) if str(exc) != exc.code else ""
        return json_response(
            {"error": exc.code, "detail": detail},
            status=exc.status,
            origin=origin,
            allowed_origins=allowed,
            extra_headers=extra,
            same_origin_host=request.host,
        )

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
        "events": get_events,
        "preflight": preflight,
    }


__all__ = [
    "ConversationDeps",
    "ConversationService",
    "make_conversation_handlers",
]
