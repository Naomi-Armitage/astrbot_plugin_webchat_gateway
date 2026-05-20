"""Storage abstract base class and row dataclasses."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date


# Sentinel for partial updates where None is a meaningful value (e.g.
# clearing tokens.expires_at to NULL). `UNSET` means "leave the column
# alone"; `None` means "set to NULL". Implementations check identity.
#
# `_UNSET` is kept as a deprecated alias for one release so external
# callers (the handler layer's payload helpers + the older AstrBot
# integrations that import directly from `storage.base`) don't break.
# Prefer the public `UNSET` going forward — `_UNSET` will be removed
# in the next minor.
class _Sentinel:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return "UNSET"


UNSET: _Sentinel = _Sentinel()
_UNSET: _Sentinel = UNSET  # deprecated alias — prefer `UNSET`


# Belt-and-braces cap on `audit_log.detail`. `core/audit.py` already
# truncates to this length before calling write_audit, but the storage
# layer enforces the same bound so any future caller that bypasses
# AuditLogger (a test, a one-off admin tool, a refactor) can't write
# a 1 MB row into SQLite (unbounded TEXT) or hit MySQL's TEXT 64KB
# cap with a transient client-side surprise. Constant lives here so
# both backends agree on the same number.
AUDIT_DETAIL_MAX = 1024


@dataclass(frozen=True)
class TokenRow:
    name: str
    token_hash: str
    daily_quota: int
    note: str
    created_at: int
    revoked_at: int | None
    expires_at: int | None


@dataclass(frozen=True)
class UsageRow:
    name: str
    day: date
    count: int


@dataclass(frozen=True)
class AuditRow:
    id: int
    ts: int
    name: str | None
    ip: str | None
    event: str
    detail: str


@dataclass(frozen=True)
class SessionMetaRow:
    token_name: str
    session_id: str
    title: str
    title_manual: bool
    pinned_at: int | None
    deleted_at: int | None
    updated_at: int
    message_count: int
    preview: str


@dataclass(frozen=True)
class UpdateRow:
    """A row from `webchat_updates`. `payload` is opaque JSON; the service
    layer is responsible for serialize/deserialize."""

    token_name: str
    pts: int
    ts: int
    event_type: str
    session_id: str
    payload: str


@dataclass(frozen=True)
class NewEvent:
    """Input shape for `append_updates` — pts is assigned by storage."""

    event_type: str
    session_id: str
    payload: str


@dataclass(frozen=True)
class FileRow:
    """A row from `webchat_files`.

    `committed=True` means the file was attached to a sent message and is
    safe from the orphan GC; `committed=False` means the file was uploaded
    but never tied to a chat-stream call (likely abandoned). `committed_at`
    captures the first-commit timestamp and is left untouched on
    idempotent re-commits.

    `storage_key` is opaque to the storage layer — Local uses a relative
    path under the configured root, R2 uses an object key. The `FileStore`
    Protocol is the only thing that interprets it.
    """

    file_id: str
    token_name: str
    session_id: str
    mime: str
    size_bytes: int
    storage_key: str
    committed: bool
    uploaded_at: int
    committed_at: int | None


class AbstractStorage(ABC):
    """Pluggable storage interface for tokens, usage, IP failures, and audit."""

    @abstractmethod
    async def initialize(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    # ----- tokens -----
    @abstractmethod
    async def create_token(
        self,
        *,
        name: str,
        token_hash: str,
        daily_quota: int,
        note: str,
        now: int,
        expires_at: int | None = None,
    ) -> None: ...

    @abstractmethod
    async def get_token_by_hash(self, token_hash: str) -> TokenRow | None: ...

    @abstractmethod
    async def get_token_by_name(self, name: str) -> TokenRow | None: ...

    @abstractmethod
    async def revoke_token(self, name: str, *, now: int) -> bool: ...

    @abstractmethod
    async def list_tokens(self, *, include_revoked: bool = False) -> list[TokenRow]: ...

    @abstractmethod
    async def update_token(
        self,
        name: str,
        *,
        daily_quota: int | None = None,
        note: str | None = None,
        expires_at: int | None | _Sentinel = _UNSET,
    ) -> bool:
        """Partial UPDATE on tokens. Returns True if a row matched.

        `expires_at` uses a sentinel because None is a meaningful value
        (clear expiry). Pass `_UNSET` to leave the column alone.
        """

    @abstractmethod
    async def set_token_revoked(
        self, name: str, *, revoked: bool, now: int
    ) -> bool:
        """Set or clear `revoked_at`. Returns True if a row matched."""

    @abstractmethod
    async def regenerate_token(self, name: str, new_token_hash: str) -> bool:
        """Rotate `token_hash` for an existing token. Returns True if matched.

        Does NOT touch `revoked_at`, `daily_quota`, `note`, `expires_at`,
        or `daily_usage`.
        """

    @abstractmethod
    async def rename_token(self, old_name: str, new_name: str) -> bool:
        """Rename a token, cascading to `daily_usage` + `audit_log`.

        Atomic (single transaction). Returns False if `old_name` is missing
        or `new_name` already exists. Caller must validate names beforehand.
        """

    # ----- daily usage -----
    @abstractmethod
    async def increment_daily_usage(self, name: str, *, day: date) -> int:
        """Atomically +1 today's counter and return the new value."""

    @abstractmethod
    async def get_today_usage(self, name: str, *, day: date) -> int: ...

    @abstractmethod
    async def get_today_usage_bulk(
        self, names: list[str], *, day: date
    ) -> dict[str, int]:
        """Fetch today's usage for many names in one query. Missing names map to 0."""

    @abstractmethod
    async def get_usage_stats(self, name: str, *, days: int) -> list[UsageRow]: ...

    # ----- ip brute-force -----
    @abstractmethod
    async def record_ip_failure(
        self, ip: str, *, now: int, max_fails: int, block_seconds: int
    ) -> int:
        """Increment and return the new fail count; sets blocked_until when threshold crossed."""

    @abstractmethod
    async def is_ip_blocked(self, ip: str, *, now: int) -> tuple[bool, int]:
        """Return (blocked, retry_after_seconds)."""

    @abstractmethod
    async def reset_ip_failures(self, ip: str) -> None: ...

    @abstractmethod
    async def decrement_ip_failure(self, ip: str) -> int:
        """Atomically pay back ONE failure to `ip`.

        Used on successful auth. Returns the post-decrement count (>=0).
        When the count would reach zero, the row is removed AND
        `blocked_until` is cleared — there's no point keeping a
        zero-counter row around. If no row exists, returns 0.

        This replaces the prior "reset to 0 on success" behaviour: an
        attacker with one valid token on the same IP could otherwise
        zero out the counter at will, defeating brute-force accounting
        for any OTHER token on that IP. Decrement-by-one preserves
        typo-recovery for legit users while capping attacker leverage
        at exactly one probe credit per legitimate auth.
        """

    @abstractmethod
    async def prune_ip_failures(self, *, before_ts: int) -> int:
        """Delete stale ip_failures rows whose `last_fail_ts < before_ts`.

        Without this, the table grows unbounded with attacker-controlled
        cardinality — every distinct probe IP leaves a row that never
        gets reset (the `reset_ip_failures` path only fires on
        successful auth from the SAME ip). Returns the row count
        actually deleted.
        """

    # ----- audit -----
    @abstractmethod
    async def write_audit(
        self,
        *,
        ts: int,
        name: str | None,
        ip: str | None,
        event: str,
        detail: str,
    ) -> None: ...

    @abstractmethod
    async def get_recent_audit(self, *, limit: int) -> list[AuditRow]: ...

    @abstractmethod
    async def list_audit(
        self,
        *,
        limit: int,
        offset: int = 0,
        event: str | None = None,
        name: str | None = None,
        ip: str | None = None,
        ts_from: int | None = None,
        ts_to: int | None = None,
    ) -> tuple[list[AuditRow], int]:
        """Filterable, paginated audit log read.

        Returns `(rows, total)`. `total` is the count matching the same
        filter (without limit/offset) so the UI can compute page count.
        Rows ordered by `ts DESC, id DESC`. `limit` clamped to 1..1000;
        `offset` clamped to ``>=0``. String filters use case-insensitive
        substring match; pass empty / None to skip.
        """

    @abstractmethod
    async def prune_audit(self, *, before_ts: int) -> int:
        """Delete audit rows whose `ts < before_ts`. Returns rows deleted.

        Without this, `audit_log` grows unbounded — every chat/admin
        request emits at least one row. The plugin runs this from the
        retention orchestrator on the configured cadence.
        """

    # ----- chat sync (v3) -----
    @abstractmethod
    async def upsert_session_meta(
        self,
        *,
        token_name: str,
        session_id: str,
        title: str | None = None,
        title_manual: bool | None = None,
        pinned_at: int | None | _Sentinel = _UNSET,
        deleted_at: int | None | _Sentinel = _UNSET,
        message_count: int | None = None,
        preview: str | None = None,
        now: int,
    ) -> SessionMetaRow:
        """UPSERT on (token_name, session_id), always writing updated_at=now.

        `title` / `title_manual` / `message_count` / `preview`: None means
        "leave alone".
        `pinned_at` / `deleted_at`: `_UNSET` means "leave alone";
        `None` means "set to NULL".

        Returns the post-write row.
        """

    @abstractmethod
    async def get_session_meta(
        self, *, token_name: str, session_id: str
    ) -> SessionMetaRow | None: ...

    @abstractmethod
    async def list_session_meta(
        self, *, token_name: str, include_deleted: bool = False
    ) -> list[SessionMetaRow]: ...

    @abstractmethod
    async def append_updates(
        self,
        *,
        token_name: str,
        events: list[NewEvent],
        now: int,
    ) -> list[int]:
        """Atomically allocate pts and INSERT all events under one write
        transaction so peers see them as a contiguous block. Returns the
        assigned pts in input order. Empty `events` is a no-op returning
        an empty list.
        """

    @abstractmethod
    async def get_updates(
        self,
        *,
        token_name: str,
        since_pts: int,
        limit: int,
    ) -> list[UpdateRow]:
        """Return rows where pts > since_pts, ordered by pts ASC, capped
        at `limit`. Caller paginates via the last returned pts.

        Filters out internal `_pruned_marker` rows; clients only see real
        events. Markers are written by `prune_chat_sync` to anchor
        MAX(pts) for tokens whose entire event history has aged past the
        retention window — they preserve the high-water mark without
        leaking real chat content.
        """

    @abstractmethod
    async def get_max_pts(self, *, token_name: str) -> int:
        """Return the largest pts for this token (0 if none).

        Used by the long-poll path to detect `tooFar` and to expose the
        current high-water mark in list responses. INCLUDES
        `_pruned_marker` rows by design: a marker IS the high-water
        sentinel that `prune_chat_sync` left behind, so excluding it
        would defeat the wrap-prevention contract.
        """

    @abstractmethod
    async def prune_chat_sync(
        self,
        *,
        events_before_ts: int,
        deleted_meta_before_ts: int,
        exclude_sessions: list[tuple[str, str]] | None = None,
    ) -> tuple[int, int]:
        """Delete `webchat_updates` rows older than `events_before_ts` AND
        `webchat_session_meta` rows whose `deleted_at` is older than
        `deleted_meta_before_ts`. Returns `(events_pruned, meta_pruned)`.

        **Does NOT delete webchat_files rows.** Callers must orchestrate
        file cleanup separately via `list_files_to_prune` →
        `file_store.delete()` per row → `delete_files_by_ids()` for those
        whose storage delete succeeded. This split exists because the
        storage object must be removed BEFORE its DB row — otherwise a
        mid-cleanup crash leaves an R2/disk object with no DB anchor
        for any future prune sweep to find. The DB-first ordering
        leaked storage objects permanently.

        Session-meta DELETE is guarded by a `NOT EXISTS (file)` check:
        if any `webchat_files` row still references this (token,
        session) — typically because a previous prune iteration failed
        to delete the storage object and left the DB row in place —
        the session_meta row is retained for the next iteration to
        retry. This preserves the cascade query's ability to
        re-discover the file (via session_meta JOIN) on subsequent
        prune passes.

        `exclude_sessions` (optional): list of `(token_name, session_id)`
        tuples that must NOT have their session_meta deleted this
        iteration. Used by the prune-loop caller to skip sessions
        whose AstrBot CM history clear failed — without skipping them,
        session_meta would be deleted while CM still holds stale
        `ImageURLPart` references, and a user re-using the same
        session_id would later see the old context / broken images
        with no way to retry the cleanup. Excluded sessions are
        retained for the next prune iteration's retry. Empty / None
        means no exclusions (the default). Implementations are
        expected to inline the exclusion as a portable
        `AND NOT (token_name = ? AND session_id = ?)` chain rather
        than relying on composite-key IN syntax that differs across
        SQLite versions.

        **Symmetric protection contract** — `exclude_sessions` is
        coupled with caller-side file-list filtering. This method
        protects only the session_meta DELETE; the caller (prune
        loop) is expected to filter its file-delete candidate list
        by the same `cm_failed` set BEFORE invoking `file_store.delete`
        / `delete_files_by_ids`. The two halves together keep an
        excluded session's state internally consistent (cascade files
        AND meta both retained until the retry succeeds). Without the
        file-side filter, a CM-clear failure would leave files deleted
        but meta retained, which is harder to diagnose and recover
        from than "nothing happened, retry next time".

        Idempotent and safe to run while the gateway is live; events
        older than the cutoff have no remaining waiters that could
        observe them.

        Always retains the latest event per token regardless of age, to
        keep MAX(pts) monotonic. A prune that empties a token's row
        completely would otherwise reset MAX(pts) to 0; new events would
        restart at pts=1 and a client whose `since` ran ahead of pts=1
        but lagged behind the new MAX would silently miss the new
        events (the `since > current_max` tooFar check only catches the
        opposite case). Keeping one row floors MAX(pts) at the highest
        seen value forever.

        Replaces retained rows that are themselves past the cutoff with
        a content-free `_pruned_marker` event so MAX(pts) stays
        monotonic without keeping stale chat content past the retention
        window. Markers carry `payload='{}'` and `event_type='_pruned_marker'`;
        `get_updates` filters them out so clients never see them as
        real events. Idempotent: rows already marked are skipped on
        subsequent prune passes via an `event_type != '_pruned_marker'`
        guard in the UPDATE.
        """

    @abstractmethod
    async def list_files_to_prune(
        self,
        *,
        deleted_meta_before_ts: int,
        uncommitted_files_before_ts: int,
        limit: int = 500,
    ) -> list[FileRow]:
        """READ-ONLY. Return file rows that should be physically deleted.

        Two sources, deduplicated by file_id:

        1. **Orphans**: `committed=0` rows with `uploaded_at <
           uncommitted_files_before_ts` — uploaded but never attached
           to a sent message (likely abandoned by a closed tab).
        2. **Cascade**: rows whose `(token_name, session_id)` matches a
           `webchat_session_meta` row about to be physically pruned
           (`deleted_at < deleted_meta_before_ts`).

        Capped at `limit` rows per source — pathological backlogs drain
        over multiple prune cycles rather than all at once. Bound keeps
        memory predictable.

        Caller is responsible for orchestrating the cleanup:

            rows = await storage.list_files_to_prune(...)
            ok_ids = []
            for r in rows:
                try:
                    await file_store.delete(storage_key=r.storage_key)
                    ok_ids.append(r.file_id)
                except Exception:
                    logger.exception(...)
            if ok_ids:
                await storage.delete_files_by_ids(ok_ids)
            await storage.prune_chat_sync(...)

        Files whose storage delete fails retain their DB row, and the
        next prune iteration re-discovers them via the same query.
        """

    @abstractmethod
    async def list_sessions_to_purge(
        self,
        *,
        deleted_before_ts: int,
        limit: int = 500,
    ) -> list[tuple[str, str]]:
        """READ-ONLY. Return `(token_name, session_id)` tuples for
        session_meta rows that `prune_chat_sync` is about to physically
        delete.

        Used by the prune loop to clear the corresponding AstrBot
        ConversationManager history (so dangling `ImageURLPart`
        segments don't survive past the file's lifecycle and produce
        broken `<img>` references if the user later re-uses the same
        session_id). The actual cm.update_conversation call lives in
        the plugin's prune loop — this method just surfaces the list.
        """

    # ----- file uploads (v5) -----
    @abstractmethod
    async def insert_file(
        self,
        *,
        file_id: str,
        token_name: str,
        session_id: str,
        mime: str,
        size_bytes: int,
        storage_key: str,
        now: int,
    ) -> None:
        """Insert a `webchat_files` row with `committed=0` and
        `uploaded_at=now`. Caller has already validated all fields and
        generated `file_id`; this is a straight INSERT — duplicate
        `file_id` raises (which would indicate a token_urlsafe
        collision, vanishingly unlikely given 96 bits of entropy).
        """

    @abstractmethod
    async def get_file(self, file_id: str) -> FileRow | None:
        """Look up a single file row by PK. Returns None if missing.

        Caller is responsible for the ownership check
        (`row.token_name == bearer.name`) — this method is a raw read
        and does not enforce auth.
        """

    @abstractmethod
    async def mark_files_committed(
        self, file_ids: list[str], *, now: int
    ) -> int:
        """Flip `committed=1` and set `committed_at=now` on the listed
        file_ids. Returns the number of rows actually updated.

        Idempotent: re-marking an already-committed row is a no-op
        (the existing `committed_at` is preserved — the first commit
        wins). Empty `file_ids` returns 0 without touching the DB.
        """

    @abstractmethod
    async def total_committed_size_for_token(self, token_name: str) -> int:
        """Sum `size_bytes` over committed files owned by this token.

        Use sparingly — `total_size_for_token` is what the upload quota
        check uses to defend against the spam-upload-then-never-commit
        DoS pattern. This committed-only variant is retained for
        diagnostic / audit purposes.
        """

    @abstractmethod
    async def total_size_for_token(self, token_name: str) -> int:
        """Sum `size_bytes` over ALL files owned by this token, both
        committed and uncommitted.

        Used by the upload quota check. Counting uncommitted is the
        defence against the "upload many, never send" abuse pattern —
        without it an attacker can write `cap × (orphan_gc_cadence /
        upload_rate)` bytes to disk/R2 before the orphan sweeper kicks
        in. With it, total per-token storage is hard-capped at
        `per_token_storage_mb` regardless of upload-vs-commit ratio.
        """

    @abstractmethod
    async def list_files_for_session(
        self, *, token_name: str, session_id: str
    ) -> list[FileRow]:
        """All committed and uncommitted file rows for one session.

        Used by `get_conversation` to backfill `attachments` onto
        message replies. Order is unspecified — callers that need a
        stable order should sort by `uploaded_at` themselves.
        """

    @abstractmethod
    async def list_uncommitted_orphans(
        self, *, older_than_ts: int, limit: int = 500
    ) -> list[FileRow]:
        """Find `committed=0` rows older than `older_than_ts`.

        Capped at `limit` to keep prune passes bounded; the daily prune
        loop iterates this until empty. Used by the orphan GC in
        `prune_chat_sync` but also exposed as a standalone primitive
        so admin tooling can inspect or trigger cleanup out-of-band.
        """

    @abstractmethod
    async def list_files_for_purged_sessions(
        self, *, deleted_before_ts: int, limit: int = 500
    ) -> list[FileRow]:
        """Files belonging to sessions whose `webchat_session_meta`
        row has `deleted_at < deleted_before_ts` (i.e. about to be
        physically pruned by the cascade in `prune_chat_sync`).

        Returns at most `limit` rows. Used internally by
        `prune_chat_sync` to assemble the list of files to remove
        from the FileStore.
        """

    @abstractmethod
    async def delete_files_by_ids(self, file_ids: list[str]) -> int:
        """Hard DELETE the listed file rows from `webchat_files`.

        Returns the number of rows actually deleted. Empty `file_ids`
        returns 0 without touching the DB. Caller is responsible for
        ensuring no concurrent reader still needs the rows — typical
        use is from the prune path, where the rows are already past
        retention.
        """
