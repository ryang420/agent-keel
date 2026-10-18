"""Fault injection (M3 design §1): a lost worker, provider 5xx, a failing sandbox, a vanished container and a killed
server each leave the session consistent, and the next turn works."""

import asyncio
import inspect
import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import uuid

import httpx
import pytest

from keel.events import InMemoryEventBus
from keel.models import Completed, TextDelta, ToolCall, ToolCallReady, Usage
from keel.providers.base import ModelError, ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.runner import CANCELLED_TOOL_RESULT, RunExecutor
from keel.store import open_store
from keel.worker import WorkerPool
from tests.sandbox_support import ClaimedExecutor, local_coordinator

needs_docker = pytest.mark.skipif(os.environ.get("KEEL_TEST_DOCKER") != "1", reason="set KEEL_TEST_DOCKER=1")
needs_subprocess = pytest.mark.skipif(os.environ.get("KEEL_TEST_SUBPROCESS") != "1",
                                      reason="set KEEL_TEST_SUBPROCESS=1")
IMAGE = os.environ.get("KEEL_TEST_DOCKER_IMAGE", "python:3.12-slim")
TERMINAL_EVENTS = {"run.completed", "run.failed", "run.cancelled"}


async def no_sleep(seconds):
    pass


def call(call_id, command):
    return ToolCallReady(ToolCall(call_id, "terminal", json.dumps({"command": command})))


def answer(text="done"):
    return [TextDelta(text), Completed(Usage(10, 2, 0, 0))]


@pytest.fixture
async def parts(store, tmp_path, monkeypatch, clock):
    """A sandboxed profile whose runs are claimed through a coordinator on a database-aligned fake clock."""
    clock.now = await store.db.database_now()
    store.coordination_clock = clock
    provider = FakeProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    work = tmp_path / "work"
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], soul="", skills=[],
                               sandbox={"image": "local", "workdir": str(work)})
    coordinator, backend = await local_coordinator(store, tmp_path / "bin", monkeypatch, owner="worker-b")
    executor = ClaimedExecutor(store, InMemoryEventBus(), registry, coordinator=coordinator, sleep=no_sleep)
    return store, provider, registry, backend, executor, work


async def start(executor, text="hi", session="s1"):
    store = executor.store
    await store.ensure_session("p1", session)
    await store.create_run(profile_id="p1", session_id=session, provider="fake", model="m", reasoning_effort=None,
                           input_parts=[{"type": "text", "text": text}], instructions=None)
    return await executor.claim_next()


async def consistent(store, session="s1"):
    """The session's messages, after checking every tool call has exactly one result before the next user message."""
    messages = [m.message for m in await store.messages(session)]
    open_calls = set()
    for message in messages:
        if message.role == "user":
            assert not open_calls, f"unanswered calls {open_calls} before a user message"
        if message.role == "tool":
            assert message.tool_call_id in open_calls, f"a result for an unknown call {message.tool_call_id}"
            open_calls.remove(message.tool_call_id)
        open_calls.update(c.id for c in message.tool_calls)
    assert not open_calls
    return messages


async def one_terminal_event(store, run_id):
    """The run's terminal event, after checking it has exactly one."""
    terminal = [e for e in await store.events_after(run_id, 0) if e.type in TERMINAL_EVENTS]
    assert len(terminal) == 1, f"{run_id} has terminal events {[e.type for e in terminal]}"
    return terminal[0]


class Dying:
    """The store as a worker sees it when it dies: once `dead` is set, every call fails."""

    def __init__(self, store):
        self._store, self.dead = store, False

    def __getattr__(self, name):
        attr = getattr(self._store, name)
        if not inspect.iscoroutinefunction(attr):
            return attr

        async def call(*args, **kwargs):
            if self.dead:
                raise ConnectionError("the worker is gone")
            return await attr(*args, **kwargs)

        return call


