"""aiosqlite storage backend."""

from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta

import aiosqlite

from .base import AbstractStorage, AuditRow, TokenRow, UsageRow
from .ddl import SCHEMA_SQLITE


class SqliteStorage(AbstractStorage):
    """File-based SQLite storage with WAL mode."""

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
        async with self._write_lock:
            await self._db.execute(
                "INSERT INTO tokens(name, token_hash, daily_quota, note, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, token_hash, daily_quota, note, now),
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

    # ----- daily usage -----
    async def increment_daily_usage(self, name: str, *, day: date) -> int:
        day_key = day.isoformat()
        async with self._write_lock:
            async with self._db.execute(
                "INSERT INTO daily_usage(name, day, count) VALUES(?, ?, 1) "
                "ON CONFLICT(name, day) DO UPDATE SET count = count + 1 "
                "RETURNING count",
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
                blocked_until = now + block_seconds if new_count >= max_fails else 0
                await self._db.execute(
                    "UPDATE ip_failures SET fail_count = ?, last_fail_ts = ?, blocked_until = ? "
                    "WHERE ip = ?",
                    (new_count, now, blocked_until, ip),
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
            "ORDER BY id DESC LIMIT ?",
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
