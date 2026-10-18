"""Kill a keel process during each sandbox lifecycle phase and recover from an independent process (design §16).

A child process runs a real worker pool and coordinator over a SQLite file and a file-backed Docker double whose
containers outlive the process. It holds one lifecycle call open, the test kills it with SIGKILL, and a fresh
instance (its own store and coordinator, with the run's lease observed as expired through an injected coordination
clock) sweeps and recovers. Recovery must not assume that an expired lease means a command stopped: every container
the dead process left is removed by exact ID and proven absent before a replacement is published.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import keel.sandbox.runtime as sandbox_runtime
from keel.events import InMemoryEventBus
from keel.models import Completed, TextDelta, Usage
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.sandbox.base import parse_policy
from keel.sandbox.coordinator import SandboxCoordinator
from keel.store import open_store
from keel.worker import WorkerPool
from tests.sandbox_support import ClaimedExecutor, FileDocker, write_helper_assets

needs_subprocess = pytest.mark.skipif(os.environ.get("KEEL_TEST_SUBPROCESS") != "1",
                                      reason="set KEEL_TEST_SUBPROCESS=1")
pytestmark = needs_subprocess
REPO = Path(__file__).resolve().parents[1]
LEASE = 30.0
SANDBOX = {"image": "img", "workdir": "/w"}
TERMINAL_EVENTS = ("run.completed", "run.failed", "run.cancelled")
PHASES = ("create", "created", "install", "start", "exec", "release")

CHILD = textwrap.dedent("""
    import asyncio
    import sys
    from pathlib import Path

    repo, url, state, phase = sys.argv[1:5]
    sys.path.insert(0, repo)
    state = Path(state)

    import keel.sandbox.runtime as runtime
    runtime.artifact_directory = lambda: state / "bin"

    from keel.events import InMemoryEventBus
    from keel.models import Completed, TextDelta, ToolCall, ToolCallReady, Usage
    from keel.providers.base import ProviderRegistry
    from keel.providers.fake import FakeProvider
    from keel.runner import RunExecutor
    from keel.sandbox.coordinator import SandboxCoordinator
    from keel.store import open_store
    from keel.worker import WorkerPool
    from tests.sandbox_support import FileDocker


    async def main():
        store = await open_store(url)
        if phase == "release":
            async def held(use):
                (state / "entered-release").write_text("")
                await asyncio.get_running_loop().create_future()
            store.release_usage = held
        coordinator = SandboxCoordinator(store, FileDocker(state, hold=phase), deployment="dep", owner="doomed",
                                         lease_seconds=30.0, idle_seconds=1800.0, reap_seconds=1.0)
        assert await coordinator.open_admission()
        provider = FakeProvider(script=[
            [ToolCallReady(ToolCall("c1", "terminal", '{"command": "echo hi"}')), Completed(Usage(5, 1, 0, 0))],
            [TextDelta("done"), Completed(Usage(5, 1, 0, 0))]])
        registry = ProviderRegistry()
        registry.register(provider)
        bus = InMemoryEventBus()
        executor = RunExecutor(store, bus, registry, coordinator=coordinator)
        pool = WorkerPool(store, bus, executor, owner="doomed", workers=1, lease_seconds=30.0,
                          coordinator=coordinator)
        await pool.start()
        await asyncio.get_running_loop().create_future()


    asyncio.run(main())
