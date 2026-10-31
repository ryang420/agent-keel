"""Command runs: one command in the profile's sandbox, without a model (docs/design/2026-10-31-command-runs.md)."""

import asyncio
from pathlib import Path
import sys
import time

import pytest

from keel.commands import wrapped_argv
from keel.events import InMemoryEventBus
from keel.models import Usage
from keel.providers.base import ProviderRegistry
from keel.runner import RunExecutor
from keel.store.base import SessionBusy
from keel.worker import WorkerPool
from tests.sandbox_support import ClaimedExecutor, local_coordinator

SANDBOX = {"image": "img", "workdir": "/w"}
COMMAND = {"argv": ["python3", "job.py"], "cwd": "/w/job", "timeout_s": 60, "env": {"KEY": "value"}}


async def test_a_command_run_is_stored_without_a_model(store):
    await store.upsert_profile("p1", models=[], sandbox=SANDBOX, soul="", skills=[])
    await store.ensure_session("p1", "s1")
    run = await store.create_command_run(profile_id="p1", session_id="s1", command=COMMAND)
    assert (run.kind, run.command, run.status) == ("command", COMMAND, "queued")
    assert (run.provider, run.model, run.reasoning_effort, run.input, run.instructions) == (None, None, None, [], None)
    assert (await store.get_run("p1", run.id)).command == COMMAND
    with pytest.raises(SessionBusy):
        await store.create_command_run(profile_id="p1", session_id="s1", command=COMMAND)


async def test_a_model_run_reads_as_kind_model(store):
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="", skills=[])
    await store.ensure_session("p1", "s1")
    run = await store.create_run(profile_id="p1", session_id="s1", provider="fake", model="m", reasoning_effort=None,
                                 input_parts=[{"type": "text", "text": "hi"}], instructions=None)
    assert (run.kind, run.command) == ("model", None)
    assert (await store.get_run("p1", run.id)).kind == "model"


async def test_a_coordinated_claim_carries_the_command(store):
    await store.upsert_profile("p1", models=[], sandbox=SANDBOX, soul="", skills=[])
    await store.ensure_session("p1", "s1")
    run = await store.create_command_run(profile_id="p1", session_id="s1", command=COMMAND)
    claim = await store.claim_run_with_usage("w1", 30, deployment="dep", daemon_id="daemon-1")
    assert claim.run.id == run.id and claim.use is not None
    assert (claim.run.kind, claim.run.command) == ("command", COMMAND)


async def test_the_schema_refuses_a_model_run_without_a_model_and_a_command_run_without_a_command(store):
    await store.upsert_profile("p1", models=[], sandbox=SANDBOX, soul="", skills=[])
    for run_id, kind in (("run_a", "model"), ("run_b", "command")):
        with pytest.raises(Exception, match="runs_kind_fields"):
            await store.db.execute(
                "INSERT INTO runs (id, profile_id, session_id, status, input, created_at, kind) "
                "VALUES (?, 'p1', ?, 'queued', '[]', 1, ?)", run_id, run_id, kind)


@pytest.fixture
async def boxed(store, tmp_path, monkeypatch):
    """A sandbox-enabled executor over host processes; profile p1 has no models and one policy env variable."""
    work = tmp_path / "work"
    work.mkdir()
    await store.upsert_profile("p1", models=[], soul="", skills=[],
                               sandbox={"image": "local", "workdir": str(work), "env": {"FROM_POLICY": "policy"}})
    coordinator, backend = await local_coordinator(store, tmp_path / "bin", monkeypatch)
    executor = ClaimedExecutor(store, InMemoryEventBus(), ProviderRegistry(), coordinator=coordinator)
    return store, executor, backend, work


async def queue_command(store, argv, *, cwd, timeout_s=30, env=None, session="s1"):
    await store.ensure_session("p1", session)
    return await store.create_command_run(profile_id="p1", session_id=session, command={
        "argv": argv, "cwd": str(cwd), "timeout_s": timeout_s, "env": env or {}})


async def run_to_end(executor, store, run):
    claimed = await executor.claim_next()
    assert claimed.id == run.id and claimed.kind == "command"
    await executor.execute(claimed)
    return await store.get_run("p1", run.id)


async def events(store, run_id):
    return [(event.type, event.data) for event in await store.events_after(run_id, 0)]


async def wait_for_status(store, run_id, statuses, seconds=10.0):
    for _ in range(int(seconds / 0.02)):
        run = await store.get_run("p1", run_id)
        if run.status in statuses:
            return run
        await asyncio.sleep(0.02)
    raise AssertionError(f"run stayed {run.status}")


async def test_a_command_runs_once_in_its_directory_and_completes_with_its_output(boxed):
    store, executor, backend, work = boxed
    job = work / "job"
    job.mkdir()
    run = await queue_command(store, ["sh", "-c", 'pwd; echo "$FROM_POLICY $FROM_RUN"'], cwd=job,
                              env={"FROM_RUN": "run"})
    done = await run_to_end(executor, store, run)
    assert (done.status, done.error_code) == ("completed", None)
    lines = done.output.splitlines()
    assert Path(lines[0]).resolve() == job.resolve() and lines[1] == "policy run"
    assert done.usage == Usage(0, 0, 0, 0)
    assert done.current_exec_id is None
    seen = await events(store, run.id)
    assert [kind for kind, _ in seen] == ["run.started", "command.started", "command.completed", "run.completed"]
    assert seen[0][1] == {"kind": "command"}
    assert seen[1][1] == {"argv": run.command["argv"], "cwd": str(job)}
    completed = seen[2][1]
    assert (completed["exit_code"], completed["timed_out"], completed["output"]) == (0, False, done.output)
    assert completed["duration_s"] >= 0
    assert [argv for _, argv in backend.exec_calls] == [wrapped_argv(run.command)]  # one dispatch, nothing else
    assert await store.messages("s1") == []


