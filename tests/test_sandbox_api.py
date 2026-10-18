"""HTTP behaviour of sandbox policy updates and enqueue under the profile guard (design section 12)."""

import asyncio
import uuid

import httpx
import pytest

from keel.api import Keel, create_app
from keel.config import ServiceCredential
from keel.events import InMemoryEventBus
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.runner import RunExecutor
from keel.sandbox.base import profile_mount_root

MODELS = [{"provider": "fake", "model": "m"}]
SANDBOX = {"image": "img", "workdir": "/w"}
OTHER_SANDBOX = {"image": "img2", "workdir": "/w"}
PROFILE = {"models": MODELS, "sandbox": SANDBOX, "soul": "old soul", "skills": []}
DEPLOYMENT, DAEMON = "dep", "daemon-1"


@pytest.fixture
async def api(store, tmp_path):
    """Two tenants' clients over one store, with no workers: queued runs stay queued until a test moves them."""
    registry = ProviderRegistry()
    registry.register(FakeProvider())
    bus = InMemoryEventBus()
    keel = Keel(store=store, bus=bus, providers=registry, executor=RunExecutor(store, bus, registry),
                credentials=(ServiceCredential(1, "key-a"), ServiceCredential(2, "key-b")), max_running=3,
                sandbox_mount_root=str(tmp_path / "mounts"))
    transport = httpx.ASGITransport(app=create_app(keel))
    owner = httpx.AsyncClient(transport=transport, base_url="http://keel", headers={"Authorization": "Bearer key-a"})
    other = httpx.AsyncClient(transport=transport, base_url="http://keel", headers={"Authorization": "Bearer key-b"})
    try:
        yield owner, other, store
    finally:
        await owner.aclose()
        await other.aclose()


async def queue(client, session: str = "s1") -> str:
    response = await client.post("/v1/profiles/p1/runs", json={
        "input": "hi", "session_id": session, "provider": "fake", "model": "m"})
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def assert_busy(response) -> None:
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "sandbox_busy"


async def insert_operation(store, *, kind: str, authority: str, outcome: str, generation_token: str,
                           original_create: str | None = None, container: str | None = None) -> str:
    operation_id = "op_" + uuid.uuid4().hex
    now = await store.coordination_now()
    await store.db.execute(
        "INSERT INTO sandbox_operations (id, kind, deployment, profile_id, daemon_id, generation, generation_token, "
        "intended_name, original_create_operation_id, container_id, owner, owner_token, authority, retired_at, "
        "outcome, created_at, updated_at) VALUES (?, ?, ?, 'p1', ?, 1, ?, ?, ?, ?, 'w1', ?, ?, ?, ?, ?, ?)",
        operation_id, kind, DEPLOYMENT, DAEMON, generation_token, "keel-sbx-" + operation_id, original_create,
        container, uuid.uuid4().hex, authority, now if authority == "retired" else None, outcome, now, now)
    return operation_id


@pytest.mark.parametrize("status", ["queued", "running", "stopping"])
async def test_a_policy_change_is_busy_while_a_run_is_in_flight(api, status):
    owner, _, store = api
    assert (await owner.put("/v1/profiles/p1", json=PROFILE)).status_code == 200
    run_id = await queue(owner)
    await store.db.execute("UPDATE runs SET status = ? WHERE id = ?", status, run_id)
    response = await owner.put("/v1/profiles/p1", json={**PROFILE, "sandbox": OTHER_SANDBOX, "soul": "new soul"})
    assert_busy(response)
    # The refusal is atomic: no other field of the replacement was applied.
    profile = await store.get_profile("p1")
    assert profile.soul == "old soul" and profile.sandbox == SANDBOX


@pytest.mark.parametrize("state", ["preparing", "active", "cleaning", "orphaned"])
async def test_a_policy_change_is_busy_while_a_usage_is_unreleased(api, state):
    owner, _, store = api
    await owner.put("/v1/profiles/p1", json=PROFILE)
    run_id = await queue(owner)
    claim = await store.claim_run_with_usage("w1", 30, deployment=DEPLOYMENT, daemon_id=DAEMON)
    assert claim is not None and claim.use is not None
    # The run already ended; its usage alone still protects the sandbox until it is released.
    await store.db.execute("UPDATE runs SET status = 'completed' WHERE id = ?", run_id)
    await store.db.execute("UPDATE sandbox_uses SET state = ? WHERE id = ?", state, claim.use.usage_id)
    assert_busy(await owner.put("/v1/profiles/p1", json={**PROFILE, "sandbox": OTHER_SANDBOX}))
    await store.db.execute("UPDATE sandbox_uses SET state = 'released', released_at = 1 WHERE id = ?",
                           claim.use.usage_id)
    changed = await owner.put("/v1/profiles/p1", json={**PROFILE, "sandbox": OTHER_SANDBOX})
    assert changed.status_code == 200 and changed.json()["sandbox"] == OTHER_SANDBOX


