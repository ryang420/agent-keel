import asyncio
import os

import asyncpg
import pytest

from keel.store import open_store
from keel.store.postgres import to_numbered


def test_placeholders_are_numbered_in_order():
    assert to_numbered("SELECT * FROM t WHERE a = ? AND b IN (?, ?)") == \
        "SELECT * FROM t WHERE a = $1 AND b IN ($2, $3)"
    assert to_numbered("SELECT 1") == "SELECT 1"


@pytest.mark.skipif(not os.environ.get("KEEL_TEST_POSTGRES_DSN"), reason="needs KEEL_TEST_POSTGRES_DSN")
async def test_instances_starting_together_do_not_race_on_the_schema():
    url = os.environ["KEEL_TEST_POSTGRES_DSN"]
    conn = await asyncpg.connect(url)
    await conn.execute("DROP TABLE IF EXISTS schema_version, memories, run_events, runs, messages, sessions, "
                       "profiles, blobs")
    await conn.close()
    stores = await asyncio.gather(*(open_store(url) for _ in range(6)))
    for store in stores:
        assert await store.ping()
        await store.close()


async def test_connect_passes_the_command_timeout_to_the_pool(monkeypatch):
    from keel.store import postgres

    seen = {}

    async def fake_pool(dsn, **kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(postgres.asyncpg, "create_pool", fake_pool)
    await postgres.PostgresDb.connect("postgresql://unused", command_timeout=7.5)
    assert seen["command_timeout"] == 7.5
