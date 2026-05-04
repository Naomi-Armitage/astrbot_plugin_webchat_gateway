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
  `(schema_version, "<version>")` on `initialize()` and reads it back to drive
  any pending ALTERs. Not a migration framework — additive columns only.

Both schemas use idempotent `IF NOT EXISTS`, so re-running on an existing database
is safe. Cross-version upgrades happen inside each backend's `initialize()`.
"""

from __future__ import annotations

CURRENT_SCHEMA_VERSION = "4"

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
        revoked_at   INTEGER,
        expires_at   INTEGER
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
    """
    CREATE TABLE IF NOT EXISTS webchat_session_meta (
        token_name    TEXT NOT NULL,
        session_id    TEXT NOT NULL,
        title         TEXT NOT NULL DEFAULT '',
        title_manual  INTEGER NOT NULL DEFAULT 0,
        pinned_at     INTEGER,
        deleted_at    INTEGER,
        updated_at    INTEGER NOT NULL,
        message_count INTEGER NOT NULL DEFAULT 0,
        preview       TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (token_name, session_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_session_meta_token_updated "
    "ON webchat_session_meta(token_name, updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS webchat_updates (
        token_name TEXT NOT NULL,
        pts        INTEGER NOT NULL,
        ts         INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        session_id TEXT NOT NULL,
        payload    TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (token_name, pts)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_webchat_updates_ts "
    "ON webchat_updates(ts)",
)


# v2 → v3 (additive only). Both backends apply these on upgrade. Idempotent
# via IF NOT EXISTS so no error guards needed; a fresh install runs these
# again from SCHEMA_SQLITE / SCHEMA_MYSQL above and lands in the same place.
V2_TO_V3_SQLITE: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS webchat_session_meta (
        token_name    TEXT NOT NULL,
        session_id    TEXT NOT NULL,
        title         TEXT NOT NULL DEFAULT '',
        title_manual  INTEGER NOT NULL DEFAULT 0,
        pinned_at     INTEGER,
        deleted_at    INTEGER,
        updated_at    INTEGER NOT NULL,
        message_count INTEGER NOT NULL DEFAULT 0,
        preview       TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (token_name, session_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_session_meta_token_updated "
    "ON webchat_session_meta(token_name, updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS webchat_updates (
        token_name TEXT NOT NULL,
        pts        INTEGER NOT NULL,
        ts         INTEGER NOT NULL,
        event_type TEXT NOT NULL,
        session_id TEXT NOT NULL,
        payload    TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (token_name, pts)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_webchat_updates_ts "
    "ON webchat_updates(ts)",
)


# v3 → v4: cache `message_count` + `preview` on session_meta to avoid the
# N+1 CM read in list_conversations. Idempotent: catch "duplicate column"
# so re-runs are safe and a fresh v4 install (which already has the
# columns from CREATE TABLE) is unaffected.
ALTER_META_ADD_COUNT_SQLITE = (
    "ALTER TABLE webchat_session_meta ADD COLUMN message_count INTEGER NOT NULL DEFAULT 0"
)
ALTER_META_ADD_PREVIEW_SQLITE = (
    "ALTER TABLE webchat_session_meta ADD COLUMN preview TEXT NOT NULL DEFAULT ''"
)
ALTER_META_ADD_COUNT_MYSQL = (
    "ALTER TABLE webchat_session_meta ADD COLUMN message_count INT NOT NULL DEFAULT 0"
)
ALTER_META_ADD_PREVIEW_MYSQL = (
    "ALTER TABLE webchat_session_meta ADD COLUMN preview VARCHAR(255) NOT NULL DEFAULT ''"
)
ALTER_UPDATES_ADD_TS_INDEX_SQLITE = (
    "CREATE INDEX IF NOT EXISTS idx_webchat_updates_ts ON webchat_updates(ts)"
)
ALTER_UPDATES_ADD_TS_INDEX_MYSQL = (
    "CREATE INDEX idx_webchat_updates_ts ON webchat_updates(ts)"
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
        expires_at   BIGINT NULL,
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
    """
    CREATE TABLE IF NOT EXISTS webchat_session_meta (
        token_name    VARCHAR(128) NOT NULL,
        session_id    VARCHAR(128) NOT NULL,
        title         VARCHAR(255) NOT NULL DEFAULT '',
        title_manual  TINYINT(1)   NOT NULL DEFAULT 0,
        pinned_at     BIGINT NULL,
        deleted_at    BIGINT NULL,
        updated_at    BIGINT NOT NULL,
        message_count INT NOT NULL DEFAULT 0,
        preview       VARCHAR(255) NOT NULL DEFAULT '',
        PRIMARY KEY (token_name, session_id),
        INDEX idx_session_meta_token_updated (token_name, updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS webchat_updates (
        token_name VARCHAR(128) NOT NULL,
        pts        BIGINT NOT NULL,
        ts         BIGINT NOT NULL,
        event_type VARCHAR(64)  NOT NULL,
        session_id VARCHAR(128) NOT NULL,
        payload    MEDIUMTEXT   NOT NULL,
        PRIMARY KEY (token_name, pts),
        INDEX idx_webchat_updates_ts (ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)


V2_TO_V3_MYSQL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS webchat_session_meta (
        token_name    VARCHAR(128) NOT NULL,
        session_id    VARCHAR(128) NOT NULL,
        title         VARCHAR(255) NOT NULL DEFAULT '',
        title_manual  TINYINT(1)   NOT NULL DEFAULT 0,
        pinned_at     BIGINT NULL,
        deleted_at    BIGINT NULL,
        updated_at    BIGINT NOT NULL,
        message_count INT NOT NULL DEFAULT 0,
        preview       VARCHAR(255) NOT NULL DEFAULT '',
        PRIMARY KEY (token_name, session_id),
        INDEX idx_session_meta_token_updated (token_name, updated_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS webchat_updates (
        token_name VARCHAR(128) NOT NULL,
        pts        BIGINT NOT NULL,
        ts         BIGINT NOT NULL,
        event_type VARCHAR(64)  NOT NULL,
        session_id VARCHAR(128) NOT NULL,
        payload    MEDIUMTEXT   NOT NULL,
        PRIMARY KEY (token_name, pts),
        INDEX idx_webchat_updates_ts (ts)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)


# v1 → v2: tokens.expires_at. Each backend runs the ALTER guarded by a
# duplicate-column catch so re-runs (and parallel pods) are safe.
ALTER_TOKENS_ADD_EXPIRES_AT_SQLITE = (
    "ALTER TABLE tokens ADD COLUMN expires_at INTEGER"
)
ALTER_TOKENS_ADD_EXPIRES_AT_MYSQL = (
    "ALTER TABLE tokens ADD COLUMN expires_at BIGINT NULL"
)
