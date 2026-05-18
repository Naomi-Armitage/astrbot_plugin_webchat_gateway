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
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, AsyncIterator

from aiohttp import web

from astrbot.api import logger

try:
    from astrbot.core.agent.message import (
        AssistantMessageSegment,
        ImageURLPart,
        TextPart,
        UserMessageSegment,
    )
except ImportError as _e:
    raise ImportError(
        "[WebChatGateway] Cannot import AssistantMessageSegment/TextPart/ImageURLPart/"
        "UserMessageSegment from astrbot.core.agent.message. This plugin requires "
        f"AstrBot >= 3.4 with multimodal support. Original error: {_e}"
    ) from _e

from ..core.audit import AuditLogger
from ..core.event_bus import EventBus
from ..core.file_lifecycle import release_files_safely
from ..core.file_store import FileStore
from ..core.ip_guard import IpGuard
from ..core.llm_bridge import LlmBridge, map_llm_error
from ..core.ratelimit import PerTokenConcurrency
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
EVENT_MESSAGE_DELETED = "message_deleted"
EVENT_HISTORY_CLEARED = "history_cleared"
EVENT_STREAM_STARTED = "stream_started"
EVENT_STREAM_ENDED = "stream_ended"


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


@dataclass(frozen=True)
class RegenerateResult:
    """Outcome of `regenerate_assistant_message` — the new assistant text
    plus the quota state the HTTP layer surfaces to the client."""

    reply: str
    remaining: int
    daily_quota: int


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


def _extract_attachment_file_ids(content: Any) -> list[str]:
    """Pull `file_id`s out of CM ImageURLPart segments.

    Each `ImageURLPart` is stored as `{"type": "image_url",
    "image_url": {"url": "...", "id": "<file_id>"}}`. We surface the
    `id` (the only piece we own) so attachments persist across the
    chat-sync 14-day event retention — CM is the long-term store,
    `webchat_updates` is just the realtime cross-device push channel.
    Without this, `get_conversation` would lose image references on
    cold refresh once the originating event aged past retention.

    Mime is NOT in the segment payload; the caller re-fetches it from
    `webchat_files` (batched once per get_conversation) so the browser
    knows what to do with the bytes.
    """
    if not isinstance(content, list):
        return []
    file_ids: list[str] = []
    for seg in content:
        if not isinstance(seg, dict):
            continue
        if seg.get("type") != "image_url":
            continue
        iu = seg.get("image_url")
        if not isinstance(iu, dict):
            continue
        fid = iu.get("id")
        if isinstance(fid, str) and fid:
            file_ids.append(fid)
    return file_ids


def _renderable_entry(item: Any) -> tuple[str, str, list[str]] | None:
    """Return `(role, text, file_ids)` if the CM entry should render, else None.

    Single source of truth for the rules `_normalize_history` and
    `_render_to_raw_indices` both apply — without this they had two
    independent walks of the same drop logic, and a future rule
    change touching one but not the other would silently desync the
    rendered-index ↔ raw-index mapping that delete/regenerate rely
    on (the splice would target the wrong row).

    Kept entries:
      * role is "user" or "assistant" (lowercased)
      * NOT an empty-text assistant with no attachments (we never
        emit those, so they're stale noise)
      * Empty-text user messages WITH no attachments are STILL kept
        — `content=""` carries the "image-only look at this" turn
        whose attachments live in the chat-sync overlay layer; the
        normalize pass then attaches them via _build_history_overlay.
    """
    if not isinstance(item, dict):
        return None
    role = str(item.get("role") or "").strip().lower()
    if role not in ("user", "assistant"):
        return None
    text = _extract_text(item.get("content"))
    file_ids = (
        list(_extract_attachment_file_ids(item.get("content")))
        if role == "user"
        else []
    )
    if not text and role != "user" and not file_ids:
        return None
    return role, text, file_ids