async def test_a_non_zero_exit_fails_the_run_and_keeps_the_output(boxed):
    store, executor, _, work = boxed
    run = await queue_command(store, ["sh", "-c", "echo partial; echo oops >&2; exit 3"], cwd=work)
    done = await run_to_end(executor, store, run)
    assert (done.status, done.error_code, done.error) == ("failed", "command_failed", "The command exited with code 3.")
    assert "partial" in done.output and "oops" in done.output
    assert done.usage == Usage(0, 0, 0, 0)
    seen = await events(store, run.id)
    assert seen[2][0] == "command.completed" and seen[2][1]["exit_code"] == 3
    assert seen[-1] == ("run.failed", {"error_code": "command_failed", "message": "The command exited with code 3."})


async def test_a_missing_directory_fails_the_run_as_an_ordinary_exit(boxed):
    store, executor, _, work = boxed
    run = await queue_command(store, ["true"], cwd=work / "missing")
    done = await run_to_end(executor, store, run)
    assert (done.status, done.error_code) == ("failed", "command_failed")
    assert "keel: cannot enter" in done.output
    assert (await store.db.fetch("SELECT state FROM sandbox_execs")) == [{"state": "finished"}]


async def test_a_timeout_kills_the_command_and_fails_the_run(boxed):
    store, executor, _, work = boxed
    run = await queue_command(store, ["sh", "-c", "echo started; sleep 30"], cwd=work, timeout_s=1)
    started = time.monotonic()
    done = await run_to_end(executor, store, run)
    assert time.monotonic() - started < 15
    assert (done.status, done.error_code) == ("failed", "command_timed_out")
    assert "started" in done.output
    completed = dict(await events(store, run.id))["command.completed"]
    assert (completed["timed_out"], completed["exit_code"]) == (True, 124)


async def test_the_run_keeps_the_last_fifty_thousand_characters(boxed):
    store, executor, _, work = boxed
    script = "print('a' * 30000 + 'b' * 30000, end='')"
    done = await run_to_end(executor, store, await queue_command(store, [sys.executable, "-c", script], cwd=work))
    assert done.status == "completed"
    assert done.output == "a" * 20_000 + "b" * 30_000


async def test_an_unverified_cleanup_fails_the_run_as_sandbox_uncertain(boxed):
    store, executor, backend, work = boxed

    async def lose_the_receipt(ticket, argv):
        raise RuntimeError("the transport dropped")

    backend.exec_hook = lose_the_receipt
    run = await queue_command(store, ["true"], cwd=work)
    done = await run_to_end(executor, store, run)
    assert (done.status, done.error_code) == ("failed", "sandbox_uncertain")
    assert [kind for kind, _ in await events(store, run.id)] == ["run.started", "command.started", "run.failed"]
    assert (await store.db.fetch("SELECT state FROM sandbox_execs")) == [{"state": "uncertain"}]


async def test_a_command_run_without_a_sandbox_backend_fails_cleanly(store):
    await store.upsert_profile("p1", models=[], sandbox={"image": "img", "workdir": "/w"}, soul="", skills=[])
    run = await queue_command(store, ["true"], cwd="/w")
    claimed = await store.claim_run("w1", await store.coordination_now() + 60)
    await RunExecutor(store, InMemoryEventBus(), ProviderRegistry()).execute(claimed)
    done = await store.get_run("p1", run.id)
    assert (done.status, done.error_code) == ("failed", "sandbox_unavailable")
    assert done.usage == Usage(0, 0, 0, 0)


async def test_a_stop_during_a_command_cancels_it_through_the_supervisor(store, tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    await store.upsert_profile("p1", models=[], soul="", skills=[], sandbox={"image": "local", "workdir": str(work)})
    coordinator, backend = await local_coordinator(store, tmp_path / "bin", monkeypatch, lease_seconds=3)
    bus = InMemoryEventBus()
    executor = RunExecutor(store, bus, ProviderRegistry(), coordinator=coordinator)
    pool = WorkerPool(store, bus, executor, owner="w1", workers=1, poll_seconds=0.01, lease_seconds=3,
                      coordinator=coordinator)
    await pool.start()
    try:
        run = await queue_command(store, ["sh", "-c", "sleep 60"], cwd=work, timeout_s=120)
        for _ in range(250):
            if backend.exec_calls:
                break
            await asyncio.sleep(0.02)
        assert backend.exec_calls
        await store.request_stop("p1", run.id)
        await bus.notify("stop:" + run.id)
        done = await wait_for_status(store, run.id, ("cancelled",))
        assert done.current_exec_id is None and backend.control_calls  # cleared only after the supervisor's receipt
        assert [kind for kind, _ in await events(store, run.id)] == ["run.started", "command.started",
                                                                    "run.cancelled"]
    finally:
        await pool.close(drain_seconds=0.1)
