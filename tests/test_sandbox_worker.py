"""The worker and executor hold sandbox usage through cleanup (lifecycle design sections 8, 11 and 15).

Usage renewal belongs to the worker until the executor's cleanup returns, independently of the run lease. Run
termination and sandbox release are different events: a terminal run keeps its usage protective until release.
"""

import asyncio
import json

import pytest

from keel.events import InMemoryEventBus
from keel.models import Completed, TextDelta, ToolCall, ToolCallReady, Usage
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.runner import CANCELLED_TOOL_RESULT, RunExecutor
from keel.sandbox.records import DockerOutcome, MAX_DIAGNOSTIC_CHARS
from keel.worker import WorkerPool
from tests.sandbox_support import (
    ClaimedExecutor, LocalDocker, local_coordinator, queue_run, seed_profile, seed_ready_sandbox,
)

LEASE = 0.9
TERMINAL = ("run.completed", "run.failed", "run.cancelled")


async def no_sleep(seconds):
    pass


def call(call_id, name="terminal", **args):
    return ToolCallReady(ToolCall(call_id, name, json.dumps(args)))


def answer(text="done"):
    return [TextDelta(text), Completed(Usage(1, 1, 0, 0))]


class Rig:
    def __init__(self, store, coordinator, backend, provider, bus, executor, work):
        self.store, self.coordinator, self.backend = store, coordinator, backend
        self.provider, self.bus, self.executor, self.work = provider, bus, executor, work

    def pool(self, **options):
        options = {"owner": "w1", "workers": 1, "poll_seconds": 0.01, "lease_seconds": LEASE,
                   "coordinator": self.coordinator, **options}
        return WorkerPool(self.store, self.bus, self.executor, **options)


async def make_rig(store, tmp_path, monkeypatch, *, lease=LEASE, skills_path=None, executor_class=RunExecutor):
    bus = InMemoryEventBus()
    coordinator, backend = await local_coordinator(store, tmp_path / "bin", monkeypatch, lease_seconds=lease,
                                                   idle_seconds=0.05, bus=bus)
    provider = FakeProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    work = tmp_path / "work"
    policy = {"image": "local", "workdir": str(work)}
    if skills_path is not None:
        policy["skills_path"] = str(skills_path)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox=policy, soul="",
                               skills=["helper"] if skills_path is not None else [])
    executor = executor_class(store, bus, registry, coordinator=coordinator, sleep=no_sleep)
    return Rig(store, coordinator, backend, provider, bus, executor, work)


@pytest.fixture
async def rig(store, tmp_path, monkeypatch):
    return await make_rig(store, tmp_path, monkeypatch)


async def eventually(check, seconds=5.0):
    for _ in range(int(seconds / 0.02)):
        result = await check()
        if result:
            return result
        await asyncio.sleep(0.02)
    raise AssertionError("the condition never held")


async def terminal_types(store, run_id):
    return [e.type for e in await store.events_after(run_id, 0) if e.type in TERMINAL]


async def has_event(store, run_id, type_):
    return any(e.type == type_ for e in await store.events_after(run_id, 0))


def hold(store, name):
    """Replace one store method with a gate that holds every call until released."""
    original = getattr(store, name)
    entered, gate = asyncio.Event(), asyncio.Event()

    async def held(*args, **kwargs):
        entered.set()
        await gate.wait()
        return await original(*args, **kwargs)

    setattr(store, name, held)
    return entered, gate


async def kill_leftovers(backend: LocalDocker):
    for exec_id in list(backend.local._groups):
        await backend.local.kill_tree(None, exec_id)


