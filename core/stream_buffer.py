"""Transient buffer for in-flight chat streams.

Bridges live SSE subscribers (the original POSTer + any peers attaching
via /chat/stream/{id}/resume) with the LLM-driving handler. Buffers are
purely transient: they exist to serve resume requests during the LLM
call and for a short grace window (~30s) after a terminal state, so a
client that JUST got the done frame and immediately reloaded can pick
up its reply from the buffer rather than racing the chat-sync write.

Two implementations behind a single Protocol:

- `InMemoryBuffer` (default): a dict keyed by stream_id, mutated under a
  single asyncio.Lock. Each entry carries an asyncio.Event that wakes
  every parked subscriber when a new chunk arrives. After waking, the
  Event is replaced with a fresh one so each chunk-append wakes the
  current waiters exactly once. A background sweeper task evicts entries
  whose grace TTL has expired.

- `RedisBuffer` (opt-in via `storage.stream_buffer_redis_dsn`): one
  Redis Stream per stream_id for chunks + a tombstone close entry, plus
  a metadata hash and a per-token sorted set for the active list and
  cap enforcement. Subscribers BLOCK on XREAD. The redis-py import is
  deferred to `__init__` so users without the package don't pay the
  ImportError at startup.

Both implementations expose `iter_subscribe(stream_id, after_seq)` —
the live-tail iterator the resume handler consumes. It yields chunks
strictly newer than `after_seq` (an `after_seq` of -1 means "everything
from seq 0"), then returns (StopAsyncIteration) once the stream reaches
a terminal state. The terminal frame itself is read separately via
`fetch_since` after the iterator returns.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Literal,
    NamedTuple,
    Protocol,
)

from astrbot.api import logger


# Hard cap defaults — overridable via the streaming config dataclass that
# agent B owns. Surfaced as constants here so the buffer module is usable
# in isolation (tests / unit harnesses) without wiring full config.
DEFAULT_GRACE_SECONDS = 30
DEFAULT_MAX_PER_TOKEN = 3
DEFAULT_MAX_GLOBAL = 200
_SWEEPER_INTERVAL_SECONDS = 5.0

# Buffer state machine literals — kept as strings so they survive a JSON
# round-trip via Redis without an enum encoder. The PLAN names them
# `pending / streaming / closed_ok / closed_incomplete / closed_failed`.
StreamState = Literal[
    "pending",
    "streaming",
    "closed_ok",
    "closed_incomplete",
    "closed_failed",
]
_TERMINAL_STATES: frozenset[str] = frozenset(
    {"closed_ok", "closed_incomplete", "closed_failed"}
)


class BufferFullError(RuntimeError):
    """Raised by `create()` when the global cap is reached AND every
    existing entry is still active (PENDING/STREAMING).

    Active entries cannot be safely evicted: doing so would silently
    terminate another token's in-flight stream and surface as
    `stream_not_found` to peer subscribers. The registry's `open()`
    catches this, logs the type explicitly, releases the lock, and
    returns None — the chat handler then surfaces it to the client
    as a 429 (same path as concurrent_request, but the audit log
    captures the real cause).
    """


class StreamBufferEntry(NamedTuple):
    """Snapshot of a stream's buffer state at the time of fetch.

    Returned by `fetch_since`; mutating the tuple has no effect on the
    underlying buffer (NamedTuple is immutable + the chunk list is a
    copy, see InMemoryBuffer.fetch_since). `final` carries the same
    payload that the resume handler will serialize into the closing
    `done` / `error` frame; it's None until close().
    """

    stream_id: str
    token_name: str
    session_id: str
    started_at: float
    state: StreamState
    chunks: list[tuple[int, str]]
    final: dict[str, Any] | None
    closed_at: float | None


# Audit callback signature: invoked by the buffer on cap-driven and
# TTL-driven eviction. Reasons are short stable codes ("ttl_expired",
# "per_token_cap", "global_cap") that the registry/audit layer can map
# to a `chat_stream_evicted` audit detail.
EvictAuditCallable = Callable[[str, str], Awaitable[None]]


class StreamBuffer(Protocol):
    """Transport-agnostic surface for stream buffering.

    All mutating calls are coroutines so the in-memory and Redis
    implementations share the same signature. Reads (`fetch_since`,
    `count`, `list_active_for_token`) are also coroutines — even
    InMemoryBuffer briefly takes its mutex on read paths so cap
    enforcement and snapshots are point-in-time consistent.
    """

    async def create(
        self, *, stream_id: str, token_name: str, session_id: str
    ) -> None: ...

    async def append_chunk(
        self, stream_id: str, seq: int, text: str
    ) -> None: ...

    async def close(
        self, stream_id: str, *, state: str, final: dict[str, Any]
    ) -> None: ...

    async def fetch_since(
        self, stream_id: str, *, after_seq: int
    ) -> StreamBufferEntry | None: ...

    async def evict(self, stream_id: str) -> None: ...

    async def list_active_for_token(
        self, token_name: str
    ) -> list[str]: ...

    async def count(self) -> int: ...

    def iter_subscribe(
        self, stream_id: str, after_seq: int
    ) -> AsyncIterator[tuple[int, str]]: ...


# ----------------------------------------------------------------------
# In-memory implementation
# ----------------------------------------------------------------------


class _InMemoryEntry:
    """Mutable per-stream record used inside InMemoryBuffer.

    Held under InMemoryBuffer._lock for all mutation. The `new_chunk`
    Event is the wake primitive for live subscribers: every append_chunk
    sets the current Event and immediately swaps in a fresh one so each
    appended chunk wakes the present set of waiters exactly once. The
    `terminal` Event is set once at close() and never replaced — late
    subscribers parking after close see it already-set and exit their
    wait immediately.
    """

    __slots__ = (
        "stream_id",
        "token_name",
        "session_id",
        "started_at",
        "state",
        "chunks",
        "final",
        "closed_at",
        "new_chunk",
        "terminal",
    )

    def __init__(
        self, *, stream_id: str, token_name: str, session_id: str
    ) -> None:
        self.stream_id: str = stream_id
        self.token_name: str = token_name
        self.session_id: str = session_id
        self.started_at: float = time.time()
        self.state: StreamState = "pending"
        self.chunks: list[tuple[int, str]] = []
        self.final: dict[str, Any] | None = None
        self.closed_at: float | None = None
        self.new_chunk: asyncio.Event = asyncio.Event()
        self.terminal: asyncio.Event = asyncio.Event()

    def snapshot(self, *, after_seq: int) -> StreamBufferEntry:
        """Build an immutable view of this entry, filtered to seq > after_seq.

        `after_seq = -1` means "everything from 0". The chunks list is a
        fresh list so callers can't mutate buffer state through the snapshot.
        """
        if after_seq < 0:
            chunks = list(self.chunks)
        else:
            # The chunk list is append-only and (seq, _) is monotonic, so
            # a linear scan from the tail is actually faster than bisect
            # for the typical resume case (after_seq is recent). Keep it
            # simple — the chunk count is bounded and small.
            chunks = [pair for pair in self.chunks if pair[0] > after_seq]
        return StreamBufferEntry(
            stream_id=self.stream_id,
            token_name=self.token_name,
            session_id=self.session_id,
            started_at=self.started_at,
            state=self.state,
            chunks=chunks,
            final=dict(self.final) if self.final is not None else None,
            closed_at=self.closed_at,
        )


class InMemoryBuffer:
    """Default StreamBuffer implementation: single-process, dict-backed."""

    def __init__(
        self,
        *,
        grace_seconds: int = DEFAULT_GRACE_SECONDS,
        max_per_token: int = DEFAULT_MAX_PER_TOKEN,
        max_global: int = DEFAULT_MAX_GLOBAL,
        on_evict: EvictAuditCallable | None = None,
    ) -> None:
        self._grace_seconds = max(1, int(grace_seconds))
        self._max_per_token = max(1, int(max_per_token))
        self._max_global = max(1, int(max_global))
        self._on_evict = on_evict
        # OrderedDict so cap eviction can drop the oldest insertion in
        # O(1) — `popitem(last=False)`. `create()` keeps this in
        # insertion-time order; we don't reorder on access.
        self._entries: OrderedDict[str, _InMemoryEntry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._sweeper_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    # --- lifecycle hooks for aiohttp_app.cleanup_ctx ---

    async def start_sweeper(self, app: Any | None = None) -> None:
        """Begin the background eviction sweeper.

        The optional `app` arg lets this be passed directly as part of an
        aiohttp `cleanup_ctx` generator. Idempotent: calling twice is a
        no-op once the task is running.
        """
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return
        self._stop_event.clear()
        self._sweeper_task = asyncio.create_task(
            self._sweep_loop(), name="webchat-stream-buffer-sweeper"
        )

    async def stop_sweeper(self) -> None:
        """Stop the sweeper and await its exit. Idempotent."""
        task = self._sweeper_task
        self._sweeper_task = None
        if task is None:
            return
        self._stop_event.set()
        if not task.done():
            try:
                await asyncio.wait_for(task, timeout=10.0)
            except asyncio.TimeoutError:
                # Sweeper is wedged on something — cancel + drop. Logged
                # so an operator notices a real bug, but we do not block
                # plugin shutdown on it.
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
                logger.warning(
                    "[WebChatGateway] stream-buffer sweeper did not exit cleanly"
                )

    async def _sweep_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=_SWEEPER_INTERVAL_SECONDS,
                    )
                    return  # stop requested
                except asyncio.TimeoutError:
                    pass
                try:
                    await self._sweep_once()
                except Exception:
                    logger.exception(
                        "[WebChatGateway] stream-buffer sweep iteration failed"
                    )
        except asyncio.CancelledError:
            return

    async def _sweep_once(self) -> None:
        now = time.time()
        cutoff = now - self._grace_seconds
        to_evict: list[str] = []
        async with self._lock:
            for stream_id, entry in self._entries.items():
                if entry.closed_at is not None and entry.closed_at < cutoff:
                    to_evict.append(stream_id)
        for stream_id in to_evict:
            await self._do_evict(stream_id, reason="ttl_expired")

    # --- buffer API ---

    async def create(
        self, *, stream_id: str, token_name: str, session_id: str
    ) -> None:
        # Pre-compute eviction targets under the lock, then execute the
        # eviction *outside* the lock (the audit callback is async and
        # may touch storage; we don't want to serialize append/close on
        # network I/O). Because each evicted stream_id is uniquely owned
        # by the in-memory dict, doing the actual entry removal up-front
        # under the lock is safe — `_do_evict` then just runs the audit.
        per_token_evict: list[str] = []
        global_evict: list[str] = []
        async with self._lock:
            if stream_id in self._entries:
                # Defensive: stream IDs are server-generated with a random
                # suffix, but if a caller ever reuses one we treat it as
                # a no-op (the existing entry wins). Logging makes the
                # collision visible for postmortem.
                logger.warning(
                    "[WebChatGateway] stream_buffer.create reused stream_id=%s",
                    stream_id,
                )
                return
            # Per-token cap. Only CLOSED_* entries are eligible for
            # eviction — an active entry being silently dropped would
            # cancel that token's in-flight stream from under it. With
            # per-token concurrency=1, an active entry on this token
            # is impossible at this point (the lock is held by us), so
            # the skip-active branch is defense-in-depth: it documents
            # the invariant and survives a future relaxation of the
            # single-flight policy.
            owned_closed: list[str] = [
                sid
                for sid, ent in self._entries.items()
                if ent.token_name == token_name
                and ent.state in _TERMINAL_STATES
            ]
            owned_total = sum(
                1 for ent in self._entries.values()
                if ent.token_name == token_name
            )
            while owned_total >= self._max_per_token and owned_closed:
                victim = owned_closed.pop(0)
                per_token_evict.append(victim)
                self._evict_locked(victim)
                owned_total -= 1
            # Global cap. Same skip-active rule: prefer the OLDEST
            # CLOSED entry first (insertion order preserved by
            # OrderedDict iteration). If every entry is still active,
            # raise BufferFullError so the caller surfaces a real
            # error rather than corrupting an in-flight stream of a
            # different token.
            while len(self._entries) >= self._max_global:
                oldest_closed: str | None = None
                for sid, ent in self._entries.items():
                    if ent.state in _TERMINAL_STATES:
                        oldest_closed = sid
                        break
                if oldest_closed is None:
                    raise BufferFullError(
                        f"global stream-buffer cap {self._max_global} "
                        "reached with all entries active; refusing to "
                        "create new stream"
                    )
                self._entries.pop(oldest_closed)
                global_evict.append(oldest_closed)
            entry = _InMemoryEntry(
                stream_id=stream_id,
                token_name=token_name,
                session_id=session_id,
            )
            self._entries[stream_id] = entry
        # Run audits outside the lock.
        for sid in per_token_evict:
            await self._audit_evict(sid, "per_token_cap")
        for sid in global_evict:
            await self._audit_evict(sid, "global_cap")

    def _evict_locked(self, stream_id: str) -> None:
        """Remove `stream_id` from `_entries` and wake any subscribers.

        Caller must hold `self._lock`. Setting both events ensures a
        currently-parked iter_subscribe wakes and discovers the entry
        is gone (its next fetch_since returns None and the iterator
        returns cleanly).
        """
        entry = self._entries.pop(stream_id, None)
        if entry is None:
            return
        entry.terminal.set()
        entry.new_chunk.set()

    async def append_chunk(
        self, stream_id: str, seq: int, text: str
    ) -> None:
        async with self._lock:
            entry = self._entries.get(stream_id)
            if entry is None:
                return
            if entry.state in _TERMINAL_STATES:
                # Late chunk arriving after close — ignore. The driver
                # should never do this, but the buffer must not corrupt
                # state if it happens.
                return
            entry.chunks.append((seq, text))
            entry.state = "streaming"
            old_event = entry.new_chunk
            entry.new_chunk = asyncio.Event()
        # Wake current waiters AFTER swapping in the fresh event so any
        # waiter that races into a subsequent wait() does so on the
        # NEW event and won't miss the next chunk's wake.
        old_event.set()

    async def close(
        self, stream_id: str, *, state: str, final: dict[str, Any]
    ) -> None:
        if state not in _TERMINAL_STATES:
            raise ValueError(
                f"close() requires a terminal state, got {state!r}"
            )
        async with self._lock:
            entry = self._entries.get(stream_id)
            if entry is None:
                return
            entry.state = state  # type: ignore[assignment]
            entry.final = dict(final)
            entry.closed_at = time.time()
            old_event = entry.new_chunk
            entry.terminal.set()
        old_event.set()

    async def fetch_since(
        self, stream_id: str, *, after_seq: int
    ) -> StreamBufferEntry | None:
        async with self._lock:
            entry = self._entries.get(stream_id)
            if entry is None:
                return None
            return entry.snapshot(after_seq=after_seq)

    async def evict(self, stream_id: str) -> None:
        await self._do_evict(stream_id, reason="manual")

    async def _do_evict(self, stream_id: str, *, reason: str) -> None:
        async with self._lock:
            if stream_id not in self._entries:
                return
            self._evict_locked(stream_id)
        await self._audit_evict(stream_id, reason)

    async def _audit_evict(self, stream_id: str, reason: str) -> None:
        if self._on_evict is None:
            return
        try:
            await self._on_evict(stream_id, reason)
        except Exception:
            logger.exception(
                "[WebChatGateway] stream-buffer eviction audit failed sid=%s",
                stream_id,
            )

    async def list_active_for_token(
        self, token_name: str
    ) -> list[str]:
        async with self._lock:
            return [
                sid
                for sid, ent in self._entries.items()
                if ent.token_name == token_name
            ]

    async def count(self) -> int:
        async with self._lock:
            return len(self._entries)

    async def iter_subscribe(
        self, stream_id: str, after_seq: int
    ) -> AsyncIterator[tuple[int, str]]:
        """Yield chunks with seq > after_seq until terminal state.

        The first batch returned is whatever the buffer already has past
        `after_seq` at the moment of subscription; subsequent batches
        are unblocked by `append_chunk`'s Event swap. Returns when the
        stream reaches a terminal state OR is evicted (entry vanishes).
        Live readers consume the terminal frame separately via a final
        `fetch_since` after this iterator returns.
        """
        last_seq = after_seq
        while True:
            async with self._lock:
                entry = self._entries.get(stream_id)
                if entry is None:
                    return
                # Snapshot newly-available chunks under the lock so
                # append_chunk can't slip a chunk in between us reading
                # the list and reading the wake-event.
                pending: list[tuple[int, str]] = []
                for seq, text in entry.chunks:
                    if seq > last_seq:
                        pending.append((seq, text))
                wake = entry.new_chunk
                terminal_set = entry.terminal.is_set()
            for seq, text in pending:
                yield (seq, text)
                if seq > last_seq:
                    last_seq = seq
            if terminal_set:
                # Terminal frame is owned by the resume handler; drop
                # the iterator here so the caller can fetch_since() and
                # write the closing frame itself.
                return
            await wake.wait()


# ----------------------------------------------------------------------
# Redis implementation
# ----------------------------------------------------------------------


# Field names for the meta hash. Stable strings — clients in different
# processes need to agree on these without an enum.
_META_TOKEN = "token"
_META_SESSION = "session_id"
_META_STATE = "state"
_META_STARTED = "started_at"
_META_CLOSED = "closed_at"
_META_FINAL = "final"

# Stream-entry field names.
_ENTRY_SEQ = "seq"
_ENTRY_TEXT = "text"
_ENTRY_FINAL = "final"  # tombstone marker

# How long XREAD blocks per round-trip. Short enough that close() is
# detected promptly even if the close-tombstone XADD hits the stream
# milliseconds after a subscriber's XREAD just timed out.
_REDIS_BLOCK_MS = 5000


class RedisBuffer:
    """StreamBuffer implementation backed by Redis Streams + hashes.

    Layout per stream_id:

    - `webchat:stream:{id}` — Redis Stream of chunks, each entry of the
      form `{seq: "<n>", text: "<chunk>"}`. A close tombstone is appended
      as `{final: "<json>"}` so subscribers blocked on XREAD wake on
      close.
    - `webchat:stream-meta:{id}` — Hash with token, session_id, state,
      started_at, closed_at, final (JSON). Updated on close.
    - `webchat:streams-by-token:{token}` — Sorted set of active stream
      ids scored by started_at, used for `list_active_for_token` and
      cap enforcement.

    The `redis.asyncio` import is deferred to `__init__` so users
    without redis-py installed don't hit ImportError on plugin load.
    """

    def __init__(
        self,
        *,
        dsn: str,
        grace_seconds: int = DEFAULT_GRACE_SECONDS,
        max_per_token: int = DEFAULT_MAX_PER_TOKEN,
        max_global: int = DEFAULT_MAX_GLOBAL,
        on_evict: EvictAuditCallable | None = None,
        key_prefix: str = "webchat",
    ) -> None:
        # Lazy import — keeps the symbol resolution off the module load
        # path so users without redis-py see no ImportError until they
        # configure a Redis DSN.
        try:
            import redis.asyncio as redis_asyncio  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "RedisBuffer requires the redis-py package. "
                "Install with: pip install redis>=4.2"
            ) from exc
        self._redis_asyncio = redis_asyncio
        self._dsn = dsn
        self._grace_seconds = max(1, int(grace_seconds))
        self._max_per_token = max(1, int(max_per_token))
        self._max_global = max(1, int(max_global))
        self._on_evict = on_evict
        self._key_prefix = key_prefix.rstrip(":")
        self._client: Any | None = None
        self._sweeper_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    # --- key helpers ---

    def _stream_key(self, stream_id: str) -> str:
        return f"{self._key_prefix}:stream:{stream_id}"

    def _meta_key(self, stream_id: str) -> str:
        return f"{self._key_prefix}:stream-meta:{stream_id}"

    def _token_index_key(self, token_name: str) -> str:
        return f"{self._key_prefix}:streams-by-token:{token_name}"

    def _global_index_key(self) -> str:
        return f"{self._key_prefix}:streams-active"

    async def _get_client(self) -> Any:
        if self._client is None:
            # `decode_responses=True` so we can treat hash + stream values
            # as str without manual .decode() at every read site.
            self._client = self._redis_asyncio.from_url(
                self._dsn, decode_responses=True
            )
        return self._client

    # --- lifecycle hooks ---

    async def start_sweeper(self, app: Any | None = None) -> None:
        if self._sweeper_task is not None and not self._sweeper_task.done():
            return
        self._stop_event.clear()
        self._sweeper_task = asyncio.create_task(
            self._sweep_loop(),
            name="webchat-stream-buffer-redis-sweeper",
        )

    async def stop_sweeper(self) -> None:
        task = self._sweeper_task
        self._sweeper_task = None
        if task is not None:
            self._stop_event.set()
            if not task.done():
                try:
                    await asyncio.wait_for(task, timeout=10.0)
                except asyncio.TimeoutError:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                logger.exception(
                    "[WebChatGateway] redis stream buffer close failed"
                )
            self._client = None

    async def _sweep_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=_SWEEPER_INTERVAL_SECONDS,
                    )
                    return
                except asyncio.TimeoutError:
                    pass
                try:
                    await self._sweep_once()
                except Exception:
                    logger.exception(
                        "[WebChatGateway] redis stream-buffer sweep failed"
                    )
        except asyncio.CancelledError:
            return

    async def _sweep_once(self) -> None:
        # The grace TTL is enforced via Redis EXPIRE on close (set to
        # `grace_seconds`), so the stream + meta keys self-evict. The
        # sweeper's only job is to keep the global + per-token indexes
        # tidy: scan, drop members whose meta key no longer exists,
        # emit ttl_expired audits.
        client = await self._get_client()
        try:
            members = await client.zrange(self._global_index_key(), 0, -1)
        except Exception:
            logger.exception(
                "[WebChatGateway] redis stream-buffer index scan failed"
            )
            return
        for stream_id in members:
            exists = await client.exists(self._meta_key(stream_id))
            if exists:
                continue
            # Meta key is gone → key naturally expired. Clean indexes
            # and audit. Catch the token from the index entry pattern;
            # we don't have the meta hash anymore, so we walk all
            # token indexes via the bookkeeping set.
            await client.zrem(self._global_index_key(), stream_id)
            try:
                # Best effort: scan token indexes to remove the orphaned
                # member. We don't know which token owned it now that
                # meta is gone, so fan out across known tokens. In
                # practice the index is rebuilt lazily on next create()
                # so a stale entry is harmless beyond a memory blip.
                cursor = 0
                pattern = self._token_index_key("*")
                while True:
                    cursor, keys = await client.scan(
                        cursor=cursor, match=pattern, count=100
                    )
                    for key in keys:
                        await client.zrem(key, stream_id)
                    if cursor == 0:
                        break
            except Exception:
                logger.exception(
                    "[WebChatGateway] redis stream-buffer index cleanup "
                    "for sid=%s failed",
                    stream_id,
                )
            if self._on_evict is not None:
                try:
                    await self._on_evict(stream_id, "ttl_expired")
                except Exception:
                    logger.exception(
                        "[WebChatGateway] redis stream-buffer evict audit "
                        "for sid=%s failed",
                        stream_id,
                    )

    # --- buffer API ---

    async def create(
        self, *, stream_id: str, token_name: str, session_id: str
    ) -> None:
        client = await self._get_client()
        now = time.time()
        # Cap enforcement before the create. Same skip-active rule as
        # the in-memory implementation: only CLOSED entries are
        # eligible for eviction. ZRANGE returns oldest-first; we walk
        # candidates and consult each meta hash for state, evicting
        # until either we have headroom or we run out of CLOSED
        # candidates (in which case we raise BufferFullError).
        per_token_evict: list[str] = []
        global_evict: list[str] = []
        token_idx = self._token_index_key(token_name)
        global_idx = self._global_index_key()
        try:
            owned = await client.zrange(token_idx, 0, -1)
        except Exception:
            logger.exception(
                "[WebChatGateway] redis stream-buffer per-token index "
                "scan failed"
            )
            owned = []
        owned_total = len(owned)
        for victim in owned:
            if owned_total < self._max_per_token:
                break
            try:
                victim_state = await client.hget(
                    self._meta_key(victim), _META_STATE
                )
            except Exception:
                logger.exception(
                    "[WebChatGateway] redis stream-buffer victim state "
                    "read failed sid=%s",
                    victim,
                )
                continue
            if victim_state not in _TERMINAL_STATES:
                continue
            await self._delete_keys(victim)
            per_token_evict.append(victim)
            owned_total -= 1
        try:
            global_count = await client.zcard(global_idx)
        except Exception:
            logger.exception(
                "[WebChatGateway] redis stream-buffer global card failed"
            )
            global_count = 0
        if global_count >= self._max_global:
            try:
                candidates = await client.zrange(global_idx, 0, -1)
            except Exception:
                logger.exception(
                    "[WebChatGateway] redis stream-buffer global drop "
                    "fetch failed"
                )
                candidates = []
            for victim in candidates:
                if global_count < self._max_global:
                    break
                try:
                    victim_state = await client.hget(
                        self._meta_key(victim), _META_STATE
                    )
                except Exception:
                    logger.exception(
                        "[WebChatGateway] redis stream-buffer global "
                        "victim state read failed sid=%s",
                        victim,
                    )
                    continue
                if victim_state not in _TERMINAL_STATES:
                    continue
                await self._delete_keys(victim)
                global_evict.append(victim)
                global_count -= 1
            if global_count >= self._max_global:
                raise BufferFullError(
                    f"global stream-buffer cap {self._max_global} reached "
                    "with all entries active; refusing to create new stream"
                )
        # Insert metadata + indexes.
        meta_key = self._meta_key(stream_id)
        try:
            pipe = client.pipeline(transaction=False)
            pipe.hset(
                meta_key,
                mapping={
                    _META_TOKEN: token_name,
                    _META_SESSION: session_id,
                    _META_STATE: "pending",
                    _META_STARTED: f"{now:.6f}",
                },
            )
            pipe.zadd(token_idx, {stream_id: now})
            pipe.zadd(global_idx, {stream_id: now})
            await pipe.execute()
        except Exception:
            logger.exception(
                "[WebChatGateway] redis stream-buffer create failed sid=%s",
                stream_id,
            )
            raise
        for sid in per_token_evict:
            await self._audit_evict(sid, "per_token_cap")
        for sid in global_evict:
            await self._audit_evict(sid, "global_cap")

    async def append_chunk(
        self, stream_id: str, seq: int, text: str
    ) -> None:
        client = await self._get_client()
        meta_key = self._meta_key(stream_id)
        try:
            state = await client.hget(meta_key, _META_STATE)
        except Exception:
            logger.exception(
                "[WebChatGateway] redis stream-buffer state read failed sid=%s",
                stream_id,
            )
            return
        if state is None:
            return
        if state in _TERMINAL_STATES:
            return
        try:
            pipe = client.pipeline(transaction=False)
            pipe.xadd(
                self._stream_key(stream_id),
                {_ENTRY_SEQ: str(seq), _ENTRY_TEXT: text},
            )
            if state != "streaming":
                pipe.hset(meta_key, _META_STATE, "streaming")
            await pipe.execute()
        except Exception:
            logger.exception(
                "[WebChatGateway] redis stream-buffer append failed sid=%s",
                stream_id,
            )

    async def close(
        self, stream_id: str, *, state: str, final: dict[str, Any]
    ) -> None:
        if state not in _TERMINAL_STATES:
            raise ValueError(
                f"close() requires a terminal state, got {state!r}"
            )
        import json

        client = await self._get_client()
        now = time.time()
        meta_key = self._meta_key(stream_id)
        stream_key = self._stream_key(stream_id)
        final_json = json.dumps(final, ensure_ascii=False, default=str)
        try:
            pipe = client.pipeline(transaction=False)
            pipe.hset(
                meta_key,
                mapping={
                    _META_STATE: state,
                    _META_CLOSED: f"{now:.6f}",
                    _META_FINAL: final_json,
                },
            )
            pipe.xadd(stream_key, {_ENTRY_FINAL: final_json})
            pipe.expire(meta_key, self._grace_seconds)
            pipe.expire(stream_key, self._grace_seconds)
            await pipe.execute()
        except Exception:
            logger.exception(
                "[WebChatGateway] redis stream-buffer close failed sid=%s",
                stream_id,
            )

    async def fetch_since(
        self, stream_id: str, *, after_seq: int
    ) -> StreamBufferEntry | None:
        import json

        client = await self._get_client()
        meta_key = self._meta_key(stream_id)
        stream_key = self._stream_key(stream_id)
        try:
            meta = await client.hgetall(meta_key)
        except Exception:
            logger.exception(
                "[WebChatGateway] redis stream-buffer meta read failed sid=%s",
                stream_id,
            )
            return None
        if not meta:
            return None
        try:
            entries = await client.xrange(stream_key, "-", "+")
        except Exception:
            logger.exception(
                "[WebChatGateway] redis stream-buffer xrange failed sid=%s",
                stream_id,
            )
            entries = []
        chunks: list[tuple[int, str]] = []
        for _entry_id, fields in entries:
            if _ENTRY_SEQ not in fields:
                continue
            try:
                seq = int(fields[_ENTRY_SEQ])
            except (TypeError, ValueError):
                continue
            if after_seq >= 0 and seq <= after_seq:
                continue
            chunks.append((seq, fields.get(_ENTRY_TEXT, "")))
        final_payload: dict[str, Any] | None = None
        raw_final = meta.get(_META_FINAL)
        if raw_final:
            try:
                parsed = json.loads(raw_final)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                final_payload = parsed
        try:
            started_at = float(meta.get(_META_STARTED) or 0.0)
        except (TypeError, ValueError):
            started_at = 0.0
        closed_raw = meta.get(_META_CLOSED)
        closed_at: float | None
        if closed_raw is None or closed_raw == "":
            closed_at = None
        else:
            try:
                closed_at = float(closed_raw)
            except (TypeError, ValueError):
                closed_at = None
        state_raw = meta.get(_META_STATE) or "pending"
        if state_raw not in (
            "pending",
            "streaming",
            "closed_ok",
            "closed_incomplete",
            "closed_failed",
        ):
            state_raw = "pending"
        return StreamBufferEntry(
            stream_id=stream_id,
            token_name=meta.get(_META_TOKEN, ""),
            session_id=meta.get(_META_SESSION, ""),
            started_at=started_at,
            state=state_raw,  # type: ignore[arg-type]
            chunks=chunks,
            final=final_payload,
            closed_at=closed_at,
        )

    async def evict(self, stream_id: str) -> None:
        await self._delete_keys(stream_id)
        await self._audit_evict(stream_id, "manual")

    async def _delete_keys(self, stream_id: str) -> None:
        client = await self._get_client()
        meta_key = self._meta_key(stream_id)
        stream_key = self._stream_key(stream_id)
        global_idx = self._global_index_key()
        try:
            token = await client.hget(meta_key, _META_TOKEN)
        except Exception:
            logger.exception(
                "[WebChatGateway] redis stream-buffer token read failed sid=%s",
                stream_id,
            )
            token = None
        try:
            pipe = client.pipeline(transaction=False)
            pipe.delete(meta_key)
            pipe.delete(stream_key)
            pipe.zrem(global_idx, stream_id)
            if token:
                pipe.zrem(self._token_index_key(token), stream_id)
            await pipe.execute()
        except Exception:
            logger.exception(
                "[WebChatGateway] redis stream-buffer delete failed sid=%s",
                stream_id,
            )

    async def _audit_evict(self, stream_id: str, reason: str) -> None:
        if self._on_evict is None:
            return
        try:
            await self._on_evict(stream_id, reason)
        except Exception:
            logger.exception(
                "[WebChatGateway] redis stream-buffer evict audit "
                "for sid=%s failed",
                stream_id,
            )

    async def list_active_for_token(
        self, token_name: str
    ) -> list[str]:
        client = await self._get_client()
        try:
            return list(
                await client.zrange(self._token_index_key(token_name), 0, -1)
            )
        except Exception:
            logger.exception(
                "[WebChatGateway] redis stream-buffer per-token list failed"
            )
            return []

    async def count(self) -> int:
        client = await self._get_client()
        try:
            return int(await client.zcard(self._global_index_key()))
        except Exception:
            logger.exception(
                "[WebChatGateway] redis stream-buffer count failed"
            )
            return 0

    async def iter_subscribe(
        self, stream_id: str, after_seq: int
    ) -> AsyncIterator[tuple[int, str]]:
        client = await self._get_client()
        stream_key = self._stream_key(stream_id)
        # First flush any chunks already in the stream past `after_seq`,
        # then block on XREAD for new ones. Track the last Redis stream
        # ID we've consumed so XREAD only returns truly new entries.
        #
        # XRANGE is INCLUSIVE on the start id, so on every iteration after
        # the first we'd re-yield the entry XREAD just emitted. Redis 6.2+
        # supports the exclusive-range syntax `(<id>`; we use it whenever
        # `last_id` advances past the initial `0-0` sentinel. This module
        # therefore requires Redis 6.2+ at runtime when the Redis backend
        # is enabled (in-memory backend is unaffected).
        last_id = "0-0"
        while True:
            xrange_start = "0-0" if last_id == "0-0" else f"({last_id}"
            try:
                entries = await client.xrange(stream_key, xrange_start, "+")
            except Exception:
                logger.exception(
                    "[WebChatGateway] redis stream-buffer subscribe xrange failed"
                )
                return
            if not entries:
                # Stream gone (close + expire happened) — done.
                meta_exists = await client.exists(self._meta_key(stream_id))
                if not meta_exists:
                    return
                break
            terminal_seen = False
            for entry_id, fields in entries:
                last_id = entry_id
                if _ENTRY_FINAL in fields:
                    terminal_seen = True
                    break
                if _ENTRY_SEQ not in fields:
                    continue
                try:
                    seq = int(fields[_ENTRY_SEQ])
                except (TypeError, ValueError):
                    continue
                if after_seq >= 0 and seq <= after_seq:
                    continue
                yield (seq, fields.get(_ENTRY_TEXT, ""))
            if terminal_seen:
                return
            # XREAD's `>` semantics already exclude `last_id`, so passing
            # the bare id here is correct. (No `(...)` prefix needed.)
            try:
                response = await client.xread(
                    {stream_key: last_id}, block=_REDIS_BLOCK_MS, count=100
                )
            except Exception:
                logger.exception(
                    "[WebChatGateway] redis stream-buffer xread failed"
                )
                return
            if not response:
                # Block timeout — loop and check for terminal state via
                # the meta hash, in case close happened with no extra
                # chunks (the close path adds a tombstone, so this is
                # mostly defensive against expire racing close).
                meta_state = await client.hget(
                    self._meta_key(stream_id), _META_STATE
                )
                if meta_state in _TERMINAL_STATES or meta_state is None:
                    return
                continue
            for _stream_name, stream_entries in response:
                for entry_id, fields in stream_entries:
                    last_id = entry_id
                    if _ENTRY_FINAL in fields:
                        return
                    if _ENTRY_SEQ not in fields:
                        continue
                    try:
                        seq = int(fields[_ENTRY_SEQ])
                    except (TypeError, ValueError):
                        continue
                    if after_seq >= 0 and seq <= after_seq:
                        continue
                    yield (seq, fields.get(_ENTRY_TEXT, ""))


__all__ = [
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_MAX_PER_TOKEN",
    "DEFAULT_MAX_GLOBAL",
    "BufferFullError",
    "EvictAuditCallable",
    "InMemoryBuffer",
    "RedisBuffer",
    "StreamBuffer",
    "StreamBufferEntry",
    "StreamState",
]
