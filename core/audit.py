"""Audit logger that writes through to the storage backend."""

from __future__ import annotations

import json
import time
from typing import Any

from astrbot.api import logger

from ..storage.base import AbstractStorage


class AuditLogger:
    def __init__(self, storage: AbstractStorage) -> None:
        self._storage = storage

    async def write(
        self,
        event: str,
        *,
        name: str | None = None,
        ip: str | None = None,
        detail: Any = None,
    ) -> None:
        if isinstance(detail, str):
            detail_str = detail
        elif detail is None:
            detail_str = ""
        else:
            try:
                detail_str = json.dumps(detail, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                detail_str = str(detail)
        if len(detail_str) > 1024:
            detail_str = detail_str[:1024]
        try:
            await self._storage.write_audit(
                ts=int(time.time()),
                name=name,
                ip=ip,
                event=event,
                detail=detail_str,
            )
        except Exception:
            logger.exception("[WebChatGateway] audit write failed event=%s", event)