async def test_an_equivalent_policy_and_other_fields_change_while_busy(api):
    owner, _, store = api
    await owner.put("/v1/profiles/p1", json=PROFILE)
    await queue(owner)
    same = {"workdir": "/w", "image": "img", "runtime": "runc", "env": {}, "mounts": []}
    response = await owner.put("/v1/profiles/p1", json={**PROFILE, "sandbox": same, "soul": "new soul",
                                                        "memory": True})
    assert response.status_code == 200, response.text
    profile = await store.get_profile("p1")
    assert profile.soul == "new soul" and profile.memory is True


async def test_omitting_the_sandbox_clears_it_and_counts_as_a_change(api):
    owner, _, store = api
    await owner.put("/v1/profiles/p1", json=PROFILE)
    run_id = await queue(owner)
    assert_busy(await owner.put("/v1/profiles/p1", json={"models": MODELS, "soul": "new soul"}))
    assert (await store.get_profile("p1")).sandbox == SANDBOX
    assert (await owner.post(f"/v1/profiles/p1/runs/{run_id}/stop")).json()["status"] == "cancelled"
    cleared = await owner.put("/v1/profiles/p1", json={"models": MODELS, "soul": "new soul"})
    assert cleared.status_code == 200 and cleared.json()["sandbox"] == {}


async def test_enabling_a_sandbox_counts_as_a_change(api):
    owner, _, store = api
    await owner.put("/v1/profiles/p1", json={**PROFILE, "sandbox": {}})
    await queue(owner)
    assert_busy(await owner.put("/v1/profiles/p1", json=PROFILE))
    assert (await store.get_profile("p1")).sandbox == {}
    # A chat-only replacement of a chat-only profile is not a sandbox change.
    assert (await owner.put("/v1/profiles/p1", json={**PROFILE, "sandbox": {}, "soul": "x"})).status_code == 200


async def test_another_tenant_sees_404_not_the_sandbox_state(api):
    owner, other, store = api
    await owner.put("/v1/profiles/p1", json=PROFILE)
    await queue(owner)
    refused = await other.put("/v1/profiles/p1", json={**PROFILE, "sandbox": OTHER_SANDBOX})
    assert refused.status_code == 404 and refused.json()["error"]["code"] == "profile_not_found"
    assert (await store.get_profile("p1")).sandbox == SANDBOX


async def test_established_mounts_stay_immutable_on_an_idle_profile(api, tmp_path):
    owner, _, _ = api
    source = profile_mount_root(str(tmp_path / "mounts"), "p1") + "/data"
    mounted = {**SANDBOX, "mounts": [{"source": source, "target": "/data"}]}
    assert (await owner.put("/v1/profiles/p1", json={**PROFILE, "sandbox": mounted})).status_code == 200
    moved = {**SANDBOX, "mounts": [{"source": source, "target": "/elsewhere"}]}
    refused = await owner.put("/v1/profiles/p1", json={**PROFILE, "sandbox": moved})
    assert refused.status_code == 400 and "mounts" in refused.json()["error"]["message"]


async def test_an_unresolved_current_operation_makes_a_policy_change_busy(api):
    owner, _, store = api
    await owner.put("/v1/profiles/p1", json=PROFILE)
    await insert_operation(store, kind="create", authority="current", outcome="dispatched",
                           generation_token=uuid.uuid4().hex)
    assert_busy(await owner.put("/v1/profiles/p1", json={**PROFILE, "sandbox": OTHER_SANDBOX}))


async def test_an_uncertain_retired_start_makes_a_policy_change_busy(api):
    owner, _, store = api
    await owner.put("/v1/profiles/p1", json=PROFILE)
    token = uuid.uuid4().hex
    create = await insert_operation(store, kind="create", authority="retired", outcome="succeeded",
                                    generation_token=token)
    await insert_operation(store, kind="start", authority="retired", outcome="uncertain", generation_token=token,
                           original_create=create, container="c" * 64)
    assert_busy(await owner.put("/v1/profiles/p1", json={**PROFILE, "sandbox": OTHER_SANDBOX}))


