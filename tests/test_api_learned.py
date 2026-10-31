"""Learned skills over the API (procedural learning design §2, §6)."""

import httpx
import pytest

from keel.api import Keel, create_app
from keel.config import ServiceCredential
from keel.events import InMemoryEventBus
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.runner import RunExecutor

PROFILE = {"models": [{"provider": "fake", "model": "m"}], "sandbox": {}, "soul": "", "skills": [], "memory": True}


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


async def test_learning_requires_memory_and_is_returned(client):
    http, store = client
    bad = await http.put("/v1/profiles/p2", json={**PROFILE, "memory": False, "learning": True})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_input"
    assert (await http.put("/v1/profiles/p2", json={**PROFILE, "learning": "yes"})).status_code == 400
    ok = await http.put("/v1/profiles/p2", json={**PROFILE, "learning": True})
    assert ok.status_code == 200 and ok.json()["learning"] is True
    assert (await store.get_profile("p2")).learning is True
    off = await http.put("/v1/profiles/p2", json=PROFILE)  # omitted means false, as for memory
    assert off.json()["learning"] is False


BASE = "/v1/profiles/p1/skills/learned"
NEW = {"name": "csv-report", "description": "Weekly CSV report", "body": "1. Load."}


async def test_create_get_and_list(client):
    http, store = client
    made = await http.post(BASE, json=NEW)
    assert made.status_code == 201
    body = made.json()
    assert (body["version"], body["origin"], body["state"], body["body"]) == (1, "consumer", "active", "1. Load.")
    assert body["stats"] == {"loads_30d": 0, "up_30d": 0, "down_30d": 0, "completed_30d": 0, "failed_30d": 0,
                             "cancelled_30d": 0}
    one = (await http.get(f"{BASE}/{body['id']}")).json()
    [version] = one["versions"]
    assert version["change"] == "create" and version["source"]["kind"] == "consumer"
    assert version["evidence"] == {"message_ids": [], "observation_ids": []}
    assert [s["name"] for s in (await http.get(BASE)).json()["skills"]] == ["csv-report"]
    assert (await http.get(f"{BASE}?state=retired")).json()["skills"] == []
    assert (await http.get(f"{BASE}?state=gone")).status_code == 400


async def test_patch_rollback_suspend_resume_and_retire(client):
    http, _ = client
    skill_id = (await http.post(BASE, json=NEW)).json()["id"]
    patched = await http.patch(f"{BASE}/{skill_id}", json={"body": "2. Better.", "expected_version": 1})
    assert patched.status_code == 200 and patched.json()["version"] == 2
    stale = await http.patch(f"{BASE}/{skill_id}", json={"body": "x", "expected_version": 1})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
    back = await http.post(f"{BASE}/{skill_id}/rollback", json={"version": 1, "expected_version": 2})
    assert back.json()["body"] == "1. Load." and back.json()["version"] == 3
    assert (await http.post(f"{BASE}/{skill_id}/rollback", json={"version": 9, "expected_version": 3})
            ).status_code == 400
    assert (await http.post(f"{BASE}/{skill_id}/suspend", json={"expected_version": 3})).json()["state"] == "suspended"
    assert (await http.post(f"{BASE}/{skill_id}/resume", json={"expected_version": 4})).json()["state"] == "active"
    assert (await http.delete(f"{BASE}/{skill_id}")).status_code == 400  # expected_version is required
    assert (await http.delete(f"{BASE}/{skill_id}?expected_version=5")).json() == {"retired": True}
    again = await http.patch(f"{BASE}/{skill_id}", json={"body": "x", "expected_version": 6})
    assert again.status_code == 409 and again.json()["error"]["code"] == "skill_retired"
    changes = [v["change"] for v in (await http.get(f"{BASE}/{skill_id}")).json()["versions"]]
    assert changes == ["create", "revise", "rollback", "suspend", "resume", "retire"]


async def test_refusals_map_to_errors(client):
    http, _ = client
    await http.post(BASE, json=NEW)
    taken = await http.post(BASE, json=NEW)
    assert taken.status_code == 409 and taken.json()["error"]["code"] == "name_taken"
    secret = await http.post(BASE, json={**NEW, "name": "other", "body": "PASSWORD=" + "hunter" + "2"})
    assert secret.status_code == 400 and secret.json()["error"]["code"] == "invalid_input"
    missing = await http.get(f"{BASE}/nope")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "learned_skill_not_found"
    bad = await http.patch(f"{BASE}/nope", json={"body": "x"})
    assert bad.status_code == 400  # expected_version is checked before the lookup


async def test_another_tenant_and_another_profile_see_nothing(client):
    http, _ = client
    skill_id = (await http.post(BASE, json=NEW)).json()["id"]
    other = {"Authorization": "Bearer key-two"}
    assert (await http.get(BASE, headers=other)).status_code == 404
    assert (await http.get(f"{BASE}/{skill_id}", headers=other)).status_code == 404
    assert (await http.put("/v1/profiles/p3", json=PROFILE)).status_code == 200
    assert (await http.get(f"/v1/profiles/p3/skills/learned/{skill_id}")).status_code == 404
    gone = await http.delete(f"/v1/profiles/p3/skills/learned/{skill_id}?expected_version=1")
    assert gone.status_code == 404 and (await http.get(f"{BASE}/{skill_id}")).json()["state"] == "active"


async def test_the_skill_routes_work_with_learning_off(client):
    http, store = client  # PROFILE has memory on and learning off
    assert (await http.post(BASE, json=NEW)).status_code == 201
