"""Dream inspection and retry over the API (learning design §7)."""

import httpx
import pytest

from keel.api import Keel, create_app
from keel.config import ServiceCredential
from keel.dream_gates import DreamRules
from keel.events import InMemoryEventBus
from keel.models import Message, TextPart
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.review_observe import StagedObservation
from keel.runner import RunExecutor
from keel.store.base import SkillSource

PROFILE = {"models": [{"provider": "fake", "model": "m"}], "sandbox": {}, "soul": "", "skills": [], "memory": True,
           "learning": True}


@pytest.fixture
async def client(store):
    registry = ProviderRegistry()
    registry.register(FakeProvider())
    bus = InMemoryEventBus()
    keel = Keel(store=store, bus=bus, providers=registry, executor=RunExecutor(store, bus, registry),
                credentials=(ServiceCredential(1, "key-one"), ServiceCredential(2, "key-two")), max_running=1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(keel)), base_url="http://keel",
                                 headers={"Authorization": "Bearer key-one"}) as http:
        assert (await http.put("/v1/profiles/p1", json=PROFILE)).status_code == 200
        yield http, store


async def queued_dream(store, clock):
    store.coordination_clock = clock
    return await more_observations(store, clock, ("s1", "s2"))


async def more_observations(store, clock, sessions):
    for session in sessions:
        await store.ensure_session("p1", session)
        row = await store.db.fetchrow("SELECT generation FROM sessions WHERE id = ?", session)
        await store._insert_observation("p1", session, row["generation"],
                                        StagedObservation("topic", "when", "how", "corrected", ()), now=store.clock())
    clock.now += 3600
    return await store.claim_dream("w1", 30.0, DreamRules(min_observations=2))


async def test_dreams_are_listed_newest_first_with_route_usage_and_changes(client, clock):
    http, store = client
    found = await queued_dream(store, clock)
    await store.fail_dream(found, "no_route", "No model.", retry=False, usage={"attempt": 1, "requests": 0})
    response = await http.get("/v1/profiles/p1/dreams")
    assert response.status_code == 200
    [dream] = response.json()["dreams"]
    assert (dream["id"], dream["status"], dream["error_code"], dream["trigger"]) == (found.job.id, "failed",
                                                                                   "no_route", "observations")
    assert set(dream) >= {"provider", "model", "attempts", "usage", "changes", "created_at", "finished_at"}
    assert (await http.get("/v1/profiles/p1/dreams?status=done")).json() == {"dreams": []}
    assert (await http.get("/v1/profiles/p1/dreams?status=bogus")).status_code == 400
    assert (await http.get("/v1/profiles/p1/dreams?limit=0")).status_code == 400
    assert (await http.get("/v1/profiles/p1/dreams?limit=101")).status_code == 400


async def test_retry_requeues_once_and_refuses_unknown_or_finished_dreams(client, clock):
    http, store = client
    found = await queued_dream(store, clock)
    await store.fail_dream(found, "internal", "boom", retry=False)
    retried = await http.post(f"/v1/profiles/p1/dreams/{found.job.id}/retry")
    assert retried.status_code == 200 and (retried.json()["status"], retried.json()["attempts"]) == ("pending", 0)
    assert (await http.post(f"/v1/profiles/p1/dreams/{found.job.id}/retry")).json()["status"] == "pending"
    assert (await http.post("/v1/profiles/p1/dreams/nope/retry")).status_code == 404
    await store.db.execute("UPDATE dream_jobs SET status = 'done' WHERE id = ?", found.job.id)
    refused = await http.post(f"/v1/profiles/p1/dreams/{found.job.id}/retry")
    assert (refused.status_code, refused.json()["error"]["code"]) == (409, "dream_not_retryable")


async def test_another_tenant_sees_no_dreams_and_cannot_retry_one(client, clock):
    http, store = client
    found = await queued_dream(store, clock)
    await store.fail_dream(found, "internal", "boom", retry=False)
    other = {"Authorization": "Bearer key-two"}
    assert (await http.get("/v1/profiles/p1/dreams", headers=other)).status_code == 404
    assert (await http.post(f"/v1/profiles/p1/dreams/{found.job.id}/retry", headers=other)).status_code == 404
    [job] = await store.list_dreams("p1")
    assert (job.status, job.attempts, job.error_code) == ("failed", 1, "internal")


async def test_retry_is_refused_while_another_dream_of_the_profile_is_open(client, clock):
    http, store = client
    failed = await queued_dream(store, clock)
    await store.fail_dream(failed, "internal", "boom", retry=False)
    second = await more_observations(store, clock, ("s3",))
    assert second is not None and second.job.id != failed.job.id
    refused = await http.post(f"/v1/profiles/p1/dreams/{failed.job.id}/retry")
    assert (refused.status_code, refused.json()["error"]["code"]) == (409, "dream_busy")
