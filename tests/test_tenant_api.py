import asyncio
import contextlib

import httpx
import pytest

from keel.api import Keel, create_app
from keel.config import ServiceCredential
from keel.events import InMemoryEventBus
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.runner import RunExecutor
from keel.sandbox.base import profile_mount_root
from keel.worker import WorkerPool
from tests.test_api import PROFILE


@contextlib.asynccontextmanager
async def _clients(store, tmp_path, tenants: tuple[int, int]):
    provider = FakeProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    bus = InMemoryEventBus()
    executor = RunExecutor(store, bus, registry)
    pool = WorkerPool(store, bus, executor, owner="w1", workers=2, poll_seconds=0.01)
    keel = Keel(store=store, bus=bus, providers=registry, executor=executor,
                credentials=(ServiceCredential(tenants[0], "key-a"), ServiceCredential(tenants[1], "key-b")),
                max_running=2, sandbox_mount_root=str(tmp_path / "mounts"))
    app = create_app(keel)
    await pool.start()
    transport = httpx.ASGITransport(app=app)
    owner = httpx.AsyncClient(transport=transport, base_url="http://keel",
                              headers={"Authorization": "Bearer key-a"})
    other = httpx.AsyncClient(transport=transport, base_url="http://keel",
                              headers={"Authorization": "Bearer key-b"})
    try:
        yield owner, other
    finally:
        await owner.aclose()
        await other.aclose()
        await pool.close(drain_seconds=0.1)


@pytest.fixture
async def clients(store, tmp_path):
    async with _clients(store, tmp_path, (1, 2)) as pair:
        yield pair


async def test_tenant_zero_and_negative_tenants_are_isolated_like_any_other(store, tmp_path):
    async with _clients(store, tmp_path, (0, -2_147_483_648)) as (zero, negative):
        created = await zero.put("/v1/profiles/acct", json=PROFILE)
        assert created.status_code == 200
        assert (await store.get_profile("acct")).tenant_id == 0
        assert (await negative.put("/v1/profiles/acct", json={**PROFILE, "soul": "stolen"})).status_code == 404
        assert (await negative.get("/v1/profiles/acct/sessions")).status_code == 404
        assert (await zero.get("/v1/profiles/acct/sessions")).status_code == 200
        assert (await negative.put("/v1/profiles/neg", json=PROFILE)).status_code == 200
        assert (await store.get_profile("neg")).tenant_id == -2_147_483_648
        assert (await zero.get("/v1/profiles/neg/sessions")).status_code == 404


async def test_a_foreign_profile_is_a_404_and_cannot_be_replaced(clients, store):
    owner, other = clients
    created = await owner.put("/v1/profiles/acct", json={**PROFILE, "soul": "owner"})
    assert created.status_code == 200
    assert (await store.get_profile("acct")).tenant_id == 1
    forged = await owner.put("/v1/profiles/acct", json={**PROFILE, "soul": "changed", "tenant": 2})
    assert forged.status_code == 200 and forged.json()["soul"] == "changed"
    assert (await store.get_profile("acct")).tenant_id == 1  # body field is ignored
    stolen = await other.put("/v1/profiles/acct", json={**PROFILE, "soul": "stolen"})
    assert stolen.status_code == 404
    assert stolen.json()["error"] == {"code": "profile_not_found", "message": "No such profile."}
    assert (await store.get_profile("acct")).soul == "changed"
    missing = await other.get("/v1/profiles/nope/sessions")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "profile_not_found"


async def test_cross_tenant_session_run_and_stream_do_nothing(clients, store):
    owner, other = clients
    await owner.put("/v1/profiles/acct", json=PROFILE)
    started = await owner.post("/v1/profiles/acct/runs", json={
        "input": "hi", "session_id": "chat", "provider": "fake", "model": "m"})
    assert started.status_code == 202
    run_id = started.json()["run_id"]
    paths = [
        ("GET", "/v1/profiles/acct/sessions"),
        ("GET", "/v1/profiles/acct/sessions/chat/messages"),
        ("GET", "/v1/profiles/acct/sessions/chat/boundary"),
        ("PATCH", "/v1/profiles/acct/sessions/chat"),
        ("DELETE", "/v1/profiles/acct/sessions/chat"),
        ("GET", "/v1/profiles/acct/runs"),
        ("GET", f"/v1/profiles/acct/runs/{run_id}"),
        ("POST", f"/v1/profiles/acct/runs/{run_id}/stop"),
        ("GET", f"/v1/profiles/acct/runs/{run_id}/events"),
    ]
    for method, path in paths:
        response = await other.request(method, path, json={"title": "x"} if method == "PATCH" else None)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "profile_not_found"
        assert "text/event-stream" not in response.headers.get("content-type", "")
        assert "data:" not in response.text
    assert (await store.get_session("acct", "chat")) is not None
    assert (await store.get_run("acct", run_id)).status != "cancelled"
    listed = await owner.get("/v1/profiles/acct/sessions")
    assert listed.status_code == 200 and listed.json()["data"]


