"""Learned skills in the store: versions, evidence, limits, replay and deletion (learning design §2, §3, §7)."""

import pytest

from keel.learned import MAX_SKILLS
from keel.store.base import ProfileIncarnationMismatch, SkillSource

CONSUMER = SkillSource("consumer")


def create(name="csv-report", **extra):
    return {"action": "create", "name": name, "description": f"How to do {name}", "body": "1. Step one.", **extra}


async def profile(store, profile_id="p1", *, learning=True, skills=()):
    await store.upsert_profile(profile_id, models=[], sandbox={}, soul="", skills=list(skills), memory=True,
                               learning=learning)


async def test_create_writes_version_1_with_its_source(store):
    await profile(store)
    result = await store.change_learned_skill("p1", create(), source=CONSUMER)
    assert result["success"] and result["skill"]["version"] == 1 and result["skill"]["state"] == "active"
    skill = await store.learned_skill("p1", result["skill"]["id"])
    assert (skill.origin, skill.body) == ("consumer", "1. Step one.") and skill.consumer_changed_at is not None
    [version] = await store.learned_skill_versions("p1", skill.id)
    assert (version.version, version.change, version.source_kind) == (1, "create", "consumer")


async def test_revise_rollback_suspend_resume_retire_each_add_a_version(store):
    await profile(store)
    skill_id = (await store.change_learned_skill("p1", create(), source=CONSUMER))["skill"]["id"]
    steps = [{"action": "revise", "body": "2. Better."}, {"action": "rollback", "version": 1},
             {"action": "suspend"}, {"action": "resume"}, {"action": "retire"}]
    for expected, op in enumerate(steps, start=1):
        result = await store.change_learned_skill("p1", {**op, "skill_id": skill_id, "expected_version": expected},
                                                  source=CONSUMER)
        assert result["success"], result
    skill = await store.learned_skill("p1", skill_id)
    assert (skill.version, skill.state, skill.body) == (6, "retired", "1. Step one.")
    changes = [v.change for v in await store.learned_skill_versions("p1", skill_id)]
    assert changes == ["create", "revise", "rollback", "suspend", "resume", "retire"]
    again = await store.change_learned_skill("p1", {"action": "revise", "skill_id": skill_id, "expected_version": 6,
                                                    "body": "x"}, source=CONSUMER)
    assert again["code"] == "retired"


async def test_a_stale_version_returns_the_live_skill_and_changes_nothing(store):
    await profile(store)
    skill_id = (await store.change_learned_skill("p1", create(), source=CONSUMER))["skill"]["id"]
    result = await store.change_learned_skill(
        "p1", {"action": "revise", "skill_id": skill_id, "expected_version": 3, "body": "x"}, source=CONSUMER)
    assert result["code"] == "version_conflict" and result["skill"]["version"] == 1
    assert len(await store.learned_skill_versions("p1", skill_id)) == 1


async def test_names_are_unique_among_live_skills_and_never_a_deployment_skill(store):
    await profile(store, skills=["pdf-tools"])
    assert (await store.change_learned_skill("p1", create("pdf-tools"), source=CONSUMER))["code"] == "name_taken"
    first = (await store.change_learned_skill("p1", create(), source=CONSUMER))["skill"]
    assert (await store.change_learned_skill("p1", create(), source=CONSUMER))["code"] == "name_taken"
    await store.change_learned_skill("p1", {"action": "retire", "skill_id": first["id"], "expected_version": 1},
                                     source=CONSUMER)
    assert (await store.change_learned_skill("p1", create(), source=CONSUMER))["success"]  # a retired name is free
    renamed = await store.change_learned_skill("p1", create("other"), source=CONSUMER)
    clash = await store.change_learned_skill("p1", {"action": "revise", "skill_id": renamed["skill"]["id"],
                                                    "expected_version": 1, "name": "csv-report"}, source=CONSUMER)
    assert clash["code"] == "name_taken"


async def test_the_cap_refuses_a_create_and_evicts_nothing(store):
    await profile(store)
    for index in range(MAX_SKILLS):
        assert (await store.change_learned_skill("p1", create(f"s{index}"), source=CONSUMER))["success"]
    refused = await store.change_learned_skill("p1", create("one-more"), source=CONSUMER)
    assert refused["code"] == "limit" and "merge or retire" in refused["error"]
    assert len(await store.learned_skills("p1")) == MAX_SKILLS


