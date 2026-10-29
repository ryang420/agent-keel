"""Consumer inspection and correction of memory (cross-session memory design §5)."""

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


async def test_get_lists_entries_with_ids_versions_usage_and_source(client):
    http, store = client
    await store.apply_memory("p1", "user", 1375, [{"action": "add", "content": "likes tea"}])
    body = (await http.get("/v1/profiles/p1/memory")).json()
    [entry] = body["user"]["entries"]
    assert entry["text"] == "likes tea" and entry["version"] == 1
    assert entry["source"] == {"kind": "foreground", "run_id": None, "session_id": None, "message_id": None}
    assert body["user"]["usage"] == "9/1375" and body["memory"] == {"usage": "0/2200", "entries": []}


async def test_patch_replaces_with_a_version_and_records_a_consumer_change(client):
    http, store = client
    await store.apply_memory("p1", "user", 1375, [{"action": "add", "content": "likes tea"}])
    [item] = await store.memory_items("p1")
    ok = await http.patch(f"/v1/profiles/p1/memory/{item.id}", json={"text": "likes coffee", "expected_version": 1})
    assert ok.status_code == 200 and ok.json()["version"] == 2 and ok.json()["source"]["kind"] == "consumer"
    stale = await http.patch(f"/v1/profiles/p1/memory/{item.id}", json={"text": "x", "expected_version": 1})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "version_conflict"
    secret = await http.patch(f"/v1/profiles/p1/memory/{item.id}",
                              json={"text": "PASSWORD=" + "hunter" + "2", "expected_version": 2})
    assert secret.status_code == 400 and secret.json()["error"]["code"] == "invalid_input"
    bad = await http.patch(f"/v1/profiles/p1/memory/{item.id}", json={"text": "x"})
    assert bad.status_code == 400


async def test_delete_removes_with_a_version(client):
    http, store = client
    await store.apply_memory("p1", "memory", 2200, [{"action": "add", "content": "uses uv"}])
    [item] = await store.memory_items("p1")
    assert (await http.delete(f"/v1/profiles/p1/memory/{item.id}?expected_version=2")).status_code == 409
    assert (await http.delete(f"/v1/profiles/p1/memory/{item.id}?expected_version=1")).json() == {"deleted": True}
    assert (await http.delete(f"/v1/profiles/p1/memory/{item.id}?expected_version=2")).status_code == 404
    assert await store.memory_entries("p1") == {"memory": [], "user": []}


async def test_another_tenant_sees_neither_the_profile_nor_the_entry(client):
    http, store = client
    await store.apply_memory("p1", "user", 1375, [{"action": "add", "content": "likes tea"}])
    [item] = await store.memory_items("p1")
    other = {"Authorization": "Bearer key-two"}
    assert (await http.get("/v1/profiles/p1/memory", headers=other)).status_code == 404
    response = await http.patch(f"/v1/profiles/p1/memory/{item.id}", headers=other,
                                json={"text": "x", "expected_version": 1})
    assert response.status_code == 404 and response.json()["error"]["code"] == "profile_not_found"
    assert (await http.put("/v1/profiles/p2", json=PROFILE, headers=other)).status_code == 200
    foreign = await http.delete(f"/v1/profiles/p2/memory/{item.id}?expected_version=1", headers=other)
    assert foreign.status_code == 404 and foreign.json()["error"]["code"] == "memory_not_found"
