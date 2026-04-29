"""DDL for SQLite and MySQL backends.

Schema parity goals
-------------------
- `tokens.name` is the public identifier; `token_hash` is what we authenticate against.
- `daily_usage` is keyed `(name, day)` — used both for the chat hot path (atomic
  increment) and admin stats (range scans on `(name, day)` are covered by the PK).
- `audit_log` is queried as the most recent N events; we order by `ts DESC, id DESC`
  so concurrent inserts that share a timestamp still produce a stable order.
- `ip_failures` is a hot, small table; brute-force guard reads/writes it on every
  failed auth.
- `_schema_meta` is a forward hook for migrations: each backend seeds
  `(schema_version, "1")` on `initialize()`. Future versions can read this row
  to decide whether to run an ALTER chain. Not a migration framework — just the
  handshake row so future-us has somewhere to look.

Both schemas use idempotent `IF NOT EXISTS`, so re-running on an existing database
is safe but does *not* perform migrations — additive-only changes for now.
"""

from __future__ import annotations

CURRENT_SCHEMA_VERSION = "1"

SCHEMA_SQLITE: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS _schema_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
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
    "CREATE INDEX IF NOT EXISTS idx_audit_ts_id ON audit_log(ts DESC, id DESC)",
)


SCHEMA_MYSQL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS _schema_meta (
        `key`   VARCHAR(64) PRIMARY KEY,
        value   VARCHAR(255) NOT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
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
        INDEX idx_audit_ts_id (ts, id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)
