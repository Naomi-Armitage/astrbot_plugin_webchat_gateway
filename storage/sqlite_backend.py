"""aiosqlite storage backend."""

from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta

import aiosqlite

from .base import (
    _UNSET,
    AbstractStorage,
    AuditRow,
    NewEvent,
    SessionMetaRow,
    TokenRow,
    UpdateRow,
    UsageRow,
    _Sentinel,
)
from .ddl import (
    ALTER_META_ADD_COUNT_SQLITE,
    ALTER_META_ADD_PREVIEW_SQLITE,
    ALTER_TOKENS_ADD_EXPIRES_AT_SQLITE,
    ALTER_UPDATES_ADD_TS_INDEX_SQLITE,
    CURRENT_SCHEMA_VERSION,
    SCHEMA_SQLITE,
    V2_TO_V3_SQLITE,
)


class SqliteStorage(AbstractStorage):
    """File-based SQLite storage with WAL mode.

    Concurrency invariant: this backend uses a single `aiosqlite.Connection`
    plus an `asyncio.Lock` (`_write_lock`) that all mutating methods acquire.
    Combined with AstrBot's "one process per plugin instance" model, this
    serializes writes safely without needing transactional read-then-write
    primitives. Adding a second connection or a worker pool would invalidate
    several methods (`record_ip_failure` in particular relies on the lock to
    bridge the SELECT/INSERT race).
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def initialize(self) -> None:
        directory = os.path.dirname(self._db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        for stmt in SCHEMA_SQLITE:
            await self._conn.execute(stmt)
        async with self._conn.execute(
            "SELECT value FROM _schema_meta WHERE key = 'schema_version'"
        ) as cursor:
            row = await cursor.fetchone()
        stored = row["value"] if row else None
        if stored is None:
            # Fresh install — CREATE TABLE IF NOT EXISTS in SCHEMA_SQLITE
            # already produced every v3 table.
            await self._conn.execute(
                "INSERT INTO _schema_meta(key, value) VALUES('schema_version', ?)",
                (CURRENT_SCHEMA_VERSION,),
            )
        else:
            if stored == "1":
                # v1 → v2: add tokens.expires_at. Tolerate "duplicate column"
                # so the migration is idempotent if a previous attempt crashed
                # between ALTER and the version write.
                try:
                    await self._conn.execute(ALTER_TOKENS_ADD_EXPIRES_AT_SQLITE)
                except aiosqlite.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
                stored = "2"
            if stored == "2":
                # v2 → v3: webchat_session_meta + webchat_updates. Both
                # statements are CREATE TABLE / CREATE INDEX IF NOT EXISTS,
                # so the SCHEMA_SQLITE pass already ran them; replaying here
                # is a no-op but keeps the migration ladder explicit.
                for stmt in V2_TO_V3_SQLITE:
                    await self._conn.execute(stmt)
                stored = "3"
            if stored == "3":
                # v3 → v4: add cached message_count + preview to session_meta
                # so list_conversations doesn't have to do an N+1 CM lookup,
                # and add the ts index on webchat_updates so the retention
                # prune can range-scan instead of full-table-scan.
                for alter in (
                    ALTER_META_ADD_COUNT_SQLITE,
                    ALTER_META_ADD_PREVIEW_SQLITE,
                ):
                    try:
                        await self._conn.execute(alter)
                    except aiosqlite.OperationalError as exc:
                        if "duplicate column" not in str(exc).lower():
                            raise
                # CREATE INDEX IF NOT EXISTS is its own idempotency.
                await self._conn.execute(ALTER_UPDATES_ADD_TS_INDEX_SQLITE)
                stored = "4"
            await self._conn.execute(
                "UPDATE _schema_meta SET value = ? WHERE key = 'schema_version'",
                (CURRENT_SCHEMA_VERSION,),
            )
        # stored == CURRENT_SCHEMA_VERSION (or any future version): no-op.
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SqliteStorage not initialized")
        return self._conn

    @staticmethod
    def _row_to_token(row: aiosqlite.Row) -> TokenRow:
        return TokenRow(
            name=row["name"],
            token_hash=row["token_hash"],
            daily_quota=int(row["daily_quota"]),
            note=row["note"] or "",
            created_at=int(row["created_at"]),
            revoked_at=(int(row["revoked_at"]) if row["revoked_at"] is not None else None),
            expires_at=(int(row["expires_at"]) if row["expires_at"] is not None else None),
        )

    # ----- tokens -----
    async def create_token(
        self,
        *,
        name: str,
        token_hash: str,
        daily_quota: int,
        note: str,
        now: int,
        expires_at: int | None = None,
    ) -> None:
        async with self._write_lock:
            await self._db.execute(
                "INSERT INTO tokens(name, token_hash, daily_quota, note, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, token_hash, daily_quota, note, now, expires_at),
            )
            await self._db.commit()

    async def get_token_by_hash(self, token_hash: str) -> TokenRow | None:
        async with self._db.execute(
            "SELECT * FROM tokens WHERE token_hash = ?", (token_hash,)
        ) as cursor:
            row = await cursor.fetchone()
        return self._row_to_token(row) if row else None

    async def get_token_by_name(self, name: str) -> TokenRow | None:
        async with self._db.execute(
            "SELECT * FROM tokens WHERE name = ?", (name,)
        ) as cursor:
            row = await cursor.fetchone()
        return self._row_to_token(row) if row else None

    async def revoke_token(self, name: str, *, now: int) -> bool:
        async with self._write_lock:
            cursor = await self._db.execute(
                "UPDATE tokens SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL",
                (now, name),
            )
            await self._db.commit()
            return cursor.rowcount > 0

    async def list_tokens(self, *, include_revoked: bool = False) -> list[TokenRow]:
        if include_revoked:
            sql = "SELECT * FROM tokens ORDER BY created_at DESC"
            args: tuple = ()
        else:
            sql = "SELECT * FROM tokens WHERE revoked_at IS NULL ORDER BY created_at DESC"
            args = ()
        async with self._db.execute(sql, args) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_token(r) for r in rows]

    async def update_token(
        self,
        name: str,
        *,
        daily_quota: int | None = None,
        note: str | None = None,
        expires_at: int | None | _Sentinel = _UNSET,
    ) -> bool:
        sets: list[str] = []
        args: list = []
        if daily_quota is not None:
            sets.append("daily_quota = ?")
            args.append(daily_quota)
        if note is not None:
            sets.append("note = ?")
            args.append(note)
        if expires_at is not _UNSET:
            sets.append("expires_at = ?")
            args.append(expires_at)
        if not sets:
            # No-op call. Treat as "matched if the token exists" so callers
            # can rely on the boolean return value uniformly.
            async with self._db.execute(
                "SELECT 1 FROM tokens WHERE name = ?", (name,)
            ) as cursor:
                return await cursor.fetchone() is not None
        sql = f"UPDATE tokens SET {', '.join(sets)} WHERE name = ?"
        args.append(name)
        async with self._write_lock:
            cursor = await self._db.execute(sql, args)
            await self._db.commit()
            return cursor.rowcount > 0

    async def set_token_revoked(
        self, name: str, *, revoked: bool, now: int
    ) -> bool:
        async with self._write_lock:
            if revoked:
                cursor = await self._db.execute(
                    "UPDATE tokens SET revoked_at = ? WHERE name = ?",
                    (now, name),
                )
            else:
                cursor = await self._db.execute(
                    "UPDATE tokens SET revoked_at = NULL WHERE name = ?",
                    (name,),
                )
            await self._db.commit()
            return cursor.rowcount > 0

    async def regenerate_token(self, name: str, new_token_hash: str) -> bool:
        async with self._write_lock:
            cursor = await self._db.execute(
                "UPDATE tokens SET token_hash = ? WHERE name = ?",
                (new_token_hash, name),
            )
            await self._db.commit()
            return cursor.rowcount > 0

    async def rename_token(self, old_name: str, new_name: str) -> bool:
        if old_name == new_name:
            async with self._db.execute(
                "SELECT 1 FROM tokens WHERE name = ?", (old_name,)
            ) as cursor:
                return await cursor.fetchone() is not None
        async with self._write_lock:
            # Check both the source row and the destination collision atomically
            # under the write lock. BEGIN/COMMIT brackets the cascade so a
            # crash mid-rename can't leave daily_usage / audit_log pointing
            # at the old name while tokens already moved.
            async with self._db.execute(
                "SELECT 1 FROM tokens WHERE name = ?", (old_name,)
            ) as cursor:
                src = await cursor.fetchone()
            if not src:
                return False
            async with self._db.execute(
                "SELECT 1 FROM tokens WHERE name = ?", (new_name,)
            ) as cursor:
                if await cursor.fetchone():
                    return False
            await self._db.execute("BEGIN")
            try:
                await self._db.execute(
                    "UPDATE tokens SET name = ? WHERE name = ?",
                    (new_name, old_name),
                )
                await self._db.execute(
                    "UPDATE daily_usage SET name = ? WHERE name = ?",
                    (new_name, old_name),
                )
                await self._db.execute(
                    "UPDATE audit_log SET name = ? WHERE name = ?",
                    (new_name, old_name),
                )
                await self._db.commit()
            except BaseException:
                try:
                    await self._db.rollback()
                except Exception:
                    pass
                raise
            return True

    # ----- daily usage -----
    async def increment_daily_usage(self, name: str, *, day: date) -> int:
        # Two statements (UPSERT then SELECT) instead of `RETURNING`, so this
        # works against SQLite < 3.35 (RETURNING was added 2021-03). Atomicity
        # is preserved by `_write_lock` plus the surrounding single transaction.
        day_key = day.isoformat()
        async with self._write_lock:
            await self._db.execute(
                "INSERT INTO daily_usage(name, day, count) VALUES(?, ?, 1) "
                "ON CONFLICT(name, day) DO UPDATE SET count = count + 1",
                (name, day_key),
            )
            async with self._db.execute(
                "SELECT count FROM daily_usage WHERE name = ? AND day = ?",
                (name, day_key),
            ) as cursor:
                row = await cursor.fetchone()
            await self._db.commit()
        return int(row["count"]) if row else 0

    async def get_today_usage(self, name: str, *, day: date) -> int:
        async with self._db.execute(
            "SELECT count FROM daily_usage WHERE name = ? AND day = ?",
            (name, day.isoformat()),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["count"]) if row else 0

    async def get_today_usage_bulk(
        self, names: list[str], *, day: date
    ) -> dict[str, int]:
        if not names:
            return {}
        placeholders = ",".join("?" for _ in names)
        sql = (
            f"SELECT name, count FROM daily_usage "
            f"WHERE day = ? AND name IN ({placeholders})"
        )
        async with self._db.execute(sql, (day.isoformat(), *names)) as cursor:
            rows = await cursor.fetchall()
        out = {n: 0 for n in names}
        for row in rows:
            out[row["name"]] = int(row["count"])
        return out

    async def get_usage_stats(self, name: str, *, days: int) -> list[UsageRow]:
        days = max(1, min(days, 365))
        today = date.today()
        first = today - timedelta(days=days - 1)
        async with self._db.execute(
            "SELECT day, count FROM daily_usage "
            "WHERE name = ? AND day >= ? AND day <= ? "
            "ORDER BY day ASC",
            (name, first.isoformat(), today.isoformat()),
        ) as cursor:
            rows = await cursor.fetchall()
        existing = {r["day"]: int(r["count"]) for r in rows}
        out: list[UsageRow] = []
        for offset in range(days):
            d = first + timedelta(days=offset)
            out.append(UsageRow(name=name, day=d, count=existing.get(d.isoformat(), 0)))
        return out

    # ----- ip failures -----
    async def record_ip_failure(
        self, ip: str, *, now: int, max_fails: int, block_seconds: int
    ) -> int:
        if max_fails <= 0:
            return 0
        async with self._write_lock:
            async with self._db.execute(
                "SELECT fail_count, first_fail_ts FROM ip_failures WHERE ip = ?",
                (ip,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                await self._db.execute(
                    "INSERT INTO ip_failures(ip, fail_count, first_fail_ts, last_fail_ts, blocked_until) "
                    "VALUES (?, 1, ?, ?, 0)",
                    (ip, now, now),
                )
                new_count = 1
            else:
                new_count = int(row["fail_count"]) + 1
                new_blocked_until = now + block_seconds if new_count >= max_fails else None
                await self._db.execute(
                    "UPDATE ip_failures "
                    "SET fail_count = ?, last_fail_ts = ?, "
                    "    blocked_until = CASE WHEN ? >= ? THEN ? ELSE blocked_until END "
                    "WHERE ip = ?",
                    (new_count, now, new_count, max_fails, new_blocked_until or 0, ip),
                )
            await self._db.commit()
        return new_count

    async def is_ip_blocked(self, ip: str, *, now: int) -> tuple[bool, int]:
        async with self._db.execute(
            "SELECT blocked_until FROM ip_failures WHERE ip = ?", (ip,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return False, 0
        blocked_until = int(row["blocked_until"])
        if blocked_until > now:
            return True, blocked_until - now
        return False, 0

    async def reset_ip_failures(self, ip: str) -> None:
        async with self._write_lock:
            await self._db.execute("DELETE FROM ip_failures WHERE ip = ?", (ip,))
            await self._db.commit()

    # ----- audit -----
    async def write_audit(
        self,
        *,
        ts: int,
        name: str | None,
        ip: str | None,
        event: str,
        detail: str,
    ) -> None:
        async with self._write_lock:
            await self._db.execute(
                "INSERT INTO audit_log(ts, name, ip, event, detail) VALUES (?, ?, ?, ?, ?)",
                (ts, name, ip, event, detail or ""),
            )
            await self._db.commit()

    async def get_recent_audit(self, *, limit: int) -> list[AuditRow]:
        limit = max(1, min(limit, 500))
        async with self._db.execute(
            "SELECT id, ts, name, ip, event, detail FROM audit_log "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            AuditRow(
                id=int(r["id"]),
                ts=int(r["ts"]),
                name=r["name"],
                ip=r["ip"],
                event=r["event"],
                detail=r["detail"] or "",
            )
            for r in rows
        ]

    # ----- chat sync (v3) -----
    @staticmethod
    def _row_to_session_meta(row: aiosqlite.Row) -> SessionMetaRow:
        # message_count / preview were added in v4. Older rows may not have
        # them populated yet on a freshly migrated DB; fall back to defaults.
        try:
            mc = int(row["message_count"])
        except (KeyError, IndexError, TypeError):
            mc = 0
        try:
            preview = row["preview"] or ""
        except (KeyError, IndexError):
            preview = ""
        return SessionMetaRow(
            token_name=row["token_name"],
            session_id=row["session_id"],
            title=row["title"] or "",
            title_manual=bool(row["title_manual"]),
            pinned_at=(
                int(row["pinned_at"]) if row["pinned_at"] is not None else None
            ),
            deleted_at=(
                int(row["deleted_at"]) if row["deleted_at"] is not None else None
            ),
            updated_at=int(row["updated_at"]),
            message_count=mc,
            preview=preview,
        )

    async def upsert_session_meta(
        self,
        *,
        token_name: str,
        session_id: str,
        title: str | None = None,
        title_manual: bool | None = None,
        pinned_at: int | None | _Sentinel = _UNSET,
        deleted_at: int | None | _Sentinel = _UNSET,
        message_count: int | None = None,
        preview: str | None = None,
        now: int,
    ) -> SessionMetaRow:
        async with self._write_lock:
            async with self._db.execute(
                "SELECT * FROM webchat_session_meta "
                "WHERE token_name = ? AND session_id = ?",
                (token_name, session_id),
            ) as cursor:
                existing = await cursor.fetchone()
            if existing is None:
                # First-write defaults: anything the caller didn't pin lands
                # at the column default. This branch sees `_UNSET` for the
                # nullable fields too — treat it the same as None (NULL).
                new_title = title if title is not None else ""
                new_manual = bool(title_manual) if title_manual is not None else False
                new_pinned = (
                    pinned_at
                    if pinned_at is not _UNSET
                    else None
                )
                new_deleted = (
                    deleted_at
                    if deleted_at is not _UNSET
                    else None
                )
                new_count = message_count if message_count is not None else 0
                new_preview = preview if preview is not None else ""
                await self._db.execute(
                    "INSERT INTO webchat_session_meta("
                    "token_name, session_id, title, title_manual, "
                    "pinned_at, deleted_at, updated_at, "
                    "message_count, preview) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        token_name,
                        session_id,
                        new_title,
                        1 if new_manual else 0,
                        new_pinned,
                        new_deleted,
                        now,
                        new_count,
                        new_preview,
                    ),
                )
            else:
                sets: list[str] = []
                args: list = []
                if title is not None:
                    sets.append("title = ?")
                    args.append(title)
                if title_manual is not None:
                    sets.append("title_manual = ?")
                    args.append(1 if title_manual else 0)
                if pinned_at is not _UNSET:
                    sets.append("pinned_at = ?")
                    args.append(pinned_at)
                if deleted_at is not _UNSET:
                    sets.append("deleted_at = ?")
                    args.append(deleted_at)
                if message_count is not None:
                    sets.append("message_count = ?")
                    args.append(message_count)
                if preview is not None:
                    sets.append("preview = ?")
                    args.append(preview)
                # updated_at is always rewritten so list/sort by updated_at
                # reflects the write — not the last user-meaningful change.
                sets.append("updated_at = ?")
                args.append(now)
                args.extend([token_name, session_id])
                await self._db.execute(
                    f"UPDATE webchat_session_meta SET {', '.join(sets)} "
                    "WHERE token_name = ? AND session_id = ?",
                    args,
                )
            async with self._db.execute(
                "SELECT * FROM webchat_session_meta "
                "WHERE token_name = ? AND session_id = ?",
                (token_name, session_id),
            ) as cursor:
                row = await cursor.fetchone()
            await self._db.commit()
        if row is None:
            raise RuntimeError("upsert_session_meta: row vanished after write")
        return self._row_to_session_meta(row)

    async def get_session_meta(
        self, *, token_name: str, session_id: str
    ) -> SessionMetaRow | None:
        async with self._db.execute(
            "SELECT * FROM webchat_session_meta "
            "WHERE token_name = ? AND session_id = ?",
            (token_name, session_id),
        ) as cursor:
            row = await cursor.fetchone()
        return self._row_to_session_meta(row) if row else None

    async def list_session_meta(
        self, *, token_name: str, include_deleted: bool = False
    ) -> list[SessionMetaRow]:
        if include_deleted:
            sql = (
                "SELECT * FROM webchat_session_meta WHERE token_name = ? "
                "ORDER BY updated_at DESC"
            )
        else:
            sql = (
                "SELECT * FROM webchat_session_meta "
                "WHERE token_name = ? AND deleted_at IS NULL "
                "ORDER BY updated_at DESC"
            )
        async with self._db.execute(sql, (token_name,)) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_session_meta(r) for r in rows]

    async def append_updates(
        self,
        *,
        token_name: str,
        events: list[NewEvent],
        now: int,
    ) -> list[int]:
        if not events:
            return []
        async with self._write_lock:
            # _write_lock serializes writes for the whole connection, so the
            # SELECT MAX(pts) and the INSERT batch run as a contiguous block;
            # no concurrent appender can slip a row in between. The PK guard
            # below stays as belt-and-braces against a future change to the
            # locking model (parallel connections, async pool, etc.).
            assigned: list[int] = []
            for attempt in range(2):
                assigned = []
                async with self._db.execute(
                    "SELECT COALESCE(MAX(pts), 0) AS m "
                    "FROM webchat_updates WHERE token_name = ?",
                    (token_name,),
                ) as cursor:
                    row = await cursor.fetchone()
                base = int(row["m"]) if row else 0
                try:
                    await self._db.execute("BEGIN")
                    for i, ev in enumerate(events):
                        pts = base + i + 1
                        await self._db.execute(
                            "INSERT INTO webchat_updates("
                            "token_name, pts, ts, event_type, "
                            "session_id, payload) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                token_name,
                                pts,
                                now,
                                ev.event_type,
                                ev.session_id,
                                ev.payload,
                            ),
                        )
                        assigned.append(pts)
                    await self._db.commit()
                    return assigned
                except aiosqlite.IntegrityError:
                    try:
                        await self._db.rollback()
                    except Exception:
                        pass
                    if attempt == 0:
                        # PK collision means another writer raced us under
                        # the same lock — only possible if the lock is ever
                        # bypassed. Recompute MAX(pts) once and retry.
                        continue
                    raise
            return assigned

    async def get_updates(
        self,
        *,
        token_name: str,
        since_pts: int,
        limit: int,
    ) -> list[UpdateRow]:
        limit = max(1, min(limit, 500))
        async with self._db.execute(
            "SELECT token_name, pts, ts, event_type, session_id, payload "
            "FROM webchat_updates "
            "WHERE token_name = ? AND pts > ? "
            "ORDER BY pts ASC LIMIT ?",
            (token_name, since_pts, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            UpdateRow(
                token_name=r["token_name"],
                pts=int(r["pts"]),
                ts=int(r["ts"]),
                event_type=r["event_type"],
                session_id=r["session_id"],
                payload=r["payload"] or "{}",
            )
            for r in rows
        ]

    async def get_max_pts(self, *, token_name: str) -> int:
        async with self._db.execute(
            "SELECT COALESCE(MAX(pts), 0) AS m FROM webchat_updates "
            "WHERE token_name = ?",
            (token_name,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row["m"]) if row else 0

    async def prune_chat_sync(
        self,
        *,
        events_before_ts: int,
        deleted_meta_before_ts: int,
    ) -> tuple[int, int]:
        async with self._write_lock:
            cur = await self._db.execute(
                "DELETE FROM webchat_updates WHERE ts < ?",
                (events_before_ts,),
            )
            events_pruned = cur.rowcount or 0
            cur = await self._db.execute(
                "DELETE FROM webchat_session_meta "
                "WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                (deleted_meta_before_ts,),
            )
            meta_pruned = cur.rowcount or 0
            await self._db.commit()
        return events_pruned, meta_pruned
