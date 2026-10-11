"""Versioned, text-only conversation backup format and import bounds."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .admin_tokens import ServiceError


TRANSFER_FORMAT = "webchat-gateway-conversations"
TRANSFER_VERSION = 1
MAX_TRANSFER_BYTES = 20 * 1024 * 1024
MAX_TRANSFER_CONVERSATIONS = 500
MAX_TRANSFER_MESSAGES = 20_000
MAX_TRANSFER_MESSAGE_CHARS = 100_000
ATTACHMENT_PLACEHOLDER = "[附件未包含在备份中]"


@dataclass(frozen=True)
class ImportedConversation:
    title: str
    title_manual: bool
    pinned: bool
    messages: tuple[tuple[str, str], ...]


def validate_import_document(value: Any) -> list[ImportedConversation]:
    """Validate the whole file before any CM or metadata writes begin."""
    if not isinstance(value, dict) or value.get("format") != TRANSFER_FORMAT:
        raise ServiceError("invalid_import", status=400)
    if type(value.get("version")) is not int or value["version"] != TRANSFER_VERSION:
        raise ServiceError("unsupported_backup_version", status=400)
    rows = value.get("conversations")
    if not isinstance(rows, list) or len(rows) > MAX_TRANSFER_CONVERSATIONS:
        raise ServiceError("invalid_import", status=400)

    imported: list[ImportedConversation] = []
    total_messages = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ServiceError("invalid_import", status=400)
        title = row.get("title")
        title_manual = row.get("title_manual")
        pinned = row.get("pinned")
        messages = row.get("messages")
        if (
            not isinstance(title, str)
            or len(title) > 255
            or not isinstance(title_manual, bool)
            or not isinstance(pinned, bool)
            or not isinstance(messages, list)
        ):
            raise ServiceError("invalid_import", status=400)
        total_messages += len(messages)
        if total_messages > MAX_TRANSFER_MESSAGES:
            raise ServiceError("invalid_import", status=400)

        checked: list[tuple[str, str]] = []
        for message in messages:
            if not isinstance(message, dict):
                raise ServiceError("invalid_import", status=400)
            role = message.get("role")
            content = message.get("content")
            if (
                role not in ("user", "assistant")
                or not isinstance(content, str)
                or len(content) > MAX_TRANSFER_MESSAGE_CHARS
                or set(message) != {"role", "content"}
            ):
                raise ServiceError("invalid_import", status=400)
            checked.append((role, content))
        imported.append(
            ImportedConversation(title, title_manual, pinned, tuple(checked))
        )
    return imported
