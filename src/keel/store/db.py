"""A minimal async database adapter, so one set of queries serves SQLite and Postgres."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import sqlite3
from typing import AsyncIterator, Protocol

import aiosqlite

from keel.store.base import UniqueViolation


class Db(Protocol):
    dialect: str

    async def execute(self, sql: str, *args) -> None: ...
    async def fetchrow(self, sql: str, *args) -> dict | None: ...
    async def fetch(self, sql: str, *args) -> list[dict]: ...
    async def executescript(self, script: str) -> None: ...
    def transaction(self): ...
    async def close(self) -> None: ...


class SqliteDb:
    """One connection, serialized by a lock; queries inside transaction() share it."""

    dialect = "sqlite"

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()
        self._in_tx: ContextVar[bool] = ContextVar(f"keel_sqlite_tx_{id(self)}", default=False)

    @classmethod
    async def connect(cls, path: str) -> "SqliteDb":
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        # foreign_keys is ignored inside a transaction, so it is set and committed with journal_mode.
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.commit()
        return cls(conn)

    async def _run(self, operation):
        if self._in_tx.get():
            return await operation()
        async with self._lock:
            try:
                result = await operation()
            except BaseException:
                await self._conn.rollback()
                raise
            await self._conn.commit()
            return result

    async def _query(self, sql: str, args: tuple, many: bool):
        try:
            async with self._conn.execute(sql, args) as cursor:
                rows = await cursor.fetchall()
        except sqlite3.IntegrityError as exc:
            if "UNIQUE" not in str(exc):
                raise
            raise UniqueViolation(str(exc)) from exc
        rows = [dict(row) for row in rows]
        return rows if many else (rows[0] if rows else None)

    async def execute(self, sql: str, *args) -> None:
        await self._run(lambda: self._query(sql, args, True))

    async def fetchrow(self, sql: str, *args) -> dict | None:
        return await self._run(lambda: self._query(sql, args, False))

    async def fetch(self, sql: str, *args) -> list[dict]:
        return await self._run(lambda: self._query(sql, args, True))

    async def executescript(self, script: str) -> None:
        async with self._lock:
            await self._conn.executescript(script)
            await self._conn.commit()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        if self._in_tx.get():
            yield
            return
        async with self._lock:
            token = self._in_tx.set(True)
            try:
                try:
                    await self._conn.execute("BEGIN IMMEDIATE")
                    yield
                except BaseException:
                    await self._conn.rollback()
                    raise
                await self._conn.commit()
            finally:
                self._in_tx.reset(token)

    async def close(self) -> None:
        await self._conn.close()
