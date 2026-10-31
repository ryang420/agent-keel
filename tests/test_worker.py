import asyncio

import pytest

from keel.events import InMemoryEventBus
from keel.models import Completed, TextDelta, Usage
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.runner import RunExecutor
from keel.sandbox.base import SandboxError
from keel.worker import WorkerPool
from tests.sandbox_support import local_coordinator


@pytest.fixture
async def pool_parts(store):
    provider = FakeProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    bus = InMemoryEventBus()
    executor = RunExecutor(store, bus, registry)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="", skills=[])
    return store, provider, bus, executor


async def queue(store, session="s1"):
    await store.ensure_session("p1", session)
    return await store.create_run(profile_id="p1", session_id=session, provider="fake", model="m",
                                  reasoning_effort=None, input_parts=[{"type": "text", "text": "hi"}],
                                  instructions=None)


async def wait_for_status(store, run_id, statuses, seconds=5.0):
    for _ in range(int(seconds / 0.02)):
        run = await store.get_run("p1", run_id)
        if run.status in statuses:
            return run
        await asyncio.sleep(0.02)
    raise AssertionError(f"run stayed {run.status}")


async def test_the_pool_runs_queued_runs(pool_parts):
    store, provider, bus, executor = pool_parts
    pool = WorkerPool(store, bus, executor, owner="w1", workers=2, poll_seconds=0.01)
    await pool.start()
    try:
        runs = [await queue(store, "s1"), await queue(store, "s2")]
        for run in runs:
            assert (await wait_for_status(store, run.id, ("completed",))).output == "ok"
    finally:
        await pool.close()


async def test_a_stop_request_cancels_a_running_run(pool_parts):
    store, provider, bus, executor = pool_parts
    provider.script = [[TextDelta("par"), "hang"]]
    pool = WorkerPool(store, bus, executor, owner="w1", workers=1, poll_seconds=0.01, lease_seconds=30)
    await pool.start()
    try:
        run = await queue(store)
        await wait_for_status(store, run.id, ("running",))
        await asyncio.sleep(0.05)
        await store.request_stop("p1", run.id)
        await bus.notify("stop:" + run.id)
        stopped = await wait_for_status(store, run.id, ("cancelled",), seconds=2)
        assert stopped.output == "par"
    finally:
        await pool.close()


async def test_sweeping_fails_runs_whose_lease_expired(store, clock):
    store.coordination_clock = clock  # lease deadlines on the fake clock, injected explicitly
    bus = InMemoryEventBus()
    executor = RunExecutor(store, bus, ProviderRegistry())
    run = await queue(store)
    await store.claim_run("dead-worker", clock.now + 30)
    pool = WorkerPool(store, bus, executor, owner="w1", clock=clock)
    assert await pool.sweep_once() == 0
    clock.now += 31
    assert await pool.sweep_once() == 1
    failed = await store.get_run("p1", run.id)
    assert failed.status == "failed" and failed.error_code == "worker_lost"
    event = (await store.events_after(run.id, 0))[-1]
    assert event.type == "run.failed" and event.data["error_code"] == "worker_lost"


async def test_close_fails_work_still_running_after_the_drain_as_worker_lost(pool_parts):
    store, provider, bus, executor = pool_parts
    provider.script = [["hang"]]
    pool = WorkerPool(store, bus, executor, owner="w1", workers=1, poll_seconds=0.01)
    await pool.start()
    run = await queue(store)
    await wait_for_status(store, run.id, ("running",))
    await pool.close(drain_seconds=0.1)
    failed = await store.get_run("p1", run.id)
    assert failed.status == "failed" and failed.error_code == "worker_lost"
    assert "shutting down" in failed.error
    types = [e.type for e in await store.events_after(run.id, 0)]
    assert types.count("run.failed") == 1 and "run.cancelled" not in types
    assert types[-1] == "run.failed"


async def test_close_lets_a_run_finish_within_the_drain_window(pool_parts):
    store, provider, bus, executor = pool_parts
    provider.script = [[0.2, TextDelta("done"), Completed(Usage(1, 1, 0, 0))]]
    pool = WorkerPool(store, bus, executor, owner="w1", workers=1, poll_seconds=0.01)
    await pool.start()
    run = await queue(store)
    await wait_for_status(store, run.id, ("running",))
    await pool.close(drain_seconds=5)
    finished = await store.get_run("p1", run.id)
    assert finished.status == "completed" and finished.output == "done"