async def test_a_worker_that_dies_mid_command_is_swept_its_generation_removed_and_the_next_turn_works(
        parts, clock, tmp_path, monkeypatch):
    store, provider, registry, backend, executor, work = parts
    provider.script = [[call("c1", f"sleep 30; touch {work}/late"), Completed(Usage(5, 1, 0, 0))]]
    dying = Dying(store)
    coordinator_a, _ = await local_coordinator(dying, tmp_path / "bin", monkeypatch, owner="worker-a",
                                               backend=backend)
    worker_a = RunExecutor(dying, InMemoryEventBus(), registry, coordinator=coordinator_a, sleep=no_sleep)
    await store.ensure_session("p1", "s1")
    await store.create_run(profile_id="p1", session_id="s1", provider="fake", model="m", reasoning_effort=None,
                           input_parts=[{"type": "text", "text": "hi"}], instructions=None)
    claim = await coordinator_a.claim()
    run = claim.run
    abandoned = asyncio.create_task(worker_a.execute(run, usage=claim.use))
    try:
        for _ in range(250):
            current = await store.get_run_by_id(run.id)
            if current.current_exec_id and backend.local._groups:
                break
            await asyncio.sleep(0.02)
        assert current.current_exec_id
        pgid = backend.local._groups[current.current_exec_id]
        dying.dead = True
        clock.now += 31
        pool_b = WorkerPool(store, InMemoryEventBus(), executor, owner="worker-b", clock=clock,
                            coordinator=executor.coordinator)
        assert await pool_b.sweep_once() == 1  # termination first, then recovery of the expired usage
        await executor.coordinator.reconcile_once()
        assert backend.removed_ids == [backend.created_ids[0]]  # recovery removes the whole generation
        assert await group_gone(pgid)
        await asyncio.wait_for(abandoned, 10)  # its command ended with the container, so worker A returned
        lost = await store.get_run("p1", run.id)
        assert (lost.status, lost.error_code) == ("failed", "worker_lost")
        assert (await one_terminal_event(store, run.id)).data["error_code"] == "worker_lost"  # the sweeper's
        assert (await store.get_run_usage(run.id)).state == "released"
        provider.script = [answer("recovered")]
        nxt = await start(executor, "again")
        await executor.execute(nxt)
        assert (await store.get_run("p1", nxt.id)).output == "recovered"
        assert (await one_terminal_event(store, nxt.id)).type == "run.completed"
        assert backend.created_id != backend.created_ids[0]  # a new generation
        messages = await consistent(store)
        assert [m.role for m in messages] == ["user", "assistant", "tool", "user", "assistant"]
        assert messages[2].text == CANCELLED_TOOL_RESULT
        assert not (work / "late").exists()
    finally:
        abandoned.cancel()
        await asyncio.gather(abandoned, return_exceptions=True)
        for exec_id in list(backend.local._groups):
            await backend.local.kill_tree(None, exec_id)


async def group_gone(pgid, seconds=5.0):
    for _ in range(int(seconds / 0.05)):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.05)
    return False


async def test_three_provider_5xx_fail_the_run_and_the_next_turn_works(parts):
    store, provider, _, _, executor, _ = parts
    provider.script = [[ModelError("server_error", "upstream 503")] for _ in range(3)] + [answer("back")]
    run = await start(executor)
    await executor.execute(run)
    failed = await store.get_run("p1", run.id)
    assert (failed.status, failed.error_code) == ("failed", "server_error")
    assert len(provider.requests) == 3
    nxt = await start(executor, "again")
    await executor.execute(nxt)
    assert (await store.get_run("p1", nxt.id)).output == "back"
    assert (await one_terminal_event(store, run.id)).type == "run.failed"
    assert (await one_terminal_event(store, nxt.id)).type == "run.completed"
    assert [m.role for m in await consistent(store)] == ["user", "user", "assistant"]


async def test_two_provider_5xx_then_success_complete_and_count_usage_once(parts):
    store, provider, _, _, executor, _ = parts
    provider.script = [[ModelError("server_error", "upstream 503")] for _ in range(2)] + [answer("fine")]
    run = await start(executor)
    await executor.execute(run)
    done = await store.get_run("p1", run.id)
    assert (done.status, done.output, done.usage) == ("completed", "fine", Usage(10, 2, 0, 0))
    assert len(provider.requests) == 3
    assert (await one_terminal_event(store, run.id)).type == "run.completed"
    await consistent(store)