async def test_a_model_wait_beyond_the_idle_timeout_keeps_the_usage_and_its_container(rig):
    rig.provider.script = [[call("c1", command="echo one"), Completed(Usage(1, 1, 0, 0))],
                           [1.5, *answer("done")]]
    pool = rig.pool()
    await pool.start()
    try:
        run = await queue_run(rig.store, "s1")
        await eventually(lambda: has_event(rig.store, run.id, "tool.completed"))
        first = await rig.store.get_run_usage(run.id)
        await asyncio.sleep(2 * LEASE / 3 + 0.1)  # past the idle timeout and at least one renewal
        await rig.coordinator.reap_idle_once()
        waiting = await rig.store.get_run_usage(run.id)
        assert waiting.state == "active" and waiting.lease_expires_at > first.lease_expires_at
        assert rig.backend.removed_ids == []
        await eventually(lambda: terminal_types(rig.store, run.id))
        assert await terminal_types(rig.store, run.id) == ["run.completed"]
        released = await eventually(lambda: _released(rig.store, run.id))
        assert released.release_reason == "normal_release"
    finally:
        await pool.close()


async def _released(store, run_id):
    usage = await store.get_run_usage(run_id)
    return usage if usage is not None and usage.state == "released" else None


async def test_a_terminal_run_keeps_renewing_its_usage_but_not_its_run_lease_until_cleanup_returns(rig):
    rig.provider.script = [[call("c1", command="echo one"), Completed(Usage(1, 1, 0, 0))], answer("done")]
    entered, gate = hold(rig.store, "release_usage")
    run_renewals = []
    renew_claim = rig.store.renew_claim

    async def counting(claim, seconds):
        run_renewals.append(claim.run.id)
        return await renew_claim(claim, seconds)

    rig.store.renew_claim = counting
    pool = rig.pool()
    await pool.start()
    try:
        run = await queue_run(rig.store, "s1")
        await asyncio.wait_for(entered.wait(), 5)
        assert await terminal_types(rig.store, run.id) == ["run.completed"]
        usage = await rig.store.get_run_usage(run.id)
        assert usage.state == "cleaning"
        assert rig.backend.removed_ids == []
        renewals_at_terminal = len(run_renewals)
        await asyncio.sleep(LEASE + 0.2)  # longer than one whole lease
        later = await rig.store.get_usage(usage.id)
        assert later.state == "cleaning" and later.lease_expires_at > usage.lease_expires_at
        assert later.lease_expires_at > await rig.store.coordination_now()
        assert len(run_renewals) <= renewals_at_terminal + 1  # at most one renewal was already in flight
        await rig.coordinator.reap_idle_once()
        await rig.coordinator.reconcile_once()
        assert rig.backend.removed_ids == []
        gate.set()
        assert (await eventually(lambda: _released(rig.store, run.id))).state == "released"
    finally:
        gate.set()
        await pool.close()


@pytest.mark.parametrize("helper", ["file", "skill"])
async def test_a_stop_during_a_file_or_skill_helper_cancels_through_its_registered_exec(
        store, tmp_path, monkeypatch, helper):
    skills = None
    if helper == "skill":
        skills = tmp_path / "skills"
        (skills / "helper").mkdir(parents=True)
        (skills / "helper" / "SKILL.md").write_text("---\nname: helper\ndescription: Helps.\n---\nUse it.\n")
    rig = await make_rig(store, tmp_path, monkeypatch, skills_path=skills)
    marker = "keel-discover" if helper == "skill" else "keel-helper"
    blocked = asyncio.Event()

    async def block(ticket, argv):
        if marker in argv:
            blocked.set()
            await asyncio.Event().wait()

    rig.backend.exec_hook = block
    rig.provider.script = [[call("c1", "read_file", path="a.txt"), Completed(Usage(1, 1, 0, 0))], answer()]
    pool = rig.pool()
    await pool.start()
    try:
        run = await queue_run(rig.store, "s1")
        await asyncio.wait_for(blocked.wait(), 5)
        await rig.store.request_stop("p1", run.id)
        await rig.bus.notify("stop:" + run.id)
        await eventually(lambda: terminal_types(rig.store, run.id))
        assert await terminal_types(rig.store, run.id) == ["run.cancelled"]
        usage = await eventually(lambda: _released(rig.store, run.id))
        rows = await rig.store.db.fetch("SELECT * FROM sandbox_execs WHERE usage_id = ?", usage.id)
        assert rows and all(row["state"] == "finished" for row in rows)
        blocked_ids = [exec_id for exec_id, argv in rig.backend.exec_calls if marker in argv]
        assert blocked_ids and set(blocked_ids) <= set(rig.backend.control_calls)
        if helper == "file":
            results = [m.message for m in await rig.store.messages("s1") if m.message.role == "tool"]
            assert [m.text for m in results] == [CANCELLED_TOOL_RESULT]
    finally:
        await pool.close()