async def test_close_stops_claiming_new_runs(pool_parts):
    store, provider, bus, executor = pool_parts
    pool = WorkerPool(store, bus, executor, owner="w1", workers=1, poll_seconds=0.01)
    await pool.start()
    await pool.close(drain_seconds=1)
    run = await queue(store)
    await asyncio.sleep(0.1)
    assert (await store.get_run("p1", run.id)).status == "queued"


async def test_claim_errors_back_off_instead_of_spinning(pool_parts):
    store, provider, bus, executor = pool_parts
    calls = []

    class Broken:
        def __getattr__(self, name):
            return getattr(store, name)

        async def claim_run(self, *args):
            calls.append(1)
            raise RuntimeError("db down")

    pool = WorkerPool(Broken(), bus, executor, owner="w1", workers=1, poll_seconds=0.02)
    await pool.start()
    await asyncio.sleep(0.5)
    await pool.close(drain_seconds=1)
    assert 2 <= len(calls) <= 8  # 0.02 s polling would be ~25 calls


class SlowRenewStore:
    """Delegates to the real store; the first run-lease renewal is wrapped by `first`."""

    def __init__(self, store, first):
        self._store = store
        self._first = first

    def __getattr__(self, name):
        return getattr(self._store, name)

    async def renew_claim(self, *args):
        first, self._first = self._first, None
        if first is None:
            return await self._store.renew_claim(*args)
        return await first(self._store, *args)


async def test_a_failing_renewal_does_not_kill_the_run_or_the_worker(pool_parts):
    store, provider, bus, executor = pool_parts
    provider.script = [[TextDelta("a"), "hang"]]
    renew_failed = asyncio.Event()

    async def boom(real, *args):
        renew_failed.set()
        raise RuntimeError("db blip")

    pool = WorkerPool(SlowRenewStore(store, boom), bus, executor, owner="w1", workers=1,
                      poll_seconds=0.01, lease_seconds=3)
    await pool.start()
    try:
        first = await queue(store, "s1")
        await asyncio.wait_for(renew_failed.wait(), 3)
        await asyncio.sleep(0.05)
        await store.request_stop("p1", first.id)
        await bus.notify("stop:" + first.id)
        await wait_for_status(store, first.id, ("cancelled",))
        second = await queue(store, "s2")
        assert (await wait_for_status(store, second.id, ("completed",))).output == "ok"
    finally:
        await pool.close()


async def test_a_stop_landing_during_a_renewal_is_not_lost(pool_parts):
    store, provider, bus, executor = pool_parts
    provider.script = [[TextDelta("par"), "hang"]]
    renewed = asyncio.Event()

    async def renew_then_linger(real, *args):
        stamp = await real.renew_claim(*args)
        renewed.set()
        await asyncio.sleep(0.3)
        return stamp

    pool = WorkerPool(SlowRenewStore(store, renew_then_linger), bus, executor, owner="w1", workers=1,
                      poll_seconds=0.01, lease_seconds=3)
    await pool.start()
    try:
        run = await queue(store)
        await asyncio.wait_for(renewed.wait(), 3)
        await store.request_stop("p1", run.id)
        await bus.notify("stop:" + run.id)
        stopped = await wait_for_status(store, run.id, ("cancelled",), seconds=0.8)
        assert stopped.output == "par"
    finally:
        await pool.close()


async def test_sweep_notifies_every_expired_run_even_if_one_notify_fails(store, clock):
    store.coordination_clock = clock
    bus = InMemoryEventBus()
    executor = RunExecutor(store, bus, ProviderRegistry())
    runs = [await queue(store, "s1"), await queue(store, "s2")]
    await store.claim_run("dead", clock.now + 30)
    await store.claim_run("dead", clock.now + 30)
    real_notify = bus.notify
    calls = []

    async def flaky(key):
        calls.append(key)
        if len(calls) == 1:
            raise RuntimeError("notify failed")
        return await real_notify(key)

    bus.notify = flaky
    pool = WorkerPool(store, bus, executor, owner="w1", clock=clock)
    clock.now += 31
    assert await pool.sweep_once() == 2
    assert len(calls) == 2
    for run in runs:
        assert [e.type for e in await store.events_after(run.id, 0)] == ["run.failed"]