async def test_a_provider_failing_after_output_is_not_retried_and_the_next_turn_works(parts):
    store, provider, _, _, executor, _ = parts
    provider.script = [[TextDelta("partial "), ModelError("server_error", "connection reset")], answer("next")]
    run = await start(executor)
    await executor.execute(run)
    failed = await store.get_run("p1", run.id)
    assert (failed.status, failed.error_code, failed.output) == ("failed", "server_error", "partial ")
    assert len(provider.requests) == 1
    nxt = await start(executor, "again")
    await executor.execute(nxt)
    assert (await store.get_run("p1", nxt.id)).output == "next"
    assert (await one_terminal_event(store, run.id)).type == "run.failed"
    assert (await one_terminal_event(store, nxt.id)).type == "run.completed"
    await consistent(store)


async def test_a_failed_sandbox_call_ends_the_run_and_the_next_run_gets_a_recovered_generation(parts):
    # The old backend recreated a vanished container and retried mid-run; a failed sandbox use now fails the run
    # as sandbox_unavailable, and recovery replaces the generation before the next run.
    store, provider, _, backend, executor, _ = parts
    failures = [ConnectionError("the container is restarting")]

    async def flaky(ticket, argv):
        if failures:
            raise failures.pop()

    backend.exec_hook = flaky
    provider.script = [[call("c1", "echo one"), Completed(Usage(5, 1, 0, 0))], answer("never")]
    run = await start(executor)
    await executor.execute(run)
    failed = await store.get_run("p1", run.id)
    assert (failed.status, failed.error_code) == ("failed", "sandbox_unavailable")
    assert (await one_terminal_event(store, run.id)).data["error_code"] == "sandbox_unavailable"
    results = [m for m in await consistent(store) if m.role == "tool"]
    assert "sandbox is unavailable" in results[0].text
    first = backend.created_id
    clock = store.coordination_clock
    clock.now += 31  # the usage stays protective until its lease lapses; then recovery removes the generation
    await executor.coordinator.reconcile_once()
    assert backend.removed_ids == [first]
    provider.script = [[call("c2", "echo two"), Completed(Usage(5, 1, 0, 0))], answer("done")]
    nxt = await start(executor, "again")
    await executor.execute(nxt)
    assert (await store.get_run("p1", nxt.id)).output == "done"
    assert backend.created_id != first
    results = [m for m in await consistent(store) if m.role == "tool"]
    assert json.loads(results[-1].text)["output"] == "two"


@needs_docker
async def test_a_container_removed_between_two_tool_calls_fails_the_run_and_recovery_replaces_it(store):
    from keel.sandbox.coordinator import SandboxCoordinator
    from keel.sandbox.docker import DockerSandbox

    backend = DockerSandbox(deployment=f"test-{uuid.uuid4().hex[:8]}", kill_grace_s=1.0)
    coordinator = SandboxCoordinator(store, backend, deployment=backend.deployment, owner="w1", lease_seconds=30.0,
                                     idle_seconds=1800.0, reap_seconds=5.0)

    class RemoveBeforeTheSecondCall(FakeProvider):
        async def stream(self, request):
            if len(self.requests) == 1:
                row = await store.db.fetchrow("SELECT container_id FROM sandboxes WHERE profile_id = ?", "p1")
                await backend._docker("rm", "-f", row["container_id"])
            async for event in super().stream(request):
                yield event

    provider = RemoveBeforeTheSecondCall(script=[[call("c1", "echo one"), Completed(Usage(5, 1, 0, 0))],
                                                 [call("c2", "echo two"), Completed(Usage(5, 1, 0, 0))],
                                                 answer("never")])
    registry = ProviderRegistry()
    registry.register(provider)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], soul="", skills=[],
                               sandbox={"image": IMAGE, "workdir": "/work"})
    executor = ClaimedExecutor(store, InMemoryEventBus(), registry, coordinator=coordinator, sleep=no_sleep)
    try:
        run = await start(executor)
        await executor.execute(run)
        failed = await store.get_run("p1", run.id)
        assert (failed.status, failed.error_code) == ("failed", "sandbox_unavailable")
        await consistent(store)
        await store.db.execute("UPDATE sandbox_uses SET lease_expires_at = 0 WHERE run_id = ?", run.id)
        await coordinator.reconcile_once()
        provider.script = [[call("c3", "echo three"), Completed(Usage(5, 1, 0, 0))], answer("done")]
        nxt = await start(executor, "again")
        await executor.execute(nxt)
        assert (await store.get_run("p1", nxt.id)).output == "done"
        assert json.loads([m for m in await consistent(store) if m.role == "tool"][-1].text)["output"] == "three"
    finally:
        await backend.remove_orphans()


