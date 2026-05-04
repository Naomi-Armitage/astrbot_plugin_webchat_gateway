"""aiomysql storage backend (lazy-imported)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import AsyncIterator
from urllib.parse import unquote, urlparse

import aiomysql
from pymysql.constants import CLIENT as _MYSQL_CLIENT_FLAGS

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
    ALTER_META_ADD_COUNT_MYSQL,
    ALTER_META_ADD_PREVIEW_MYSQL,
    ALTER_TOKENS_ADD_EXPIRES_AT_MYSQL,
    ALTER_UPDATES_ADD_TS_INDEX_MYSQL,
    CURRENT_SCHEMA_VERSION,
    SCHEMA_MYSQL,
    V2_TO_V3_MYSQL,
)


# MySQL error 1060: Duplicate column name. Catching by code (rather than
# string-matching the message) keeps the migration robust against locale-
# translated server messages.
_ERR_DUP_COLUMN = 1060
# MySQL error 1061: Duplicate key name (re-running CREATE INDEX).
_ERR_DUP_KEY_NAME = 1061


def _parse_dsn(dsn: str) -> dict:
    """Parse mysql://user:pass@host:port/dbname into aiomysql kwargs."""
    parsed = urlparse(dsn)
    if parsed.scheme not in {"mysql", "mariadb"}:
        raise ValueError(f"unsupported DSN scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError("DSN missing host")
    db = (parsed.path or "").lstrip("/")
    if not db:
        raise ValueError("DSN missing database name")
    return {
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or "root"),
        "password": unquote(parsed.password or ""),
        "db": db,
        "charset": "utf8mb4",
        "autocommit": False,
    }


