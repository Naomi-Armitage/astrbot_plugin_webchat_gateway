"""Regression tests for Drop retention cleanup."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio


class _StubCM:
    async def get_curr_conversation_id(self, umo: str) -> str | None:
        del umo
        return None

    async def update_conversation(
        self, *, unified_msg_origin: str, conversation_id: str, history: list
    ) -> None:
        del unified_msg_origin, conversation_id, history


class _RecordingFileStore:
    def __init__(self, storage, *, token_name: str, result: bool) -> None:
        self.storage = storage
        self.token_name = token_name
        self.result = result
        self.delete_calls: list[str] = []
        self.reference_counts_at_delete: list[int] = []

    async def delete(self, *, storage_key: str) -> bool:
        self.delete_calls.append(storage_key)
        self.reference_counts_at_delete.append(
            await self.storage.count_drop_file_references(
                token_name=self.token_name, file_id="shared-file"
            )
        )
        return self.result


@pytest_asyncio.fixture
async def storage(tmp_path: Path):
    from astrbot_plugin_webchat_gateway.storage.sqlite_backend import (
        SqliteStorage,
    )

    value = SqliteStorage(str(tmp_path / "retention.db"))
    await value.initialize()
    yield value
    await value.close()


async def _add_file_message(storage, *, message_now: int, file_id: str) -> int:
    return await storage.append_drop_message(
        token_name="alice",
        device_id="device",
        device_name="Desktop",
        kind="file",
        text="",
        file_id=file_id,
        filename="shared.txt",
        mime="text/plain",
        size_bytes=1,
        now=message_now,
    )


@pytest.mark.asyncio
async def test_drop_retention_keeps_shared_file_until_last_message(
    storage,
) -> None:
    from astrbot_plugin_webchat_gateway.core.prune_orchestrator import (
        PruneOrchestrator,
        PruneRetentionConfig,
    )

    await storage.insert_file(
        file_id="shared-file",
        token_name="alice",
        session_id="drop",
        mime="text/plain",
        size_bytes=1,
        storage_key="alice/shared.txt",
        now=1,
        filename="shared.txt",
    )
    first = await _add_file_message(storage, message_now=10, file_id="shared-file")
    second = await _add_file_message(storage, message_now=11, file_id="shared-file")
    await storage.soft_delete_drop_message(
        token_name="alice", message_id=first, now=100
    )
    await storage.soft_delete_drop_message(
        token_name="alice", message_id=second, now=100
    )

    file_store = _RecordingFileStore(storage, token_name="alice", result=True)
    orchestrator = PruneOrchestrator(
        storage=storage,
        file_store=file_store,
        cm=_StubCM(),
        config=PruneRetentionConfig(drop_deleted_retention_seconds=100),
    )
    removed, files_deleted = await orchestrator._run_drop_retention(now=1000)

    assert (removed, files_deleted) == (2, 1)
    assert file_store.delete_calls == ["alice/shared.txt"]
    # The first message is removed while another message still references
    # the file; the storage object is released only for the final reference.
    assert file_store.reference_counts_at_delete == [1]
    assert await storage.get_file("shared-file") is None
    assert await storage.list_drop_messages(
        token_name="alice",
        limit=10,
        before_id=None,
        include_deleted=True,
    ) == []


@pytest.mark.asyncio
async def test_drop_retention_keeps_message_when_file_release_fails(
    storage,
) -> None:
    from astrbot_plugin_webchat_gateway.core.prune_orchestrator import (
        PruneOrchestrator,
        PruneRetentionConfig,
    )

    await storage.insert_file(
        file_id="shared-file",
        token_name="alice",
        session_id="drop",
        mime="text/plain",
        size_bytes=1,
        storage_key="alice/shared.txt",
        now=1,
        filename="shared.txt",
    )
    message_id = await _add_file_message(
        storage, message_now=10, file_id="shared-file"
    )
    await storage.soft_delete_drop_message(
        token_name="alice", message_id=message_id, now=100
    )

    file_store = _RecordingFileStore(storage, token_name="alice", result=False)
    orchestrator = PruneOrchestrator(
        storage=storage,
        file_store=file_store,
        cm=_StubCM(),
        config=PruneRetentionConfig(drop_deleted_retention_seconds=100),
    )
    removed, files_deleted = await orchestrator._run_drop_retention(now=1000)

    assert (removed, files_deleted) == (0, 0)
    assert file_store.delete_calls == ["alice/shared.txt"]
    row = await storage.get_drop_message(
        token_name="alice", message_id=message_id
    )
    assert row is not None
    assert row.deleted_at == 100
    assert await storage.get_file("shared-file") is not None