async def test_invalid_text_is_refused(store):
    await profile(store)
    bad = await store.change_learned_skill("p1", create(body="PASSWORD=" + "hunter" + "2"), source=CONSUMER)
    assert bad["code"] == "invalid" and await store.learned_skills("p1") == []
    assert (await store.change_learned_skill("p1", {"action": "fly"}, source=CONSUMER))["code"] == "invalid"


async def test_require_learning_checks_the_flag_at_commit(store):
    await profile(store, learning=False)
    refused = await store.change_learned_skill("p1", create(), source=CONSUMER, require_learning=True)
    assert refused["code"] == "learning_disabled"
    assert (await store.change_learned_skill("p1", create(), source=CONSUMER))["success"]  # the consumer may


async def test_a_foreground_write_cites_the_message_and_a_consumed_requested_observation(store):
    await profile(store)
    await store.ensure_session("p1", "s1")
    source = SkillSource("foreground", run_id="r1", session_id="s1", message_id=7, tool_call_id="c1")
    result = await store.change_learned_skill("p1", create(), source=source, mutation_key="skill:r1:c1:x",
                                              require_learning=True)
    [version] = await store.learned_skill_versions("p1", result["skill"]["id"])
    assert version.message_ids == (7,) and len(version.observation_ids) == 1
    [obs] = await store.db.fetch("SELECT outcome, state, session_id, consumed_by FROM lesson_observations")
    assert (obs["outcome"], obs["state"], obs["session_id"]) == ("requested", "consumed", "s1")
    assert obs["consumed_by"] == f"skill:{result['skill']['id']}:1"
    replay = await store.change_learned_skill("p1", create(), source=source, mutation_key="skill:r1:c1:x",
                                              require_learning=True)
    assert replay["replayed"] is True and replay["skill"] == result["skill"]
    assert len(await store.learned_skills("p1")) == 1


async def test_loads_are_recorded_per_run_and_counted_per_version(store, clock):
    await profile(store)
    skill_id = (await store.change_learned_skill("p1", create(), source=CONSUMER))["skill"]["id"]
    await store.record_skill_load("p1", "s1", "r1", skill_id, 1)
    await store.record_skill_load("p1", "s1", "r1", skill_id, 1)  # once per run
    await store.record_skill_load("p1", "s1", "r2", skill_id, 1)
    assert await store.skill_load_counts("p1", since=0) == {(skill_id, 1): 2}
    with pytest.raises(ProfileIncarnationMismatch):
        await store.record_skill_load("p1", "s1", "r3", skill_id, 1, profile_incarnation="stale")


async def test_another_profile_sees_nothing(store):
    await profile(store)
    await profile(store, "p2")
    skill_id = (await store.change_learned_skill("p1", create(), source=CONSUMER))["skill"]["id"]
    assert await store.learned_skill("p2", skill_id) is None and await store.learned_skills("p2") == []
    assert await store.learned_skill_named("p2", "csv-report") is None
    foreign = await store.change_learned_skill("p2", {"action": "retire", "skill_id": skill_id,
                                                      "expected_version": 1}, source=CONSUMER)
    assert foreign["code"] == "not_found" and "skill" not in foreign


async def test_session_deletion_detaches_evidence_and_keeps_the_skill(store):
    await profile(store)
    await store.ensure_session("p1", "s1")
    source = SkillSource("foreground", run_id="r1", session_id="s1", message_id=7, tool_call_id="c1")
    skill_id = (await store.change_learned_skill("p1", create(), source=source))["skill"]["id"]
    await store.record_skill_load("p1", "s1", "r1", skill_id, 1)
    assert await store.delete_session("p1", "s1")
    [version] = await store.learned_skill_versions("p1", skill_id)
    assert (version.session_id, version.run_id, version.message_ids, version.observation_ids) == (None, None, (), ())
    assert (await store.learned_skill("p1", skill_id)).body == "1. Step one."
    for table in ("lesson_observations", "skill_loads"):
        assert (await store.db.fetchrow(f"SELECT COUNT(*) AS n FROM {table}"))["n"] == 0
