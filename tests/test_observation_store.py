"""Observations committed with a memory review, and the store's observation reads (learning design §4)."""


from keel.background import Background
from keel.events import InMemoryEventBus
from keel.models import Completed, TextDelta, Usage
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.review_observe import StagedObservation
from keel.runner import RunExecutor
from keel.store.base import SkillSource

OK = [TextDelta("ok"), Completed(Usage(1, 1, 0, 0))]


async def no_sleep(seconds):
    pass


async def setup(store, clock, *, learning=True):
    store.coordination_clock = clock
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="", skills=[],
                               memory=True, learning=learning)
    registry = ProviderRegistry()
    registry.register(FakeProvider(script=[OK]))
    executor = RunExecutor(store, InMemoryEventBus(), registry, sleep=no_sleep, background=Background())
    await store.ensure_session("p1", "s1")
    await store.set_title("p1", "s1", "Kept", "user")
    await store.create_run(profile_id="p1", session_id="s1", provider="fake", model="m", reasoning_effort="low",
                           input_parts=[{"type": "text", "text": "I prefer tea"}], instructions=None)
    run = await store.claim_run("w1", 9_999_999)
    await executor.execute(run)
    return run


def observed(evidence, outcome="corrected"):
    return StagedObservation("weekly report", "Asked for the report", "Conclusion first", outcome, tuple(evidence))


async def claim(store):
    return await store.claim_review("w1", 30.0, turns=1, idle_seconds=0.0, max_age_seconds=0.0)


async def commit(store, job, items):
    user = job.job.source_from + 1
    return await store.commit_review(job, {}, end=(user + 1, 0), done=True, evidence=user + 1, usage=None,
                                     observations=tuple(items))


async def test_observations_commit_with_the_review_and_carry_loaded_skills(store, clock):
    run = await setup(store, clock)
    skill = await store.change_learned_skill("p1", {"action": "create", "name": "weekly-report",
                                                    "description": "Weekly report", "body": "1."},
                                             source=SkillSource("consumer"))
    await store.record_skill_load("p1", "s1", run.id, skill["skill"]["id"], 1)
    user = await store.run_user_message_id("s1", run.id)
    job = await claim(store)
    outcome = await store.commit_review(job, {}, end=(user + 1, 0), done=True, evidence=user + 1, usage=None,
                                        observations=(observed([user, user + 1]),))
    assert outcome == "committed"
    [item] = await store.observations("p1")
    assert (item.state, item.outcome, item.evidence, item.review_job_id) == ("pending", "corrected",
                                                                              (user, user + 1), job.job.id)
    assert item.loaded == ((skill["skill"]["id"], 1),) and item.session_id == "s1"


async def test_learning_off_at_commit_drops_observations_and_keeps_the_memory_commit(store, clock):
    run = await setup(store, clock)
    user = await store.run_user_message_id("s1", run.id)
    job = await claim(store)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="", skills=[],
                               memory=True, learning=False)
    assert await commit(store, job, [observed([user])]) == "committed"
    assert await store.observations("p1") == []


async def test_the_job_cap_is_enforced_at_commit(store, clock):
    run = await setup(store, clock)
    user = await store.run_user_message_id("s1", run.id)
    job = await claim(store)
    assert await commit(store, job, [observed([user]) for _ in range(6)]) == "committed"
    assert len(await store.observations("p1")) == 5


async def test_a_lost_or_refused_commit_writes_no_observation(store, clock):
    run = await setup(store, clock)
    user = await store.run_user_message_id("s1", run.id)
    job = await claim(store)
    clock.now += 60
    assert await commit(store, job, [observed([user])]) == "lost"
    assert await store.observations("p1") == []


async def test_review_message_runs_and_requested_ids(store, clock):
    run = await setup(store, clock)
    user = await store.run_user_message_id("s1", run.id)
    assert await store.review_message_runs("p1", "s1", {user, user + 1}) == {user: True, user + 1: True}
    assert await store.review_message_runs("p1", "s1", set()) == {}
    await store.db.execute("UPDATE runs SET capped = 1 WHERE id = ?", run.id)
    assert await store.review_message_runs("p1", "s1", {user, user + 1}) == {user: False, user + 1: False}
    assert await store.requested_message_ids("p1", "s1") == set()
    await store.change_learned_skill("p1", {"action": "create", "name": "weekly-report", "description": "d",
                                            "body": "1."},
                                     source=SkillSource("foreground", run_id=run.id, session_id="s1",
                                                        message_id=user, tool_call_id="c1"))
    assert await store.requested_message_ids("p1", "s1") == {user}


async def test_expiry_marks_old_pending_observations_only(store, clock):
    run = await setup(store, clock)
    user = await store.run_user_message_id("s1", run.id)
    job = await claim(store)
    old = clock.now
    await commit(store, job, [observed([user])])
    clock.now += 100
    middle = clock.now
    claim_row = job.job
    await store._insert_observation("p1", "s1", job.generation, observed([user]), now=clock.now + 1,
                                    review_job_id=claim_row.id)
    await store._insert_observation("p1", "s1", job.generation, observed([user], "requested"), now=old,
                                    state="consumed", consumed_by="skill:x:1")
    assert await store.expire_observations(middle) == 1
    states = sorted((item.state, item.outcome) for item in await store.observations("p1"))
    assert states == [("consumed", "requested"), ("expired", "corrected"), ("pending", "corrected")]


async def test_observations_are_profile_private(store, clock):
    run = await setup(store, clock)
    user = await store.run_user_message_id("s1", run.id)
    await commit(store, await claim(store), [observed([user])])
    assert len(await store.observations("p1")) == 1
    assert await store.observations("p2") == []