async def test_repeated_worker_cancellation_keeps_renewing_until_executor_cleanup_returns(rig):
    rig.provider.script = [[call("c1", command="echo one"), Completed(Usage(1, 1, 0, 0))], answer("done")]
    entered, gate = hold(rig.store, "release_usage")
    pool = rig.pool()
    await pool.start()
    try:
        run = await queue_run(rig.store, "s1")
        await asyncio.wait_for(entered.wait(), 5)
        [worker] = [task for task in pool._tasks if task.get_name() == "keel-worker-0"]
        usage = await rig.store.get_run_usage(run.id)
        for _ in range(3):
            worker.cancel()
            await asyncio.sleep(LEASE / 2)
        assert not worker.done()
        later = await rig.store.get_usage(usage.id)
        assert later.state == "cleaning" and later.lease_expires_at > usage.lease_expires_at
        gate.set()
        await asyncio.wait_for(asyncio.gather(worker, return_exceptions=True), 5)
        assert (await rig.store.get_usage(usage.id)).state == "released"
    finally:
        gate.set()
        await pool.close()


async def test_repeated_executor_cancellation_during_cleanup_still_releases_and_returns(store, tmp_path,
                                                                                         monkeypatch):
    rig = await make_rig(store, tmp_path, monkeypatch, lease=30.0, executor_class=ClaimedExecutor)
    rig.provider.script = [[call("c1", command="echo one"), Completed(Usage(1, 1, 0, 0))], answer("done")]
    entered, gate = hold(rig.store, "release_usage")
    await queue_run(rig.store, "s1")
    run = await rig.executor.claim_next()
    task = asyncio.create_task(rig.executor.execute(run))
    await asyncio.wait_for(entered.wait(), 5)
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0.05)
    gate.set()
    await asyncio.wait_for(task, 5)
    assert not task.cancelled() and task.cancelling() == 0
    assert await terminal_types(rig.store, run.id) == ["run.completed"]
    assert (await rig.store.get_run_usage(run.id)).state == "released"


async def test_a_database_failure_cancels_the_run_by_its_last_confirmed_usage_deadline(rig):
    rig.provider.script = [[call("c1", command="echo one"), Completed(Usage(1, 1, 0, 0))], ["hang"]]
    loop = asyncio.get_running_loop()
    confirmed = []
    renew_usage = rig.store.renew_usage
    down = False

    async def flaky(use, seconds):
        if down:
            raise ConnectionError("the database is unreachable")
        sent = loop.time()
        stamp = await renew_usage(use, seconds)
        if stamp is not None:
            confirmed.append(sent + (stamp.expires_at - stamp.observed_at))
        return stamp

    rig.store.renew_usage = flaky
    pool = rig.pool()
    await pool.start()
    try:
        run = await queue_run(rig.store, "s1")
        await eventually(lambda: has_event(rig.store, run.id, "tool.completed"))
        await asyncio.sleep(LEASE / 3 + 0.1)
        down = True
        deadline = confirmed[-1] if confirmed else loop.time() + LEASE
        await eventually(lambda: terminal_types(rig.store, run.id), seconds=LEASE + 3)
        ended = loop.time()
        assert ended <= deadline + 0.5  # cancelled by the last confirmed deadline, plus the commit itself
        failed = await rig.store.get_run("p1", run.id)
        assert (failed.status, failed.error_code) == ("failed", "sandbox_unavailable")
        assert len(rig.backend.exec_calls) == 1  # no new exec after the lease could not be confirmed
    finally:
        await pool.close()


