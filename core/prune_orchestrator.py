"""Background retention/orphan cleanup orchestrator for the gateway.

Originally lived as `WebChatGatewayPlugin._chat_sync_prune_loop`
(185 lines) in main.py — extracted so the plugin entry class stays
under ~600 lines and so the orchestration can be exercised in
isolation (the dependencies are now explicit constructor args, not
`self._XXX` reaches).

`run_iteration` performs one sweep. The caller (`main.py`) owns the
loop + sleep + boot-delay so the lifecycle bits live next to the
plugin's `_start`/`_stop`. Each sweep:

    1. List candidate file rows (orphans + cascade) — read-only.
    2. List sessions about to be physically pruned — read-only.
    3. Clear AstrBot CM history for each candidate session. Collect
       (token, session) pairs whose CM clear raised into `cm_failed`.
    4. Filter the file candidates to exclude any whose (token, session)
       is in `cm_failed`. Symmetry guarantee: a CM-clear failure
       protects BOTH session_meta AND its cascade files this
       iteration, so an operator inspecting a stalled retry sees a
       consistent snapshot rather than "files gone but meta retained"
       which would be harder to diagnose.
    5. For each filtered file: `file_store.delete(storage_key)`, then
       `delete_files_by_ids()` for the ones whose storage delete
       succeeded (storage-first, DB-second). See `release_files_safely`.
    6. `storage.prune_chat_sync(exclude_sessions=cm_failed)` — drops
       old events + soft-deleted session_meta. The session_meta DELETE
       has both a `NOT EXISTS(file)` guard and a `NOT (token=? AND
       session=?)` chain for the explicit cm_failed exclusions.
    7. In-memory housekeeping for unbounded caches: cookie-logout
       tracker, IP-failures table, EventBus condvars, R2 per-key
       locks. Each is best-effort and a failure here is logged but
       doesn't block the next iteration. Critically, step 7 ALWAYS
       runs even if steps 1-6 raised — otherwise a transient DB
       hiccup would let the unbounded caches grow for the full
       interval.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from astrbot.api import logger

from .file_lifecycle import release_files_safely

if TYPE_CHECKING:
    from ..storage.base import AbstractStorage
    from .cookie_logout import CookieLogoutTracker
    from .event_bus import EventBus
    from .file_store import FileStore


class _CMLike(Protocol):
    """Subset of AstrBot ConversationManager we actually use here."""

    async def get_curr_conversation_id(self, umo: str) -> str | None: ...

    async def update_conversation(
        self,
        *,
        unified_msg_origin: str,
        conversation_id: str,
        history: list[Any],
    ) -> None: ...


@dataclass(frozen=True)
class PruneRetentionConfig:
    """Retention cutoffs the orchestrator applies on each sweep.

    Cadence and boot-delay are intentionally NOT here — the caller
    (`main.py`) owns the loop lifecycle so the `_stop` teardown can
    cancel cleanly without going through the orchestrator.
    """

    events_retention_seconds: int = 14 * 86400
    deleted_meta_retention_seconds: int = 90 * 86400
    upload_orphan_retention_seconds: int = 3600
    ip_failures_retention_seconds: int = 24 * 3600


class PruneOrchestrator:
    """Periodic retention + orphan cleanup with bounded-cache housekeeping."""

    def __init__(
        self,
        *,
        storage: AbstractStorage,
        file_store: FileStore | None,
        cm: _CMLike,
        config: PruneRetentionConfig,
        cookie_logout_tracker: CookieLogoutTracker | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._storage = storage
        self._file_store = file_store
        self._cm = cm
        self._cfg = config
        self._cookie_logout_tracker = cookie_logout_tracker
        self._event_bus = event_bus

    async def run_iteration(self) -> None:
        """Single sweep. Public for testability.

        Data prune (steps 1-6) and bounded-cache housekeeping
        (step 7) are scoped separately. A failure in steps 1-6 is
        logged but does NOT skip step 7: the housekeeping bounds
        ip_failures / EventBus / cookie tracker / R2 key-locks
        regardless of whether the data prune succeeded, so a
        transient DB error can't leave the unbounded caches growing
        for the full interval.
        """
        now = int(time.time())
        try:
            await self._run_data_prune(now)
        except Exception:
            logger.exception(
                "[WebChatGateway] prune data-sweep raised; continuing to housekeeping"
            )
        await self._run_housekeeping(now)

    async def _run_data_prune(self, now: int) -> None:
        """Steps 1-6 — list / CM clear / filter / file delete / DB DELETE."""
        storage = self._storage
        file_store = self._file_store

        # Step 1: list candidate file rows (read-only).
        files_to_delete = await storage.list_files_to_prune(
            deleted_meta_before_ts=now - self._cfg.deleted_meta_retention_seconds,
            uncommitted_files_before_ts=now - self._cfg.upload_orphan_retention_seconds,
        )

        # Step 2: list sessions about to be pruned (read-only).
        sessions_to_purge = await storage.list_sessions_to_purge(
            deleted_before_ts=now - self._cfg.deleted_meta_retention_seconds,
        )

        # Step 3: CM clear, collect failures. The CM call runs BEFORE
        # file deletion so a failure can protect the session's cascade
        # files in step 4 — without this ordering the files would be
        # gone before we knew CM was stuck.
        sessions_purged = 0
        cm_failed: list[tuple[str, str]] = []
        for token_name, session_id in sessions_to_purge:
            umo = f"webchat_gateway:{token_name}:{session_id}"
            try:
                cid = await self._cm.get_curr_conversation_id(umo)
                if cid:
                    await self._cm.update_conversation(
                        unified_msg_origin=umo,
                        conversation_id=cid,
                        history=[],
                    )
                # "Purged" counts both "cleared CM content" and "no CM
                # conversation to clear" — both are successful outcomes.
                # Only outright exceptions count as failures.
                sessions_purged += 1
            except Exception:
                logger.exception(
                    "[WebChatGateway] CM clear during prune failed "
                    "token=%s session=%s",
                    token_name,
                    session_id,
                )
                cm_failed.append((token_name, session_id))

        # Step 4: filter files by cm_failed sessions. Symmetric
        # protection: a session whose CM clear failed retains its meta
        # (via exclude_sessions in step 6) AND its cascade files (via
        # this filter). Orphans without a session_meta row are
        # unaffected — they're never in cm_failed (the source is
        # list_sessions_to_purge).
        cm_failed_set = set(cm_failed)
        if cm_failed_set:
            filtered_files = [
                r
                for r in files_to_delete
                if (r.token_name, r.session_id) not in cm_failed_set
            ]
        else:
            filtered_files = files_to_delete
        files_protected = len(files_to_delete) - len(filtered_files)

        # Step 5: storage delete first, then DB rows of those that
        # succeeded. Failed-storage-delete rows survive and are
        # re-discovered next iter.
        files_deleted = 0
        if file_store is not None and filtered_files:
            files_deleted = await release_files_safely(
                storage=storage,
                file_store=file_store,
                rows=filtered_files,
                log_label="prune_loop",
            )

        # Step 6: events + session_meta. session_meta DELETE uses
        # `NOT EXISTS(file)` so any cascade file whose storage delete
        # failed in step 5 keeps its session_meta around for next iter
        # to retry. The `exclude_sessions` list adds the second retry
        # mechanism for CM-clear failures.
        events_pruned, meta_pruned = await storage.prune_chat_sync(
            events_before_ts=now - self._cfg.events_retention_seconds,
            deleted_meta_before_ts=now - self._cfg.deleted_meta_retention_seconds,
            exclude_sessions=cm_failed or None,
        )
        if events_pruned or meta_pruned or files_to_delete or sessions_purged or cm_failed:
            logger.info(
                "[WebChatGateway] chat-sync prune: events=%d meta=%d "
                "files=%d/%d cm_cleared=%d cm_failed=%d files_protected=%d",
                events_pruned,
                meta_pruned,
                files_deleted,
                len(filtered_files),
                sessions_purged,
                len(cm_failed),
                files_protected,
            )

    async def _run_housekeeping(self, now: int) -> None:
        """Step 7 — bounded-cache housekeeping.

        Always runs from `run_iteration`, even when the data prune
        raised. Each individual prune is best-effort and isolated
        so one failing cache doesn't starve the others.
        """
        storage = self._storage
        file_store = self._file_store
        if self._cookie_logout_tracker is not None:
            try:
                self._cookie_logout_tracker.prune_expired()
            except Exception:
                logger.exception(
                    "[WebChatGateway] cookie_logout_tracker prune failed"
                )
        try:
            await storage.prune_ip_failures(
                before_ts=now - self._cfg.ip_failures_retention_seconds,
            )
        except Exception:
            logger.exception("[WebChatGateway] prune_ip_failures failed")
        if self._event_bus is not None:
            try:
                await self._event_bus.prune_idle()
            except Exception:
                logger.exception("[WebChatGateway] event_bus prune_idle failed")
        if file_store is not None and hasattr(file_store, "prune_idle_key_locks"):
            try:
                await file_store.prune_idle_key_locks()
            except Exception:
                logger.exception(
                    "[WebChatGateway] file_store key-lock prune failed"
                )


__all__ = ["PruneOrchestrator", "PruneRetentionConfig"]
