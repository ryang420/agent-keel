"""Open the store named by a database URL."""

from __future__ import annotations

import time
from typing import Callable

from keel.store.db import SqliteDb
from keel.store.sql import SqlStore


async def open_store(url: str, clock: Callable[[], float] = time.time) -> SqlStore:
    if url.startswith("sqlite:///"):
        db = await SqliteDb.connect(url[len("sqlite:///"):])
    elif url.startswith(("postgresql://", "postgres://")):
        from keel.store.postgres import PostgresDb

        db = await PostgresDb.connect(url)
    else:
        raise ValueError(f"unsupported database URL: {url.split(':', 1)[0]}")
    store = SqlStore(db, clock=clock)
    await store.init()
    return store