async def test_a_failing_status_check_does_not_orphan_the_execution(pool_parts):
    store, provider, bus, executor = pool_parts
    provider.script = [["hang"]]

    class FlakyStatus:
        def __init__(self):
            self.calls = 0

        def __getattr__(self, name):
            return getattr(store, name)

        async def get_run_by_id(self, run_id):
            self.calls += 1
            if self.calls <= 2:
                raise RuntimeError("db blip")
            return await store.get_run_by_id(run_id)

    pool = WorkerPool(FlakyStatus(), bus, executor, owner="w1", workers=1, poll_seconds=0.01)
    await pool.start()
    run = await queue(store)
    await wait_for_status(store, run.id, ("running",))
    await asyncio.sleep(0.1)
    await pool.close(drain_seconds=0.1)
    assert (await store.get_run("p1", run.id)).status == "failed"
    runs = [t for t in asyncio.all_tasks() if t.get_name().startswith("keel-run-")]
    assert all(t.done() for t in runs)


async def test_a_stop_during_a_command_ends_the_run_quickly(store, tmp_path, monkeypatch):
    from keel.models import ToolCall, ToolCallReady

    provider = FakeProvider(script=[[ToolCallReady(ToolCall("c1", "terminal", '{"command": "sleep 60"}')),
                                     Completed(Usage(1, 1, 0, 0))]])
    registry = ProviderRegistry()
    registry.register(provider)
    bus = InMemoryEventBus()
    coordinator, backend = await local_coordinator(store, tmp_path / "bin", monkeypatch, lease_seconds=3)
    executor = RunExecutor(store, bus, registry, coordinator=coordinator)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], soul="", skills=[],
                               sandbox={"image": "local", "workdir": str(tmp_path / "w")})
    pool = WorkerPool(store, bus, executor, owner="w1", workers=1, poll_seconds=0.01, lease_seconds=3,
                      coordinator=coordinator)
    await pool.start()
    try:
        run = await queue(store)
        for _ in range(250):
            if any(e.type == "tool.started" for e in await store.events_after(run.id, 0)):
                break
            await asyncio.sleep(0.02)
        await store.request_stop("p1", run.id)
        await bus.notify("stop:" + run.id)
        done = await wait_for_status(store, run.id, ("cancelled",), seconds=10)
        assert done.status == "cancelled"
        assert (await store.get_run_by_id(run.id)).current_exec_id is None  # cleared only after a receipt
        assert backend.control_calls
    finally:
        await pool.close(drain_seconds=0.1)


async def lost_run(store, clock, coordinator):
    """A sandboxed run whose worker claimed it and died, with its lease past due on the fake clock."""
    clock.now = await store.db.database_now()
    store.coordination_clock = clock
    run = await queue(store)
    assert (await coordinator.claim()).run.id == run.id
    clock.now += 31
    return run


async def test_the_sweeper_never_creates_a_sandbox(store, clock, tmp_path, monkeypatch):
    coordinator, backend = await local_coordinator(store, tmp_path / "bin", monkeypatch)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], soul="", skills=[],
                               sandbox={"image": "local", "workdir": str(tmp_path / "w")})
    run = await lost_run(store, clock, coordinator)
    executor = RunExecutor(store, InMemoryEventBus(), ProviderRegistry(), coordinator=coordinator)
    pool = WorkerPool(store, InMemoryEventBus(), executor, owner="w1", clock=clock, coordinator=coordinator)
    assert await pool.sweep_once() == 1
    assert "create" not in backend.calls and backend.exec_calls == [] and backend.control_calls == []
    assert not (tmp_path / "w").exists()
    assert (await store.get_run("p1", run.id)).error_code == "worker_lost"


