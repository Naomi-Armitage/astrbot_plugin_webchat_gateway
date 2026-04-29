"""aiomysql storage backend (lazy-imported)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import AsyncIterator
from urllib.parse import unquote, urlparse

import aiomysql

from .base import AbstractStorage, AuditRow, TokenRow, UsageRow
from .ddl import CURRENT_SCHEMA_VERSION, SCHEMA_MYSQL


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
        self._pool = await aiomysql.create_pool(
            minsize=1,
            maxsize=5,
            pool_recycle=3600,
            **self._kwargs,
        )
        async with self._write_tx() as conn:
            async with conn.cursor() as cur:
                for stmt in SCHEMA_MYSQL:
                    await cur.execute(stmt)
                await cur.execute(
                    "INSERT IGNORE INTO _schema_meta(`key`, value) VALUES('schema_version', %s)",
                    (CURRENT_SCHEMA_VERSION,),
                )

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
    ) -> None:
        async with self._write_tx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO tokens(name, token_hash, daily_quota, note, created_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (name, token_hash, daily_quota, note, now),
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