async def test_a_retired_stopped_only_create_and_its_cleanup_never_make_an_idle_profile_busy(api):
    owner, _, store = api
    await owner.put("/v1/profiles/p1", json=PROFILE)
    # An undelivered create keeps its tombstone indefinitely; another retired create is being cleaned by exact ID.
    await insert_operation(store, kind="create", authority="retired", outcome="uncertain",
                           generation_token=uuid.uuid4().hex)
    token = uuid.uuid4().hex
    create = await insert_operation(store, kind="create", authority="retired", outcome="uncertain",
                                    generation_token=token)
    cleanup = await insert_operation(store, kind="remove", authority="current", outcome="dispatched",
                                     generation_token=token, original_create=create, container="d" * 64)
    await store.db.execute("UPDATE sandbox_operations SET cleanup_operation_id = ?, discovered_container_id = ? "
                           "WHERE id = ?", cleanup, "d" * 64, create)
    changed = await owner.put("/v1/profiles/p1", json={**PROFILE, "sandbox": OTHER_SANDBOX})
    assert changed.status_code == 200, changed.text


async def test_an_enqueue_holding_the_profile_guard_makes_a_concurrent_policy_change_busy(api, monkeypatch):
    owner, _, store = api
    await owner.put("/v1/profiles/p1", json=PROFILE)
    entered, release = asyncio.Event(), asyncio.Event()
    real = store.create_run

    async def held(**kwargs):
        entered.set()
        await release.wait()
        return await real(**kwargs)

    monkeypatch.setattr(store, "create_run", held)
    enqueue = asyncio.create_task(queue(owner))
    await asyncio.wait_for(entered.wait(), 5)
    update = asyncio.create_task(owner.put("/v1/profiles/p1", json={**PROFILE, "sandbox": OTHER_SANDBOX}))
    await asyncio.sleep(0.2)
    assert not update.done()  # the policy update waits for the enqueue's guard
    release.set()
    await enqueue
    assert_busy(await update)
    assert (await store.get_profile("p1")).sandbox == SANDBOX


async def test_an_enqueue_after_a_policy_update_validates_against_it(api, monkeypatch):
    owner, _, store = api
    await owner.put("/v1/profiles/p1", json=PROFILE)
    entered, release = asyncio.Event(), asyncio.Event()
    real = store.upsert_profile

    async def held(*args, **kwargs):
        entered.set()
        await release.wait()
        return await real(*args, **kwargs)

    monkeypatch.setattr(store, "upsert_profile", held)
    replacement = {**PROFILE, "models": [{"provider": "fake", "model": "other"}]}
    update = asyncio.create_task(owner.put("/v1/profiles/p1", json=replacement))
    await asyncio.wait_for(entered.wait(), 5)
    enqueue = asyncio.create_task(owner.post("/v1/profiles/p1/runs", json={
        "input": "hi", "session_id": "s1", "provider": "fake", "model": "m"}))
    await asyncio.sleep(0.2)
    release.set()
    assert (await update).status_code == 200
    refused = await enqueue
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "model_not_allowed"
    assert await store.list_runs("p1", ("queued",)) == []


async def test_a_recovering_profile_still_queues_within_capacity_and_stops(api):
    owner, _, store = api
    await owner.put("/v1/profiles/p1", json=PROFILE)
    now = await store.coordination_now()
    await store.db.execute(
        "INSERT INTO sandboxes (deployment, profile_id, daemon_id, generation, state, transitioned_at) "
        "VALUES (?, 'p1', ?, 1, 'draining', ?)", DEPLOYMENT, DAEMON, now)
    runs = [await queue(owner, session=f"s{index}") for index in range(3)]
    assert [run["status"] for run in (await owner.get("/v1/profiles/p1/runs")).json()["data"]] == ["queued"] * 3
    full = await owner.post("/v1/profiles/p1/runs", json={
        "input": "hi", "session_id": "s9", "provider": "fake", "model": "m"})
    assert full.status_code == 429 and full.json()["error"]["code"] == "at_capacity"
    # The draining profile consumes no claim, and a queued stop still commits its terminal event atomically.
    assert await store.claim_run_with_usage("w1", 30, deployment=DEPLOYMENT, daemon_id=DAEMON) is None
    stopped = await owner.post(f"/v1/profiles/p1/runs/{runs[0]}/stop")
    assert stopped.json()["status"] == "cancelled"
    assert [event.type for event in await store.events_after(runs[0], 0)] == ["run.cancelled"]
