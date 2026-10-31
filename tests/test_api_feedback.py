"""Run feedback over the API and the outcome statistics of learned skills (learning design §6)."""

import httpx
import pytest

from keel.api import Keel, create_app
from keel.config import ServiceCredential
from keel.events import InMemoryEventBus
from keel.models import Message, TextPart
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
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


async def finished_run(store, session="s1", profile_id="p1", queued=False):
    await store.ensure_session(profile_id, session)
    run = await store.create_run(profile_id=profile_id, session_id=session, provider="fake", model="m",
                                 reasoning_effort=None, input_parts=[{"type": "text", "text": "hi"}],
                                 instructions=None)
    if queued:
        return run
    await store.claim_run("w1", 10**12)
    await store.append_message(session, run.id, Message("user", (TextPart("hi"),)))
    await store.append_message(session, run.id, Message("assistant", (TextPart("ok"),)))
    await store.finish_run(run.id, "completed", output="ok")
    return run


def url(run_id, profile="p1"):
    return f"/v1/profiles/{profile}/runs/{run_id}/feedback"


async def test_put_get_replace_and_delete_round_trip(client):
    http, store = client
    run = await finished_run(store)
    assert (await http.get(url(run.id))).json()["error"]["code"] == "feedback_not_found"
    put = await http.put(url(run.id), json={"rating": "up", "comment": "Exactly right"})
    assert put.status_code == 200
    body = put.json()
    assert (body["run_id"], body["rating"], body["comment"], body["suspended"]) == (run.id, "up", "Exactly right", [])
    assert {"created_at", "updated_at"} <= body.keys()
    again = (await http.put(url(run.id), json={"rating": "down"})).json()
    assert (again["rating"], again["comment"]) == ("down", None)
    got = (await http.get(url(run.id))).json()
    assert got["rating"] == "down" and "suspended" not in got
    assert (await http.delete(url(run.id))).json() == {"deleted": True}
    gone = await http.delete(url(run.id))
    assert gone.status_code == 404 and gone.json()["error"]["code"] == "feedback_not_found"
    assert (await http.get(url(run.id))).status_code == 404


async def test_invalid_input_is_rejected(client):
    http, store = client
    run = await finished_run(store)
    secret = "PASSWORD=" + "hunter" + "2"
    for body in ({"rating": "meh"}, {}, {"rating": "up", "comment": secret}, {"rating": "up", "comment": "x" * 2001},
                 {"rating": "up", "comment": 5}):
        response = await http.put(url(run.id), json=body)
        assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_input", body
    assert (await http.get(url(run.id))).status_code == 404


async def test_unknown_queued_foreign_and_other_profile_runs(client):
    http, store = client
    missing = await http.put(url("nope"), json={"rating": "up"})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "run_not_found"
    for method in (http.get, http.delete):
        assert (await method(url("nope"))).json()["error"]["code"] == "run_not_found"
    queued = await finished_run(store, session="q", queued=True)
    refused = await http.put(url(queued.id), json={"rating": "up"})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "run_not_terminal"
    run = await finished_run(store)
    other = await http.put(url(run.id), headers={"Authorization": "Bearer key-two"}, json={"rating": "up"})
    assert other.status_code == 404
    assert (await http.put("/v1/profiles/p2", json=PROFILE)).status_code == 200
    wrong = await http.put(url(run.id, "p2"), json={"rating": "up"})
    assert wrong.status_code == 404 and wrong.json()["error"]["code"] == "run_not_found"


async def test_three_down_ratings_suspend_a_learned_skill_and_show_in_stats(client):
    http, store = client
    made = await store.change_learned_skill("p1", {"action": "create", "name": "weekly-report",
                                                   "description": "Weekly report", "body": "1."},
                                            source=SkillSource("consumer"))
    skill_id = made["skill"]["id"]
    responses = []
    for index in range(3):
        run = await finished_run(store, session=f"s{index}")
        await store.record_skill_load("p1", f"s{index}", run.id, skill_id, 1)
        responses.append((await http.put(url(run.id), json={"rating": "down"})).json())
    assert [r["suspended"] for r in responses] == [[], [], [skill_id]]
    skill = (await http.get(f"/v1/profiles/p1/skills/learned/{skill_id}")).json()
    assert skill["state"] == "suspended"
    # statistics are per version: the suspension is version 2, which no run has loaded yet
    assert skill["version"] == 2 and skill["stats"]["down_30d"] == 0 and skill["stats"]["loads_30d"] == 0
    assert (await store.skill_outcomes("p1", 0))[(skill_id, 1)].down == 3
