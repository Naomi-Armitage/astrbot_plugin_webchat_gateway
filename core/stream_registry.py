"""Stream registry — bookkeeping layer over StreamBuffer.

Owns the lifecycle of a single chat-stream: lock acquisition, buffer
entry creation, chat-sync `stream_started` / `stream_ended` events,
chat-history persistence on terminal states, and the matching audit
events. The HTTP `/chat/stream` POST handler drives a stream by
calling `open() → append() → close_*` and never touches the buffer or
the lock directly.

Lifecycle states:

    open()            → lock acquired, buffer entry created (PENDING),
                        chat_stream_started audit, stream_started event.
    append(handle, t) → buffer chunk appended, seq returned.
    close_ok(...)     → record_chat_pair(incomplete=False), stream_ended
                        event with status="ok", buffer CLOSED_OK,
                        lock released, chat_stream_completed audit.
    close_incomplete  → record_chat_pair(incomplete=True), stream_ended
                        event with status="incomplete", buffer
                        CLOSED_INCOMPLETE, lock released,
                        chat_stream_partial audit.
    close_failed(...) → no persist (no content), stream_ended event with
                        status="failed", buffer CLOSED_FAILED, lock
                        released, chat_stream_failed audit.

Order inside each close_* method is fixed by the PLAN:
    persist → emit event → close buffer → release lock → audit
A persist failure DOES NOT block the lock release: subsequent steps
run inside their own try-blocks so the registry never leaks a held
lock or buffer entry on transient storage errors.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from .audit import AuditLogger
from .ratelimit import PerTokenConcurrency
from .stream_buffer import BufferFullError, StreamBuffer, StreamBufferEntry


# Validation regex shared between registry (id construction) and the
# resume HTTP handler (id allow-list on the URL path). 128 chars covers
# the worst case of long token+session names without bloat. Token-name
# and session-id segments are themselves bounded upstream
# (`token_name` ≤ 64, `session_id` ≤ 128 in chat handler), but the
# regex is the canonical gatekeeper.
STREAM_ID_PATTERN: re.Pattern[str] = re.compile(
    r"^[a-zA-Z0-9_:.\-]{1,128}$"
)


# Audit event vocabulary. Strings live here rather than in audit.py so
# the chat-streaming module is self-contained; audit.py's docstring
# enumerates the canonical set but the strings are arbitrary.
_AUDIT_STARTED = "chat_stream_started"
_AUDIT_COMPLETED = "chat_stream_completed"
_AUDIT_PARTIAL = "chat_stream_partial"
_AUDIT_FAILED = "chat_stream_failed"


@dataclass
class StreamHandle:
    """Per-stream context returned by `StreamRegistry.open`.

    Carries everything close_* needs without having to re-look-up the
    buffer entry: stream_id (also serves as the lock holder tag),
    token_name, session_id, and the seq counter (driver-allocated, so
    we can't recompute from the buffer). `started_at` is the moment
    `open()` returned and the registry took ownership of the lock —
    used in audits.

    `_used` is a defensive guard against double-close: the close_*
    methods set it to True on entry, so a subsequent close call on the
    same handle becomes a no-op rather than double-releasing the lock.
    """

    stream_id: str
    token_name: str
    session_id: str
    started_at: float
    next_seq: int = 0
    _used: bool = field(default=False, repr=False)


class StreamRegistry:
    def __init__(
        self,
        *,
        buffer: StreamBuffer,
        concurrency: PerTokenConcurrency,
        audit: AuditLogger,
        conv_service: Any,
    ) -> None:
        self._buffer = buffer
        self._concurrency = concurrency
        self._audit = audit
        self._conv_service = conv_service

    # --- buffer surface for the resume handler ---

    @property
    def buffer(self) -> StreamBuffer:
        """The underlying buffer.

        Exposed read-only so the resume handler can call
        `iter_subscribe(...)` directly — that path is a long-lived async
        generator and wrapping it in a registry forwarder would obscure
        the lifecycle without buying anything.
        """
        return self._buffer

    async def fetch(
        self, *, stream_id: str, token_name: str
    ) -> StreamBufferEntry | None:
        """Return the buffer entry IFF it exists AND belongs to `token_name`.

        Encapsulates the cross-token-returns-404 invariant so the resume
        handler doesn't have to compare token names itself: a snapshot
        whose `token_name` differs from the requesting token is
        indistinguishable, from the caller's perspective, from a missing
        stream — preventing existence-by-timing leaks across tokens.

        `after_seq=-1` returns the full chunk list; the resume handler
        does its own seq-window filtering when emitting the replay.
        """
        snapshot = await self._buffer.fetch_since(stream_id, after_seq=-1)
        if snapshot is None:
            return None
        if snapshot.token_name != token_name:
            return None
        return snapshot

    # --- id construction ---

    @staticmethod
    def _new_stream_id(token_name: str) -> str:
        """Build a stream id matching `STREAM_ID_PATTERN`.

        Format: `{token}:{ms}-{hex8}` — token-scoped, monotonic within a
        millisecond by the random suffix, and short enough to fit URL
        paths without further encoding.

        `session_id` is intentionally NOT embedded: callers may use
        arbitrary-charset session identifiers (Chinese, spaces, '+',
        URL-unsafe punctuation) which would fail STREAM_ID_PATTERN and
        regress on v1 `/chat`. The session_id is still tracked on the
        buffer entry and StreamHandle, so cross-token isolation and
        peer-attach lookups don't depend on it being part of the id.
        token_name is admin-validated upstream against a strict charset,
        so embedding it is safe.
        """
        return (
            f"{token_name}:"
            f"{int(time.time() * 1000)}-{secrets.token_hex(4)}"
        )

    # --- lifecycle ---

    async def open(
        self, *, token_name: str, session_id: str
    ) -> StreamHandle | None:
        """Acquire the per-token lock and create a new buffer entry.

        Returns None if another stream is already in flight for this
        token (caller responds with 429). On success, returns a
        StreamHandle the caller drives via append/close_* and emits
        the `chat_stream_started` audit + `stream_started` chat-sync
        audit event before any chunks are pushed.

        The chat-sync `stream_started` event is intentionally emitted by
        the HTTP handler only after the SSE handshake and stream_id frame
        are written. Emitting it here would let peer devices attach to a
        stream that the origin client never successfully opened.
        """
        stream_id = self._new_stream_id(token_name)
        # Defensive: the constructor format always satisfies the
        # pattern, but a future change to _new_stream_id could regress.
        # Cheap to re-validate and the alternative (a malformed id
        # poisoning later URL routing) is bad.
        if not STREAM_ID_PATTERN.match(stream_id):
            logger.error(
                "[WebChatGateway] generated stream_id failed validation: %s",
                stream_id,
            )
            return None
        acquired = await self._concurrency.acquire_with_id(
            token_name, stream_id
        )
        if not acquired:
            return None
        # Buffer entry has to exist before stream_started fires, so a
        # peer device that immediately attaches via resume sees a real
        # PENDING buffer rather than a 404.
        try:
            await self._buffer.create(
                stream_id=stream_id,
                token_name=token_name,
                session_id=session_id,
            )
        except BufferFullError:
            # All entries active and the global cap is hit — log as a
            # distinct event so an operator can tell the postmortem
            # apart from a generic buffer.create failure (e.g. Redis
            # outage). The handler converts our None return into the
            # same 429 it would emit on lock contention.
            logger.warning(
                "[WebChatGateway] stream-registry buffer full; refusing "
                "new stream sid=%s",
                stream_id,
            )
            try:
                await asyncio.shield(
                    self._concurrency.release(token_name, stream_id)
                )
            except Exception:
                logger.exception(
                    "[WebChatGateway] stream-registry release after "
                    "buffer-full failed sid=%s",
                    stream_id,
                )
            return None
        except Exception:
            logger.exception(
                "[WebChatGateway] stream-registry buffer.create failed sid=%s",
                stream_id,
            )
            # Shielded so a CancelledError raised mid-cleanup can't leave
            # the per-token lock held forever. The release itself is
            # idempotent — a no-op if the holder doesn't match.
            try:
                await asyncio.shield(
                    self._concurrency.release(token_name, stream_id)
                )
            except Exception:
                logger.exception(
                    "[WebChatGateway] stream-registry release after "
                    "buffer.create failure failed sid=%s",
                    stream_id,
                )
            return None
        handle = StreamHandle(
            stream_id=stream_id,
            token_name=token_name,
            session_id=session_id,
            started_at=time.time(),
        )
        try:
            await self._audit.write(
                _AUDIT_STARTED,
                name=token_name,
                detail={
                    "stream_id": stream_id,
                    "session_id": session_id,
                },
            )
        except Exception:
            logger.exception(
                "[WebChatGateway] audit chat_stream_started failed sid=%s",
                stream_id,
            )
        return handle

    async def append(self, handle: StreamHandle, text: str) -> int:
        """Append a chunk of `text` to the stream's buffer.

        Returns the seq assigned to the chunk. The seq is monotonic
        per-handle starting at 0 and is owned by the registry (not the
        buffer), so a future implementation could back the buffer with
        a transport that doesn't return per-entry sequence numbers.
        """
        if handle._used:
            raise RuntimeError(
                "stream handle already closed; cannot append after close_*"
            )
        seq = handle.next_seq
        handle.next_seq = seq + 1
        await self._buffer.append_chunk(handle.stream_id, seq, text)
        return seq

    # --- terminal closes ---

    async def close_ok(
        self,
        handle: StreamHandle,
        *,
        user_text: str,
        full_text: str,
        remaining: int,
        daily_quota: int,
    ) -> None:
        """Successful completion: persist + emit ok + close OK + audit."""
        await self._close(
            handle,
            buffer_state="closed_ok",
            buffer_final={
                "remaining": remaining,
                "daily_quota": daily_quota,
                "incomplete": False,
            },
            event_status="ok",
            audit_event=_AUDIT_COMPLETED,
            audit_detail={
                "stream_id": handle.stream_id,
                "session_id": handle.session_id,
                "reply_len": len(full_text),
                "remaining": remaining,
                "incomplete": False,
            },
            persist=lambda: self._conv_service.record_chat_pair(
                token_name=handle.token_name,
                session_id=handle.session_id,
                user_text=user_text,
                assistant_text=full_text,
                incomplete=False,
                user_already_emitted=True,
            ),
        )

    async def close_incomplete(
        self,
        handle: StreamHandle,
        *,
        user_text: str,
        partial_text: str,
        remaining: int,
        daily_quota: int,
        reason: str,
    ) -> None:
        """Aborted-with-content: persist as incomplete + emit incomplete +
        close INCOMPLETE + audit."""
        await self._close(
            handle,
            buffer_state="closed_incomplete",
            buffer_final={
                "remaining": remaining,
                "daily_quota": daily_quota,
                "incomplete": True,
            },
            event_status="incomplete",
            audit_event=_AUDIT_PARTIAL,
            audit_detail={
                "stream_id": handle.stream_id,
                "session_id": handle.session_id,
                "reply_len": len(partial_text),
                "remaining": remaining,
                "incomplete": True,
                "reason": reason,
            },
            persist=lambda: self._conv_service.record_chat_pair(
                token_name=handle.token_name,
                session_id=handle.session_id,
                user_text=user_text,
                assistant_text=partial_text,
                incomplete=True,
                user_already_emitted=True,
            ),
        )

    async def close_failed(
        self,
        handle: StreamHandle,
        *,
        error_code: str,
    ) -> None:
        """Aborted-no-content: emit failed + close FAILED + audit, no persist.

        The previous user message stays in CM history alone (or doesn't
        land at all — record_chat_pair was never called) and the user
        can resend.
        """
        await self._close(
            handle,
            buffer_state="closed_failed",
            buffer_final={"error": error_code},
            event_status="failed",
            audit_event=_AUDIT_FAILED,
            audit_detail={
                "stream_id": handle.stream_id,
                "session_id": handle.session_id,
                "error": error_code,
            },
            persist=None,
        )

    # --- shared close path ---

    async def _close(
        self,
        handle: StreamHandle,
        *,
        buffer_state: str,
        buffer_final: dict[str, Any],
        event_status: str,
        audit_event: str,
        audit_detail: dict[str, Any],
        persist: Any,
    ) -> None:
        """Single implementation of the close pipeline.

        Step order is fixed: persist → emit event → close buffer →
        release lock → audit. Steps 1-3 (persist, emit, buffer.close)
        run inside a try-block; step 4 (release lock) sits in the
        matching `finally` and is `asyncio.shield`-wrapped because
        `asyncio.CancelledError` is NOT an `Exception` subclass — a
        cancellation in any earlier step would otherwise skip the
        release entirely and leak the per-token lock until process
        restart. Step 5 (audit) runs after the finally; if it gets
        cancelled too we lose the audit row, which is the right
        tradeoff vs. never releasing the lock.
        """
        if handle._used:
            return
        handle._used = True
        try:
            # 1. Persist (skipped on failed-no-content). Must not block
            #    lock release: a transient storage error here can't be
            #    allowed to pin the per-token slot.
            if persist is not None:
                try:
                    await persist()
                except Exception:
                    logger.exception(
                        "[WebChatGateway] stream-registry persist failed sid=%s",
                        handle.stream_id,
                    )
            # 2. Emit chat-sync stream_ended. This wakes peer devices'
            #    typing indicators. Same best-effort posture as
            #    stream_started.
            try:
                await self._conv_service.emit_stream_ended(
                    token_name=handle.token_name,
                    session_id=handle.session_id,
                    stream_id=handle.stream_id,
                    status=event_status,
                )
            except Exception:
                logger.exception(
                    "[WebChatGateway] emit_stream_ended failed sid=%s",
                    handle.stream_id,
                )
            # 3. Close the buffer entry. Setting state + final atomically
            #    here is what unblocks parked iter_subscribe iterators
            #    (see InMemoryBuffer.close); resume callers race-free
            #    observe a closed entry from this point on.
            try:
                await self._buffer.close(
                    handle.stream_id,
                    state=buffer_state,
                    final=buffer_final,
                )
            except Exception:
                logger.exception(
                    "[WebChatGateway] stream-registry buffer.close failed sid=%s",
                    handle.stream_id,
                )
        finally:
            # 4. Release the per-token concurrency lock. SHIELDED so a
            #    CancelledError propagating up from steps 1-3 cannot
            #    skip the release — `release` is itself fast (a single
            #    mutex round-trip) so the shielded await is safe.
            try:
                await asyncio.shield(
                    self._concurrency.release(
                        handle.token_name, handle.stream_id
                    )
                )
            except Exception:
                logger.exception(
                    "[WebChatGateway] stream-registry release failed sid=%s",
                    handle.stream_id,
                )
        # 5. Audit last. Outside the try/finally because if everything
        #    above was cancelled we'd rather drop this audit row than
        #    further extend the cancellation window.
        try:
            await self._audit.write(
                audit_event,
                name=handle.token_name,
                detail=audit_detail,
            )
        except Exception:
            logger.exception(
                "[WebChatGateway] audit %s failed sid=%s",
                audit_event,
                handle.stream_id,
            )


__all__ = [
    "STREAM_ID_PATTERN",
    "StreamHandle",
    "StreamRegistry",
]
