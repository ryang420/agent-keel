"""Drain, sandbox retirement and owned-data purge for one complete profile deletion."""

import asyncio
import uuid

import pytest

from keel.deletion import (
    ProfileDeletionBusyError,
    ProfileDeletionService,
    ProfileDeletionUnavailableError,
)
from keel.models import Message, TextPart
from keel.sandbox.records import SandboxBusy
from keel.store.base import ProfileDeletionBusy

MODELS = [{"provider": "fake", "model": "m"}]
OP_A = "11111111-1111-4111-8111-111111111111"
OP_B = "22222222-2222-4222-8222-222222222222"


async def put_profile(store, profile_id="p1", tenant_id=1):
    return await store.upsert_profile(profile_id, models=MODELS, sandbox={}, soul="s", skills=[],
                                      tenant_id=tenant_id)


async def add_run(store, profile_id="p1", status="queued", session_id=None):
    session_id = session_id or f"{profile_id}-run-{uuid.uuid4().hex[:8]}"
    await store.ensure_session(profile_id, session_id)
    run = await store.create_run(profile_id=profile_id, session_id=session_id, provider="fake", model="m",
                                 reasoning_effort=None, input_parts=[{"type": "text", "text": "hi"}],
                                 instructions=None)
    if status == "running":
        await store.db.execute(
            "UPDATE runs SET status = 'running', lease_owner = 'w', lease_expires_at = ? WHERE id = ?",
            10**12, run.id)
    if status == "stopping":
        await store.db.execute(
            "UPDATE runs SET status = 'stopping', lease_owner = 'w', lease_expires_at = ? WHERE id = ?",
            10**12, run.id)
    return run


async def seeded(store):
    await put_profile(store)
    await store.ensure_session("p1", "p1-s")
    run = await add_run(store)
    await store.append_message("p1-s", run.id, Message("user", (TextPart("hi"),)))
    await store.put_blob("p1", "p1-s", "text/plain", "aGk=")
    await store.apply_memory("p1", "user", 2200, [{"action": "add", "content": "likes tea"}])
    return run


async def fake_worker(store, profile_id, stop, done=None):
    """Finish every run the service moved to stopping, as a real worker would."""
    while not stop.is_set():
        for run in await store.list_runs(profile_id, ("stopping",)):
            transition = await store.finish_run(run.id, "cancelled")
            if done is not None and transition is not None:
                done.append(transition)
        await asyncio.sleep(0.01)


class CheckingCoordinator:
    """Prove the runs are terminal before allowing the purge, then succeed."""

    def __init__(self, store, run_ids):
        self.store = store
        self.run_ids = run_ids
        self.called = False

    async def retire_profile(self, profile_id, *, timeout_seconds):
        self.called = True
        assert timeout_seconds > 0
        assert await self.store.list_runs(profile_id, ("queued", "running", "stopping")) == []
        for run_id in self.run_ids:
            terminal = [event for event in await self.store.events_after(run_id, 0)
                        if event.type in ("run.completed", "run.failed", "run.cancelled")]
            assert len(terminal) == 1, f"run {run_id} has {len(terminal)} terminal events"
            assert terminal[0].type == "run.cancelled"


class UncertainCoordinator:
    async def retire_profile(self, profile_id, *, timeout_seconds):
        raise SandboxBusy("an uncertain container removal")


class BrokenCoordinator:
    async def retire_profile(self, profile_id, *, timeout_seconds):
        raise RuntimeError("docker unavailable")


async def test_delete_cancels_queued_and_running_work(store):
    await put_profile(store)
    queued = await add_run(store)
    running = await add_run(store, status="running")
    stop = asyncio.Event()
    worker = asyncio.create_task(fake_worker(store, "p1", stop))
    coordinator = CheckingCoordinator(store, [queued.id, running.id])
    service = ProfileDeletionService(store, coordinator=coordinator)
    try:
        await service.delete("p1", 1, OP_A, timeout_seconds=5)
    finally:
        stop.set()
        await worker

    assert coordinator.called
    assert await store.get_profile("p1") is None
    record = await store.get_profile_deletion("p1")
    assert record is not None and record.phase == "purged"