""")


class Frozen:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


async def queue(store, text: str):
    await store.ensure_session("p1", "s1")
    return await store.create_run(profile_id="p1", session_id="s1", provider="fake", model="m",
                                  reasoning_effort=None, input_parts=[{"type": "text", "text": text}],
                                  instructions=None)


async def wait_for_file(path: Path, child: subprocess.Popen, log: Path, seconds: float = 30.0) -> None:
    for _ in range(int(seconds / 0.02)):
        if path.exists():
            return
        if child.poll() is not None:
            raise AssertionError(f"the child exited with {child.returncode}: {log.read_text()[-2000:]}")
        await asyncio.sleep(0.02)
    raise AssertionError(f"the child never reached {path.name}: {log.read_text()[-2000:]}")


async def terminal_events(store, run_id: str) -> list[str]:
    return [event.type for event in await store.events_after(run_id, 0) if event.type in TERMINAL_EVENTS]


def absence_verified_before_create(journal, old_id: str, new_id: str) -> bool:
    created = journal.index(("create", new_id))
    return ("inspect", old_id, "absent") in journal[:created]


@pytest.mark.parametrize("phase", PHASES)
async def test_a_process_killed_during_each_lifecycle_phase_is_recovered_by_another(tmp_path, monkeypatch, phase):
    state = tmp_path / "daemon"
    write_helper_assets(state / "bin")
    url = f"sqlite:///{tmp_path / 'keel.sqlite3'}"
    store = await open_store(url)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox=SANDBOX, soul="",
                               skills=[])
    run = await queue(store, "hi")
    await store.close()

    script, log = tmp_path / "child.py", tmp_path / "child.log"
    script.write_text(CHILD)
    with log.open("w") as output:
        child = subprocess.Popen([sys.executable, str(script), str(REPO), url, str(state), phase], cwd=REPO,
                                 stdout=output, stderr=subprocess.STDOUT)
    try:
        await wait_for_file(state / f"entered-{phase}", child, log)
    finally:
        child.send_signal(signal.SIGKILL)
        child.wait(10)
    assert child.returncode == -signal.SIGKILL

    # A fresh, independent instance takes over. Its clock sees the dead process's leases as expired.
    monkeypatch.setattr(sandbox_runtime, "artifact_directory", lambda: state / "bin")
    backend = FileDocker(state)
    left_behind = backend.existing()
    assert bool(left_behind) == (phase != "create")  # only the undelivered create left no container
    store = await open_store(url)
    try:
        provider = FakeProvider(script=[[TextDelta("recovered"), Completed(Usage(5, 1, 0, 0))]])
        registry = ProviderRegistry()
        registry.register(provider)
        coordinator = SandboxCoordinator(store, backend, deployment="dep", owner="survivor", lease_seconds=LEASE,
                                         idle_seconds=1800.0, reap_seconds=1.0)
        executor = ClaimedExecutor(store, InMemoryEventBus(), registry, coordinator=coordinator)
        pool = WorkerPool(store, InMemoryEventBus(), executor, owner="survivor", coordinator=coordinator)
        store.coordination_clock = Frozen(await store.db.database_now() + LEASE + 1)
        try:
            await coordinator.open_admission()
            await pool.sweep_once()
            for _ in range(3):  # classification, orphaning and exact-ID removal may take separate passes
                await coordinator.reconcile_once()
        finally:
            store.coordination_clock = None

        lost = await store.get_run_by_id(run.id)
        terminal_event_count = len(await terminal_events(store, run.id))
        assert terminal_event_count == 1
        if phase == "release":
            assert lost.status == "completed"  # the run ended before its sandbox cleanup was cut short
        else:
            # Startup reconciliation (as the service runs it before its workers) may fail the run before the lease
            # sweep does; either way the run ends once, through the atomic terminal transition.
            assert lost.status == "failed" and lost.error_code in ("worker_lost", "sandbox_unavailable")
        usage = await store.get_run_usage(run.id)
        assert usage.state == "released"
        assert backend.existing() == [], "every container the dead process left was removed"
        journal = backend.journal()
        for old in left_behind:
            assert ("remove", old) in journal and ("inspect", old, "absent") in journal

        assert await coordinator.open_admission()
        nxt = await queue(store, "again")
        claimed = await executor.claim_next()
        assert claimed.id == nxt.id
        await executor.execute(claimed)
        assert (await store.get_run_by_id(nxt.id)).output == "recovered"
        assert await terminal_events(store, nxt.id) == ["run.completed"]
        current = backend.running()
        assert len(current) == 1 and current[0] not in left_behind
        journal = backend.journal()
        for old in left_behind:
            old_id_absence_was_verified_before_new_publish = absence_verified_before_create(journal, old, current[0])
            assert old_id_absence_was_verified_before_new_publish

        if phase == "create":
            # The dead process's create request is finally delivered: its retained tombstone quarantines it.
            create = await store.get_operation(usage.create_operation_id)
            assert create.resolved_at is None
            late = await backend.create_generation(create, parse_policy(SANDBOX, mount_root="/"))
            await coordinator.reconcile_once()
            assert backend.existing() == current
            assert ("remove", late.observation.container_id) in backend.journal()
            assert (await store.get_operation(create.id)).confirmed_absent_at is not None
    finally:
        await store.close()