async def test_a_blocked_profile_at_the_queue_head_does_not_stop_a_healthy_profile(rig):
    await seed_ready_sandbox(rig.store, sandbox={"image": "local", "workdir": str(rig.work)})
    await rig.store.db.execute("UPDATE sandboxes SET state = 'blocked' WHERE profile_id = ?", "p1")
    await seed_profile(rig.store, "p2", sandbox={})
    blocked = await queue_run(rig.store, "s1")
    healthy = await queue_run(rig.store, "s2", profile_id="p2")
    pool = rig.pool()
    await pool.start()
    try:
        await eventually(lambda: terminal_types(rig.store, healthy.id))
        assert await terminal_types(rig.store, healthy.id) == ["run.completed"]
        assert (await rig.store.get_run("p1", blocked.id)).status == "queued"
        assert rig.backend.calls == []
    finally:
        await pool.close()


async def test_shutdown_leaves_uncertain_exec_evidence_and_the_current_exec_pointer(rig):
    rig.provider.script = [[call("c1", command="sleep 30"), Completed(Usage(1, 1, 0, 0))]]

    async def lost(ticket):
        raise RuntimeError("the supervisor did not answer")

    rig.backend.control_hook = lost
    pool = rig.pool()
    await pool.start()
    try:
        run = await queue_run(rig.store, "s1")
        await eventually(lambda: _current_exec(rig.store, run.id))
        await asyncio.sleep(0.2)
        await pool.close(drain_seconds=0.1)
        lost_run = await rig.store.get_run("p1", run.id)
        assert (lost_run.status, lost_run.error_code) == ("failed", "worker_lost")
        assert await terminal_types(rig.store, run.id) == ["run.failed"]
        rows = await rig.store.db.fetch("SELECT * FROM sandbox_execs WHERE run_id = ?", run.id)
        assert [row["state"] for row in rows] == ["uncertain"]
        assert lost_run.current_exec_id is not None  # the pointer is not cleared without a cleanup receipt
        usage = await rig.store.get_run_usage(run.id)
        assert usage.state != "released"
        sandbox = await rig.store.db.fetchrow("SELECT state FROM sandboxes WHERE profile_id = ?", "p1")
        assert sandbox["state"] == "draining"
    finally:
        await pool.close()
        await kill_leftovers(rig.backend)


async def _current_exec(store, run_id):
    run = await store.get_run_by_id(run_id)
    return run.current_exec_id


async def test_a_late_model_result_after_termination_launches_no_exec(store, tmp_path, monkeypatch):
    rig = await make_rig(store, tmp_path, monkeypatch, lease=30.0, executor_class=ClaimedExecutor)
    rig.provider.script = [[call("c1", command="echo one"), Completed(Usage(1, 1, 0, 0))],
                           [0.4, call("c2", command=f"touch {tmp_path}/late"), Completed(Usage(1, 1, 0, 0))]]
    await queue_run(rig.store, "s1")
    run = await rig.executor.claim_next()
    task = asyncio.create_task(rig.executor.execute(run))
    await eventually(lambda: has_event(rig.store, run.id, "tool.completed"))
    await eventually(lambda: _requests(rig.provider, 2))
    forced = await rig.store.finish_run(run.id, "failed", error_code="worker_lost", error="the worker was lost")
    assert forced.event is not None
    await asyncio.wait_for(task, 5)
    assert len(rig.backend.exec_calls) == 1
    assert not (tmp_path / "late").exists()
    assert await terminal_types(rig.store, run.id) == ["run.failed"]
    results = [m.message for m in await rig.store.messages("s1") if m.message.role == "tool"]
    assert [m.tool_call_id for m in results] == ["c1", "c2"] and results[1].text == CANCELLED_TOOL_RESULT
    assert (await rig.store.get_run_usage(run.id)).state == "released"