def _normalize_history(raw: Any) -> list[dict]:
    """CM history is JSON; render it as `[{role, content, attachments?}, ...]`.
    Tool calls, system messages, anything we can't flatten to text are
    dropped — the chat UI doesn't render them.

    Empty-text USER messages are preserved (with `content=""`) — these
    are image-only "look at this" turns. The chat-sync overlay layer
    pairs them with their attachments via `_build_history_overlay`. If
    we dropped them here the overlay would have nothing to match against
    and the user's image-only bubble would silently vanish on cold
    refresh.

    Empty-text ASSISTANT messages are dropped — an assistant turn with
    no text and no attachments (we don't generate images) is just noise.

    Attachments: each user message that has ImageURLPart segments in
    CM gets `attachments=[{"file_id": ...}, ...]` populated here.
    Caller is responsible for enriching with `mime` via a batch
    `get_file` lookup (cheap — one query per get_conversation).
    Surfacing attachments from CM (rather than from `webchat_updates`)
    means image references survive the 14-day chat-sync prune.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        kept = _renderable_entry(item)
        if kept is None:
            continue
        role, text, file_ids = kept
        entry: dict[str, Any] = {"role": role, "content": text}
        if file_ids:
            # Mime is filled in by the caller via a batched lookup.
            # We carry the file_id list shape that matches the wire
            # format on `message_added` events for consistency.
            entry["attachments"] = [{"file_id": fid} for fid in file_ids]
        out.append(entry)
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
        file_store: FileStore,
        concurrency: PerTokenConcurrency | None = None,
        llm_bridge: LlmBridge | None = None,
    ) -> None:
        self._storage = storage
        self._audit = audit
        self._event_bus = event_bus
        self._cm = cm
        self._file_store = file_store
        # When set, clear_history blocks on this lock so it cannot run
        # while a /chat/stream is in flight for the same token. See
        # ConversationDeps.concurrency for the race rationale.
        self._concurrency = concurrency
        # Optional — required by regenerate_assistant_message. None is
        # allowed so tests that don't exercise regenerate can construct
        # the service without spinning up an AstrBot context.
        self._llm_bridge = llm_bridge

    @staticmethod
    def _now() -> int:
        return int(time.time())

    @asynccontextmanager
    async def _with_concurrency(
        self,
        *,
        token_name: str,
        operation: str,
        session_id: str,
        ip: str | None,
    ) -> AsyncIterator[None]:
        """Serialise the wrapped block against the per-token lock.

        Three CM-mutating endpoints (clear_history, delete_message_by_index,
        regenerate_assistant_message) need the same envelope: acquire the
        token's `PerTokenConcurrency` lock, surface contention as 429
        concurrent_request, and write a `concurrent_block` audit row so
        operators can correlate user-visible retries with lock contention.
        Tests construct the service without a concurrency manager — in
        that case we yield through.
        """
        if self._concurrency is None:
            yield
            return
        async with self._concurrency.acquire(token_name) as acquired:
            if not acquired:
                await self._audit.write(
                    "concurrent_block",
                    name=token_name,
                    ip=ip,
                    detail={"operation": operation, "session_id": session_id},
                )
                raise ServiceError("concurrent_request", status=429)
            yield

    async def _cm_history_raw(
        self, *, token_name: str, session_id: str
    ) -> tuple[list[Any], str | None]:
        """Return the raw CM history list (NOT `_normalize_history`'d) and
        the conversation_id. Returns `([], None)` if no conversation exists
        and `([], cid)` if the conversation exists but has no history.

        The raw list is the canonical wire format CM stores: each item is
        a dict like `{"role": "user", "content": [...segments...]}` where
        `content` may be a string or a list of segment dicts. The
        delete/regenerate paths need this unfiltered shape so they can
        splice a single entry out and write the resulting list back via
        `update_conversation(history=<list>)` without disturbing any
        system / tool-call entries that `_normalize_history` would have
        dropped from the user-facing rendering.
        """
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
        if not isinstance(parsed, list):
            return [], cid
        return parsed, cid

    @staticmethod
    def _render_to_raw_indices(raw: list[Any]) -> list[int]:
        """Map rendered-history indices (what the client sees) to raw-CM
        indices.

        `_normalize_history` filters the raw CM list by dropping non-
        user/assistant roles and empty-content assistant turns. The
        delete/regenerate endpoints accept a `message_index` that is
        0-based into the RENDERED list (because that's what the client
        actually sees and can point at). To splice the raw CM list, we
        need the inverse mapping.

        Returns a list where `out[rendered_idx] == raw_idx`. If a
        rendered index has no surviving entry the list is shorter than
        the rendered history — callers should bounds-check on
        `len(out)`, which equals the size of `_normalize_history(raw)`.

        Shares the keep-predicate `_renderable_entry` with
        `_normalize_history` so a future rule change can't desync
        the index mapping from the rendered output.
        """
        out: list[int] = []
        for i, item in enumerate(raw):
            if _renderable_entry(item) is not None:
                out.append(i)
        return out

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
        user_attachments: list[dict] | None = None,
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

        When `user_attachments` is non-empty, the user segment is built
        with both a `TextPart(text=user_text)` AND one `ImageURLPart`
        per attachment so the LLM sees the images on subsequent turns
        too. Each ImageURLPart wraps the absolute on-disk path returned
        by `file_store.open_local_path` — for LocalFileStore that's the
        real path; for R2FileStore it's the LRU-cached fetch under
        AstrBot's temp dir. Attachments without a resolvable path are
        skipped with a warning (the message still persists with text
        and any other resolved images).

        Logs and swallows on failure so `record_chat_pair`'s "must
        never raise" contract holds even if CM is temporarily broken.
        """
        umo = _umo(token_name, session_id)
        attachments = user_attachments or []
        try:
            cid = await self._cm.get_curr_conversation_id(umo)
            if not cid:
                cid = await self._cm.new_conversation(
                    umo,
                    platform_id="webchat_gateway",
                    title=session_id,
                    persona_id=None,
                )
            user_parts: list[Any] = [TextPart(text=user_text)] if user_text else []
            for att in attachments:
                file_id = att.get("file_id") if isinstance(att, dict) else None
                if not isinstance(file_id, str) or not file_id:
                    continue
                try:
                    row = await self._storage.get_file(file_id)
                except Exception:
                    logger.exception(
                        "[WebChatGateway] _cm_persist_pair get_file failed file_id=%s",
                        file_id,
                    )
                    continue
                if row is None or row.token_name != token_name:
                    logger.warning(
                        "[WebChatGateway] _cm_persist_pair attachment missing "
                        "or cross-token file_id=%s",
                        file_id,
                    )
                    continue
                try:
                    local_path = await self._file_store.open_local_path(
                        storage_key=row.storage_key
                    )
                except Exception:
                    logger.exception(
                        "[WebChatGateway] _cm_persist_pair open_local_path "
                        "failed key=%s",
                        row.storage_key,
                    )
                    local_path = None
                if not local_path:
                    logger.warning(
                        "[WebChatGateway] _cm_persist_pair could not resolve "
                        "local path for file_id=%s",
                        file_id,
                    )
                    continue
                # AstrBot's ImageURLPart accepts any URL; passing a
                # `file://` URL keeps providers happy across backends
                # (local fs, R2 cache) without juggling base64 inline.
                # Use Path.as_uri() so Windows paths like
                # `C:\Users\...\file.jpg` come out as the spec'd
                # `file:///C:/Users/.../file.jpg` (triple-slash + forward-
                # slashes) instead of the malformed `file://C:\Users\...`
                # we'd get from string concatenation.
                if local_path.startswith(("http://", "https://", "file://")):
                    file_url = local_path
                else:
                    try:
                        file_url = Path(local_path).resolve().as_uri()
                    except ValueError:
                        # Relative path → as_uri() refuses; fall back to
                        # a best-effort POSIX-style file:// URI.
                        file_url = (
                            "file:///"
                            + local_path.replace("\\", "/").lstrip("/")
                        )
                user_parts.append(
                    ImageURLPart(
                        image_url=ImageURLPart.ImageURL(url=file_url, id=file_id)
                    )
                )
            if not user_parts:
                # Defensive: at least one part is required by Message.
                user_parts = [TextPart(text=user_text or "")]
            await self._cm.add_message_pair(
                cid=cid,
                user_message=UserMessageSegment(content=user_parts),
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

    async def _build_history_overlay(
        self, *, token_name: str, session_id: str
    ) -> tuple[set[int], dict[int, list[dict]]]:
        """Walk `webchat_updates` + CM history once and return overlays.

        Returns:
            (incomplete_indices, attachments_map)

        - `incomplete_indices`: 0-based CM history indices of assistant
          messages whose chat-sync `message_added` event flagged
          `incomplete: true`.
        - `attachments_map`: CM history index → list of `{file_id, mime}`
          dicts pulled off the matching user `message_added` event payload.

        Both overlays are derived from the same event scan + history
        walk so the caller only pays for one storage round trip per
        get_conversation call. Events pruned past the retention window
        simply don't appear in either overlay — by design (PLAN_chat_streaming_v2.md
        for incomplete; PLAN_image_upload.md §"Wire protocol" for attachments).
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
                "[WebChatGateway] _build_history_overlay scan failed"
            )
            return set(), {}

        if not session_events:
            return set(), {}

        # Find the last `history_cleared` event for this session and drop
        # everything at or before it — those `message_added` rows refer
        # to a wiped CM history.
        last_clear_idx = -1
        for i, row in enumerate(session_events):
            if row.event_type == EVENT_HISTORY_CLEARED:
                last_clear_idx = i
        if last_clear_idx >= 0:
            session_events = session_events[last_clear_idx + 1 :]

        # Build the in-order list of (role, content, incomplete, attachments).
        msg_events: list[tuple[str, str, bool, list[dict]]] = []
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
            if not isinstance(role, str):
                continue
            if not isinstance(content, str):
                content = ""
            if role not in ("user", "assistant"):
                continue
            incomplete = bool(payload.get("incomplete"))
            raw_attachments = payload.get("attachments")
            attachments: list[dict] = []
            if isinstance(raw_attachments, list):
                for att in raw_attachments:
                    if not isinstance(att, dict):
                        continue
                    fid = att.get("file_id")
                    mime = att.get("mime")
                    if not isinstance(fid, str) or not fid:
                        continue
                    entry: dict[str, Any] = {"file_id": fid}
                    if isinstance(mime, str) and mime:
                        entry["mime"] = mime
                    attachments.append(entry)
            msg_events.append((role, content, incomplete, attachments))

        if not msg_events:
            return set(), {}

        # Walk CM history alongside the event list. For each CM entry,
        # advance the event pointer until we find a matching (role,
        # content); record incomplete flag for assistant messages and
        # attachments for user messages.
        cm_messages, _cid = await self._cm_history(
            token_name=token_name, session_id=session_id
        )
        incomplete_indices: set[int] = set()
        attachments_map: dict[int, list[dict]] = {}
        ev_ptr = 0
        for cm_idx, msg in enumerate(cm_messages):
            target_role = msg["role"]
            target_content = msg["content"]
            # Advance ev_ptr to the first event matching this CM entry.
            while ev_ptr < len(msg_events):
                ev_role, ev_content, ev_incomplete, ev_attachments = msg_events[
                    ev_ptr
                ]
                ev_ptr += 1
                if ev_role == target_role and ev_content == target_content:
                    if ev_incomplete and target_role == "assistant":
                        incomplete_indices.add(cm_idx)
                    if ev_attachments and target_role == "user":
                        attachments_map[cm_idx] = ev_attachments
                    break
            else:
                # Event list exhausted; remaining CM entries have no
                # surviving event record (pruned) → leave them unflagged.
                break
        return incomplete_indices, attachments_map

    async def _build_incomplete_map(
        self, *, token_name: str, session_id: str
    ) -> set[int]:
        """Backwards-compatible thin wrapper around `_build_history_overlay`.

        Kept for any caller that only cares about the incomplete flag —
        the new code path uses the dual-overlay helper directly so the
        attachments backfill comes free.
        """
        incomplete, _ = await self._build_history_overlay(
            token_name=token_name, session_id=session_id
        )
        return incomplete

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
        # Stamp `incomplete: true` on assistant messages from the event
        # overlay AND enrich `attachments` with mime via storage lookup.
        #
        # Attachments source-of-truth: CM history's ImageURLPart segments.
        # `_normalize_history` already populates `entry["attachments"]`
        # with `[{"file_id": ...}]` for any user message that has them.
        # We then batch-look-up `mime` from `webchat_files` once per
        # `get_conversation` call (a single SELECT IN (...)) and stitch
        # it onto each attachment dict. This makes CM the long-term
        # store of attachments — `webchat_updates` is just the realtime
        # cross-device push channel and can be pruned (default 14 days)
        # without dropping image references from history.
        #
        # The event overlay is now only used for `incomplete` (which
        # CM doesn't natively model). Event-derived attachments override
        # CM-derived ones, but in practice they agree — both originated
        # from the same `record_chat_pair` write.
        incomplete_indices, attachments_map_from_events = (
            await self._build_history_overlay(
                token_name=token_name, session_id=session_id
            )
        )
        # Batch-fetch mimes for every file_id we'll surface (CM + events
        # merged). One query for the whole history.
        all_file_ids: set[str] = set()
        for i, msg in enumerate(messages):
            for att in msg.get("attachments") or []:
                fid = att.get("file_id")
                if isinstance(fid, str):
                    all_file_ids.add(fid)
            for att in attachments_map_from_events.get(i, []):
                fid = att.get("file_id") if isinstance(att, dict) else None
                if isinstance(fid, str):
                    all_file_ids.add(fid)
        mime_by_file_id: dict[str, str] = {}
        for fid in all_file_ids:
            try:
                row = await self._storage.get_file(fid)
            except Exception:
                logger.exception(
                    "[WebChatGateway] get_file during conversation enrich failed sid=%s",
                    session_id,
                )
                row = None
            # Cross-token row leaks are impossible here: the file_id
            # came out of THIS session's CM segments, which were
            # written under THIS token via _cm_persist_pair.
            if row is not None:
                mime_by_file_id[fid] = row.mime
        if incomplete_indices or attachments_map_from_events or all_file_ids:
            decorated: list[dict] = []
            for i, msg in enumerate(messages):
                out = dict(msg)
                if (
                    msg["role"] == "assistant"
                    and i in incomplete_indices
                ):
                    out["incomplete"] = True
                # Prefer CM-derived attachments (long-term store);
                # fall back to events if CM didn't have them (e.g.
                # close_incomplete partial-write race).
                attachments_for_msg = msg.get("attachments") or list(
                    attachments_map_from_events.get(i, [])
                )
                if attachments_for_msg and msg["role"] == "user":
                    enriched: list[dict] = []
                    for att in attachments_for_msg:
                        fid = att.get("file_id") if isinstance(att, dict) else None
                        if not isinstance(fid, str):
                            continue
                        item = {"file_id": fid}
                        if fid in mime_by_file_id:
                            item["mime"] = mime_by_file_id[fid]
                        elif isinstance(att, dict) and isinstance(
                            att.get("mime"), str
                        ):
                            item["mime"] = att["mime"]
                        enriched.append(item)
                    if enriched:
                        out["attachments"] = enriched
                    else:
                        out.pop("attachments", None)
                decorated.append(out)
            messages = decorated
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
        # Serialize with /chat and /chat/stream on the same token. If a
        # stream is currently active, its record_chat_pair / CM persist
        # has not yet run; clearing right now would list the attachment
        # rows BEFORE the stream's CM write but delete them AFTER, so
        # CM would land an ImageURLPart pointing at a now-missing file
        # and the next history fetch renders a broken thumbnail. The
        # non-blocking `acquire` returns False on contention — surface
        # that as 429 concurrent_request so the user retries once the
        # stream finishes, consistent with /chat's own contention
        # behavior on the same token.
        async with self._with_concurrency(
            token_name=token_name,
            operation="clear_history",
            session_id=session_id,
            ip=ip,
        ):
            return await self._clear_history_inner(
                token_name=token_name, session_id=session_id, ip=ip
            )

    async def _clear_history_inner(
        self,
        *,
        token_name: str,
        session_id: str,
        ip: str | None,
    ) -> SessionMetaRow:
        umo = _umo(token_name, session_id)
        # Collect attachment rows BEFORE the CM wipe so we know what to
        # delete from FileStore. clear_history must release these files
        # too — without this they'd stay committed=1 in webchat_files
        # forever (no longer referenced by CM, won't be picked up by
        # orphan GC which only sees committed=0 rows), silently eating
        # the token's storage quota for every "clear" the user clicks.
        try:
            attachment_rows = await self._storage.list_files_for_session(
                token_name=token_name, session_id=session_id
            )
        except Exception:
            logger.exception(
                "[WebChatGateway] list_files_for_session during clear failed"
            )
            attachment_rows = []
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
        # Delete attachment storage objects FIRST, then their DB rows —
        # `release_files_safely` enforces this order so a mid-cleanup
        # crash leaves the DB row pointing at a missing object, which
        # the next orphan / cascade prune sweep naturally retries. The
        # alternative (DB-first) leaks R2 objects with no DB anchor for
        # any future cleanup pass to find. The orphan GC also won't
        # pick these up (committed=1 → not orphan), so this is the
        # ONLY release path for clear_history.
        if attachment_rows:
            await release_files_safely(
                storage=self._storage,
                file_store=self._file_store,
                rows=attachment_rows,
                log_label="clear_history",
            )
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

    async def _resolve_target_raw_index(
        self,
        *,
        token_name: str,
        session_id: str,
        message_index: int,
    ) -> tuple[list[Any], str, int, dict[str, Any], str]:
        """Map a client-supplied rendered-history index to its raw CM entry.

        Returns `(raw_history, conversation_id, raw_idx, target_entry,
        role)`. Raises `ServiceError("session_not_found", 404)` if the
        session is empty, or `ServiceError("message_not_found", 404)` if
        the index is out of the rendered-view range. Role is normalized
        lowercase; callers still need to enforce their own role
        constraint (delete accepts user|assistant; regenerate requires
        assistant).
        """
        raw_history, cid = await self._cm_history_raw(
            token_name=token_name, session_id=session_id
        )
        if not cid or not raw_history:
            raise ServiceError("session_not_found", status=404)
        rendered_to_raw = self._render_to_raw_indices(raw_history)
        if message_index < 0 or message_index >= len(rendered_to_raw):
            raise ServiceError("message_not_found", status=404)
        raw_idx = rendered_to_raw[message_index]
        target = raw_history[raw_idx]
        if not isinstance(target, dict):
            raise ServiceError("message_not_found", status=404)
        role = str(target.get("role") or "").strip().lower()
        if role not in ("user", "assistant"):
            # _render_to_raw_indices already filters non-user/assistant
            # out — defensive recheck against future filter-rule drift.
            raise ServiceError("message_not_found", status=404)
        return raw_history, cid, raw_idx, target, role

    async def _release_orphaned_attachments(
        self,
        *,
        token_name: str,
        session_id: str,
        removed_entries: list[Any],
        surviving_history: list[Any],
        log_label: str,
    ) -> None:
        """Release attachment files referenced ONLY by `removed_entries`.

        Walks the removed entries to collect their file_ids, then subtracts
        any file_id still referenced by `surviving_history` (cross-message
        attachment reuse is rare but legal — copy-pasted images, retry of
        the same turn, etc.). The diff goes through `release_files_safely`.

        If `list_files_for_session` itself fails (rare DB hiccup), we
        degrade to "release nothing" rather than crash the caller. These
        leaked files stay at `committed=1` so the periodic uncommitted-
        orphan sweep WON'T catch them; they're cleaned up only on the
        next successful `clear_history` for the session, or by the 90-
        day session cascade.
        """
        removed_file_ids: set[str] = set()
        for entry in removed_entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("role") or "").strip().lower() != "user":
                continue
            for fid in _extract_attachment_file_ids(entry.get("content")):
                removed_file_ids.add(fid)
        if not removed_file_ids:
            return
        still_referenced: set[str] = set()
        for surviving in surviving_history:
            if not isinstance(surviving, dict):
                continue
            if str(surviving.get("role") or "").strip().lower() != "user":
                continue
            for fid in _extract_attachment_file_ids(surviving.get("content")):
                still_referenced.add(fid)
        to_release_ids = removed_file_ids - still_referenced
        if not to_release_ids:
            return
        try:
            session_files = await self._storage.list_files_for_session(
                token_name=token_name, session_id=session_id
            )
        except Exception:
            logger.exception(
                "[WebChatGateway] list_files_for_session during %s failed",
                log_label,
            )
            return
        rows_to_release = [
            r for r in session_files if r.file_id in to_release_ids
        ]
        if rows_to_release:
            await release_files_safely(
                storage=self._storage,
                file_store=self._file_store,
                rows=rows_to_release,
                log_label=log_label,
            )

    async def delete_message_by_index(
        self,
        *,
        token_name: str,
        session_id: str,
        message_index: int,
        ip: str | None = None,
    ) -> SessionMetaRow:
        """Delete a single message at `message_index` (0-based into the
        client-visible rendered history) from CM history, release any
        attachment files no longer referenced by the surviving history,
        and emit a `message_deleted` event for peers.

        Acquires the per-token concurrency lock; raises
        `ServiceError("concurrent_request", 429)` on contention (a
        `/chat/stream` in flight on the same token). Raises
        `ServiceError("session_not_found", 404)` if the session has no
        CM conversation or `("message_not_found", 404)` if the index is
        out of range.

        File release uses the same `release_files_safely` helper as
        `clear_history`. A file_id is only released when NO surviving
        message in the truncated history still references it — this
        protects against the case where a user re-uses an attachment
        across two messages (rare, but possible via copy-paste). The
        check walks the spliced raw CM list once after the splice.
        """
        async with self._with_concurrency(
            token_name=token_name,
            operation="delete_message",
            session_id=session_id,
            ip=ip,
        ):
            return await self._delete_message_inner(
                token_name=token_name,
                session_id=session_id,
                message_index=message_index,
                ip=ip,
            )

    async def _delete_message_inner(
        self,
        *,
        token_name: str,
        session_id: str,
        message_index: int,
        ip: str | None,
    ) -> SessionMetaRow:
        raw_history, cid, raw_idx, removed_entry, removed_role = (
            await self._resolve_target_raw_index(
                token_name=token_name,
                session_id=session_id,
                message_index=message_index,
            )
        )

        # Splice the raw list in place. Keep all non-rendered entries
        # (system / tool calls) intact — we only drop the one chosen
        # by message_index.
        new_history = list(raw_history)
        del new_history[raw_idx]

        try:
            await self._cm.update_conversation(
                unified_msg_origin=_umo(token_name, session_id),
                conversation_id=cid,
                history=new_history,
            )
        except Exception:
            logger.exception(
                "[WebChatGateway] CM.update_conversation(delete) failed"
            )
            raise ServiceError("delete_failed", status=500) from None

        await self._release_orphaned_attachments(
            token_name=token_name,
            session_id=session_id,
            removed_entries=[removed_entry],
            surviving_history=new_history,
            log_label="delete_message_by_index",
        )

        # Recompute message_count + preview from the new rendered view
        # so peer `list_conversations` calls reflect the deletion. The
        # delete event itself does NOT carry count/preview — the client
        # recomputes locally; the meta cache is for cold sidebar loads.
        new_rendered = _normalize_history(new_history)
        new_count = len(new_rendered)
        preview_text = (
            _extract_text(new_history[-1].get("content"))
            if new_history
            and isinstance(new_history[-1], dict)
            else ""
        )
        # If the trailing raw entry is non-renderable (e.g. tool call),
        # walk back to the last user/assistant text for a meaningful
        # sidebar preview. Empty preview falls through harmlessly.
        if not preview_text and new_rendered:
            preview_text = new_rendered[-1].get("content") or ""
        now = self._now()
        row = await self._storage.upsert_session_meta(
            token_name=token_name,
            session_id=session_id,
            message_count=new_count,
            preview=preview_text[:_PREVIEW_CHARS],
            now=now,
        )
        await self._storage.append_updates(
            token_name=token_name,
            events=[
                NewEvent(
                    event_type=EVENT_MESSAGE_DELETED,
                    session_id=session_id,
                    payload=json.dumps(
                        {"index": message_index, "role": removed_role},
                        ensure_ascii=False,
                    ),
                )
            ],
            now=now,
        )
        await self._event_bus.notify(token_name)
        await self._audit.write(
            "conv_message_deleted",
            name=token_name,
            ip=ip,
            detail={
                "session_id": session_id,
                "index": message_index,
                "role": removed_role,
            },
        )
        return row

    async def regenerate_assistant_message(
        self,
        *,
        token_name: str,
        session_id: str,
        message_index: int,
        username: str = "WebUser",
        token_daily_quota: int,
        ip: str | None = None,
    ) -> RegenerateResult:
        """Drop the assistant message at `message_index` (0-based into the
        rendered history), truncate CM history to `[0, message_index)`, run
        the non-streaming LLM call on the truncated context, append the new
        assistant reply, and emit `message_deleted` + `message_added` events.

        Acquires the per-token concurrency lock; raises
        `ServiceError("concurrent_request", 429)` on contention.

        Raises `ServiceError("session_not_found", 404)` if no conversation
        exists for the session; `("message_not_found", 404)` if the index
        is out of range OR the message at that index isn't an assistant
        turn; `("quota_exceeded", 429)` if the token's daily quota is hit;
        propagates `("llm_timeout", 504)` / `("empty_reply", 502)` /
        `("llm_call_failed", 500)` for upstream LLM failures (file release
        on failure is documented inline). `token_daily_quota` is the
        TokenRow's `daily_quota` — passed in rather than re-fetched here
        so the handler can keep auth and service concerns separate.

        `username` is passed through to `LlmBridge.generate_reply` so the
        provider's prompt builder uses the same `[Current User Message]`
        framing as the original /chat call. Defaults to "WebUser" to
        match the chat handler's default when the client doesn't supply
        one.
        """
        if self._llm_bridge is None:
            # Defensive — main.py always wires this. A missing bridge
            # is a deployment bug, not a runtime condition the user
            # should see.
            raise ServiceError("internal_error", status=500)
        async with self._with_concurrency(
            token_name=token_name,
            operation="regenerate",
            session_id=session_id,
            ip=ip,
        ):
            return await self._regenerate_inner(
                token_name=token_name,
                session_id=session_id,
                message_index=message_index,
                username=username,
                token_daily_quota=token_daily_quota,
                ip=ip,
            )

    async def _regenerate_inner(
        self,
        *,
        token_name: str,
        session_id: str,
        message_index: int,
        username: str,
        token_daily_quota: int,
        ip: str | None,
    ) -> RegenerateResult:
        raw_history, cid, target_raw_idx, _target_entry, target_role = (
            await self._resolve_target_raw_index(
                token_name=token_name,
                session_id=session_id,
                message_index=message_index,
            )
        )
        if target_role != "assistant":
            # Only assistant messages can be regenerated. The preceding
            # user turn stays in place and is fed back to the LLM as
            # fresh context.
            raise ServiceError("message_not_found", status=404)

        # Recover the most recent user message (text + image refs) from
        # the surviving rendered history. LlmBridge.generate_reply needs
        # both: `message=` for the textual prompt and `image_urls=` so a
        # multimodal turn doesn't silently degrade to text-only on
        # regeneration. Walk back from target_raw_idx to the closest
        # user entry. If none exists (rare — assistant at the start of
        # the conversation, possible via tool-call only context) we 404.
        last_user_text = ""
        last_user_file_ids: list[str] = []
        for entry in reversed(raw_history[:target_raw_idx]):
            if not isinstance(entry, dict):
                continue
            if str(entry.get("role") or "").strip().lower() != "user":
                continue
            last_user_text = _extract_text(entry.get("content"))
            last_user_file_ids = list(
                _extract_attachment_file_ids(entry.get("content"))
            )
            break
        if not last_user_text and not last_user_file_ids:
            raise ServiceError("message_not_found", status=404)

        # Quota check BEFORE any side effect. A 429 here must not leave
        # CM truncated or attachments released; ordering matches /chat.
        today = date.today()
        try:
            today_count = await self._storage.get_today_usage(
                token_name, day=today
            )
        except Exception:
            logger.exception(
                "[WebChatGateway] get_today_usage during regenerate failed"
            )
            today_count = 0
        if today_count >= token_daily_quota:
            await self._audit.write(
                "quota_exceeded",
                name=token_name,
                ip=ip,
                detail={
                    "today_count": today_count,
                    "quota": token_daily_quota,
                    "operation": "regenerate",
                },
            )
            raise ServiceError("quota_exceeded", status=429)

        # Resolve attachments to provider-visible local paths so the
        # regenerate sees the same multimodal context the original /chat
        # call did. Done AFTER the quota gate so a quota-blocked
        # regenerate doesn't probe the file store.
        image_urls: list[str] = []
        for fid in last_user_file_ids:
            try:
                row = await self._storage.get_file(fid)
            except Exception:
                logger.exception(
                    "[WebChatGateway] get_file during regenerate failed "
                    "file_id=%s",
                    fid,
                )
                continue
            if row is None:
                continue
            # Defense-in-depth ownership recheck. The file_id came from
            # CM history under this token's session and should already
            # be owned — every other resolve site (chat.py:377,
            # _cm_persist_pair, _release_attached_files) verifies the
            # same. Skipping here would, on any future CM-corruption
            # path, let regenerate leak another token's bytes into
            # this LLM call.
            if row.token_name != token_name or row.session_id != session_id:
                logger.warning(
                    "[WebChatGateway] regenerate skipped cross-scope "
                    "attachment token=%s session=%s file=%s",
                    token_name,
                    session_id,
                    fid,
                )
                continue
            try:
                local_path = await self._file_store.open_local_path(
                    storage_key=row.storage_key
                )
            except Exception:
                logger.exception(
                    "[WebChatGateway] open_local_path during regenerate "
                    "failed key=%s",
                    row.storage_key,
                )
                continue
            if local_path:
                image_urls.append(local_path)

        # Truncate CM history to [0, target_raw_idx) and persist BEFORE
        # the LLM call. LlmBridge.generate_reply reads from CM via
        # `_history_text`; with the truncated history in place the
        # provider sees the right context.
        truncated_history = raw_history[:target_raw_idx]
        dropped_tail = raw_history[target_raw_idx:]
        umo = _umo(token_name, session_id)
        try:
            await self._cm.update_conversation(
                unified_msg_origin=umo,
                conversation_id=cid,
                history=truncated_history,
            )
        except Exception:
            logger.exception(
                "[WebChatGateway] CM.update_conversation(regenerate truncate)"
                " failed"
            )
            raise ServiceError("regenerate_failed", status=500) from None

        # Release attachments referenced ONLY by the dropped tail. The new
        # assistant reply we're about to generate is text-only, so the
        # final reference set equals truncated_history's reference set.
        await self._release_orphaned_attachments(
            token_name=token_name,
            session_id=session_id,
            removed_entries=dropped_tail,
            surviving_history=truncated_history,
            log_label="regenerate_assistant_message",
        )

        try:
            reply = await self._llm_bridge.generate_reply(
                token_name=token_name,
                session_id=session_id,
                username=username,
                message=last_user_text,
                image_urls=image_urls or None,
            )
        except Exception as exc:
            code, status, audit_event = map_llm_error(exc)
            if status == 500:
                logger.exception(
                    "[WebChatGateway] regenerate LLM call failed"
                )
            await self._audit.write(
                audit_event,
                name=token_name,
                ip=ip,
                detail={
                    "operation": "regenerate",
                    "error": str(exc)[:200] if status == 500 else "",
                },
            )
            raise ServiceError(code, status=status) from None

        # LLM succeeded — append the new assistant reply to the
        # truncated history. We use `AssistantMessageSegment` /
        # `TextPart` (the same shape `_cm_persist_pair` writes via
        # `add_message_pair`) so the regenerated entry's wire format
        # stays uniform with the rest of CM history. `model_dump()`
        # yields the dict CM stores; using `update_conversation`
        # rather than `add_message_pair` avoids appending a phantom
        # empty user turn (the existing user is already at
        # target_raw_idx-1).
        new_assistant_entry = AssistantMessageSegment(
            content=[TextPart(text=reply)]
        ).model_dump()
        final_history = truncated_history + [new_assistant_entry]
        try:
            await self._cm.update_conversation(
                unified_msg_origin=umo,
                conversation_id=cid,
                history=final_history,
            )
        except Exception:
            logger.exception(
                "[WebChatGateway] CM.update_conversation(regenerate write)"
                " failed"
            )
            raise ServiceError("regenerate_failed", status=500) from None

        # Increment quota AFTER the CM write succeeds — matches /chat
        # ordering (LLM → CM → usage → audit). A crash between LLM
        # and usage-increment leaves the user with a regenerated
        # reply persisted but not counted; that's strictly preferable
        # to the inverse (charged for a reply that was never persisted).
        try:
            new_count = await self._storage.increment_daily_usage(
                token_name, day=today
            )
        except Exception:
            logger.exception(
                "[WebChatGateway] increment_daily_usage during regenerate failed"
            )
            new_count = today_count + 1
        remaining = max(0, token_daily_quota - new_count)

        # Update session meta — net message count is unchanged (delete 1,
        # add 1) but updated_at + preview need to refresh. The new
        # rendered history is recomputed so a non-renderable trailing
        # entry doesn't desync the count cache.
        new_rendered = _normalize_history(final_history)
        new_meta_count = len(new_rendered)
        preview_text = reply[:_PREVIEW_CHARS]
        now = self._now()
        await self._storage.upsert_session_meta(
            token_name=token_name,
            session_id=session_id,
            message_count=new_meta_count,
            preview=preview_text,
            now=now,
        )

        # Build per-tail deletion events. Mid-history regenerate also
        # drops every rendered entry after the target (the LLM context
        # only includes [0, target) and we just persisted that). Emit
        # one message_deleted for each dropped rendered index in
        # DESCENDING order so peers splicing head-to-tail don't see
        # indices shift between events. Then message_added lands at
        # the slot the target used to occupy.
        rendered_to_raw = self._render_to_raw_indices(raw_history)
        deletion_events: list[NewEvent] = []
        for r in range(len(rendered_to_raw) - 1, message_index - 1, -1):
            raw_i = rendered_to_raw[r]
            if raw_i >= len(raw_history) or not isinstance(raw_history[raw_i], dict):
                continue
            dropped_role = str(
                raw_history[raw_i].get("role") or ""
            ).strip().lower()
            if dropped_role not in ("user", "assistant"):
                continue
            deletion_events.append(
                NewEvent(
                    event_type=EVENT_MESSAGE_DELETED,
                    session_id=session_id,
                    payload=json.dumps(
                        {"index": r, "role": dropped_role},
                        ensure_ascii=False,
                    ),
                )
            )
        deletion_events.append(
            NewEvent(
                event_type=EVENT_MESSAGE_ADDED,
                session_id=session_id,
                payload=json.dumps(
                    {"role": "assistant", "content": reply},
                    ensure_ascii=False,
                ),
            )
        )
        await self._storage.append_updates(
            token_name=token_name,
            events=deletion_events,
            now=now,
        )
        await self._event_bus.notify(token_name)
        await self._audit.write(
            "conv_message_regenerated",
            name=token_name,
            ip=ip,
            detail={
                "session_id": session_id,
                "index": message_index,
                "reply_len": len(reply),
                "remaining": remaining,
            },
        )
        return RegenerateResult(
            reply=reply,
            remaining=remaining,
            daily_quota=token_daily_quota,
        )

    async def record_chat_pair(
        self,
        *,
        token_name: str,
        session_id: str,
        user_text: str,
        assistant_text: str,
        incomplete: bool = False,
        user_already_emitted: bool = False,
        user_attachments: list[dict] | None = None,
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
                user_attachments=user_attachments,
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
                user_payload: dict[str, Any] = {
                    "role": "user",
                    "content": user_text,
                }
                if user_attachments:
                    user_payload["attachments"] = list(user_attachments)
                events.append(
                    NewEvent(
                        event_type=EVENT_MESSAGE_ADDED,
                        session_id=session_id,
                        payload=json.dumps(user_payload, ensure_ascii=False),
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
        attachments: list[dict] | None = None,
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

        `attachments` (optional) — list of `{file_id, mime}` dicts; when
        non-empty, the user message_added payload carries them under an
        `attachments` key so peer devices render the image bubble
        immediately too. record_chat_pair at close intentionally does
        NOT re-emit the user event (user_already_emitted=True path), so
        attachments only appear here for the streaming flow.

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
            user_payload: dict[str, Any] = {
                "role": "user",
                "content": user_text,
            }
            if attachments:
                user_payload["attachments"] = list(attachments)
            events.append(
                NewEvent(
                    event_type=EVENT_MESSAGE_ADDED,
                    session_id=session_id,
                    payload=json.dumps(user_payload, ensure_ascii=False),
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
        non-streaming LLM call, appends the new assistant reply, and
        emits `message_deleted` + `message_added` events for peers.

        Responses:
          200 `{ok: true, reply: str, remaining: int, daily_quota: int}`.
          400 `{error: "invalid_payload"|"invalid_json"}`.
          401 — auth gate.
          404 `{error: "session_not_found"|"message_not_found"}`.
          429 `{error: "concurrent_request"}` — another in-flight op.
          429 `{error: "quota_exceeded"}` — daily quota hit.
          502 `{error: "empty_reply"}` — provider returned no tokens.
          504 `{error: "llm_timeout"}` — provider stalled.
          500 `{error: "llm_call_failed"|"regenerate_failed"|"internal_error"}`.
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
        try:
            result = await service.regenerate_assistant_message(
                token_name=gated.token.name,
                session_id=session_id,
                message_index=raw_index,
                token_daily_quota=gated.token.daily_quota,
                ip=gated.ip,
            )
        except ServiceError as exc:
            return _err(request, gated.origin, exc)
        except Exception:
            logger.exception("[WebChatGateway] regenerate_message failed")
            return _err(
                request, gated.origin, ServiceError("internal_error", status=500)
            )
        return json_response(
            {
                "ok": True,
                "reply": result.reply,
                "remaining": result.remaining,
                "daily_quota": result.daily_quota,
            },
            origin=gated.origin,
            allowed_origins=gated.allowed,
            same_origin_host=gated.same_host,
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
