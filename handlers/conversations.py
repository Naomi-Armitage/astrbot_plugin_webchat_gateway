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

try:
    from astrbot.core.agent.message import (
        AssistantMessageSegment,
        TextPart,
        UserMessageSegment,
    )
except ImportError as _e:
    raise ImportError(
        "[WebChatGateway] Cannot import AssistantMessageSegment/TextPart/UserMessageSegment "
        "from astrbot.core.agent.message. This plugin requires AstrBot >= 3.4. "
        f"Original error: {_e}"
    ) from _e

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
EVENT_STREAM_STARTED = "stream_started"
EVENT_STREAM_ENDED = "stream_ended"


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
    when message segments are involved (UserMessageSegment / AssistantMessageSegment
    wrapping TextPart). Anything else collapses to "".
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

    async def _cm_persist_pair(
        self,
        *,
        token_name: str,
        session_id: str,
        user_text: str,
        assistant_text: str,
    ) -> None:
        """Append the user/assistant pair to AstrBot CM history.

        Resolves the conversation id via `get_curr_conversation_id` —
        the bridge normally created the conversation before the LLM
        call, so this returns a real cid. The fallback (cid is None)
        creates a new conversation with `persona_id=None` and
        `title=session_id`; this only fires on edge cases where the
        bridge skipped its `new_conversation` step (e.g., the
        non-stream path failed before calling _generate_reply_inner
        but record_chat_pair was somehow invoked anyway).

        Logs and swallows on failure so `record_chat_pair`'s "must
        never raise" contract holds even if CM is temporarily broken.
        """
        umo = _umo(token_name, session_id)
        try:
            cid = await self._cm.get_curr_conversation_id(umo)
            if not cid:
                cid = await self._cm.new_conversation(
                    umo,
                    platform_id="webchat_gateway",
                    title=session_id,
                    persona_id=None,
                )
            await self._cm.add_message_pair(
                cid=cid,
                user_message=UserMessageSegment(
                    content=[TextPart(text=user_text)]
                ),
                assistant_message=AssistantMessageSegment(
                    content=[TextPart(text=assistant_text)]
                ),
            )
        except Exception:
            logger.exception(
                "[WebChatGateway] _cm_persist_pair failed token=%s session=%s",
                token_name,
                session_id,
            )

    async def _build_incomplete_map(
        self, *, token_name: str, session_id: str
    ) -> set[int]:
        """Return 0-based indices of incomplete assistant messages in CM
        history order for `(token_name, session_id)`.

        Strategy: scan `webchat_updates` for this token in pts-ascending
        order, filter to this session, drop everything at or before the
        last `history_cleared` event (those events describe a wiped
        history that no longer corresponds to CM), then walk the
        remaining `message_added` events alongside the CM history with a
        sliding pointer so duplicate `(role, content)` pairs match in
        order. Assistant messages whose matched event carries
        `incomplete: true` are recorded.

        When retention pruning has dropped the relevant `message_added`
        event, the corresponding CM index simply does not appear in the
        result — by design (PLAN_chat_streaming_v2.md).
        """
        # Page through webchat_updates for this token. The cap is high
        # enough that real users never hit it (retention pruning keeps
        # the table bounded) but guards against pathological growth from
        # a misconfigured retention policy.
        scan_limit = 50
        max_rows = 5000
        seen = 0
        since = 0
        session_events: list[UpdateRow] = []
        try:
            while seen < max_rows:
                batch = await self._storage.get_updates(
                    token_name=token_name,
                    since_pts=since,
                    limit=scan_limit,
                )
                if not batch:
                    break
                for row in batch:
                    if row.session_id == session_id:
                        session_events.append(row)
                seen += len(batch)
                since = batch[-1].pts
                if len(batch) < scan_limit:
                    break
        except Exception:
            logger.exception(
                "[WebChatGateway] _build_incomplete_map scan failed"
            )
            return set()

        if not session_events:
            return set()

        # Find the last `history_cleared` event for this session and drop
        # everything at or before it — those `message_added` rows refer
        # to a wiped CM history.
        last_clear_idx = -1
        for i, row in enumerate(session_events):
            if row.event_type == EVENT_HISTORY_CLEARED:
                last_clear_idx = i
        if last_clear_idx >= 0:
            session_events = session_events[last_clear_idx + 1 :]

        # Build the in-order list of (role, content, incomplete).
        msg_events: list[tuple[str, str, bool]] = []
        for row in session_events:
            if row.event_type != EVENT_MESSAGE_ADDED:
                continue
            try:
                payload = json.loads(row.payload) if row.payload else {}
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            role = payload.get("role")
            content = payload.get("content")
            if not isinstance(role, str) or not isinstance(content, str):
                continue
            if role not in ("user", "assistant"):
                continue
            incomplete = bool(payload.get("incomplete"))
            msg_events.append((role, content, incomplete))

        if not msg_events:
            return set()

        # Walk CM history alongside the event list. For each CM entry,
        # advance the event pointer until we find a matching (role,
        # content); if the event flagged it as incomplete and the role
        # is assistant, record the CM index.
        cm_messages, _cid = await self._cm_history(
            token_name=token_name, session_id=session_id
        )
        result: set[int] = set()
        ev_ptr = 0
        for cm_idx, msg in enumerate(cm_messages):
            target_role = msg["role"]
            target_content = msg["content"]
            # Advance ev_ptr to the first event matching this CM entry.
            while ev_ptr < len(msg_events):
                ev_role, ev_content, ev_incomplete = msg_events[ev_ptr]
                ev_ptr += 1
                if ev_role == target_role and ev_content == target_content:
                    if ev_incomplete and target_role == "assistant":
                        result.add(cm_idx)
                    break
            else:
                # Event list exhausted; remaining CM entries have no
                # surviving event record (pruned) → leave them unflagged.
                break
        return result

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
        # Stamp `incomplete: true` on assistant messages whose chat-sync
        # event marked them as such. The flag is reconstructed from the
        # event log on every fetch — CM doesn't store it natively. Old
        # messages whose events have been pruned simply lose the flag.
        incomplete_indices = await self._build_incomplete_map(
            token_name=token_name, session_id=session_id
        )
        if incomplete_indices:
            messages = [
                ({**msg, "incomplete": True}
                 if i in incomplete_indices and msg["role"] == "assistant"
                 else msg)
                for i, msg in enumerate(messages)
            ]
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
        incomplete: bool = False,
        user_already_emitted: bool = False,
    ) -> None:
        # Must never raise: the chat reply has already been delivered to the
        # client. A failure here is a sync hiccup, not a chat error. We
        # log + audit instead, and the next list_conversations call on the
        # affected device will catch up the missing state.
        #
        # Persistence ownership: this method writes the user/assistant pair
        # to BOTH AstrBot CM (so prompt-context history surfaces it next
        # turn) AND the chat-sync layer (event log + meta cache for the
        # web UI). LlmBridge used to own the CM write, but the streaming
        # incomplete path needs to persist partial replies too — moving
        # the CM write here keeps a single source of truth for "the turn
        # is done, persist whatever we have".
        #
        # `user_already_emitted=True` is set by the streaming close path
        # (close_ok / close_incomplete) — emit_stream_started has already
        # emitted the user's message_added event at stream start so peer
        # devices saw the user's bubble immediately. Skipping the user
        # event here avoids duplicate renders. CM still gets the FULL
        # pair via add_message_pair regardless of the flag, because CM
        # only gets one write per turn (at close).
        try:
            await self._cm_persist_pair(
                token_name=token_name,
                session_id=session_id,
                user_text=user_text,
                assistant_text=assistant_text,
            )
        except Exception:
            # _cm_persist_pair already logs; the chat-sync layer below is
            # independent so we proceed regardless.
            pass
        try:
            now = self._now()
            existing = await self._storage.get_session_meta(
                token_name=token_name, session_id=session_id
            )
            events: list[NewEvent] = []
            # +1 if the user message was already counted at stream_started,
            # +2 otherwise (non-stream /chat that still emits the pair here).
            count_delta = 1 if user_already_emitted else 2
            new_count = (existing.message_count if existing else 0) + count_delta
            preview = assistant_text[:_PREVIEW_CHARS]
            if existing is None:
                # session_created should NOT fire if user_already_emitted
                # — emit_stream_started already did. Without this guard
                # peers would see two session_created events for one new
                # session and the second would be a no-op only because
                # applyEvent guards `if (!store.sessions[sid])`.
                if not user_already_emitted:
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
                    # Defensive: a stream that emitted stream_started
                    # SHOULD have left a session meta row behind. If
                    # somehow there's no row, recover by upserting one
                    # without re-firing session_created (peers already
                    # got it).
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
            if not user_already_emitted:
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
            assistant_payload: dict[str, Any] = {
                "role": "assistant",
                "content": assistant_text,
            }
            if incomplete:
                assistant_payload["incomplete"] = True
            events.append(
                NewEvent(
                    event_type=EVENT_MESSAGE_ADDED,
                    session_id=session_id,
                    payload=json.dumps(
                        assistant_payload, ensure_ascii=False
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
                        "incomplete": incomplete,
                        "error": str(exc)[:200],
                    },
                )
            except Exception:
                # Audit logging itself failed — already logged via logger
                # above. Swallow so chat reply is never blocked.
                pass

    async def emit_stream_started(
        self,
        *,
        token_name: str,
        session_id: str,
        user_text: str,
        stream_id: str,
    ) -> None:
        """Emit `session_created` (if new) + `message_added(user)` +
        `stream_started` atomically.

        Emitting the user's message at stream START (not stream END)
        means peer devices render the user's bubble immediately when
        the stream begins, instead of waiting up to several seconds
        for the assistant reply to land. The CM write for the
        (user, assistant) pair still happens at stream close via
        `record_chat_pair` — atomicity in CM history is preserved
        because the pair is appended together once the assistant text
        is final.

        Sidebar count + preview reflect the user's message immediately
        (count = N+1, preview = user_text); record_chat_pair at close
        will update count again to N+2 and preview to the assistant
        text. The intermediate state is visible to peers but harmless.

        Must never raise — failures are logged + audited.
        """
        try:
            now = self._now()
            started_at_ms = int(time.time() * 1000)
            existing = await self._storage.get_session_meta(
                token_name=token_name, session_id=session_id
            )
            events: list[NewEvent] = []
            preview = user_text[:_PREVIEW_CHARS]
            if existing is None:
                # New session: emit session_created, upsert meta with
                # initial count of 1 (just the user message).
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
                    message_count=1,
                    preview=preview,
                    now=now,
                )
            else:
                # Existing session — same un-delete logic as
                # record_chat_pair so a soft-deleted session resurfaces
                # consistently when the user resumes typing into it.
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
                    message_count=existing.message_count + 1,
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
                    event_type=EVENT_STREAM_STARTED,
                    session_id=session_id,
                    payload=json.dumps(
                        {
                            "stream_id": stream_id,
                            "started_at": started_at_ms,
                        },
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
                "[WebChatGateway] emit_stream_started failed token=%s session=%s",
                token_name,
                session_id,
            )
            try:
                await self._audit.write(
                    "sync_record_failed",
                    name=token_name,
                    detail={
                        "session_id": session_id,
                        "stream_id": stream_id,
                        "event": EVENT_STREAM_STARTED,
                        "error": str(exc)[:200],
                    },
                )
            except Exception:
                pass

    async def emit_stream_ended(
        self,
        *,
        token_name: str,
        session_id: str,
        stream_id: str,
        status: str,
    ) -> None:
        """Append a single `stream_ended` event and notify the event
        bus. `status` is one of `'ok' | 'incomplete' | 'failed'`.
        NO CM write. Must never raise.
        """
        try:
            now = self._now()
            await self._storage.append_updates(
                token_name=token_name,
                events=[
                    NewEvent(
                        event_type=EVENT_STREAM_ENDED,
                        session_id=session_id,
                        payload=json.dumps(
                            {
                                "stream_id": stream_id,
                                "status": status,
                            },
                            ensure_ascii=False,
                        ),
                    )
                ],
                now=now,
            )
            await self._event_bus.notify(token_name)
        except Exception as exc:
            logger.exception(
                "[WebChatGateway] emit_stream_ended failed token=%s session=%s",
                token_name,
                session_id,
            )
            try:
                await self._audit.write(
                    "sync_record_failed",
                    name=token_name,
                    detail={
                        "session_id": session_id,
                        "stream_id": stream_id,
                        "event": EVENT_STREAM_ENDED,
                        "status": status,
                        "error": str(exc)[:200],
                    },
                )
            except Exception:
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
