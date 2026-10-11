"""Validation for timestamped Edge Drop text imports."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from .admin_tokens import ServiceError


EDGE_DROP_IMPORT_FORMAT = "edge-drop-text-v1"
MAX_EDGE_DROP_IMPORT_BYTES = 20 * 1024 * 1024
MAX_EDGE_DROP_IMPORT_MESSAGES = 1000
MAX_EDGE_DROP_TEXT_CHARS = 16_000
EDGE_DROP_IMPORT_DEVICE_ID = "edge-drop-import"
EDGE_DROP_IMPORT_DEVICE_NAME = "Edge Drop 导入"


@dataclass(frozen=True)
class EdgeDropImportMessage:
    text: str
    created_at: int


def validate_edge_drop_import(value: Any) -> list[EdgeDropImportMessage]:
    """Reject malformed/oversized files before any Drop row is created."""
    if not isinstance(value, dict) or value.get("format") != EDGE_DROP_IMPORT_FORMAT:
        raise ServiceError("invalid_drop_import", status=400)
    rows = value.get("messages")
    if not isinstance(rows, list) or not 0 < len(rows) <= MAX_EDGE_DROP_IMPORT_MESSAGES:
        raise ServiceError("invalid_drop_import", status=400)

    checked: list[EdgeDropImportMessage] = []
    latest = int(time.time()) + 86400
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"text", "created_at"}:
            raise ServiceError("invalid_drop_import", status=400)
        text = row["text"]
        created_at = row["created_at"]
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > MAX_EDGE_DROP_TEXT_CHARS
            or type(created_at) is not int
            or not 946684800 <= created_at <= latest
        ):
            raise ServiceError("invalid_drop_import", status=400)
        checked.append(EdgeDropImportMessage(text=text, created_at=created_at))
    return checked
