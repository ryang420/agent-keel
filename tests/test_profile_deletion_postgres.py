"""PostgreSQL parity for durable deletion claims, fences and completion receipts."""

from __future__ import annotations

import asyncio

import pytest

from keel.deletion import ProfileDeletionService
from keel.store import open_store
from keel.store.base import ProfileDeletionBusy

OP_A = "11111111-1111-4111-8111-111111111111"
OP_B = "22222222-2222-4222-8222-222222222222"
MODELS = [{"provider": "fake", "model": "m"}]


async def put(store, profile_id="p1"):
    return await store.upsert_profile(profile_id, models=MODELS, sandbox={}, soul="s", skills=[], tenant_id=1)


async def test_competing_deletion_claims_have_one_winner(pg_dsn):
    first = await open_store(pg_dsn)
    second = await open_store(pg_dsn)
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


async def test_a_paused_writer_is_serialized_with_the_deletion_fence(pg_dsn):
    writer_store = await open_store(pg_dsn)
    deleter_store = await open_store(pg_dsn)
    try:
        await put(writer_store)
        entered, release = asyncio.Event(), asyncio.Event()

        async def paused_apply_memory(profile_id, target, limit, ops):
            async with writer_store.db.transaction():
                await writer_store.lock_profile(profile_id)
                await writer_store.assert_profile_admitted(profile_id)
                entered.set()  # admitted, but not yet written
                await release.wait()
                await writer_store.db.execute(
                    "INSERT INTO memory_entries (id, profile_id, target, position, text, version, state, "
                    "created_at, updated_at) VALUES (?, ?, ?, 0, ?, 1, 'active', 1.0, 1.0)",
                    "late-entry", profile_id, target, "old writer committed after purge")
                return {"success": True, "entries": []}

        writer_store.apply_memory = paused_apply_memory
        writer = asyncio.create_task(
            writer_store.apply_memory("p1", "user", 2200, [{"action": "add", "content": "old writer"}]))
        await asyncio.wait_for(entered.wait(), timeout=5)

        service = ProfileDeletionService(deleter_store)
        deletion = asyncio.create_task(service.delete("p1", 1, OP_A, timeout_seconds=5))
        await asyncio.sleep(0.2)
        assert not deletion.done(), "the fence must wait for an admitted writer to commit"

        release.set()
        await writer
        await deletion
        assert await deleter_store.get_profile("p1") is None
        assert (await deleter_store.memory_entries("p1"))["user"] == []
        assert (await deleter_store.get_profile_deletion("p1")).phase == "purged"
    finally:
        await writer_store.close()
        await deleter_store.close()


async def test_guard_and_completion_serialize_across_instances(pg_dsn):
    first = await open_store(pg_dsn)
    second = await open_store(pg_dsn)
    try:
        await put(first)
        record = await first.begin_profile_deletion("p1", 1, OP_A)
        record = await first.advance_profile_deletion(record, "draining")
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


async def test_profile_put_guard_takes_advisory_lock_before_the_profile_row(pg_dsn):
    writer = await open_store(pg_dsn)
    deleter = await open_store(pg_dsn)
    row_held, resume_writer, delete_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
    try:
        await put(writer)

        async def put_path():
            async with writer.profile_guard("lock-order"):
                row_held.set()
                await resume_writer.wait()
                await writer.upsert_profile("lock-order", models=MODELS, sandbox={}, soul="changed", skills=[],
                                            tenant_id=1)

        # The target is a second profile so the first setup call does not create it.
        await writer.upsert_profile("lock-order", models=MODELS, sandbox={}, soul="s", skills=[], tenant_id=1)
        put_task = asyncio.create_task(put_path())
        await asyncio.wait_for(row_held.wait(), 5)
        async def delete_path():
            delete_started.set()
            await ProfileDeletionService(deleter).delete("lock-order", 1, OP_A, timeout_seconds=5)

        delete_task = asyncio.create_task(delete_path())
        await asyncio.wait_for(delete_started.wait(), 5)
        # Let PostgreSQL reach its lock acquisition while the writer owns the guard. Under the old row-first order,
        # deletion can take the advisory lock and then deadlock as the writer resumes into upsert_profile.
        await asyncio.sleep(0.1)
        assert not delete_task.done()
        resume_writer.set()
        results = await asyncio.wait_for(asyncio.gather(put_task, delete_task, return_exceptions=True), 8)
        assert results == [None, None]
        assert await deleter.get_profile("lock-order") is None
    finally:
        resume_writer.set()
        await writer.close()
        await deleter.close()
