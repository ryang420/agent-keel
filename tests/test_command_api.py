"""Command runs over the HTTP API (docs/design/2026-10-31-command-runs.md)."""

import asyncio

import httpx
import pytest

from keel.api import Keel, create_app
from keel.config import ServiceCredential
from keel.events import InMemoryEventBus
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.runner import RunExecutor
from keel.worker import WorkerPool
from tests.sandbox_support import local_coordinator

AUTH = {"Authorization": "Bearer test-key"}
ALL = ("queued", "running", "stopping", "completed", "failed", "cancelled")


@pytest.fixture
async def boxed(store, tmp_path, monkeypatch):
    """A service whose runs use a coordinated sandbox over host processes. Profile p1 has a sandbox and no models."""
    coordinator, backend = await local_coordinator(store, tmp_path / "bin", monkeypatch)
    registry = ProviderRegistry()
    registry.register(FakeProvider())
    bus = InMemoryEventBus()
    executor = RunExecutor(store, bus, registry, coordinator=coordinator)
    pool = WorkerPool(store, bus, executor, owner="w1", workers=2, poll_seconds=0.01, coordinator=coordinator)
    keel = Keel(store=store, bus=bus, providers=registry, executor=executor,
                credentials=(ServiceCredential(1, "test-key"),), max_running=4, coordinator=coordinator,
                command_timeout_max_s=600)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(keel)), base_url="http://keel",
                               headers=AUTH)
    work = tmp_path / "work"
    (work / "job").mkdir(parents=True)
    profile = {"models": [], "sandbox": {"image": "local", "workdir": str(work)}}
    assert (await client.put("/v1/profiles/p1", json=profile)).status_code == 200
    await pool.start()
    yield client, store, work, backend
    await client.aclose()
    await pool.close(drain_seconds=0.1)


def command_run(work, *argv, session="job-1", **extra):
    return {"kind": "command", "session_id": session,
            "command": {"argv": list(argv), "cwd": str(work / "job"), "timeout_s": 60, **extra}}


async def settle(client, run_id, seconds=10.0):
    for _ in range(int(seconds / 0.02)):
        body = (await client.get(f"/v1/profiles/p1/runs/{run_id}")).json()
        if body["status"] in ("completed", "failed", "cancelled"):
            return body
        await asyncio.sleep(0.02)
    raise AssertionError(body)


async def test_a_command_run_on_a_profile_without_models_completes(boxed):
    client, store, work, _ = boxed
    started = await client.post("/v1/profiles/p1/runs", json=command_run(work, "sh", "-c", "echo hello"))
    assert started.status_code == 202, started.json()
    run = await settle(client, started.json()["run_id"])
    assert (run["status"], run["kind"], run["output"]) == ("completed", "command", "hello\n")
    assert run["command"] == {"argv": ["sh", "-c", "echo hello"], "cwd": str(work / "job"), "timeout_s": 60,
                              "env": {}}
    assert (run["provider"], run["model"], run["error_code"]) == (None, None, None)
    assert run["usage"] == {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}
    listed = (await client.get("/v1/profiles/p1/runs?status=completed")).json()["data"]
    assert [(r["run_id"], r["kind"]) for r in listed] == [(run["run_id"], "command")]
    assert [e.type for e in await store.events_after(run["run_id"], 0)] == [
        "run.started", "command.started", "command.completed", "run.completed"]


@pytest.mark.parametrize(("change", "message"), [
    (lambda work: {"provider": "fake"}, "takes no provider"),
    (lambda work: {"model": "m"}, "takes no model"),
    (lambda work: {"input": "hi"}, "takes no input"),
    (lambda work: {"instructions": "Be brief."}, "takes no instructions"),
    (lambda work: {"reasoning_effort": "low"}, "takes no reasoning_effort"),
    (lambda work: {"file_context": "x"}, "takes no file_context"),
    (lambda work: {"kind": "shell"}, "kind must be"),
    (lambda work: {"command": None}, "command must be an object"),
    (lambda work: {"command": {"argv": [], "cwd": str(work), "timeout_s": 5}}, "command.argv"),
    (lambda work: {"command": {"argv": ["-l", "true"], "cwd": str(work), "timeout_s": 5}}, "must not start with '-'"),
    (lambda work: {"command": {"argv": ["true"], "cwd": "/etc", "timeout_s": 5}}, "under the sandbox workdir"),
    (lambda work: {"command": {"argv": ["true"], "cwd": str(work), "timeout_s": 601}}, "from 1 to 600"),
    (lambda work: {"command": {"argv": ["true"], "cwd": str(work), "timeout_s": 5, "env": {"A": "x\ny"}}},
     "command.env"),
])
async def test_bad_command_runs_are_refused_before_anything_is_queued(boxed, change, message):
    client, store, work, _ = boxed
    response = await client.post("/v1/profiles/p1/runs", json={**command_run(work, "true"), **change(work)})
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_input"
    assert message in response.json()["error"]["message"]
    assert await store.list_runs("p1", ALL) == []


