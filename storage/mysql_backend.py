"""aiomysql storage backend (lazy-imported)."""

from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import unquote, urlparse

import aiomysql

from .base import AbstractStorage, AuditRow, TokenRow, UsageRow
from .ddl import SCHEMA_MYSQL


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
            minsize=1, maxsize=5, **self._kwargs
        )
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cur:
                for stmt in SCHEMA_MYSQL:
                    await cur.execute(stmt)
            await conn.commit()

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
        async with self._db.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO tokens(name, token_hash, daily_quota, note, created_at) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (name, token_hash, daily_quota, note, now),
                )
            await conn.commit()

    async def get_token_by_hash(self, token_hash: str) -> TokenRow | None:
        async with self._db.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT * FROM tokens WHERE token_hash = %s", (token_hash,)
                )
                row = await cur.fetchone()
        return self._row_to_token(row) if row else None

    async def get_token_by_name(self, name: str) -> TokenRow | None:
        async with self._db.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute("SELECT * FROM tokens WHERE name = %s", (name,))
                row = await cur.fetchone()
        return self._row_to_token(row) if row else None

    async def revoke_token(self, name: str, *, now: int) -> bool:
        async with self._db.acquire() as conn:
            async with conn.cursor() as cur:
                affected = await cur.execute(
                    "UPDATE tokens SET revoked_at = %s "
                    "WHERE name = %s AND revoked_at IS NULL",
                    (now, name),
                )
            await conn.commit()
        return bool(affected)

    async def list_tokens(self, *, include_revoked: bool = False) -> list[TokenRow]:
        sql = "SELECT * FROM tokens"
        if not include_revoked:
            sql += " WHERE revoked_at IS NULL"
        sql += " ORDER BY created_at DESC"
        async with self._db.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(sql)
                rows = await cur.fetchall()
        return [self._row_to_token(r) for r in rows]

    # ----- daily usage -----
    async def increment_daily_usage(self, name: str, *, day: date) -> int:
        async with self._db.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO daily_usage(name, day, count) VALUES(%s, %s, 1) "
                    "ON DUPLICATE KEY UPDATE count = count + 1",
                    (name, day),
                )
                await cur.execute(
                    "SELECT count FROM daily_usage WHERE name = %s AND day = %s",
                    (name, day),
                )
                row = await cur.fetchone()
            await conn.commit()
        return int(row[0]) if row else 0

    async def get_today_usage(self, name: str, *, day: date) -> int:
        async with self._db.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT count FROM daily_usage WHERE name = %s AND day = %s",
                    (name, day),
                )
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def get_usage_stats(self, name: str, *, days: int) -> list[UsageRow]:
        days = max(1, min(days, 365))
        today = date.today()
        first = today - timedelta(days=days - 1)
        async with self._db.acquire() as conn:
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
        async with self._db.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT fail_count FROM ip_failures WHERE ip = %s FOR UPDATE",
                    (ip,),
                )
                row = await cur.fetchone()
                if row is None:
                    await cur.execute(
                        "INSERT INTO ip_failures(ip, fail_count, first_fail_ts, last_fail_ts, blocked_until) "
                        "VALUES (%s, 1, %s, %s, 0)",
                        (ip, now, now),
                    )
                    new_count = 1
                else:
                    new_count = int(row[0]) + 1
                    blocked_until = now + block_seconds if new_count >= max_fails else 0
                    await cur.execute(
                        "UPDATE ip_failures SET fail_count = %s, last_fail_ts = %s, blocked_until = %s "
                        "WHERE ip = %s",
                        (new_count, now, blocked_until, ip),
                    )
            await conn.commit()
        return new_count

    async def is_ip_blocked(self, ip: str, *, now: int) -> tuple[bool, int]:
        async with self._db.acquire() as conn:
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
        async with self._db.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM ip_failures WHERE ip = %s", (ip,))
            await conn.commit()

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
        async with self._db.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO audit_log(ts, name, ip, event, detail) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (ts, name, ip, event, detail or ""),
                )
            await conn.commit()

    async def get_recent_audit(self, *, limit: int) -> list[AuditRow]:
        limit = max(1, min(limit, 500))
        async with self._db.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT id, ts, name, ip, event, detail FROM audit_log "
                    "ORDER BY id DESC LIMIT %s",
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
