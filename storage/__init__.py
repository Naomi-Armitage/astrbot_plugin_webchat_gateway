"""Storage backend factory."""

from __future__ import annotations

from .base import (
    _UNSET,
    AbstractStorage,
    AuditRow,
    NewEvent,
    SessionMetaRow,
    TokenRow,
    UpdateRow,
    UsageRow,
)


def get_storage(driver: str, *, sqlite_path: str = "", mysql_dsn: str = "") -> AbstractStorage:
    if driver == "sqlite":
        from .sqlite_backend import SqliteStorage

        if not sqlite_path:
            raise ValueError("sqlite_path is required for sqlite driver")
        return SqliteStorage(sqlite_path)

    if driver == "mysql":
        try:
            import aiomysql  # noqa: F401  - presence check
        except ImportError as exc:
            raise RuntimeError(
                "MySQL backend requires `aiomysql`; install with `pip install aiomysql`"
            ) from exc
        from .mysql_backend import MysqlStorage

        if not mysql_dsn:
            raise ValueError("mysql_dsn is required for mysql driver")
        return MysqlStorage(mysql_dsn)

    raise ValueError(f"unknown storage driver: {driver}")


__all__ = [
    "AbstractStorage",
    "TokenRow",
    "UsageRow",
    "AuditRow",
    "SessionMetaRow",
    "UpdateRow",
    "NewEvent",
    "_UNSET",
    "get_storage",
]