async def test_a_command_run_needs_a_sandboxed_profile_and_a_model_run_still_needs_a_model(boxed):
    client, _, work, _ = boxed
    assert (await client.put("/v1/profiles/p2", json={"models": []})).status_code == 200
    response = await client.post("/v1/profiles/p2/runs", json=command_run(work, "true"))
    assert response.status_code == 400 and "needs a profile with a sandbox" in response.json()["error"]["message"]
    refused = await client.post("/v1/profiles/p1/runs", json={"input": "hi", "provider": "fake", "model": "m"})
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "model_not_allowed"


async def test_model_runs_are_unchanged_and_refuse_a_command(boxed):
    client, store, work, _ = boxed
    profile = {"models": [{"provider": "fake", "model": "m"}], "sandbox": {"image": "local", "workdir": str(work)}}
    assert (await client.put("/v1/profiles/p1", json=profile)).status_code == 200
    mixed = await client.post("/v1/profiles/p1/runs", json={"input": "hi", "provider": "fake", "model": "m",
                                                           "command": {"argv": ["true"]}})
    assert mixed.status_code == 400 and "kind 'command'" in mixed.json()["error"]["message"]
    started = await client.post("/v1/profiles/p1/runs", json={"input": "hi", "provider": "fake", "model": "m",
                                                             "session_id": "chat"})
    run = await settle(client, started.json()["run_id"])
    assert (run["status"], run["kind"], run["provider"]) == ("completed", "model", "fake")
    assert "command" not in run
    first = (await store.events_after(run["run_id"], 0))[0]
    assert (first.type, first.data) == ("run.started", {"provider": "fake", "model": "m", "reasoning_effort": None})


async def test_one_run_per_session_and_stop_apply_to_command_runs(boxed):
    client, _, work, backend = boxed
    first = (await client.post("/v1/profiles/p1/runs", json=command_run(work, "sh", "-c", "sleep 60"))).json()
    busy = await client.post("/v1/profiles/p1/runs", json=command_run(work, "true"))
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "session_busy"
    for _ in range(250):
        if backend.exec_calls:
            break
        await asyncio.sleep(0.02)
    assert backend.exec_calls
    assert (await client.post(f"/v1/profiles/p1/runs/{first['run_id']}/stop")).status_code == 200
    assert (await settle(client, first["run_id"], seconds=15))["status"] == "cancelled"


async def test_another_tenants_key_cannot_start_a_command_run(store, tmp_path, monkeypatch):
    coordinator, _ = await local_coordinator(store, tmp_path / "bin", monkeypatch)
    registry = ProviderRegistry()
    bus = InMemoryEventBus()
    executor = RunExecutor(store, bus, registry, coordinator=coordinator)
    keel = Keel(store=store, bus=bus, providers=registry, executor=executor,
                credentials=(ServiceCredential(1, "key-a"), ServiceCredential(2, "key-b")), max_running=4,
                coordinator=coordinator, command_timeout_max_s=600)
    transport = httpx.ASGITransport(app=create_app(keel))
    work = tmp_path / "work"
    work.mkdir()
    async with httpx.AsyncClient(transport=transport, base_url="http://keel",
                                 headers={"Authorization": "Bearer key-a"}) as owner, \
            httpx.AsyncClient(transport=transport, base_url="http://keel",
                              headers={"Authorization": "Bearer key-b"}) as other:
        profile = {"models": [], "sandbox": {"image": "local", "workdir": str(work)}}
        assert (await owner.put("/v1/profiles/p1", json=profile)).status_code == 200
        response = await other.post("/v1/profiles/p1/runs", json=command_run(work, "true"))
        assert response.status_code == 404 and response.json()["error"]["code"] == "profile_not_found"
        assert await store.list_runs("p1", ALL) == []