async def _requests(provider, count):
    return len(provider.requests) >= count


async def test_a_sandbox_failure_after_its_result_fails_the_run_and_the_next_run_recovers(store, tmp_path,
                                                                                         monkeypatch):
    rig = await make_rig(store, tmp_path, monkeypatch, lease=30.0, executor_class=ClaimedExecutor)

    async def lose(ticket, argv):
        if "echo boom" in " ".join(argv):
            raise ConnectionError("the transport was lost")

    rig.backend.exec_hook = lose
    rig.provider.script = [[call("c1", command="echo boom"), call("c2", command="echo two"),
                            Completed(Usage(1, 1, 0, 0))], answer("never")]
    await queue_run(rig.store, "s1")
    run = await rig.executor.claim_next()
    await rig.executor.execute(run)
    failed = await rig.store.get_run("p1", run.id)
    assert (failed.status, failed.error_code) == ("failed", "sandbox_unavailable")
    assert len(failed.error) <= MAX_DIAGNOSTIC_CHARS
    assert len(rig.provider.requests) == 1  # the run did not silently continue
    results = [m.message for m in await rig.store.messages("s1") if m.message.role == "tool"]
    assert [m.tool_call_id for m in results] == ["c1", "c2"]
    assert "sandbox is unavailable" in results[0].text and results[1].text == CANCELLED_TOOL_RESULT
    usage = await rig.store.get_run_usage(run.id)
    assert usage.state == "cleaning"  # an uncertain exec keeps the usage protective
    first_container = rig.backend.created_id

    # The owner is gone; once its lease lapses, recovery removes the generation and a new run gets a new one.
    await rig.store.db.execute("UPDATE sandbox_uses SET lease_expires_at = ? WHERE id = ?",
                               await rig.store.coordination_now() - 1, usage.id)
    await rig.coordinator.reconcile_once()
    assert rig.backend.removed_ids == [first_container]
    rig.backend.exec_hook = None
    rig.provider.script = [[call("c3", command="echo three"), Completed(Usage(1, 1, 0, 0))], answer("again")]
    await queue_run(rig.store, "s1")
    nxt = await rig.executor.claim_next()
    await rig.executor.execute(nxt)
    assert (await rig.store.get_run("p1", nxt.id)).output == "again"
    assert rig.backend.created_id != first_container


async def test_a_preparation_failure_reports_a_bounded_redacted_diagnostic(store, tmp_path, monkeypatch):
    rig = await make_rig(store, tmp_path, monkeypatch, lease=30.0, executor_class=ClaimedExecutor)
    secret = "hunter" + "2"
    rig.backend.set_result("create", DockerOutcome(
        "definite_failure", diagnostic=f"pull failed: password={secret} " + "x" * 2000))
    await queue_run(rig.store, "s1")
    run = await rig.executor.claim_next()
    await rig.executor.execute(run)
    failed = await rig.store.get_run("p1", run.id)
    assert (failed.status, failed.error_code) == ("failed", "sandbox_unavailable")
    assert secret not in failed.error and len(failed.error) <= MAX_DIAGNOSTIC_CHARS
    assert rig.provider.requests == []
    assert (await rig.store.get_run_usage(run.id)).state == "released"


async def test_the_cancellation_budget_is_grace_plus_reap_plus_transport_and_one_second_for_the_runner(rig):
    assert rig.coordinator.control_budget_s == pytest.approx(0.5 + rig.coordinator.reap_seconds + 15.0)
    assert rig.executor.cancel_budget_s == pytest.approx(rig.coordinator.control_budget_s + 1.0)


