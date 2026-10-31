"""Procedural learning acceptance (design §10), rows 11 and 12: no cross-owner access by any route, tool, query or
retry; session deletion, profile deletion and recreation follow §7 with no resurrection from stale jobs."""

import json
import uuid

import httpx
import pytest

from keel.api import Keel, create_app
from keel.config import ServiceCredential
from keel.dream_gates import DreamRules
from keel.dream_stage import DreamProposal
from keel.events import InMemoryEventBus
from keel.models import Message, TextPart
from keel.providers.fake import FakeProvider
from keel.runner import RunExecutor
from keel.store.base import SkillSource
from keel.tools.base import ToolContext
from keel.tools.skill_tools import skill_view, skills_list

from tests.learning_support import MODELS, observe, registry

PROFILE = {"models": MODELS, "sandbox": {}, "soul": "", "skills": [], "memory": True, "learning": True}
SECRET = {"action": "create", "name": "private-method", "description": "Only profile A's method",
          "body": "1. A's step."}
RULES = DreamRules(min_observations=2, debounce_seconds=0.0, interval_seconds=1e9, daily_attempts=100)


@pytest.fixture
async def client(store):
    providers = registry(FakeProvider())
    bus = InMemoryEventBus()
    keel = Keel(store=store, bus=bus, providers=providers, executor=RunExecutor(store, bus, providers),
                credentials=(ServiceCredential(1, "key-one"), ServiceCredential(2, "key-two")), max_running=1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(keel)), base_url="http://keel",
                                 headers={"Authorization": "Bearer key-one"}) as http:
        for profile in ("pa", "pb"):
            assert (await http.put(f"/v1/profiles/{profile}", json=PROFILE)).status_code == 200
        assert (await http.put("/v1/profiles/pc", json=PROFILE,
                               headers={"Authorization": "Bearer key-two"})).status_code == 200
        yield http, store


async def seed_a(store, clock):
    """Profile A: a learned skill, two pending observations and a failed dream."""
    store.coordination_clock = clock
    skill = (await store.change_learned_skill("pa", SECRET, source=SkillSource("consumer")))["skill"]
    clock.now += 10
    for session in ("a1", "a2"):
        await observe(store, session, profile="pa")
    claim = await store.claim_dream("w1", 30.0, RULES)
    await store.fail_dream(claim, "internal", "boom", retry=False)
    return skill, claim.job.id


async def test_no_route_tool_query_or_retry_crosses_an_owner(client, clock):
    http, store = client
    skill, dream_id = await seed_a(store, clock)
    other_tenant = {"Authorization": "Bearer key-two"}
    for path in ("/v1/profiles/pa/skills/learned", f"/v1/profiles/pa/skills/learned/{skill['id']}",
                 "/v1/profiles/pa/dreams"):
        assert (await http.get(path, headers=other_tenant)).status_code == 404
    assert (await http.post(f"/v1/profiles/pa/dreams/{dream_id}/retry", headers=other_tenant)).status_code == 404
    # the same tenant's other profile sees none of A's rows, even by A's ids
    assert (await http.get("/v1/profiles/pb/skills/learned")).json() == {"skills": []}
    assert (await http.get(f"/v1/profiles/pb/skills/learned/{skill['id']}")).status_code == 404
    assert (await http.get("/v1/profiles/pb/dreams")).json() == {"dreams": []}
    assert (await http.post(f"/v1/profiles/pb/dreams/{dream_id}/retry")).status_code == 404
    patched = await http.patch(f"/v1/profiles/pb/skills/learned/{skill['id']}",
                               json={"expected_version": 1, "body": "stolen"})
    assert patched.status_code == 404
    # tools run with B's trusted context cannot see A's skill
    ctx = ToolContext(store=store, profile_id="pb")
    viewed = json.loads((await skill_view({"name": "private-method"}, ctx)).content)
    assert viewed["success"] is False
    listed = json.loads((await skills_list({}, ctx)).content)
    assert all(entry["name"] != "private-method" for entry in listed["skills"])
    # queries are scoped: B has no observations, outcomes or dream input
    assert await store.observations("pb") == []
    assert await store.skill_text_outcomes("pb") == {}
    claimed = await store.claim_dream("w2", 30.0, RULES)  # A is due again (a failed dream blocks nothing)
    assert claimed is None or claimed.job.profile_id == "pa"
    for found in await store.list_dreams("pb"):
        assert found.profile_id == "pb"
    assert (await store.learned_skill("pa", skill["id"])).body == SECRET["body"]


async def test_session_deletion_keeps_skills_and_profile_recreation_resurrects_nothing(client, clock):
    http, store = client
    store.coordination_clock = clock
    await store.ensure_session("pa", "a1")
    run = await store.create_run(profile_id="pa", session_id="a1", provider="fake", model="m", reasoning_effort=None,
                                 input_parts=[{"type": "text", "text": "hi"}], instructions=None)
    await store.claim_run("w1", 10**12)
    message = await store.append_message("a1", run.id, Message("user", (TextPart("From now on drop empty rows."),)))
    await store.append_message("a1", run.id, Message("assistant", (TextPart("ok"),)))
    await store.finish_run(run.id, "completed", output="ok")
    made = await store.change_learned_skill("pa", SECRET, source=SkillSource("foreground", run_id=run.id,
                                                                            session_id="a1",
                                                                            message_id=message.id))
    await store.record_skill_load("pa", "a1", run.id, made["skill"]["id"], 1)
    await store.put_feedback("pa", run.id, "up", "Good")
    assert (await http.delete("/v1/profiles/pa/sessions/a1")).status_code in (200, 204)
    assert await store.observations("pa") == []
    assert await store.get_feedback("pa", run.id) is None
    assert await store.db.fetch("SELECT 1 FROM skill_loads WHERE profile_id = 'pa'") == []
    [version] = await store.learned_skill_versions("pa", made["skill"]["id"])
    assert version.message_ids == () and version.body == SECRET["body"]  # evidence detached, text kept
    # profile deletion and recreation: a stale dream claim from the old incarnation writes nothing
    clock.now += 10
    for session in ("a2", "a3"):
        await observe(store, session, profile="pa")
    stale = await store.claim_dream("w1", 30.0, RULES)
    operation = str(uuid.uuid4())
    deleted = await http.request("DELETE", "/v1/profiles/pa", json={"operation_id": operation})
    assert deleted.status_code in (200, 202)
    assert (await http.post(f"/v1/profiles/pa/deletion/{operation}/complete")).status_code == 200
    assert (await http.put("/v1/profiles/pa", json=PROFILE)).status_code == 200
    assert await store.commit_dream(stale, DreamProposal(), usage=None) in ("lost", "discarded")
    assert await store.learned_skills("pa") == [] and await store.list_dreams("pa") == []
    assert await store.observations("pa") == []