async def test_a_sweep_survives_an_unreachable_sandbox(store, clock, tmp_path, monkeypatch):
    coordinator, _ = await local_coordinator(store, tmp_path / "bin", monkeypatch)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], soul="", skills=[],
                               sandbox={"image": "local", "workdir": str(tmp_path / "w")})
    run = await lost_run(store, clock, coordinator)

    async def down(run_id):
        raise SandboxError("docker is down")

    coordinator.recover_run = down
    executor = RunExecutor(store, InMemoryEventBus(), ProviderRegistry(), coordinator=coordinator)
    pool = WorkerPool(store, InMemoryEventBus(), executor, owner="w1", clock=clock, coordinator=coordinator)
    assert await pool.sweep_once() == 1
    swept = await store.get_run("p1", run.id)
    assert (swept.status, swept.error_code) == ("failed", "worker_lost")
    assert [e.type for e in await store.events_after(run.id, 0)] == ["run.failed"]


async def test_prune_once_deletes_events_older_than_the_retention(store, clock):
    executor = RunExecutor(store, InMemoryEventBus(), ProviderRegistry())
    run = await queue(store)
    await store.append_event(run.id, "run.started", {})
    await store.finish_run(run.id, "completed")
    pool = WorkerPool(store, InMemoryEventBus(), executor, owner="w1", clock=clock, event_retention_s=7 * 86_400)
    assert await pool.prune_once() == 0
    clock.now += 8 * 86_400
    assert await pool.prune_once() == 2  # run.started and run.completed


async def test_after_a_successful_prune_the_next_scans_only_runs_finished_since(store, clock):
    calls = []

    class Spy:
        def __getattr__(self, name):
            return getattr(store, name)

        async def prune_events(self, finished_before, finished_after=None):
            calls.append((finished_before, finished_after))
            if len(calls) == 1:
                raise RuntimeError("db blip")
            return 0

    executor = RunExecutor(store, InMemoryEventBus(), ProviderRegistry())
    pool = WorkerPool(Spy(), InMemoryEventBus(), executor, owner="w1", clock=clock, event_retention_s=100,
                      maintenance_s=10)
    with pytest.raises(RuntimeError):
        await pool.prune_once()
    await pool.prune_once()
    clock.now += 10
    await pool.prune_once()
    assert calls == [(900, None), (900, None), (910, 890)]


async def test_the_maintenance_task_prunes_at_start_and_stops_on_close(store, clock):
    executor = RunExecutor(store, InMemoryEventBus(), ProviderRegistry())
    run = await queue(store)
    await store.append_event(run.id, "run.started", {})
    await store.finish_run(run.id, "completed")
    clock.now += 8 * 86_400
    pool = WorkerPool(store, InMemoryEventBus(), executor, owner="w1", workers=1, poll_seconds=0.01, clock=clock,
                      event_retention_s=7 * 86_400)
    await pool.start()
    [maintenance] = [t for t in pool._tasks if t.get_name() == "keel-maintenance"]
    try:
        for _ in range(100):
            if not await store.events_after(run.id, 0):
                break
            await asyncio.sleep(0.02)
        assert await store.events_after(run.id, 0) == []
    finally:
        await asyncio.wait_for(pool.close(drain_seconds=0.1), 2)
    assert maintenance.done() and pool._tasks == []


async def test_prune_once_without_a_retention_deletes_nothing(store, clock):
    executor = RunExecutor(store, InMemoryEventBus(), ProviderRegistry())
    run = await queue(store)
    await store.append_event(run.id, "run.started", {})
    await store.finish_run(run.id, "completed")
    clock.now += 365 * 86_400
    pool = WorkerPool(store, InMemoryEventBus(), executor, owner="w1", clock=clock)
    assert await pool.prune_once() == 0
    assert len(await store.events_after(run.id, 0)) == 2


async def test_backfill_once_indexes_pending_history(pool_parts):
    from keel.models import Message, TextPart

    store, _, bus, executor = pool_parts
    await store.ensure_session("p1", "s1")
    stored = await store.append_message("s1", None, Message("user", (TextPart("old words"),)))
    await store.db.execute("DELETE FROM message_tokens")
    await store.db.execute("INSERT INTO history_backfill (profile_id, done_through, ceiling, updated_at) "
                           "VALUES ('p1', 0, ?, 0)", stored.id)
    worker = WorkerPool(store, bus, executor, owner="w1")
    assert await worker.backfill_once() == 1
    assert await worker.backfill_once() is None


