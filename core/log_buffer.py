"""In-memory log ring buffer for the admin panel's live log viewer.

A ``logging.Handler`` subclass captures records emitted on the AstrBot
logger (`astrbot.api.logger` / the framework's stub in tests) and stores
a normalised dict shape into a bounded deque. The admin HTTP layer
exposes two views:

  * `GET  /admin/logs?since=&level=&grep=&limit=` — historical replay
    of the buffer's tail, with simple server-side filtering.
  * `GET  /admin/logs/stream` — SSE that pushes each new record as it
    arrives. Browsers connect once on tab open and stay subscribed.

The buffer is process-local and intentionally NOT persisted: this is a
diagnostic surface, not an audit trail (that's what `audit_log` is for).
A plugin restart clears the buffer; restart-survival semantics would
require disk writes that aren't worth the maintenance burden for what
is really just a "what's the loop doing right now" view.

Capacity is bounded so a busy plugin can't blow up RAM. Old entries
rotate out FIFO. Subscribers (the SSE handler) get woken up via an
asyncio.Event so they don't have to poll.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
import traceback
from collections import deque
from dataclasses import dataclass


# Hard cap on retained entries. 5000 is enough to cover a typical
# bug-hunting session (~30 min at 50 lines/min) without keeping the
# entire bot history. Operators who need more should pipe to an
# external log sink (AstrBot main log, journald, syslog, Loki, …).
DEFAULT_CAPACITY = 5000

# Canonical level names exposed to the admin panel filter dropdown.
# Keep ordered so the UI can render a sorted list.
LEVEL_NAMES: tuple[str, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


@dataclass(frozen=True)
class LogEntry:
    """Normalised view of a ``logging.LogRecord``.

    The id is a monotonic per-process counter — clients use it as a
    cursor (``?since=<id>``) for catching up after a stream
    interruption. Wall-clock ``ts`` is separate because the id
    intentionally has no relation to time (a buffer that wrapped
    around would otherwise need monotonic-time bookkeeping).
    """

    id: int
    ts: float
    level: str
    logger: str
    message: str
    # Truncated exception traceback. None for non-exception records.
    # Capped so a noisy traceback can't dominate the buffer.
    exc: str | None = None


# Exception tracebacks can be huge (Python's traceback module returns
# the whole stack as a multi-line string). Cap so one exception doesn't
# push the buffer over OOM thresholds on a hot loop. Operators who need
# the full traceback still see it in the AstrBot main log via the same
# logger.exception() call site.
_MAX_EXC_CHARS = 4000

# Per-message char cap. Same reasoning — defensive against runaway
# loggers (e.g. dumping a 1 MB request body into a log line).
_MAX_MESSAGE_CHARS = 4000


class LogBuffer:
    """Bounded FIFO of LogEntry. Thread-safe for record ingestion AND
    coroutine-safe for subscription fan-out.

    Ingestion happens from logging's thread (could be the main thread,
    could be a worker — `logging` doesn't guarantee). Subscription
    happens from aiohttp's event loop thread. The lock guards both the
    deque and the wake-event swap.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = max(1, int(capacity))
        self._entries: deque[LogEntry] = deque(maxlen=self._capacity)
        self._lock = threading.Lock()
        # Monotonic id counter. Used by clients to "give me everything
        # after id X" for cursored replay.
        self._next_id = itertools.count(start=1)
        # Subscriber wake-up. Each new entry sets all events, then
        # subscribers swap in a fresh Event for the next wait. Lets us
        # avoid an asyncio.Queue per subscriber (which would each carry
        # their own bounded buffer separately from this one).
        self._wake_events: list[asyncio.Event] = []

    @property
    def capacity(self) -> int:
        return self._capacity

    def append(self, entry: LogEntry) -> None:
        """Thread-safe insertion. Called by `LogBufferHandler.emit`."""
        with self._lock:
            self._entries.append(entry)
            events = self._wake_events
            self._wake_events = []
        # Fire wake-ups outside the lock so a slow subscriber can't
        # block ingestion. Each Event is single-use; subscribers
        # register a fresh one each iteration.
        for ev in events:
            try:
                # event.set() must run on the loop that owns the event.
                # All subscribers register from the aiohttp loop, so a
                # plain set() is safe — but if this assumption ever
                # breaks (e.g. someone subscribes from a different
                # loop), we'd silently lose the wake. Caller responsibility.
                ev.set()
            except RuntimeError:
                # Loop closed mid-emit — the subscriber went away.
                continue

    def snapshot(
        self,
        *,
        since: int = 0,
        level: str | None = None,
        grep: str | None = None,
        limit: int = 500,
    ) -> tuple[list[LogEntry], int]:
        """Return up to ``limit`` entries past ``since`` that match the
        optional filters, plus the highest id observed (so the caller
        can store it as the next ``since``).

        ``level`` is "at-or-above" semantics (asking for WARNING also
        gets ERROR + CRITICAL). ``grep`` is a case-insensitive
        substring match against ``message``. Both are applied
        server-side because the SSE stream uses the same predicates —
        keeping them in one place avoids client/server divergence.
        """
        threshold = _level_no(level) if level else 0
        needle = grep.lower() if grep else None
        limit = max(1, min(int(limit), self._capacity))
        with self._lock:
            tail = list(self._entries)
        out: list[LogEntry] = []
        max_id_seen = since
        for entry in tail:
            if entry.id > max_id_seen:
                max_id_seen = entry.id
            if entry.id <= since:
                continue
            if threshold and _level_no(entry.level) < threshold:
                continue
            if needle and needle not in entry.message.lower():
                continue
            out.append(entry)
            if len(out) >= limit:
                # Truncated reply — caller can call again with
                # since=out[-1].id. The max_id we report is the
                # last MATCHED entry, NOT the last buffer id, so a
                # filter-narrowed call doesn't accidentally skip
                # ahead and miss entries the next filter relaxation
                # would have included.
                max_id_seen = out[-1].id
                break
        return out, max_id_seen

    async def wait_for_new(self, *, timeout: float | None = None) -> None:
        """Block (on the loop) until a new entry lands OR ``timeout``
        elapses. The SSE handler uses this to pump.
        """
        ev = asyncio.Event()
        with self._lock:
            self._wake_events.append(ev)
        try:
            if timeout is None:
                await ev.wait()
            else:
                try:
                    await asyncio.wait_for(ev.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
        finally:
            # Pop our event if it's still pending — append() popped the
            # whole list on the last fire, but if we time out without a
            # new entry, our event is still in there.
            with self._lock:
                try:
                    self._wake_events.remove(ev)
                except ValueError:
                    pass

    def next_id(self) -> int:
        """Mint the next id WITHOUT inserting an entry — used by the
        handler before constructing a LogEntry so the id reflects
        wall-clock arrival even if entry construction raises."""
        return next(self._next_id)


def _level_no(name: str | None) -> int:
    if not name:
        return 0
    val = logging.getLevelName(name.upper())
    return val if isinstance(val, int) else 0


class LogBufferHandler(logging.Handler):
    """Push records into a `LogBuffer`. Install with
    ``logging.getLogger("astrbot.stub").addHandler(handler)`` at plugin
    start, remove at stop.

    Inherits the logger's existing level filtering — if AstrBot is
    configured to suppress DEBUG, we don't see DEBUG either. That's
    deliberate: the admin viewer is a window into the SAME log stream
    operators see in the AstrBot main log, not a privileged tap that
    bypasses level config.
    """

    def __init__(self, buffer: LogBuffer) -> None:
        super().__init__()
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            try:
                message = record.getMessage()
            except Exception:
                # %-formatting failure — surface the raw fmt instead of
                # silently dropping the record.
                message = record.msg if isinstance(record.msg, str) else repr(record.msg)
            if len(message) > _MAX_MESSAGE_CHARS:
                message = message[: _MAX_MESSAGE_CHARS - 1] + "…"
            exc_text: str | None = None
            if record.exc_info:
                try:
                    exc_text = "".join(
                        traceback.format_exception(*record.exc_info)
                    )
                except Exception:
                    exc_text = None
                if exc_text and len(exc_text) > _MAX_EXC_CHARS:
                    exc_text = exc_text[: _MAX_EXC_CHARS - 1] + "…"
            entry = LogEntry(
                id=self._buffer.next_id(),
                ts=record.created,
                level=record.levelname,
                logger=record.name,
                message=message,
                exc=exc_text,
            )
            self._buffer.append(entry)
        except Exception:
            # Never raise out of emit() — logging.Handler.handleError is
            # the standard escape hatch.
            self.handleError(record)


def entry_to_dict(entry: LogEntry) -> dict:
    """Wire shape for /admin/logs JSON + SSE payloads."""
    payload: dict = {
        "id": entry.id,
        "ts": entry.ts,
        "level": entry.level,
        "logger": entry.logger,
        "message": entry.message,
    }
    if entry.exc:
        payload["exc"] = entry.exc
    return payload


__all__ = [
    "DEFAULT_CAPACITY",
    "LEVEL_NAMES",
    "LogBuffer",
    "LogBufferHandler",
    "LogEntry",
    "entry_to_dict",
]
