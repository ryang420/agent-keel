"""Open the store named by a database URL."""

from __future__ import annotations

import time
from typing import Callable

from keel.store.db import SqliteDb
from keel.store.sql import SqlStore


async def open_store(url: str, clock: Callable[[], float] = time.time, *,
                     command_timeout: float | None = None) -> SqlStore:
    """`command_timeout` bounds every Postgres query; SQLite ignores it."""
    if url.startswith("sqlite:///"):
        db = await SqliteDb.connect(url[len("sqlite:///"):])
    elif url.startswith(("postgresql://", "postgres://")):
        from keel.store.postgres import PostgresDb

        db = await (PostgresDb.connect(url) if command_timeout is None
                    else PostgresDb.connect(url, command_timeout=command_timeout))
    else:
        raise ValueError(f"unsupported database URL: {url.split(':', 1)[0]}")
    store = SqlStore(db, clock=clock)
    try:
        await store.init()
    except BaseException:
        await store.close()
        raise
    return store
