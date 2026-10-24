"""Postgres adapter: a pool, with the connection of an open transaction carried in a context variable."""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import AsyncIterator

import asyncpg

from keel.store.base import UniqueViolation


def to_numbered(sql: str) -> str:
    """Turn `?` placeholders into Postgres's `$1, $2, …` (the queries hold no literal `?`)."""
    parts = sql.split("?")
    out = [parts[0]]
    for index, part in enumerate(parts[1:], start=1):
        out.append(f"${index}{part}")
    return "".join(out)


# A stalled database must fail a query, not hang a worker or a request forever. The service passes a third of the
# lease (at least 5 s), so one stalled query cannot use up the lease of the run waiting on it.
COMMAND_TIMEOUT = 30
CONNECT_TIMEOUT = 10


class PostgresDb:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        self._conn: ContextVar[asyncpg.Connection | None] = ContextVar(f"keel_pg_conn_{id(self)}", default=None)

    @classmethod
    async def connect(cls, dsn: str, *, command_timeout: float = COMMAND_TIMEOUT) -> "PostgresDb":
        return cls(await asyncpg.create_pool(dsn, min_size=1, max_size=10, command_timeout=command_timeout,
                                             timeout=CONNECT_TIMEOUT))

    async def _call(self, method: str, sql: str, args: tuple):
        conn = self._conn.get()
        try:
            if conn is not None:
                return await getattr(conn, method)(to_numbered(sql), *args)
            async with self._pool.acquire() as pooled:
                return await getattr(pooled, method)(to_numbered(sql), *args)
        except asyncpg.UniqueViolationError as exc:
            raise UniqueViolation(str(exc)) from exc

    async def execute(self, sql: str, *args) -> None:
        await self._call("execute", sql, args)

    async def fetchrow(self, sql: str, *args) -> dict | None:
        row = await self._call("fetchrow", sql, args)
        return dict(row) if row is not None else None

    async def fetch(self, sql: str, *args) -> list[dict]:
        return [dict(row) for row in await self._call("fetch", sql, args)]

    async def executescript(self, script: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(script)

    async def database_now(self) -> float:
        """Unix epoch seconds from the database server, unaffected by host clock skew."""
        row = await self.fetchrow("SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision AS now")
        return float(row["now"])

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        if self._conn.get() is not None:
            yield
            return
        async with self._pool.acquire() as conn:
            token = self._conn.set(conn)
            try:
                async with conn.transaction():
                    yield
            finally:
                self._conn.reset(token)

    async def close(self) -> None:
        await self._pool.close()