async def test_the_sweeper_commits_and_notifies_termination_before_recovery(rig):
    order = []
    real_notify = rig.bus.notify

    async def notify(key):
        order.append(("notify", key))
        await real_notify(key)

    async def recover(run_id):
        order.append(("recover", run_id))

    rig.bus.notify = notify
    rig.coordinator.recover_run = recover
    await queue_run(rig.store, "s1")
    claim = await rig.coordinator.claim()  # a worker that then died
    await asyncio.sleep(LEASE + 0.1)
    pool = rig.pool()
    assert await pool.sweep_once() == 1
    assert order == [("notify", claim.run.id), ("recover", claim.run.id)]
    swept = await rig.store.get_run("p1", claim.run.id)
    assert (swept.status, swept.error_code) == ("failed", "worker_lost")
    assert not hasattr(rig.executor, "kill_orphaned_command")


async def test_a_sandbox_executor_needs_a_coordinated_worker_and_claim(rig):
    with pytest.raises(ValueError):
        WorkerPool(rig.store, rig.bus, rig.executor, owner="w1")
    await queue_run(rig.store, "s1")
    legacy = await rig.store.claim_run("w1", await rig.store.coordination_now() + 30)
    await rig.executor.execute(legacy)
    failed = await rig.store.get_run("p1", legacy.id)
    assert (failed.status, failed.error_code) == ("failed", "sandbox_unavailable")
    assert rig.backend.calls == [] and rig.backend.exec_calls == []
    assert rig.provider.requests == []


async def test_lease_expiry_uses_database_time_despite_host_clock_skew(store, clock):
    await seed_profile(store, sandbox={})
    run = await queue_run(store, "s1")
    await store.claim_run("w1", await store.coordination_now() + 30)
    clock.now += 10_000_000_000  # a host wall clock far in the future
    assert await store.expire_leases() == []
    await store.db.execute("UPDATE runs SET lease_expires_at = ? WHERE id = ?",
                           await store.db.database_now() - 1, run.id)
    clock.now = 1_000.0  # and far in the past
    [expired] = await store.expire_leases()
    assert expired.run.id == run.id and expired.event.type == "run.failed"


async def test_a_lost_usage_lease_does_not_spin_run_lease_renewal_during_cleanup(rig):
    rig.provider.script = [[call("c1", command="sleep 30"), Completed(Usage(1, 1, 0, 0))]]
    close_tool = rig.executor._close_tool

    async def slow_close(run, state):
        await asyncio.sleep(1.5)  # a slow but bounded cancellation cleanup
        await close_tool(run, state)

    rig.executor._close_tool = slow_close
    renewals = []
    renew_claim = rig.store.renew_claim

    async def counting(claim, seconds):
        renewals.append(claim.run.id)
        return await renew_claim(claim, seconds)

    rig.store.renew_claim = counting
    pool = rig.pool()
    await pool.start()
    try:
        run = await queue_run(rig.store, "s1")
        await eventually(lambda: _current_exec(rig.store, run.id))
        await rig.store.db.execute("UPDATE sandbox_uses SET state = 'orphaned' WHERE run_id = ?", run.id)
        before = len(renewals)
        await eventually(lambda: terminal_types(rig.store, run.id), seconds=10)
        # About 1.5 s of cleanup at one renewal per lease/3 (0.3 s): a handful, never a busy loop.
        assert len(renewals) - before <= 10
    finally:
        await pool.close()
        await kill_leftovers(rig.backend)