async def test_the_backfill_loop_indexes_pending_history_and_stops_on_close(pool_parts, monkeypatch):
    from keel import worker as worker_module
    from keel.models import Message, TextPart

    store, _, bus, executor = pool_parts
    await store.ensure_session("p1", "s1")
    stored = await store.append_message("s1", None, Message("user", (TextPart("loop words"),)))
    await store.db.execute("DELETE FROM message_tokens")
    await store.db.execute("INSERT INTO history_backfill (profile_id, done_through, ceiling, updated_at) "
                           "VALUES ('p1', 0, ?, 0)", stored.id)
    monkeypatch.setattr(worker_module, "BACKFILL_IDLE_S", 0.01)
    pool = WorkerPool(store, bus, executor, owner="w1", workers=1, poll_seconds=0.01)
    await pool.start()
    try:
        for _ in range(100):
            if await store.history_index_complete("p1"):
                break
            await asyncio.sleep(0.02)
        assert await store.history_index_complete("p1")
    finally:
        await asyncio.wait_for(pool.close(drain_seconds=0.1), 2)
    assert pool._tasks == []


async def test_the_pool_runs_memory_review_loops_and_releases_on_close(pool_parts):
    store, _, bus, executor = pool_parts
    calls = []

    class Reviewer:
        async def run_once(self, owner):
            calls.append(owner)
            await asyncio.sleep(3600)

    pool = WorkerPool(store, bus, executor, owner="w1", workers=0, reviewer=Reviewer(), review_workers=2)
    await pool.start()
    await asyncio.sleep(0.05)
    assert calls == ["w1", "w1"]
    assert sorted(t.get_name() for t in pool._tasks if t.get_name().startswith("keel-memory-review")) == [
        "keel-memory-review-0", "keel-memory-review-1"]
    await asyncio.wait_for(pool.close(), 5)


class CountingJob:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    async def run_once(self, owner):
        self.calls += 1
        return self.results.pop(0) if self.results else False


async def test_the_review_loop_runs_reviews_and_dreams_and_survives_a_failing_one(pool_parts):
    store, _, bus, executor = pool_parts

    class Broken:
        async def run_once(self, owner):
            raise RuntimeError("boom")

    dreamer = CountingJob([True, True])
    pool = WorkerPool(store, bus, executor, owner="w1", workers=0, reviewer=Broken(), dreamer=dreamer)
    await pool.start()
    try:
        for _ in range(50):
            if dreamer.calls >= 3:
                break
            await asyncio.sleep(0.01)
    finally:
        await pool.close()
    assert dreamer.calls >= 3


async def test_a_pool_with_only_a_dreamer_starts_the_loop(pool_parts):
    store, _, bus, executor = pool_parts
    dreamer = CountingJob([True])
    pool = WorkerPool(store, bus, executor, owner="w1", workers=0, dreamer=dreamer)
    await pool.start()
    try:
        for _ in range(50):
            if dreamer.calls:
                break
            await asyncio.sleep(0.01)
    finally:
        await pool.close()
    assert dreamer.calls >= 1


async def test_expire_once_expires_only_observations_older_than_the_window(store, clock):
    from keel.observations import EXPIRY_SECONDS
    from keel.review_observe import StagedObservation
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="", skills=[])
    await store.ensure_session("p1", "s1")
    item = StagedObservation("weekly report", "Asked", "Conclusion first", "corrected", ())
    old = await store._insert_observation("p1", "s1", "g", item, now=clock.now)
    clock.now += EXPIRY_SECONDS + 86_400
    recent = await store._insert_observation("p1", "s1", "g", item, now=clock.now - 86_400)
    executor = RunExecutor(store, InMemoryEventBus(), ProviderRegistry())
    pool = WorkerPool(store, InMemoryEventBus(), executor, owner="w1", clock=clock)
    assert await pool.expire_once() == 1
    states = {o.id: o.state for o in await store.observations("p1")}
    assert states == {old: "expired", recent: "pending"}


async def test_the_maintenance_task_runs_without_an_event_retention(store, clock):
    executor = RunExecutor(store, InMemoryEventBus(), ProviderRegistry())
    pool = WorkerPool(store, InMemoryEventBus(), executor, owner="w1", workers=1, poll_seconds=0.01, clock=clock)
    await pool.start()
    try:
        assert [t for t in pool._tasks if t.get_name() == "keel-maintenance"]
    finally:
        await asyncio.wait_for(pool.close(drain_seconds=0.1), 2)
