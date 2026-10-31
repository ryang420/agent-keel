"""The dream's atomic commit: skill versions, cited evidence, observation states and the job in one transaction,
refused whole when anything it read moved (learning design §5)."""

import json

import pytest

from keel.dream_gates import DreamRules
from keel.dream_stage import StagedDream
from keel.review_observe import StagedObservation
from keel.store.base import SkillSource

LEASE = 30.0
RULES = DreamRules(min_observations=2)
MODELS = [{"provider": "fake", "model": "m"}]
USAGE = {"attempt": 1, "requests": 1, "usage": {}}


@pytest.fixture
def dreams(store, clock):
    store.coordination_clock = clock
    return store


async def setup(store, clock, sessions=("s1", "s2")):
    await store.upsert_profile("p1", models=MODELS, sandbox={}, soul="", skills=["shipped-skill"], memory=True,
                               learning=True)
    ids = []
    for session in sessions:
        await store.ensure_session("p1", session)
        row = await store.db.fetchrow("SELECT generation FROM sessions WHERE id = ?", session)
        ids.append(await store._insert_observation(
            "p1", session, row["generation"], StagedObservation("csv cleanup", "When exporting", "Drop empty rows",
                                                                "corrected", ()), now=store.clock()))
    clock.now += 3600
    claim = await store.claim_dream("w1", LEASE, RULES)
    assert claim is not None
    return claim, ids


async def stage(store):
    return StagedDream(await store.observations("p1", states=("pending",)),
                       await store.learned_skills("p1", states=("active", "suspended")), {"shipped-skill"})


def call(staged, tool, **args):
    content, error = staged.call(tool, json.dumps(args))
    assert not error, content


async def skill(store, name="weekly-report", description="Write the weekly report"):
    await store.upsert_profile("p1", models=MODELS, sandbox={}, soul="", skills=["shipped-skill"], memory=True,
                               learning=True)
    made = await store.change_learned_skill("p1", {"action": "create", "name": name, "description": description,
                                                   "body": "1."}, source=SkillSource("dream", job_id="old"))
    return made["skill"]["id"]


async def test_a_create_commits_with_its_evidence_and_consumes_the_cited_observations(dreams, clock):
    claim, ids = await setup(dreams, clock)
    staged = await stage(dreams)
    call(staged, "create_skill", name="csv-cleanup", description="Clean a CSV export", body="1. Drop empty rows.",
         observations=ids, rationale="corrected twice")
    assert await dreams.commit_dream(claim, staged.proposal, usage=USAGE) == "committed"
    [made] = await dreams.learned_skills("p1")
    assert (made.name, made.origin, made.version, made.state) == ("csv-cleanup", "dream", 1, "active")
    [version] = await dreams.learned_skill_versions("p1", made.id)
    assert (version.source_kind, version.job_id, version.rationale) == ("dream", claim.job.id, "corrected twice")
    assert set(version.observation_ids) == set(ids)
    assert {(o.state, o.consumed_by) for o in await dreams.observations("p1")} == {("consumed",
                                                                                     f"dream:{claim.job.id}")}
    [job] = await dreams.list_dreams("p1")
    assert job.status == "done" and job.usage[-1]["requests"] == 1
    assert job.changes == {"skills": [{"action": "create", "skill_id": made.id, "name": "csv-cleanup",
                                       "version": 1}], "consumed": sorted(ids), "dismissed": []}


async def test_a_skill_that_moved_discards_the_whole_proposal_for_a_retry(dreams, clock):
    target = await skill(dreams)
    claim, ids = await setup(dreams, clock)
    staged = await stage(dreams)
    call(staged, "revise_skill", skill_id=target, body="2.", observations=ids)
    call(staged, "create_skill", name="csv-cleanup", description="Clean a CSV export", body="1.", observations=ids)
    await dreams.change_learned_skill("p1", {"action": "revise", "skill_id": target, "expected_version": 1,
                                             "body": "1b."}, source=SkillSource("foreground"))
    assert await dreams.commit_dream(claim, staged.proposal, usage=USAGE) == "conflict"
    assert [s.name for s in await dreams.learned_skills("p1")] == ["weekly-report"]
    assert {o.state for o in await dreams.observations("p1")} == {"pending"}
    [job] = await dreams.list_dreams("p1")
    assert (job.status, job.error_code) == ("pending", "conflict")


