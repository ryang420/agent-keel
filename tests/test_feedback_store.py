"""Run feedback, its observation, outcome statistics and automatic suspension (learning design §6)."""

from keel.models import Message, TextPart
from keel.store.base import SkillOutcome, SkillSource

MODELS = [{"provider": "fake", "model": "m"}]


async def profile(store, *, learning=True, profile_id="p1"):
    await store.upsert_profile(profile_id, models=MODELS, sandbox={}, soul="", skills=[], memory=True,
                               learning=learning)


async def finished_run(store, session="s1", status="completed", profile_id="p1"):
    """A run with a user and an assistant message, finished in `status` (`queued` leaves it unfinished)."""
    await store.ensure_session(profile_id, session)
    run = await store.create_run(profile_id=profile_id, session_id=session, provider="fake", model="m",
                                 reasoning_effort=None, input_parts=[{"type": "text", "text": "hi"}],
                                 instructions=None)
    if status == "queued":
        return run
    await store.append_message(session, run.id, Message("user", (TextPart("hi"),)))
    await store.append_message(session, run.id, Message("assistant", (TextPart("ok"),)))
    if status == "failed":
        await store.finish_run(run.id, "failed", error_code="internal", error="boom")
    else:
        await store.finish_run(run.id, status, output="ok")
    return run


async def skill(store):
    made = await store.change_learned_skill("p1", {"action": "create", "name": "weekly-report",
                                                   "description": "Weekly report", "body": "1."},
                                            source=SkillSource("consumer"))
    return made["skill"]["id"]


async def rated_load(store, skill_id, version, rating, index, status="completed"):
    run = await finished_run(store, session=f"s{index}", status=status)
    await store.record_skill_load("p1", f"s{index}", run.id, skill_id, version)
    if rating:
        return await store.put_feedback("p1", run.id, rating, None)
    return None


async def test_feedback_is_stored_with_an_observation_citing_the_run(store):
    await profile(store)
    run = await finished_run(store)
    feedback, code, suspended = await store.put_feedback("p1", run.id, "up", "Exactly what I wanted")
    assert code is None and suspended == [] and feedback.rating == "up"
    [item] = await store.observations("p1")
    assert (item.outcome, item.situation, item.run_id, item.state) == ("feedback_up", "Exactly what I wanted",
                                                                       run.id, "pending")
    assert len(item.evidence) == 2 and feedback.observation_id == item.id
    assert await store.get_feedback("p1", run.id) == feedback


async def test_replacing_feedback_replaces_a_pending_observation_but_not_a_consumed_one(store, clock):
    await profile(store)
    run = await finished_run(store)
    await store.put_feedback("p1", run.id, "up", None)
    await store.put_feedback("p1", run.id, "down", "no")
    [item] = await store.observations("p1")
    assert item.outcome == "feedback_down"
    await store.db.execute("UPDATE lesson_observations SET state = 'consumed' WHERE id = ?", item.id)
    clock.now += 10  # a later content time, so the order by created_at is deterministic
    feedback, _, _ = await store.put_feedback("p1", run.id, "up", None)
    items = await store.observations("p1")
    assert [(i.outcome, i.state) for i in items] == [("feedback_down", "consumed"), ("feedback_up", "pending")]
    assert feedback.observation_id == items[1].id


async def test_without_learning_feedback_is_stored_without_an_observation(store):
    await profile(store, learning=False)
    run = await finished_run(store)
    feedback, code, _ = await store.put_feedback("p1", run.id, "down", "bad")
    assert code is None and feedback.observation_id is None and feedback.comment == "bad"
    assert await store.observations("p1") == []


async def test_the_run_must_exist_belong_to_the_profile_and_be_terminal(store):
    await profile(store)
    await profile(store, profile_id="p2")
    assert await store.put_feedback("p1", "nope", "up", None) == (None, "not_found", [])
    other = await finished_run(store, profile_id="p2")
    assert await store.put_feedback("p1", other.id, "up", None) == (None, "not_found", [])
    queued = await finished_run(store, session="q", status="queued")
    assert await store.put_feedback("p1", queued.id, "up", None) == (None, "not_terminal", [])
    assert await store.get_feedback("p1", queued.id) is None


async def test_delete_removes_feedback_and_its_pending_observation(store):
    await profile(store)
    run = await finished_run(store)
    assert await store.delete_feedback("p1", run.id) is False
    await store.put_feedback("p1", run.id, "up", None)
    assert await store.delete_feedback("p1", run.id) is True
    assert await store.get_feedback("p1", run.id) is None and await store.observations("p1") == []


async def test_outcomes_count_loads_ratings_and_end_states_per_version(store):
    await profile(store)
    skill_id = await skill(store)
    await rated_load(store, skill_id, 1, "up", 0)
    await rated_load(store, skill_id, 1, "down", 1)
    await rated_load(store, skill_id, 1, None, 2, status="failed")
    outcomes = await store.skill_outcomes("p1", 0)
    assert outcomes == {(skill_id, 1): SkillOutcome(3, 1, 1, 2, 1, 0)}
    assert await store.skill_outcomes("p1", store.clock() + 10) == {}


