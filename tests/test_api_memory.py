"""Consumer inspection and correction of memory (cross-session memory design §5)."""

import httpx
import pytest

from keel.api import Keel, create_app
from keel.config import ServiceCredential
from keel.events import InMemoryEventBus
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.runner import RunExecutor
from keel.store.base import ProfileIncarnationMismatch

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


async def test_patch_answers_404_when_the_entry_vanishes_after_the_write(client, monkeypatch):
    http, store = client
    await store.apply_memory("p1", "user", 1375, [{"action": "add", "content": "likes tea"}])
    [item] = await store.memory_items("p1")
    real, calls = store.memory_item, []

    async def vanishing(profile_id, entry_id):
        calls.append(entry_id)
        return await real(profile_id, entry_id) if len(calls) == 1 else None

    monkeypatch.setattr(store, "memory_item", vanishing)
    response = await http.patch(f"/v1/profiles/p1/memory/{item.id}",
                                json={"text": "likes coffee", "expected_version": 1})
    assert response.status_code == 404 and response.json()["error"]["code"] == "memory_not_found"


async def test_a_stale_profile_incarnation_answers_404_profile_not_found(client, monkeypatch):
    http, store = client
    await store.apply_memory("p1", "user", 1375, [{"action": "add", "content": "likes tea"}])
    [item] = await store.memory_items("p1")

    async def stale(profile_id, incarnation):
        raise ProfileIncarnationMismatch(profile_id)

    monkeypatch.setattr(store, "_check_profile_incarnation", stale)
    patch = await http.patch(f"/v1/profiles/p1/memory/{item.id}", json={"text": "x", "expected_version": 1})
    delete = await http.delete(f"/v1/profiles/p1/memory/{item.id}?expected_version=1")
    for response in (patch, delete):
        assert response.status_code == 404 and response.json()["error"]["code"] == "profile_not_found"


async def failed_review(store, clock):
    """A finished run whose review failed for good: the job the routes show and retry."""
    from keel.models import Message, TextPart

    store.coordination_clock = clock
    await store.ensure_session("p1", "s1")
    run = await store.create_run(profile_id="p1", session_id="s1", provider="fake", model="m",
                                 reasoning_effort=None, input_parts=[{"type": "text", "text": "hi"}],
                                 instructions=None)
    await store.append_message("s1", run.id, Message("user", (TextPart("I prefer tea"),)))
    await store.finish_run(run.id, "completed", output="")
    claim = await store.claim_review("w1", 30.0, turns=1, idle_seconds=60.0, max_age_seconds=600.0)
    await store.fail_review(claim, "budget", "The review ran out of its input budget.", retry=False,
                            usage={"attempt": 1, "outcome": "budget", "requests": 0, "usage": {}})
    return claim.job.id


async def test_reviews_list_status_without_transcript_and_retry_once(client, clock):
    http, store = client
    job_id = await failed_review(store, clock)
    body = (await http.get("/v1/profiles/p1/memory/reviews")).json()
    [review] = body["reviews"]
    assert (review["id"], review["status"], review["error_code"], review["session_id"]) == (
        job_id, "failed", "budget", "s1")
    assert "I prefer tea" not in str(body)
    assert set(review) == {"id", "session_id", "status", "attempts", "error_code", "error", "provider", "model",
                           "source", "cursor", "usage", "created_at", "updated_at", "finished_at"}
    retried = await http.post(f"/v1/profiles/p1/memory/reviews/{job_id}/retry")
    assert retried.status_code == 200 and retried.json()["status"] == "pending"
    again = await http.post(f"/v1/profiles/p1/memory/reviews/{job_id}/retry")
    assert again.status_code == 200 and again.json()["status"] == "pending"
    assert [r["status"] for r in (await http.get("/v1/profiles/p1/memory/reviews?status=pending")).json()[
        "reviews"]] == ["pending"]


async def test_review_routes_are_owned_and_validated(client, clock):
    http, store = client
    job_id = await failed_review(store, clock)
    assert (await http.post("/v1/profiles/p1/memory/reviews/nope/retry")).status_code == 404
    other = {"Authorization": "Bearer key-two"}
    assert (await http.get("/v1/profiles/p1/memory/reviews", headers=other)).status_code == 404
    assert (await http.post(f"/v1/profiles/p1/memory/reviews/{job_id}/retry", headers=other)).status_code == 404
    assert (await http.get("/v1/profiles/p1/memory/reviews?status=bogus")).status_code == 400
    assert (await http.get("/v1/profiles/p1/memory/reviews?limit=0")).status_code == 400
    assert (await http.get("/v1/profiles/p1/memory/reviews?limit=101")).status_code == 400


async def test_retry_refuses_finished_and_orphaned_reviews(client, clock):
    http, store = client
    job_id = await failed_review(store, clock)
    await store.db.execute("UPDATE sessions SET generation = 'other' WHERE id = 's1'")
    gone = await http.post(f"/v1/profiles/p1/memory/reviews/{job_id}/retry")
    assert gone.status_code == 409 and gone.json()["error"]["code"] == "review_source_gone"
    await store.db.execute("UPDATE memory_review_jobs SET status = 'done' WHERE id = ?", job_id)
    done = await http.post(f"/v1/profiles/p1/memory/reviews/{job_id}/retry")
    assert done.status_code == 409 and done.json()["error"]["code"] == "review_not_retryable"