async def test_an_observation_that_expired_meanwhile_is_a_conflict(dreams, clock):
    claim, ids = await setup(dreams, clock)
    staged = await stage(dreams)
    call(staged, "create_skill", name="csv-cleanup", description="Clean a CSV export", body="1.", observations=ids)
    await dreams.expire_observations(clock.now + 1)
    assert await dreams.commit_dream(claim, staged.proposal, usage=USAGE) == "conflict"
    assert await dreams.learned_skills("p1") == []


async def test_learning_turned_off_before_the_commit_writes_nothing(dreams, clock):
    claim, ids = await setup(dreams, clock)
    staged = await stage(dreams)
    call(staged, "create_skill", name="csv-cleanup", description="Clean a CSV export", body="1.", observations=ids)
    await dreams.upsert_profile("p1", models=MODELS, sandbox={}, soul="", skills=[], memory=True, learning=False)
    assert await dreams.commit_dream(claim, staged.proposal, usage=USAGE) == "discarded"
    assert await dreams.learned_skills("p1") == []
    [job] = await dreams.list_dreams("p1")
    assert (job.status, job.error_code) == ("discarded", "learning_disabled")


async def test_a_lapsed_lease_commits_nothing(dreams, clock):
    claim, ids = await setup(dreams, clock)
    staged = await stage(dreams)
    call(staged, "create_skill", name="csv-cleanup", description="Clean a CSV export", body="1.", observations=ids)
    clock.now += LEASE + 1
    assert await dreams.commit_dream(claim, staged.proposal, usage=USAGE) == "lost"
    assert await dreams.learned_skills("p1") == []


async def test_a_merge_retires_the_source_and_reactivates_a_suspended_target(dreams, clock):
    target = await skill(dreams)
    source = await skill(dreams, "weekly-summary", "Summarise the week")
    await dreams.change_learned_skill("p1", {"action": "suspend", "skill_id": target, "expected_version": 1},
                                      source=SkillSource("automatic"))
    claim, ids = await setup(dreams, clock)
    staged = await stage(dreams)
    call(staged, "merge_skills", target_id=target, source_ids=[source], body="1. Merged.", observations=ids)
    assert await dreams.commit_dream(claim, staged.proposal, usage=USAGE) == "committed"
    merged, retired = await dreams.learned_skill("p1", target), await dreams.learned_skill("p1", source)
    assert (merged.state, merged.version, merged.body) == ("active", 3, "1. Merged.")
    assert (retired.state, retired.version) == ("retired", 2)
    *_, last = await dreams.learned_skill_versions("p1", target)
    assert (last.change, set(last.observation_ids)) == ("merge", set(ids))
    *_, gone = await dreams.learned_skill_versions("p1", source)
    assert (gone.change, gone.rationale) == ("retire", "merged into weekly-report")
    assert {o.state for o in await dreams.observations("p1")} == {"consumed"}


async def test_dismissed_observations_keep_their_reason_and_a_no_op_completes(dreams, clock):
    claim, ids = await setup(dreams, clock)
    staged = await stage(dreams)
    call(staged, "dismiss_observation", observation_id=ids[0], reason="a one-off choice")
    assert await dreams.commit_dream(claim, staged.proposal, usage=USAGE) == "committed"
    found = {o.id: o for o in await dreams.observations("p1")}
    assert (found[ids[0]].state, found[ids[0]].note) == ("dismissed", "a one-off choice")
    assert found[ids[1]].state == "pending"
    [job] = await dreams.list_dreams("p1")
    assert job.status == "done" and job.changes["dismissed"] == [ids[0]]


async def test_a_name_taken_by_the_foreground_meanwhile_rolls_back_everything(dreams, clock):
    claim, ids = await setup(dreams, clock, sessions=("s1", "s2", "s3"))
    staged = await stage(dreams)
    call(staged, "dismiss_observation", observation_id=ids[0], reason="noise")
    call(staged, "create_skill", name="csv-cleanup", description="Clean a CSV export", body="1.",
         observations=ids[1:])
    await dreams.change_learned_skill("p1", {"action": "create", "name": "csv-cleanup", "description": "Other",
                                             "body": "1."}, source=SkillSource("consumer"))
    assert await dreams.commit_dream(claim, staged.proposal, usage=USAGE) == "conflict"
    assert {o.state for o in await dreams.observations("p1")} == {"pending"}
    assert [s.description for s in await dreams.learned_skills("p1")] == ["Other"]
