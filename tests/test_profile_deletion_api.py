"""HTTP delete/complete contract for complete profile deletion."""

import httpx
import pytest

from keel.api import Keel, create_app
from keel.config import ServiceCredential
from keel.events import InMemoryEventBus
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.runner import RunExecutor
from keel.store.base import ProfileDeletionBusy

OP_A = "11111111-1111-4111-8111-111111111111"
OP_B = "22222222-2222-4222-8222-222222222222"
PROFILE = {"models": [{"provider": "fake", "model": "m"}], "sandbox": {}, "soul": "", "skills": []}


@pytest.fixture
async def api(store):
    registry = ProviderRegistry()
    registry.register(FakeProvider())
    bus = InMemoryEventBus()
    executor = RunExecutor(store, bus, registry)
    keel = Keel(store=store, bus=bus, providers=registry, executor=executor,
                credentials=(ServiceCredential(1, "key-1"), ServiceCredential(2, "key-2")),
                max_running=2)
    app = create_app(keel)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://keel",
                                 headers={"Authorization": "Bearer key-1"}) as client:
        client.other = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://keel",
                                         headers={"Authorization": "Bearer key-2"})
        yield client, store
        await client.other.aclose()


async def test_delete_keeps_put_blocked_until_complete(api):
    client, store = api
    assert (await client.put("/v1/profiles/p1", json=PROFILE)).status_code == 200

    deleted = await client.request("DELETE", "/v1/profiles/p1", json={"operation_id": OP_A})
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "blocked": True}
    assert await store.get_profile("p1") is None
    assert (await store.get_profile_deletion("p1")).phase == "purged"

    fenced = await client.put("/v1/profiles/p1", json=PROFILE)
    assert fenced.status_code == 409
    assert fenced.json()["error"]["code"] == "profile_deletion_busy"
    fenced_session = await client.post("/v1/profiles/p1/runs",
                                       json={"input": "hi", "provider": "fake", "model": "m"})
    assert fenced_session.status_code == 409

    completed = await client.post(f"/v1/profiles/p1/deletion/{OP_A}/complete")
    assert completed.status_code == 200
    assert completed.json() == {"completed": True}
    assert await store.get_profile_deletion("p1") is None

    recreated = await client.put("/v1/profiles/p1", json=PROFILE)
    assert recreated.status_code == 200


async def test_delete_disconnect_retry_is_idempotent(api):
    client, store = api
    await client.put("/v1/profiles/p1", json=PROFILE)
    first = await client.request("DELETE", "/v1/profiles/p1", json={"operation_id": OP_A})
    retry = await client.request("DELETE", "/v1/profiles/p1", json={"operation_id": OP_A})
    assert first.status_code == retry.status_code == 200
    assert retry.json() == {"deleted": True, "blocked": True}
    assert (await store.get_profile_deletion("p1")).operation_id == OP_A

    assert (await client.post(f"/v1/profiles/p1/deletion/{OP_A}/complete")).json() == {"completed": True}
    # A lost completion response may be retried after the fence is gone.
    assert (await client.post(f"/v1/profiles/p1/deletion/{OP_A}/complete")).json() == {"completed": True}


async def test_delete_never_exposes_foreign_profile(api):
    client, store = api
    assert (await client.other.put("/v1/profiles/p1", json=PROFILE)).status_code == 200

    denied = await client.request("DELETE", "/v1/profiles/p1", json={"operation_id": OP_A})
    assert denied.status_code == 404
    assert denied.json()["error"]["code"] == "profile_not_found"
    assert await store.get_profile_deletion("p1") is None
    assert await store.get_profile("p1") is not None


async def test_complete_retry_after_recreation_is_harmless(api):
    client, store = api
    await client.put("/v1/profiles/p1", json=PROFILE)
    await client.request("DELETE", "/v1/profiles/p1", json={"operation_id": OP_A})
    await client.post(f"/v1/profiles/p1/deletion/{OP_A}/complete")
    await client.put("/v1/profiles/p1", json=PROFILE)

    late = await client.post(f"/v1/profiles/p1/deletion/{OP_A}/complete")
    assert late.status_code == 200
    assert late.json() == {"completed": True}
    assert await store.get_profile("p1") is not None

    # A new operation is independent of the old receipt.
    assert (await client.request("DELETE", "/v1/profiles/p1",
                                 json={"operation_id": OP_B})).status_code == 200
    assert (await store.get_profile_deletion("p1")).operation_id == OP_B


async def test_malformed_operation_id_is_rejected(api):
    client, store = api
    await client.put("/v1/profiles/p1", json=PROFILE)
    bad = await client.request("DELETE", "/v1/profiles/p1", json={"operation_id": "not-a-uuid"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_input"
    assert await store.get_profile_deletion("p1") is None

    missing = await client.request("DELETE", "/v1/profiles/p1", json={})
    assert missing.status_code == 400
    bad_complete = await client.post("/v1/profiles/p1/deletion/nope/complete")
    assert bad_complete.status_code == 400


async def test_unknown_profile_is_fenced_then_cleaned(api):
    client, store = api
    deleted = await client.request("DELETE", "/v1/profiles/never-seen", json={"operation_id": OP_A})
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True, "blocked": True}
    assert (await store.get_profile_deletion("never-seen")).phase == "purged"
    assert (await client.post(f"/v1/profiles/never-seen/deletion/{OP_A}/complete")).status_code == 200


async def test_conflicting_operation_is_rejected(api):
    client, store = api
    await client.put("/v1/profiles/p1", json=PROFILE)
    await client.request("DELETE", "/v1/profiles/p1", json={"operation_id": OP_A})
    conflict = await client.request("DELETE", "/v1/profiles/p1", json={"operation_id": OP_B})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "profile_deletion_busy"
    assert (await store.get_profile_deletion("p1")).operation_id == OP_A
