"""DDL for SQLite and MySQL backends."""

from __future__ import annotations

SCHEMA_SQLITE: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS tokens (
        name         TEXT PRIMARY KEY,
        token_hash   TEXT NOT NULL UNIQUE,
        daily_quota  INTEGER NOT NULL DEFAULT 200,
        note         TEXT NOT NULL DEFAULT '',
        created_at   INTEGER NOT NULL,
        revoked_at   INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tokens_hash ON tokens(token_hash)",
    """
    CREATE TABLE IF NOT EXISTS daily_usage (
        name  TEXT NOT NULL,
        day   TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (name, day)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ip_failures (
        ip            TEXT PRIMARY KEY,
        fail_count    INTEGER NOT NULL DEFAULT 0,
        first_fail_ts INTEGER NOT NULL,
        last_fail_ts  INTEGER NOT NULL,
        blocked_until INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id     INTEGER PRIMARY KEY AUTOINCREMENT,
        ts     INTEGER NOT NULL,
        name   TEXT,
        ip     TEXT,
        event  TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC)",
)


SCHEMA_MYSQL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS tokens (
        name         VARCHAR(128) PRIMARY KEY,
        token_hash   CHAR(64) NOT NULL UNIQUE,
        daily_quota  INT NOT NULL DEFAULT 200,
        note         VARCHAR(255) NOT NULL DEFAULT '',
        created_at   BIGINT NOT NULL,
        revoked_at   BIGINT NULL,
        INDEX idx_tokens_hash (token_hash)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_usage (
        name  VARCHAR(128) NOT NULL,
        day   DATE NOT NULL,
        count INT NOT NULL DEFAULT 0,
        PRIMARY KEY (name, day)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS ip_failures (
        ip            VARCHAR(64) PRIMARY KEY,
        fail_count    INT NOT NULL DEFAULT 0,
        first_fail_ts BIGINT NOT NULL,
        last_fail_ts  BIGINT NOT NULL,
        blocked_until BIGINT NOT NULL DEFAULT 0
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id     BIGINT PRIMARY KEY AUTO_INCREMENT,
        ts     BIGINT NOT NULL,
        name   VARCHAR(128) NULL,
        ip     VARCHAR(64) NULL,
        event  VARCHAR(64) NOT NULL,
        detail TEXT NOT NULL,
        INDEX idx_audit_ts (ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)