async def test_one_bounded_control_ends_a_stop_even_when_the_first_control_never_answers(store, tmp_path,
                                                                                       monkeypatch):
    rig = await make_rig(store, tmp_path, monkeypatch, lease=30.0, executor_class=ClaimedExecutor)
    rig.coordinator.control_budget_s = 0.3
    rig.provider.script = [[call("c1", command="sleep 30"), Completed(Usage(1, 1, 0, 0))]]

    async def never(ticket):
        await asyncio.Event().wait()

    rig.backend.control_hook = never
    await queue_run(rig.store, "s1")
    run = await rig.executor.claim_next()
    task = asyncio.create_task(rig.executor.execute(run))
    try:
        exec_id = await eventually(lambda: _current_exec(rig.store, run.id))
        await asyncio.sleep(0.2)
        started = asyncio.get_running_loop().time()
        task.cancel()
        await asyncio.wait_for(asyncio.shield(task), 3)
        assert asyncio.get_running_loop().time() - started < 0.3 + 1.0 + 0.5
        assert rig.backend.control_calls == [exec_id]  # one control request for the whole stop
        row = await rig.store.db.fetchrow("SELECT state FROM sandbox_execs WHERE id = ?", exec_id)
        assert row["state"] == "uncertain"
        assert await terminal_types(rig.store, run.id) == ["run.cancelled"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await kill_leftovers(rig.backend)


async def test_a_second_cancel_joins_the_same_control(store, tmp_path, monkeypatch):
    rig = await make_rig(store, tmp_path, monkeypatch, lease=30.0, executor_class=ClaimedExecutor)
    rig.provider.script = [[call("c1", command="sleep 30"), Completed(Usage(1, 1, 0, 0))]]
    entered, answer_now = asyncio.Event(), asyncio.Event()

    async def slow(ticket):
        entered.set()
        await answer_now.wait()

    rig.backend.control_hook = slow
    await queue_run(rig.store, "s1")
    run = await rig.executor.claim_next()
    task = asyncio.create_task(rig.executor.execute(run))
    exec_id = await eventually(lambda: _current_exec(rig.store, run.id))
    await asyncio.sleep(0.2)
    task.cancel()
    await asyncio.wait_for(entered.wait(), 3)
    task.cancel()  # a repeated stop while the first control is in flight
    await asyncio.sleep(0.1)
    answer_now.set()
    await asyncio.wait_for(task, 5)
    assert rig.backend.control_calls == [exec_id]
    row = await rig.store.db.fetchrow("SELECT state FROM sandbox_execs WHERE id = ?", exec_id)
    assert row["state"] == "finished"
    assert await terminal_types(rig.store, run.id) == ["run.cancelled"]
    assert (await rig.store.get_run_by_id(run.id)).current_exec_id is None
    assert (await rig.store.get_run_usage(run.id)).state == "released"


async def test_concurrent_kill_requests_for_one_exec_send_one_control(rig):
    await queue_run(rig.store, "s1")
    claim = await rig.coordinator.claim()
    bound = await rig.coordinator.prepare(claim.use)
    await rig.store.begin_exec(bound, "original")
    gate = asyncio.Event()

    async def slow(ticket):
        await gate.wait()

    rig.backend.control_hook = slow
    first = asyncio.create_task(rig.coordinator.kill_tree(bound, "original"))
    second = asyncio.create_task(rig.coordinator.kill_tree(bound, "original"))
    await asyncio.sleep(0.1)
    gate.set()
    await asyncio.gather(first, second)
    await rig.coordinator.kill_tree(bound, "original")  # a later retry reads the same outcome
    assert rig.backend.control_calls == ["original"]


async def test_usage_renewal_follows_the_lease_the_coordinator_grants(store, tmp_path, monkeypatch):
    rig = await make_rig(store, tmp_path, monkeypatch)  # the coordinator grants LEASE
    rig.provider.script = [[call("c1", command="echo one"), Completed(Usage(1, 1, 0, 0))],
                           [2 * LEASE, *answer("done")]]
    pool = rig.pool(lease_seconds=30.0)  # a pool configured with a much longer lease of its own
    await pool.start()
    try:
        run = await queue_run(rig.store, "s1")
        await eventually(lambda: has_event(rig.store, run.id, "tool.completed"))
        await asyncio.sleep(LEASE + 0.2)
        usage = await rig.store.get_run_usage(run.id)
        assert usage.state == "active" and usage.lease_expires_at > await rig.store.coordination_now()
        await eventually(lambda: terminal_types(rig.store, run.id), seconds=10)
        assert await terminal_types(rig.store, run.id) == ["run.completed"]
        assert (await eventually(lambda: _released(rig.store, run.id))).release_reason == "normal_release"
    finally:
        await pool.close()