LAUNCHER = textwrap.dedent("""
    import sys

    import uvicorn

    from keel.config import ServiceCredential, Settings
    from keel.models import TextDelta
    from keel.providers.base import ProviderRegistry
    from keel.providers.fake import FakeProvider
    from keel.service import create_service

    db, port, mode = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    providers = ProviderRegistry()
    providers.register(FakeProvider(script=[[TextDelta("par"), "hang"]] if mode == "hang" else []))
    settings = Settings(credentials=(ServiceCredential(1, "k"),), database_url=f"sqlite:///{db}",
                        sandbox="none", workers=1, lease_seconds=2.0)
    uvicorn.run(create_service(settings, providers=providers), host="127.0.0.1", port=port, log_level="warning")
""")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def wait_healthy(client, server, seconds=20.0):
    for _ in range(int(seconds / 0.1)):
        if server.poll() is not None:
            raise AssertionError(f"the server exited with {server.returncode}")
        try:
            if (await client.get("/healthz")).status_code == 200:
                return
        except httpx.TransportError:
            pass
        await asyncio.sleep(0.1)
    raise AssertionError("the server did not start")


async def wait_status(client, run_id, statuses, seconds=20.0):
    for _ in range(int(seconds / 0.1)):
        body = (await client.get(f"/v1/profiles/p1/runs/{run_id}")).json()
        if body["status"] in statuses:
            return body
        await asyncio.sleep(0.1)
    raise AssertionError(body)


@needs_subprocess
async def test_a_server_killed_with_sigkill_loses_its_run_and_the_session_continues(tmp_path):
    db, launcher = tmp_path / "keel.sqlite3", tmp_path / "launch.py"
    launcher.write_text(LAUNCHER)
    port = free_port()

    def spawn(mode):
        return subprocess.Popen([sys.executable, str(launcher), str(db), str(port), mode])

    server = spawn("hang")
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers={"Authorization": "Bearer k"},
                                     timeout=10) as client:
            await wait_healthy(client, server)
            await client.put("/v1/profiles/p1", json={"models": [{"provider": "fake", "model": "m"}]})
            payload = {"input": "hi", "session_id": "s1", "provider": "fake", "model": "m"}
            run_id = (await client.post("/v1/profiles/p1/runs", json=payload)).json()["run_id"]
            await wait_status(client, run_id, {"running"})
            watcher = await open_store(f"sqlite:///{db}")
            try:
                for _ in range(250):  # the delta follows the stored user message
                    if any(e.type == "message.delta" for e in await watcher.events_after(run_id, 0)):
                        break
                    await asyncio.sleep(0.02)
            finally:
                await watcher.close()
            server.send_signal(signal.SIGKILL)
            server.wait(10)
            server = spawn("ok")
            await wait_healthy(client, server)
            lost = await wait_status(client, run_id, {"failed"})
            assert lost["error_code"] == "worker_lost"
            again = {**payload, "input": "again"}
            nxt = (await client.post("/v1/profiles/p1/runs", json=again)).json()["run_id"]
            assert (await wait_status(client, nxt, {"completed"}))["output"] == "ok"
    finally:
        server.kill()
        server.wait(10)
    store = await open_store(f"sqlite:///{db}")
    try:
        assert (await one_terminal_event(store, run_id)).data["error_code"] == "worker_lost"
        assert (await one_terminal_event(store, nxt)).type == "run.completed"
        assert [m.role for m in await consistent(store)] == ["user", "user", "assistant"]
    finally:
        await store.close()