async def test_three_down_ratings_suspend_the_current_version_without_a_model_call(store):
    await profile(store)
    skill_id = await skill(store)
    suspended = []
    before = (await store.learned_skill("p1", skill_id)).consumer_changed_at
    for index in range(3):
        run = await finished_run(store, session=f"s{index}")
        await store.record_skill_load("p1", f"s{index}", run.id, skill_id, 1)
        _, _, suspended = await store.put_feedback("p1", run.id, "down", None)
    assert suspended == [skill_id]
    current = await store.learned_skill("p1", skill_id)
    assert (current.state, current.version) == ("suspended", 2) and current.consumer_changed_at == before
    last = (await store.learned_skill_versions("p1", skill_id))[-1]
    assert (last.change, last.source_kind) == ("suspend", "automatic")


async def test_suspension_needs_more_down_than_up_the_current_version_and_learning(store):
    await profile(store)
    skill_id = await skill(store)
    for index in range(3):
        await rated_load(store, skill_id, 1, "up", index)
    for index in range(3, 6):
        _, _, suspended = await rated_load(store, skill_id, 1, "down", index)
        assert suspended == []
    assert (await store.learned_skill("p1", skill_id)).state == "active"
    # a revision after the loads: version 1's down ratings do not suspend version 2
    other = await store.change_learned_skill("p1", {"action": "create", "name": "other", "description": "d",
                                                    "body": "1."}, source=SkillSource("consumer"))
    other_id = other["skill"]["id"]
    for index in range(10, 13):
        await rated_load(store, other_id, 1, None, index)
    await store.change_learned_skill("p1", {"action": "revise", "skill_id": other_id, "expected_version": 1,
                                            "body": "2."}, source=SkillSource("consumer"))
    for index in range(10, 13):
        run_id = (await store.db.fetchrow("SELECT id FROM runs WHERE session_id = ?", f"s{index}"))["id"]
        _, _, suspended = await store.put_feedback("p1", run_id, "down", None)
    assert suspended == [] and (await store.learned_skill("p1", other_id)).state == "active"


async def test_without_learning_downs_are_stored_and_the_skill_stays_active(store):
    await profile(store)
    skill_id = await skill(store)
    runs = []
    for index in range(3):
        run = await finished_run(store, session=f"s{index}")
        await store.record_skill_load("p1", f"s{index}", run.id, skill_id, 1)
        runs.append(run)
    await store.upsert_profile("p1", models=MODELS, sandbox={}, soul="", skills=[], memory=True, learning=False)
    for run in runs:
        _, _, suspended = await store.put_feedback("p1", run.id, "down", None)
    assert suspended == [] and (await store.skill_outcomes("p1", 0))[(skill_id, 1)].down == 3
    assert (await store.learned_skill("p1", skill_id)).state == "active"


async def test_session_deletion_removes_the_sessions_feedback(store):
    await profile(store)
    first, second = await finished_run(store, session="a"), await finished_run(store, session="b")
    await store.put_feedback("p1", first.id, "up", None)
    await store.put_feedback("p1", second.id, "up", None)
    assert await store.delete_session("p1", "a") is True
    assert await store.get_feedback("p1", first.id) is None and await store.get_feedback("p1", second.id) is not None


async def test_text_versions_ignore_state_changes(store):
    await profile(store)
    skill_id = await skill(store)
    assert await store.skill_text_versions("p1") == {skill_id: 1}
    await store.change_learned_skill("p1", {"action": "suspend", "skill_id": skill_id, "expected_version": 1},
                                     source=SkillSource("consumer"))
    await store.change_learned_skill("p1", {"action": "resume", "skill_id": skill_id, "expected_version": 2},
                                     source=SkillSource("consumer"))
    assert await store.skill_text_versions("p1") == {skill_id: 1}
    await store.change_learned_skill("p1", {"action": "revise", "skill_id": skill_id, "expected_version": 3,
                                            "body": "2."}, source=SkillSource("consumer"))
    assert await store.skill_text_versions("p1") == {skill_id: 4}


async def test_a_feedback_observation_is_stamped_on_the_content_clock_and_orders_before_a_later_change(store, clock):
    await profile(store)
    skill_id = await skill(store)
    run = await finished_run(store)
    await store.put_feedback("p1", run.id, "down", "no")
    [item] = await store.observations("p1")
    assert item.created_at == clock()
    clock.now += 10
    await store.change_learned_skill("p1", {"action": "revise", "skill_id": skill_id, "expected_version": 1,
                                            "body": "2."}, source=SkillSource("consumer"))
    changed = (await store.learned_skill("p1", skill_id)).consumer_changed_at
    assert item.created_at < changed


async def test_a_down_rating_after_a_skill_load_records_the_loaded_version_on_the_observation(store):
    await profile(store)
    skill_id = await skill(store)
    run = await finished_run(store)
    await store.record_skill_load("p1", "s1", run.id, skill_id, 1)
    await store.put_feedback("p1", run.id, "down", "wrong")
    [item] = await store.observations("p1")
    assert item.outcome == "feedback_down" and item.loaded == ((skill_id, 1),)