async def test_two_profiles_in_one_tenant_do_not_share_a_session_id_or_memory(clients, store):
    owner, _other = clients
    await owner.put("/v1/profiles/p1", json={**PROFILE, "memory": True})
    await owner.put("/v1/profiles/p2", json={**PROFILE, "memory": True})
    first = await owner.post("/v1/profiles/p1/runs", json={
        "input": "hi", "session_id": "chat", "provider": "fake", "model": "m"})
    assert first.status_code == 202
    second = await owner.post("/v1/profiles/p2/runs", json={
        "input": "hi", "session_id": "chat", "provider": "fake", "model": "m"})
    assert second.status_code == 404 and second.json()["error"]["code"] == "session_not_found"
    await store.apply_memory("p1", "memory", 2200, [{"action": "add", "content": "FACT-P1"}])
    await store.apply_memory("p2", "memory", 2200, [{"action": "add", "content": "FACT-P2"}])
    assert (await store.memory_entries("p1"))["memory"] == ["FACT-P1"]
    assert (await store.memory_entries("p2"))["memory"] == ["FACT-P2"]
    sessions = (await owner.get("/v1/profiles/p2/sessions")).json()["data"]
    assert sessions == []


async def test_private_mounts_cannot_overlap_another_profile(clients, store, tmp_path):
    owner, other = clients
    root = str(tmp_path / "mounts")
    p1_root = profile_mount_root(root, "p1")
    sandbox = {"image": "python:3.12-slim", "workdir": "/work", "mounts": [
        {"source": p1_root + "/private", "target": "/data"}]}
    body = {**PROFILE, "sandbox": sandbox}
    assert (await owner.put("/v1/profiles/p1", json=body)).status_code == 200
    for client, profile_id in ((owner, "p2"), (other, "p3")):
        response = await client.put(f"/v1/profiles/{profile_id}", json={
            **body, "sandbox": {**sandbox, "mounts": [
                {"source": p1_root + "/private/child", "target": "/secret", "read_only": True}]}})
        assert response.status_code == 400
        assert await store.get_profile(profile_id) is None
    own = {**body, "sandbox": {**sandbox, "mounts": [
        {"source": profile_mount_root(root, "p2") + "/private", "target": "/data"}]}}
    assert (await owner.put("/v1/profiles/p2", json=own)).status_code == 200
    nested = {**body, "sandbox": {**sandbox, "mounts": [
        {"source": p1_root, "target": "/parent"},
        {"source": p1_root + "/private", "target": "/data"}]}}
    assert (await owner.put("/v1/profiles/p1", json=nested)).status_code == 400
    removed = await owner.put("/v1/profiles/p1", json={**PROFILE, "sandbox": {}})
    assert removed.status_code == 400
    assert (await store.get_profile("p1")).sandbox == sandbox


async def test_a_legacy_mount_policy_requires_operator_migration(clients, store):
    owner, _other = clients
    await store.upsert_profile("legacy", models=PROFILE["models"], sandbox={
        "image": "python:3.12-slim", "workdir": "/work",
        "mounts": [{"source": "/old/location", "target": "/data"}]}, soul="", skills=[], tenant_id=1)
    response = await owner.put("/v1/profiles/legacy", json=PROFILE)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "sandbox_requires_migration"


async def test_concurrent_profiles_cannot_claim_the_same_mount(clients, tmp_path):
    owner, other = clients
    source = profile_mount_root(str(tmp_path / "mounts"), "p1") + "/exclusive"
    body = {**PROFILE, "sandbox": {"image": "python:3.12-slim", "workdir": "/work", "mounts": [
        {"source": source, "target": "/private"}]}}
    first, second = await asyncio.gather(
        owner.put("/v1/profiles/p1", json=body), other.put("/v1/profiles/p2", json=body))
    assert sorted((first.status_code, second.status_code)) == [200, 400]
