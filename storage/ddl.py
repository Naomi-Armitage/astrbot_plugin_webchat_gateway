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

CURRENT_SCHEMA_VERSION = "6"

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
    """
    CREATE TABLE IF NOT EXISTS webchat_files (
        file_id      TEXT PRIMARY KEY,
        token_name   TEXT NOT NULL,
        session_id   TEXT NOT NULL,
        mime         TEXT NOT NULL,
        size_bytes   INTEGER NOT NULL,
        storage_key  TEXT NOT NULL,
        committed    INTEGER NOT NULL DEFAULT 0,
        uploaded_at  INTEGER NOT NULL,
        committed_at INTEGER,
        filename     TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_webchat_files_token_session "
    "ON webchat_files (token_name, session_id)",
    "CREATE INDEX IF NOT EXISTS idx_webchat_files_uncommitted "
    "ON webchat_files (committed, uploaded_at)",
    # Drop (multi-device self-to-self file/note transfer, no LLM). Lives in
    # its own table because reusing webchat_session_meta / webchat_updates
    # would either pollute AstrBot's CM history (TECH_DEBT §1 calls this out
    # for the user/assistant pair semantic) or force every reader to branch
    # on event_type. The `id` is a server-assigned monotonic counter per
    # token (NOT a UUID) so peer devices can dedup by id and so history
    # pagination is a simple `WHERE id < ?` range scan.
    """
    CREATE TABLE IF NOT EXISTS webchat_drop_messages (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        token_name  TEXT    NOT NULL,
        device_id   TEXT    NOT NULL,
        device_name TEXT    NOT NULL DEFAULT '',
        kind        TEXT    NOT NULL,         -- 'text' | 'file'
        text        TEXT    NOT NULL DEFAULT '',
        file_id     TEXT    NULL,             -- nullable: text-only messages
        filename    TEXT    NOT NULL DEFAULT '',
        mime        TEXT    NOT NULL DEFAULT '',
        size_bytes  INTEGER NOT NULL DEFAULT 0,
        created_at  INTEGER NOT NULL,
        deleted_at  INTEGER NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_drop_messages_token_created "
    "ON webchat_drop_messages (token_name, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_drop_messages_token_deleted "
    "ON webchat_drop_messages (token_name, deleted_at)",
    # FK with ON DELETE SET NULL: when a webchat_files row is hard-deleted
    # (commit-failure release, orphan GC, or clear), the message keeps its
    # text content but the file link goes NULL so the client renders a
    # tombstone ("文件已删除") instead of a broken thumbnail. CASCADE would
    # be silent data loss for any text-only co-message sharing the file.
    "CREATE INDEX IF NOT EXISTS idx_drop_messages_file "
    "ON webchat_drop_messages (file_id)",
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
    """
    CREATE TABLE IF NOT EXISTS webchat_files (
        file_id      VARCHAR(32)  NOT NULL,
        token_name   VARCHAR(128) NOT NULL,
        session_id   VARCHAR(128) NOT NULL,
        mime         VARCHAR(64)  NOT NULL,
        size_bytes   BIGINT NOT NULL,
        storage_key  VARCHAR(512) NOT NULL,
        committed    TINYINT(1)   NOT NULL DEFAULT 0,
        uploaded_at  BIGINT NOT NULL,
        committed_at BIGINT NULL,
        filename     VARCHAR(255) NOT NULL DEFAULT '',
        PRIMARY KEY (file_id),
        INDEX idx_webchat_files_token_session (token_name, session_id),
        INDEX idx_webchat_files_uncommitted (committed, uploaded_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS webchat_drop_messages (
        id          BIGINT PRIMARY KEY AUTO_INCREMENT,
        token_name  VARCHAR(128) NOT NULL,
        device_id   VARCHAR(64)  NOT NULL,
        device_name VARCHAR(128) NOT NULL DEFAULT '',
        kind        VARCHAR(16)  NOT NULL,
        text        TEXT         NOT NULL,
        file_id     VARCHAR(32)  NULL,
        filename    VARCHAR(255) NOT NULL DEFAULT '',
        mime        VARCHAR(64)  NOT NULL DEFAULT '',
        size_bytes  BIGINT NOT NULL DEFAULT 0,
        created_at  BIGINT NOT NULL,
        deleted_at  BIGINT NULL,
        INDEX idx_drop_messages_token_id (token_name, id),
        INDEX idx_drop_messages_token_deleted (token_name, deleted_at),
        INDEX idx_drop_messages_file (file_id)
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


# v4 → v5: introduce webchat_files table for image uploads. Idempotent
# via IF NOT EXISTS so a fresh v5 install (which already created the
# table from SCHEMA_*) and a re-run both land in the same place.
V4_TO_V5_SQLITE: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS webchat_files (
        file_id      TEXT PRIMARY KEY,
        token_name   TEXT NOT NULL,
        session_id   TEXT NOT NULL,
        mime         TEXT NOT NULL,
        size_bytes   INTEGER NOT NULL,
        storage_key  TEXT NOT NULL,
        committed    INTEGER NOT NULL DEFAULT 0,
        uploaded_at  INTEGER NOT NULL,
        committed_at INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_webchat_files_token_session "
    "ON webchat_files (token_name, session_id)",
    "CREATE INDEX IF NOT EXISTS idx_webchat_files_uncommitted "
    "ON webchat_files (committed, uploaded_at)",
)


V4_TO_V5_MYSQL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS webchat_files (
        file_id      VARCHAR(32)  NOT NULL,
        token_name   VARCHAR(128) NOT NULL,
        session_id   VARCHAR(128) NOT NULL,
        mime         VARCHAR(64)  NOT NULL,
        size_bytes   BIGINT NOT NULL,
        storage_key  VARCHAR(512) NOT NULL,
        committed    TINYINT(1)   NOT NULL DEFAULT 0,
        uploaded_at  BIGINT NOT NULL,
        committed_at BIGINT NULL,
        PRIMARY KEY (file_id),
        INDEX idx_webchat_files_token_session (token_name, session_id),
        INDEX idx_webchat_files_uncommitted (committed, uploaded_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)


# v5 → v6: Drop (multi-device self-to-self file/note transfer). Additive.
# Two changes:
#   1. webchat_files.filename — preserves the upload's original name so the
#      serve endpoint can serve non-image files with a real filename
#      (Content-Disposition: attachment; filename="...") and the client
#      can render it without re-deriving. Idempotent via the duplicate-
#      column guard both backends apply.
#   2. webchat_drop_messages — see SCHEMA_*_SQLITE for rationale; same
#      CREATE TABLE / CREATE INDEX idempotency as the v4 → v5 webchat_files
#      migration. The DROP_SESSION_ID column is omitted (Drop uses the
#      literal "drop" session; this lives on webchat_files.session_id).
ALTER_FILES_ADD_FILENAME_SQLITE = (
    "ALTER TABLE webchat_files ADD COLUMN filename TEXT NOT NULL DEFAULT ''"
)
ALTER_FILES_ADD_FILENAME_MYSQL = (
    "ALTER TABLE webchat_files ADD COLUMN filename VARCHAR(255) NOT NULL DEFAULT ''"
)

V5_TO_V6_SQLITE: tuple[str, ...] = (
    # NOTE: the webchat_files.filename ALTER is NOT in this tuple — it
    # runs separately, guarded by the duplicate-column catch (the tuple
    # statements are only IF NOT EXISTS-idempotent, which ALTER is not).
    """
    CREATE TABLE IF NOT EXISTS webchat_drop_messages (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        token_name  TEXT    NOT NULL,
        device_id   TEXT    NOT NULL,
        device_name TEXT    NOT NULL DEFAULT '',
        kind        TEXT    NOT NULL,
        text        TEXT    NOT NULL DEFAULT '',
        file_id     TEXT    NULL,
        filename    TEXT    NOT NULL DEFAULT '',
        mime        TEXT    NOT NULL DEFAULT '',
        size_bytes  INTEGER NOT NULL DEFAULT 0,
        created_at  INTEGER NOT NULL,
        deleted_at  INTEGER NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_drop_messages_token_created "
    "ON webchat_drop_messages (token_name, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_drop_messages_token_deleted "
    "ON webchat_drop_messages (token_name, deleted_at)",
    "CREATE INDEX IF NOT EXISTS idx_drop_messages_file "
    "ON webchat_drop_messages (file_id)",
)

V5_TO_V6_MYSQL: tuple[str, ...] = (
    # Same split as the sqlite variant: the ALTER lives outside this
    # tuple, guarded by error 1060.
    """
    CREATE TABLE IF NOT EXISTS webchat_drop_messages (
        id          BIGINT PRIMARY KEY AUTO_INCREMENT,
        token_name  VARCHAR(128) NOT NULL,
        device_id   VARCHAR(64)  NOT NULL,
        device_name VARCHAR(128) NOT NULL DEFAULT '',
        kind        VARCHAR(16)  NOT NULL,
        text        TEXT         NOT NULL,
        file_id     VARCHAR(32)  NULL,
        filename    VARCHAR(255) NOT NULL DEFAULT '',
        mime        VARCHAR(64)  NOT NULL DEFAULT '',
        size_bytes  BIGINT NOT NULL DEFAULT 0,
        created_at  BIGINT NOT NULL,
        deleted_at  BIGINT NULL,
        INDEX idx_drop_messages_token_id (token_name, id),
        INDEX idx_drop_messages_token_deleted (token_name, deleted_at),
        INDEX idx_drop_messages_file (file_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)
