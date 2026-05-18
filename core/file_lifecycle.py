"""Shared helpers for releasing webchat_files rows + storage objects together.

Three call sites currently need to delete an attachment file's storage
object AND its DB row as a pair:

    1. `StreamRegistry._release_attached_files` — fired on close_failed
       for a stream that had attachments but produced no content.
    2. `ConversationService.clear_history` — user wipes the chat; all
       attachment files for the session must be released too.
    3. `WebChatGatewayPlugin._chat_sync_prune_loop` — periodic cleanup
       of orphaned uploads + cascade from physically-pruned sessions.

The shared invariant: **storage objects are deleted BEFORE the DB rows.**
If we deleted the DB row first and then the storage delete crashed/raised,
the DB has no record pointing at the surviving R2 key — the object is a
permanent orphan no future prune sweep can find. By deleting storage
first and only removing the DB row on success, a mid-cleanup crash leaves
the DB row referencing a missing object: the next prune pass naturally
re-discovers it via the orphan / cascade lists and retries.

Best-effort throughout: per-row failures are logged but never re-raised.
The callers cannot afford to fail their operation (stream cleanup, clear
history, prune) on a flaky storage step.
"""

from __future__ import annotations

from typing import Any, Iterable

from astrbot.api import logger

from ..storage.base import FileRow
from .file_store import FileStore


async def release_files_safely(
    *,
    storage: Any,
    file_store: FileStore | None,
    rows: Iterable[FileRow],
    log_label: str = "release_files_safely",
) -> int:
    """Delete each row's storage object FIRST, then the DB rows of those
    whose storage delete succeeded. Returns the count of rows fully
    cleaned up (storage + DB both gone).

    ``file_store`` may be None in test scenarios — in that case we skip
    the storage step entirely and just delete the DB rows for the given
    file_ids.

    Per-row exceptions are caught + logged with ``log_label`` as a
    prefix so operators tailing the audit log can correlate failures
    back to the originating call site (stream registry vs. clear vs.
    prune). The caller should not retry on its own; the next prune
    iteration will re-discover any leftover rows.
    """
    rows_list = [r for r in rows if r is not None]
    if not rows_list:
        return 0
    if file_store is None:
        # Production paths always wire a FileStore (main.py constructs
        # one unconditionally). A None here means a test harness
        # forgot to pass one; refuse to delete DB rows rather than
        # silently bypass the "storage-first, DB-second" invariant
        # that protects against partial-failure orphan creation.
        raise RuntimeError(
            f"{log_label}: release_files_safely called without a "
            "FileStore — refusing to delete DB rows. Wire a FileStore "
            "(LocalFileStore is the simplest test choice) or skip the "
            "release in this test path."
        )
    storage_deleted_ids: list[str] = []
    for row in rows_list:
        try:
            await file_store.delete(storage_key=row.storage_key)
        except Exception:
            logger.exception(
                "[WebChatGateway] %s: file_store.delete failed key=%s",
                log_label,
                row.storage_key,
            )
            continue
        storage_deleted_ids.append(row.file_id)
    if storage_deleted_ids:
        try:
            await storage.delete_files_by_ids(storage_deleted_ids)
        except Exception:
            logger.exception(
                "[WebChatGateway] %s: delete_files_by_ids failed ids=%d",
                log_label,
                len(storage_deleted_ids),
            )
            return 0
    return len(storage_deleted_ids)


__all__ = ["release_files_safely"]
