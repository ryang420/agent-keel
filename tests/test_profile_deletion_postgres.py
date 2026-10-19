"""PostgreSQL parity for durable deletion claims, fences and completion receipts."""

from __future__ import annotations

import asyncio
import os

import pytest

from keel.store import open_store
from keel.store.base import ProfileDeletionBusy

DSN = os.environ.get("KEEL_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(DSN is None, reason="needs KEEL_TEST_POSTGRES_DSN")
OP_A = "11111111-1111-4111-8111-111111111111"
OP_B = "22222222-2222-4222-8222-222222222222"
MODELS = [{"provider": "fake", "model": "m"}]


async def put(store, profile_id="p1"):
    return await store.upsert_profile(profile_id, models=MODELS, sandbox={}, soul="s", skills=[], tenant_id=1)


async def test_competing_deletion_claims_have_one_winner():
    from tests.sandbox_support import reset_postgres_schema

    await reset_postgres_schema(DSN)
    first = await open_store(DSN)
    second = await open_store(DSN)
    try:
        await put(first)
        results = await asyncio.gather(
            first.begin_profile_deletion("p1", 1, OP_A),
            second.begin_profile_deletion("p1", 1, OP_B),
            return_exceptions=True,
        )
        winners = [r for r in results if not isinstance(r, Exception)]
        losers = [r for r in results if isinstance(r, Exception)]
        assert len(winners) == 1
        assert isinstance(losers[0], ProfileDeletionBusy)
        assert (await second.get_profile_deletion("p1")).operation_id == winners[0].operation_id

        # Every other writer sees the fence through its own connection.
        with pytest.raises(ProfileDeletionBusy):
            await second.upsert_profile("p1", models=MODELS, sandbox={}, soul="s", skills=[], tenant_id=1)
    finally:
        await first.close()
        await second.close()


async def test_guard_and_completion_serialize_across_instances():
    from tests.sandbox_support import reset_postgres_schema

    await reset_postgres_schema(DSN)
    first = await open_store(DSN)
    second = await open_store(DSN)
    try:
        await put(first)
        record = await first.begin_profile_deletion("p1", 1, OP_A)
        record = await first.advance_profile_deletion(record, "draining")
        record = await first.advance_profile_deletion(record, "purged")
        await first.purge_profile_data(record)
        await first.complete_profile_deletion("p1", 1, OP_A)

        # The receipt is visible from the other instance and acknowledges a retry.
        await second.complete_profile_deletion("p1", 1, OP_A)
        assert await second.get_profile_deletion("p1") is None
        await put(second)
        assert (await second.get_profile("p1")).tenant_id == 1
    finally:
        await first.close()
        await second.close()
