"""Shared overlay helpers + event-type constants for the conversations
layer.

Pure-function utilities that turn CM-stored message records into the
shapes the chat-sync layer + handler render path consume. No I/O, no
plugin-internal imports beyond the AstrBot logger (which is only used
for warnings in the optional retry paths inside helpers that depend on
this module).

Lives outside handlers/conversations.py so the service module
(handlers/conversations_service.py) and the HTTP handler factory
(handlers/conversations.py) can both pull these without forming a
circular dependency.
"""

from __future__ import annotations

from typing import Any


# Event types emitted via the chat-sync `webchat_updates` table. The
# service layer is the single writer; handlers / clients consume.
EVENT_SESSION_CREATED = "session_created"
EVENT_SESSION_META_UPDATED = "session_meta_updated"
EVENT_MESSAGE_ADDED = "message_added"
EVENT_MESSAGE_DELETED = "message_deleted"
EVENT_HISTORY_CLEARED = "history_cleared"
EVENT_STREAM_STARTED = "stream_started"
EVENT_STREAM_ENDED = "stream_ended"


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


__all__ = [
    "EVENT_SESSION_CREATED",
    "EVENT_SESSION_META_UPDATED",
    "EVENT_MESSAGE_ADDED",
    "EVENT_MESSAGE_DELETED",
    "EVENT_HISTORY_CLEARED",
    "EVENT_STREAM_STARTED",
    "EVENT_STREAM_ENDED",
    "_extract_text",
    "_extract_attachment_file_ids",
    "_renderable_entry",
    "_normalize_history",
]