async def test_uncertain_container_keeps_fence(store):
    await put_profile(store)
    await add_run(store)
    stop = asyncio.Event()
    worker = asyncio.create_task(fake_worker(store, "p1", stop))
    service = ProfileDeletionService(store, coordinator=UncertainCoordinator())
    try:
        with pytest.raises(ProfileDeletionBusyError):
            await service.delete("p1", 1, OP_A, timeout_seconds=2)
    finally:
        stop.set()
        await worker

    record = await store.get_profile_deletion("p1")
    assert record is not None and record.phase == "draining"
    assert await store.get_profile("p1") is not None, "nothing is purged while teardown is uncertain"
    with pytest.raises(Exception):
        await store.begin_profile_deletion("p1", 1, OP_B)


async def test_a_broken_dependency_keeps_the_fence_retryable(store):
    await put_profile(store)
    await add_run(store)
    stop = asyncio.Event()
    worker = asyncio.create_task(fake_worker(store, "p1", stop))
    service = ProfileDeletionService(store, coordinator=BrokenCoordinator())
    try:
        with pytest.raises(ProfileDeletionUnavailableError):
            await service.delete("p1", 1, OP_A, timeout_seconds=2)
    finally:
        stop.set()
        await worker

    record = await store.get_profile_deletion("p1")
    assert record is not None and record.phase == "draining"
    assert await store.get_profile("p1") is not None


async def test_purge_removes_owned_transcripts_memory_and_blobs(store):
    await seeded(store)
    await put_profile(store, "p2", tenant_id=2)
    await add_run(store, profile_id="p2")
    before = await store.memory_entries("p1")
    assert before["user"] == ["likes tea"]

    await ProfileDeletionService(store).delete("p1", 1, OP_A, timeout_seconds=5)

    assert await store.get_profile("p1") is None
    assert await store.list_sessions("p1") == []
    assert await store.memory_entries("p1") == {"memory": [], "user": []}
    assert await store.list_runs("p1", ("queued", "running", "stopping", "completed", "failed", "cancelled")) == []

    assert await store.get_profile("p2") is not None
    assert len(await store.list_sessions("p2")) == 1

    await ProfileDeletionService(store).complete("p1", 1, OP_A)
    assert await store.get_profile_deletion("p1") is None


async def test_background_review_cannot_repopulate_memory(store):
    await put_profile(store)
    service = ProfileDeletionService(store, coordinator=UncertainCoordinator())
    await add_run(store)
    stop = asyncio.Event()
    worker = asyncio.create_task(fake_worker(store, "p1", stop))
    try:
        with pytest.raises(ProfileDeletionBusyError):
            await service.delete("p1", 1, OP_A, timeout_seconds=2)
        with pytest.raises(ProfileDeletionBusy):
            await store.apply_memory("p1", "user", 2200, [{"action": "add", "content": "late"}])
    finally:
        stop.set()
        await worker


async def test_a_completed_reset_rejects_a_late_memory_write(store):
    await put_profile(store)
    await ProfileDeletionService(store).delete("p1", 1, OP_A, timeout_seconds=5)
    await ProfileDeletionService(store).complete("p1", 1, OP_A)

    with pytest.raises(ProfileDeletionBusy):
        await store.apply_memory("p1", "memory", 2200, [{"action": "add", "content": "late"}])

    # Re-provisioning the same deterministic id is a new incarnation and is allowed.
    await put_profile(store)
    await store.apply_memory("p1", "memory", 2200, [{"action": "add", "content": "fresh"}])
    assert (await store.memory_entries("p1"))["memory"] == ["fresh"]


async def test_workers_drain_without_guard_deadlock(store):
    await put_profile(store)
    await add_run(store)
    service = ProfileDeletionService(store)

    async def contender():
        await asyncio.sleep(0.05)
        assert await store.claim_run("worker-1", 10**12) is None
        return "no-claim"

    result, _ = await asyncio.gather(
        service.delete("p1", 1, OP_A, timeout_seconds=5),
        contender(),
        return_exceptions=False,
    )
    assert result is None
    assert await store.get_profile_deletion("p1") is not None
    assert await store.get_profile("p1") is None


async def test_duplicate_requests_converge_and_leave_one_purge(store):
    await put_profile(store)
    await add_run(store)
    service = ProfileDeletionService(store)
    results = await asyncio.gather(
        service.delete("p1", 1, OP_A, timeout_seconds=5),
        service.delete("p1", 1, OP_A, timeout_seconds=5),
        return_exceptions=True,
    )
    assert all(result is None for result in results), results
    record = await store.get_profile_deletion("p1")
    assert record is not None and record.phase == "purged"
    assert await store.get_profile("p1") is None
