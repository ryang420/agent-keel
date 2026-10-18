"""Open the store named by a database URL."""

from __future__ import annotations

import time
from typing import Callable

from keel.store.db import SqliteDb
from keel.store.ownership import require_tenant_ownership
from keel.store.sql import SqlStore


async def open_store(url: str, clock: Callable[[], float] = time.time, *,
                     coordination_clock: Callable[[], float] | None = None,
                     command_timeout: float | None = None, tenant_ids: tuple[int, ...] | None = None,
                     tenant_backfill: dict[str, int] | None = None) -> SqlStore:
    """Open the store; service startup migrates and enforces ownership atomically when tenant_ids are provided."""
    if url.startswith("sqlite:///"):
        db = await SqliteDb.connect(url[len("sqlite:///"):])
    elif url.startswith(("postgresql://", "postgres://")):
        from keel.store.postgres import PostgresDb

        db = await (PostgresDb.connect(url) if command_timeout is None
                    else PostgresDb.connect(url, command_timeout=command_timeout))
    else:
        raise ValueError(f"unsupported database URL: {url.split(':', 1)[0]}")
    store = SqlStore(db, clock=clock, coordination_clock=coordination_clock)
    try:
        if tenant_ids is None:
            await store.init()
        else:
            async with db.transaction():
                await store.init()
                await require_tenant_ownership(db, tenant_ids, tenant_backfill or {})
    except BaseException:
        await store.close()
        raise
    return store
