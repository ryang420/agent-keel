import asyncio

import pytest

from keel.events import InMemoryEventBus
from keel.models import Completed, TextDelta, Usage
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.runner import RunExecutor
from keel.sandbox.base import SandboxError, parse_policy
from keel.sandbox.local import LocalSandbox
from keel.worker import WorkerPool


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
    """Delegates to the real store; the first renew_lease is wrapped by `first`."""

    def __init__(self, store, first):
        self._store = store
        self._first = first

    def __getattr__(self, name):
        return getattr(self._store, name)

    async def renew_lease(self, *args):
        first, self._first = self._first, None
        if first is None:
            return await self._store.renew_lease(*args)
        return await first(self._store, *args)


async def test_a_failing_renewal_does_not_kill_the_run_or_the_worker(pool_parts):
    store, provider, bus, executor = pool_parts
    provider.script = [[TextDelta("a"), "hang"]]
    renew_failed = asyncio.Event()

    async def boom(real, *args):
        renew_failed.set()
        raise RuntimeError("db blip")

    pool = WorkerPool(SlowRenewStore(store, boom), bus, executor, owner="w1", workers=1,
                      poll_seconds=0.01, lease_seconds=0.06)
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
        status = await real.renew_lease(*args)
        renewed.set()
        await asyncio.sleep(0.3)
        return status

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


async def test_sweep_emits_for_every_expired_run_even_if_one_emit_fails(store, clock):
    bus = InMemoryEventBus()
    executor = RunExecutor(store, bus, ProviderRegistry())
    runs = [await queue(store, "s1"), await queue(store, "s2")]
    await store.claim_run("dead", clock.now + 30)
    await store.claim_run("dead", clock.now + 30)
    real_emit = executor.emit
    calls = []

    async def flaky(run_id, *args):
        calls.append(run_id)
        if len(calls) == 1:
            raise RuntimeError("emit failed")
        return await real_emit(run_id, *args)

    executor.emit = flaky
    pool = WorkerPool(store, bus, executor, owner="w1", clock=clock)
    clock.now += 31
    assert await pool.sweep_once() == 2
    assert len(calls) == 2


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


async def test_a_stop_during_a_command_ends_the_run_quickly(store, tmp_path):
    from keel.models import ToolCall, ToolCallReady
    from keel.sandbox.local import LocalSandbox

    provider = FakeProvider(script=[[ToolCallReady(ToolCall("c1", "terminal", '{"command": "sleep 60"}')),
                                     Completed(Usage(1, 1, 0, 0))]])
    registry = ProviderRegistry()
    registry.register(provider)
    bus = InMemoryEventBus()
    executor = RunExecutor(store, bus, registry, sandbox=LocalSandbox(kill_grace_s=0.5))
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], soul="", skills=[],
                               sandbox={"image": "local", "workdir": str(tmp_path / "w")})
    pool = WorkerPool(store, bus, executor, owner="w1", workers=1, poll_seconds=0.01, lease_seconds=3)
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
        assert done.current_exec_id is None
    finally:
        await pool.close(drain_seconds=0.1)


async def test_sweeping_kills_the_command_a_lost_worker_left_running(store, clock, tmp_path):
    work = tmp_path / "w"
    policy = {"image": "local", "workdir": str(work)}
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox=policy, soul="",
                               skills=[])
    sandbox = LocalSandbox(kill_grace_s=0.5)
    box = await sandbox.ensure("p1", parse_policy(policy))
    command = asyncio.create_task(sandbox.exec(box, ["sh", "-c", f"sleep 30; touch {work}/late"], cwd=str(work),
                                               timeout_s=60, exec_id="x1"))
    for _ in range(100):
        if "x1" in sandbox._groups:
            break
        await asyncio.sleep(0.02)
    run = await queue(store)
    await store.claim_run("dead-worker", clock.now + 30)
    await store.set_current_exec(run.id, "x1")
    executor = RunExecutor(store, InMemoryEventBus(), ProviderRegistry(), sandbox=sandbox)
    pool = WorkerPool(store, InMemoryEventBus(), executor, owner="w1", clock=clock)
    clock.now += 31
    assert await pool.sweep_once() == 1
    result = await asyncio.wait_for(command, 5)
    assert result.exit_code != 0 and not (work / "late").exists()
    swept = await store.get_run("p1", run.id)
    assert (swept.status, swept.error_code, swept.current_exec_id) == ("failed", "worker_lost", None)


async def test_a_sweep_survives_an_unreachable_sandbox(store, clock, tmp_path):
    class Down(LocalSandbox):
        async def ensure(self, profile_id, policy):
            raise SandboxError("docker is down")

    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], soul="", skills=[],
                               sandbox={"image": "local", "workdir": str(tmp_path / "w")})
    run = await queue(store)
    await store.claim_run("dead-worker", clock.now + 30)
    await store.set_current_exec(run.id, "x1")
    executor = RunExecutor(store, InMemoryEventBus(), ProviderRegistry(), sandbox=Down())
    pool = WorkerPool(store, InMemoryEventBus(), executor, owner="w1", clock=clock)
    clock.now += 31
    assert await pool.sweep_once() == 1
    swept = await store.get_run("p1", run.id)
    assert (swept.status, swept.current_exec_id) == ("failed", None)
    assert [e.type for e in await store.events_after(run.id, 0)] == ["run.failed"]


async def test_prune_once_deletes_events_older_than_the_retention(store, clock):
    executor = RunExecutor(store, InMemoryEventBus(), ProviderRegistry())
    run = await queue(store)
    await store.append_event(run.id, "run.started", {})
    await store.finish_run(run.id, "completed")
    pool = WorkerPool(store, InMemoryEventBus(), executor, owner="w1", clock=clock, event_retention_s=7 * 86_400)
    assert await pool.prune_once() == 0
    clock.now += 8 * 86_400
    assert await pool.prune_once() == 1


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
    assert len(await store.events_after(run.id, 0)) == 1