class MysqlStorage(AbstractStorage):
    """MySQL/MariaDB storage using aiomysql connection pool."""

    def __init__(self, dsn: str) -> None:
        self._kwargs = _parse_dsn(dsn)
        self._pool: aiomysql.Pool | None = None

    async def initialize(self) -> None:
        # CLIENT.FOUND_ROWS switches MySQL's UPDATE rowcount semantics from
        # "rows changed" to "rows matched". Without it, an UPDATE that sets
        # a column to its current value reports rowcount=0, which storage
        # callers here interpret as "row missing" — incorrectly turning
        # set_token_revoked(False) on a non-revoked token (NULL→NULL) into
        # a 404. Switching to FOUND_ROWS aligns mysql with sqlite's default
        # "matched" semantics for these methods. The original revoke_token
        # SQL keeps `AND revoked_at IS NULL` in its WHERE clause so its
        # "did we transition" semantic is unaffected by this flag.
        self._pool = await aiomysql.create_pool(
            minsize=1,
            maxsize=5,
            pool_recycle=3600,
            client_flag=_MYSQL_CLIENT_FLAGS.FOUND_ROWS,
            **self._kwargs,
        )
        async with self._write_tx() as conn:
            async with conn.cursor() as cur:
                for stmt in SCHEMA_MYSQL:
                    await cur.execute(stmt)
                await cur.execute(
                    "SELECT value FROM _schema_meta WHERE `key` = 'schema_version'"
                )
                row = await cur.fetchone()
                stored = row[0] if row else None
                if stored is None:
                    await cur.execute(
                        "INSERT INTO _schema_meta(`key`, value) VALUES('schema_version', %s)",
                        (CURRENT_SCHEMA_VERSION,),
                    )
                else:
                    if stored == "1":
                        try:
                            await cur.execute(ALTER_TOKENS_ADD_EXPIRES_AT_MYSQL)
                        except aiomysql.OperationalError as exc:
                            # exc.args[0] is the MySQL error code on aiomysql.
                            if not exc.args or exc.args[0] != _ERR_DUP_COLUMN:
                                raise
                        stored = "2"
                    if stored == "2":
                        # v2 → v3: webchat_session_meta + webchat_updates.
                        # CREATE TABLE IF NOT EXISTS is idempotent; SCHEMA_MYSQL
                        # already ran the same statements above.
                        for stmt in V2_TO_V3_MYSQL:
                            await cur.execute(stmt)
                        stored = "3"
                    if stored == "3":
                        # v3 → v4: cache message_count + preview on session_meta
                        # to drop the N+1 CM read in list_conversations, plus an
                        # index on webchat_updates(ts) so the retention prune
                        # range-scans instead of full-scans.
                        for alter in (
                            ALTER_META_ADD_COUNT_MYSQL,
                            ALTER_META_ADD_PREVIEW_MYSQL,
                        ):
                            try:
                                await cur.execute(alter)
                            except aiomysql.OperationalError as exc:
                                if not exc.args or exc.args[0] != _ERR_DUP_COLUMN:
                                    raise
                        try:
                            await cur.execute(ALTER_UPDATES_ADD_TS_INDEX_MYSQL)
                        except aiomysql.OperationalError as exc:
                            if not exc.args or exc.args[0] != _ERR_DUP_KEY_NAME:
                                raise
                        stored = "4"
                    await cur.execute(
                        "UPDATE _schema_meta SET value = %s WHERE `key` = 'schema_version'",
                        (CURRENT_SCHEMA_VERSION,),
                    )
                # stored == CURRENT_SCHEMA_VERSION (or any future version): no-op.

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    @property
    def _db(self) -> aiomysql.Pool:
        if self._pool is None:
            raise RuntimeError("MysqlStorage not initialized")
        return self._pool

    @asynccontextmanager
    async def _write_tx(self) -> AsyncIterator[aiomysql.Connection]:
        # autocommit=False on the pool means a write that raises mid-transaction
        # would leave the connection with an open transaction (and possibly
        # held row locks) when the pool reclaims it. Wrap every write in
        # commit-on-success / rollback-on-anything-else so connections always
        # return clean. BaseException covers asyncio.CancelledError, which
        # otherwise would skip the rollback path.
        async with self._db.acquire() as conn:
            try:
                yield conn
                await conn.commit()
            except BaseException:
                try:
                    await conn.rollback()
                except Exception:
                    pass
                raise

    @asynccontextmanager
    async def _read_tx(self) -> AsyncIterator[aiomysql.Connection]:
        # autocommit=False starts an implicit transaction on the first SELECT
        # under REPEATABLE READ. Without an explicit rollback, the pooled
        # connection keeps that transaction (and its snapshot) open across
        # checkout boundaries, so the next caller can read stale data and
        # MVCC undo logs grow unboundedly. Always close the read transaction
        # before returning the connection to the pool.
        async with self._db.acquire() as conn:
            try:
                yield conn
            finally:
                try:
                    await conn.rollback()
                except Exception:
                    pass

    @staticmethod
    def _row_to_token(row: dict) -> TokenRow:
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
        async with self._write_tx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO tokens(name, token_hash, daily_quota, note, created_at, expires_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (name, token_hash, daily_quota, note, now, expires_at),
                )

    async def get_token_by_hash(self, token_hash: str) -> TokenRow | None:
        async with self._read_tx() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM tokens WHERE token_hash = %s", (token_hash,)
                )
                row = await cur.fetchone()
        return self._row_to_token(row) if row else None

    async def get_token_by_name(self, name: str) -> TokenRow | None:
        async with self._read_tx() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("SELECT * FROM tokens WHERE name = %s", (name,))
                row = await cur.fetchone()
        return self._row_to_token(row) if row else None

    async def revoke_token(self, name: str, *, now: int) -> bool:
        async with self._write_tx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE tokens SET revoked_at = %s "
                    "WHERE name = %s AND revoked_at IS NULL",
                    (now, name),
                )
                affected = cur.rowcount
        return affected > 0

    async def list_tokens(self, *, include_revoked: bool = False) -> list[TokenRow]:
        sql = "SELECT * FROM tokens"
        if not include_revoked:
            sql += " WHERE revoked_at IS NULL"
        sql += " ORDER BY created_at DESC"
        async with self._read_tx() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql)
                rows = await cur.fetchall()
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
            sets.append("daily_quota = %s")
            args.append(daily_quota)
        if note is not None:
            sets.append("note = %s")
            args.append(note)
        if expires_at is not _UNSET:
            sets.append("expires_at = %s")
            args.append(expires_at)
        if not sets:
            async with self._read_tx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT 1 FROM tokens WHERE name = %s", (name,)
                    )
                    return await cur.fetchone() is not None
        args.append(name)
        sql = f"UPDATE tokens SET {', '.join(sets)} WHERE name = %s"
        async with self._write_tx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, args)
                affected = cur.rowcount
        return affected > 0

    async def set_token_revoked(
        self, name: str, *, revoked: bool, now: int
    ) -> bool:
        async with self._write_tx() as conn:
            async with conn.cursor() as cur:
                if revoked:
                    await cur.execute(
                        "UPDATE tokens SET revoked_at = %s WHERE name = %s",
                        (now, name),
                    )
                else:
                    await cur.execute(
                        "UPDATE tokens SET revoked_at = NULL WHERE name = %s",
                        (name,),
                    )
                affected = cur.rowcount
        return affected > 0

    async def regenerate_token(self, name: str, new_token_hash: str) -> bool:
        async with self._write_tx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE tokens SET token_hash = %s WHERE name = %s",
                    (new_token_hash, name),
                )
                affected = cur.rowcount
        return affected > 0

    async def rename_token(self, old_name: str, new_name: str) -> bool:
        if old_name == new_name:
            async with self._read_tx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT 1 FROM tokens WHERE name = %s", (old_name,)
                    )
                    return await cur.fetchone() is not None
        async with self._write_tx() as conn:
            async with conn.cursor() as cur:
                # SELECT FOR UPDATE locks both rows together so a parallel
                # rename can't slip in between the existence check and the
                # cascade. _write_tx commits on success, rolls back otherwise.
                await cur.execute(
                    "SELECT name FROM tokens WHERE name IN (%s, %s) FOR UPDATE",
                    (old_name, new_name),
                )
                found = {r[0] for r in await cur.fetchall()}
                if old_name not in found:
                    return False
                if new_name in found:
                    return False
                await cur.execute(
                    "UPDATE tokens SET name = %s WHERE name = %s",
                    (new_name, old_name),
                )
                await cur.execute(
                    "UPDATE daily_usage SET name = %s WHERE name = %s",
                    (new_name, old_name),
                )
                await cur.execute(
                    "UPDATE audit_log SET name = %s WHERE name = %s",
                    (new_name, old_name),
                )
        return True

    # ----- daily usage -----
    async def increment_daily_usage(self, name: str, *, day: date) -> int:
        # Single round-trip atomic +1 with the new value returned via LAST_INSERT_ID().
        # See https://dev.mysql.com/doc/refman/8.0/en/information-functions.html#function_last-insert-id
        async with self._write_tx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO daily_usage(name, day, count) VALUES(%s, %s, 1) "
                    "ON DUPLICATE KEY UPDATE count = LAST_INSERT_ID(count + 1)",
                    (name, day),
                )
                new_count = cur.lastrowid
        return int(new_count or 1)

    async def get_today_usage(self, name: str, *, day: date) -> int:
        async with self._read_tx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT count FROM daily_usage WHERE name = %s AND day = %s",
                    (name, day),
                )
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def get_today_usage_bulk(
        self, names: list[str], *, day: date
    ) -> dict[str, int]:
        if not names:
            return {}
        placeholders = ",".join(["%s"] * len(names))
        sql = (
            f"SELECT name, count FROM daily_usage "
            f"WHERE day = %s AND name IN ({placeholders})"
        )
        async with self._read_tx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (day, *names))
                rows = await cur.fetchall()
        out = {n: 0 for n in names}
        for row in rows:
            out[row[0]] = int(row[1])
        return out

    async def get_usage_stats(self, name: str, *, days: int) -> list[UsageRow]:
        days = max(1, min(days, 365))
        today = date.today()
        first = today - timedelta(days=days - 1)
        async with self._read_tx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT day, count FROM daily_usage "
                    "WHERE name = %s AND day >= %s AND day <= %s "
                    "ORDER BY day ASC",
                    (name, first, today),
                )
                rows = await cur.fetchall()
        existing = {r[0]: int(r[1]) for r in rows}
        out: list[UsageRow] = []
        for offset in range(days):
            d = first + timedelta(days=offset)
            # MySQL DATE comes back as datetime.date already
            out.append(UsageRow(name=name, day=d, count=existing.get(d, 0)))
        return out

    # ----- ip failures -----
    async def record_ip_failure(
        self, ip: str, *, now: int, max_fails: int, block_seconds: int
    ) -> int:
        if max_fails <= 0:
            return 0
        # Race protection: SELECT ... FOR UPDATE locks no row when the IP is
        # absent (no gap lock guarantee for PK lookups under read-committed),
        # so two concurrent first-failures can both attempt INSERT and the
        # second hits a duplicate-key error. We catch IntegrityError on the
        # INSERT branch and fall through to the UPDATE branch — net effect
        # is identical to having lost the SELECT race.
        async with self._write_tx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT fail_count FROM ip_failures WHERE ip = %s FOR UPDATE",
                    (ip,),
                )
                row = await cur.fetchone()
                if row is None:
                    try:
                        await cur.execute(
                            "INSERT INTO ip_failures(ip, fail_count, first_fail_ts, last_fail_ts, blocked_until) "
                            "VALUES (%s, 1, %s, %s, 0)",
                            (ip, now, now),
                        )
                        new_count = 1
                    except aiomysql.IntegrityError:
                        await cur.execute(
                            "SELECT fail_count FROM ip_failures WHERE ip = %s FOR UPDATE",
                            (ip,),
                        )
                        row = await cur.fetchone()
                        new_count = int(row[0]) + 1 if row else 1
                        new_blocked_until = (
                            now + block_seconds if new_count >= max_fails else None
                        )
                        await cur.execute(
                            "UPDATE ip_failures "
                            "SET fail_count = %s, last_fail_ts = %s, "
                            "    blocked_until = CASE WHEN %s >= %s THEN %s ELSE blocked_until END "
                            "WHERE ip = %s",
                            (new_count, now, new_count, max_fails, new_blocked_until or 0, ip),
                        )
                else:
                    new_count = int(row[0]) + 1
                    new_blocked_until = now + block_seconds if new_count >= max_fails else None
                    await cur.execute(
                        "UPDATE ip_failures "
                        "SET fail_count = %s, last_fail_ts = %s, "
                        "    blocked_until = CASE WHEN %s >= %s THEN %s ELSE blocked_until END "
                        "WHERE ip = %s",
                        (new_count, now, new_count, max_fails, new_blocked_until or 0, ip),
                    )
        return new_count

    async def is_ip_blocked(self, ip: str, *, now: int) -> tuple[bool, int]:
        async with self._read_tx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT blocked_until FROM ip_failures WHERE ip = %s", (ip,)
                )
                row = await cur.fetchone()
        if not row:
            return False, 0
        blocked_until = int(row[0])
        if blocked_until > now:
            return True, blocked_until - now
        return False, 0

    async def reset_ip_failures(self, ip: str) -> None:
        async with self._write_tx() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM ip_failures WHERE ip = %s", (ip,))

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
        async with self._write_tx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO audit_log(ts, name, ip, event, detail) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (ts, name, ip, event, detail or ""),
                )

    async def get_recent_audit(self, *, limit: int) -> list[AuditRow]:
        limit = max(1, min(limit, 500))
        async with self._read_tx() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT id, ts, name, ip, event, detail FROM audit_log "
                    "ORDER BY ts DESC, id DESC LIMIT %s",
                    (limit,),
                )
                rows = await cur.fetchall()
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
    def _row_to_session_meta(row: dict) -> SessionMetaRow:
        # message_count / preview were added in v4; tolerate missing keys on
        # rows fetched between the migration ALTER and any later refresh.
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
            message_count=int(row.get("message_count") or 0),
            preview=row.get("preview") or "",
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
        async with self._write_tx() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                # SELECT FOR UPDATE locks the existing row (if any) so a
                # parallel upsert serializes; if absent, the unique PK on
                # INSERT below catches the race and we fall through to the
                # UPDATE branch via IntegrityError.
                await cur.execute(
                    "SELECT * FROM webchat_session_meta "
                    "WHERE token_name = %s AND session_id = %s FOR UPDATE",
                    (token_name, session_id),
                )
                existing = await cur.fetchone()
                if existing is None:
                    new_title = title if title is not None else ""
                    new_manual = (
                        bool(title_manual) if title_manual is not None else False
                    )
                    new_pinned = pinned_at if pinned_at is not _UNSET else None
                    new_deleted = deleted_at if deleted_at is not _UNSET else None
                    new_count = message_count if message_count is not None else 0
                    new_preview = preview if preview is not None else ""
                    try:
                        await cur.execute(
                            "INSERT INTO webchat_session_meta("
                            "token_name, session_id, title, title_manual, "
                            "pinned_at, deleted_at, updated_at, "
                            "message_count, preview) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
                    except aiomysql.IntegrityError:
                        # Lost the existence race; re-read and fall through
                        # to the UPDATE branch as if `existing` had been
                        # populated on the first SELECT.
                        await cur.execute(
                            "SELECT * FROM webchat_session_meta "
                            "WHERE token_name = %s AND session_id = %s FOR UPDATE",
                            (token_name, session_id),
                        )
                        existing = await cur.fetchone()
                if existing is not None:
                    sets: list[str] = []
                    args: list = []
                    if title is not None:
                        sets.append("title = %s")
                        args.append(title)
                    if title_manual is not None:
                        sets.append("title_manual = %s")
                        args.append(1 if title_manual else 0)
                    if pinned_at is not _UNSET:
                        sets.append("pinned_at = %s")
                        args.append(pinned_at)
                    if deleted_at is not _UNSET:
                        sets.append("deleted_at = %s")
                        args.append(deleted_at)
                    if message_count is not None:
                        sets.append("message_count = %s")
                        args.append(message_count)
                    if preview is not None:
                        sets.append("preview = %s")
                        args.append(preview)
                    sets.append("updated_at = %s")
                    args.append(now)
                    args.extend([token_name, session_id])
                    await cur.execute(
                        f"UPDATE webchat_session_meta SET {', '.join(sets)} "
                        "WHERE token_name = %s AND session_id = %s",
                        args,
                    )
                await cur.execute(
                    "SELECT * FROM webchat_session_meta "
                    "WHERE token_name = %s AND session_id = %s",
                    (token_name, session_id),
                )
                row = await cur.fetchone()
        if row is None:
            raise RuntimeError("upsert_session_meta: row vanished after write")
        return self._row_to_session_meta(row)

    async def get_session_meta(
        self, *, token_name: str, session_id: str
    ) -> SessionMetaRow | None:
        async with self._read_tx() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM webchat_session_meta "
                    "WHERE token_name = %s AND session_id = %s",
                    (token_name, session_id),
                )
                row = await cur.fetchone()
        return self._row_to_session_meta(row) if row else None

    async def list_session_meta(
        self, *, token_name: str, include_deleted: bool = False
    ) -> list[SessionMetaRow]:
        if include_deleted:
            sql = (
                "SELECT * FROM webchat_session_meta WHERE token_name = %s "
                "ORDER BY updated_at DESC"
            )
        else:
            sql = (
                "SELECT * FROM webchat_session_meta "
                "WHERE token_name = %s AND deleted_at IS NULL "
                "ORDER BY updated_at DESC"
            )
        async with self._read_tx() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql, (token_name,))
                rows = await cur.fetchall()
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
        async with self._write_tx() as conn:
            async with conn.cursor() as cur:
                # SELECT MAX(pts) FOR UPDATE acquires a next-key lock on the
                # token's index range under InnoDB's default REPEATABLE READ,
                # so a concurrent appender for the same token blocks here
                # until our INSERTs commit. The PK collision retry below is
                # belt-and-braces — it only fires if isolation drops to
                # READ COMMITTED (no gap locks) and we lose the race.
                assigned: list[int] = []
                for attempt in range(2):
                    assigned = []
                    await cur.execute(
                        "SELECT COALESCE(MAX(pts), 0) "
                        "FROM webchat_updates WHERE token_name = %s FOR UPDATE",
                        (token_name,),
                    )
                    row = await cur.fetchone()
                    base = int(row[0]) if row else 0
                    try:
                        for i, ev in enumerate(events):
                            pts = base + i + 1
                            await cur.execute(
                                "INSERT INTO webchat_updates("
                                "token_name, pts, ts, event_type, "
                                "session_id, payload) "
                                "VALUES (%s, %s, %s, %s, %s, %s)",
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
                        return assigned
                    except aiomysql.IntegrityError:
                        if attempt == 0:
                            # Roll back the partial transaction so the retry
                            # doesn't observe phantom rows from this batch.
                            await conn.rollback()
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
        async with self._read_tx() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT token_name, pts, ts, event_type, session_id, payload "
                    "FROM webchat_updates "
                    "WHERE token_name = %s AND pts > %s "
                    "ORDER BY pts ASC LIMIT %s",
                    (token_name, since_pts, limit),
                )
                rows = await cur.fetchall()
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
        async with self._read_tx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT COALESCE(MAX(pts), 0) FROM webchat_updates "
                    "WHERE token_name = %s",
                    (token_name,),
                )
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def prune_chat_sync(
        self,
        *,
        events_before_ts: int,
        deleted_meta_before_ts: int,
    ) -> tuple[int, int]:
        async with self._write_tx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM webchat_updates WHERE ts < %s",
                    (events_before_ts,),
                )
                events_pruned = cur.rowcount or 0
                await cur.execute(
                    "DELETE FROM webchat_session_meta "
                    "WHERE deleted_at IS NOT NULL AND deleted_at < %s",
                    (deleted_meta_before_ts,),
                )
                meta_pruned = cur.rowcount or 0
        return events_pruned, meta_pruned
