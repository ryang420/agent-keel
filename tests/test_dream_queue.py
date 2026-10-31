"""The dream queue: triggers, one lease per profile shared with memory reviews, a daily attempt cap, leases on
database time and operator retry (learning design §5, §7)."""

from dataclasses import replace

import pytest

from keel.dream_gates import DreamRules
from keel.models import Message, TextPart
from keel.review_observe import StagedObservation
from keel.store.base import DreamBusy, DreamNotRetryable

LEASE = 30.0
RULES = DreamRules()
MODELS = [{"provider": "fake", "model": "m"}]


@pytest.fixture
def dreams(store, clock):
    store.coordination_clock = clock
    return store


async def learning_profile(store, profile="p1", learning=True):
    return await store.upsert_profile(profile, models=MODELS, sandbox={}, soul="", skills=[], memory=True,
                                      learning=learning)


async def completed_run(store, session="s0", profile="p1"):
    """A completed run: the dream's model route, and a memory review that is due a minute later."""
    await store.ensure_session(profile, session)
    run = await store.create_run(profile_id=profile, session_id=session, provider="fake", model="m",
                                 reasoning_effort=None, input_parts=[{"type": "text", "text": "hi"}],
                                 instructions=None)
    await store.append_message(session, run.id, Message("user", (TextPart("hi"),)))
    await store.append_message(session, run.id, Message("assistant", (TextPart("ok"),)))
    await store.finish_run(run.id, "completed", output="ok")
    return run


async def observe(store, session="s1", outcome="corrected", count=1, profile="p1"):
    await store.ensure_session(profile, session)
    row = await store.db.fetchrow("SELECT generation FROM sessions WHERE id = ?", session)
    return [await store._insert_observation(profile, session, row["generation"],
                                            StagedObservation(f"weekly report {index}", "When asked", "Do it",
                                                              outcome, ()), now=store.clock())
            for index in range(count)]


async def claim(store, owner="w1", rules=RULES):
    return await store.claim_dream(owner, LEASE, rules)


async def test_eight_observations_are_due_an_hour_after_the_last(dreams, clock):
    await learning_profile(dreams)
    await completed_run(dreams)
    await observe(dreams, count=8)
    clock.now += 3599
    assert await claim(dreams) is None
    clock.now += 1
    found = await claim(dreams)
    assert (found.job.trigger, found.job.provider, found.job.model, found.job.attempts) == ("observations", "fake",
                                                                                          "m", 1)
    assert found.input_at == clock.now and found.job.status == "running"


async def test_a_few_old_observations_are_due_after_a_day(dreams, clock):
    await learning_profile(dreams)
    await observe(dreams, count=3)
    clock.now += 3600
    assert await claim(dreams) is None
    clock.now += 86_400 - 3600
    found = await claim(dreams)
    assert found.job.trigger == "interval" and found.job.provider is None  # no completed run to take a route from


async def test_a_down_rating_on_a_loaded_skill_or_a_suspension_is_due_after_the_debounce(dreams, clock):
    await learning_profile(dreams)
    [plain] = await observe(dreams, outcome="feedback_down")
    clock.now += 3600
    assert await claim(dreams) is None
    await dreams.db.execute("UPDATE lesson_observations SET loaded = ? WHERE id = ?",
                            '[{"skill_id": "k1", "version": 1}]', plain)
    assert (await claim(dreams)).job.trigger == "feedback"


async def test_an_automatic_suspension_is_due(dreams, clock):
    await learning_profile(dreams)
    await observe(dreams)
    await dreams.db.execute(
        "INSERT INTO learned_skill_versions (profile_id, skill_id, version, name, description, body, change, "
        "source_kind, created_at) VALUES ('p1', 'k1', 2, 'n', 'd', 'b', 'suspend', 'automatic', ?)", clock.now)
    clock.now += 3600
    assert (await claim(dreams)).job.trigger == "suspension"


async def test_after_a_dream_only_new_observations_count(dreams, clock):
    await learning_profile(dreams)
    await observe(dreams, count=8)
    clock.now += 3600
    first = await claim(dreams)
    await dreams.db.execute("UPDATE dream_jobs SET status = 'done', lease_token = NULL WHERE id = ?", first.job.id)
    clock.now += 3600
    assert await claim(dreams) is None  # the same eight were already given to a dream
    await observe(dreams, session="s2", count=8)
    clock.now += 3600
    assert (await claim(dreams)).job.trigger == "observations"


async def test_no_dream_is_queued_without_learning(dreams, clock):
    await learning_profile(dreams, learning=False)
    await observe(dreams, count=8)  # rows written directly; with learning off nothing may queue from them
    clock.now += 3600
    assert await claim(dreams) is None
    assert await dreams.list_dreams("p1") == []


async def test_a_dream_and_a_review_of_one_profile_never_run_together(dreams, clock):
    await learning_profile(dreams)
    await completed_run(dreams)
    await observe(dreams, count=8)
    clock.now += 3600
    dream = await claim(dreams)
    rules = {"turns": 10, "idle_seconds": 60.0, "max_age_seconds": 600.0}
    assert await dreams.claim_review("w2", LEASE, **rules) is None
    await dreams.fail_dream(dream, "internal", "boom", retry=False)
    review = await dreams.claim_review("w2", LEASE, **rules)
    assert review is not None
    clock.now += 1  # the queued dream is newer than the failed one
    assert await claim(dreams) is None  # a new dream is queued (a failed one blocks nothing) but the review runs
    [queued, failed] = await dreams.list_dreams("p1")
    assert (queued.status, failed.status) == ("pending", "failed")


async def test_attempts_per_day_are_capped(dreams, clock):
    rules = replace(RULES, daily_attempts=1)
    await learning_profile(dreams)
    await observe(dreams, count=8)
    clock.now += 3600
    first = await claim(dreams, rules=rules)
    await dreams.fail_dream(first, "internal", "boom", retry=True)
    clock.now += 60
    assert await claim(dreams, rules=rules) is None
    clock.now += 86_400
    assert (await claim(dreams, rules=rules)).job.id == first.job.id


async def test_an_expired_lease_is_retried_and_fails_after_three_attempts(dreams, clock):
    await learning_profile(dreams)
    await observe(dreams, count=8)
    clock.now += 3600
    for attempt in (1, 2, 3):
        found = await claim(dreams)
        assert found.job.attempts == attempt and await dreams.renew_dream(found, LEASE)
        clock.now += LEASE + 1
    await claim(dreams)  # expires the third lease (and queues a fresh dream: a failed one blocks nothing)
    [job, *_] = await dreams.list_dreams("p1", status="failed")
    assert (job.attempts, job.error_code) == (3, "lease_expired")


async def test_release_returns_the_job_without_using_an_attempt(dreams, clock):
    await learning_profile(dreams)
    await observe(dreams, count=8)
    clock.now += 3600
    found = await claim(dreams)
    await dreams.release_dream(found)
    [job] = await dreams.list_dreams("p1")
    assert (job.status, job.attempts) == ("pending", 0)
    assert await dreams.db.fetch("SELECT * FROM dream_attempts") == []


async def test_owner_checks_see_disabled_learning_and_deletion(dreams, clock):
    await learning_profile(dreams)
    await observe(dreams, count=8)
    clock.now += 3600
    found = await claim(dreams)
    assert await dreams.dream_owner_gone(found) is None
    await learning_profile(dreams, learning=False)
    assert await dreams.dream_owner_gone(found) == "learning_disabled"


async def test_retry_requeues_a_failed_job_and_refuses_finished_or_busy_ones(dreams, clock):
    await learning_profile(dreams)
    await observe(dreams, count=8)
    clock.now += 3600
    found = await claim(dreams)
    await dreams.fail_dream(found, "model_unavailable", "gone", retry=False)
    retried = await dreams.retry_dream("p1", found.job.id)
    assert (retried.status, retried.attempts, retried.error_code) == ("pending", 0, None)
    assert (await dreams.retry_dream("p1", found.job.id)).status == "pending"  # idempotent
    assert await dreams.retry_dream("p2", found.job.id) is None  # a foreign profile sees nothing
    again = await claim(dreams)
    await dreams.fail_dream(again, "internal", "boom", retry=False)
    await observe(dreams, session="s2", count=8)
    clock.now += 3600
    other = await claim(dreams)  # a new job for the new observations
    with pytest.raises(DreamBusy):
        await dreams.retry_dream("p1", found.job.id)
    await dreams.db.execute("UPDATE dream_jobs SET status = 'done' WHERE id = ?", other.job.id)
    with pytest.raises(DreamNotRetryable):
        await dreams.retry_dream("p1", other.job.id)


async def test_list_is_newest_first_bounded_and_filterable(dreams, clock):
    await learning_profile(dreams)
    for round in range(3):
        await observe(dreams, session=f"s{round}", count=8)
        clock.now += 3600
        found = await claim(dreams)
        await dreams.fail_dream(found, "internal", "boom", retry=False)
    jobs = await dreams.list_dreams("p1", limit=2)
    assert len(jobs) == 2 and jobs[0].created_at > jobs[1].created_at
    assert {job.status for job in await dreams.list_dreams("p1", status="failed")} == {"failed"}


async def test_profiles_that_are_not_due_never_crowd_out_one_that_is(dreams, clock, monkeypatch):
    monkeypatch.setattr("keel.store.dreams._BATCH", 3)
    for index in range(5):  # each dreamed after its observations: old pending rows, nothing new, not a day yet
        profile = f"idle{index}"
        await learning_profile(dreams, profile)
        await observe(dreams, session=f"i{index}", count=2, profile=profile)
    clock.now += 3600
    await dreams.db.execute(
        "INSERT INTO dream_jobs (id, profile_id, profile_incarnation, status, trigger, next_attempt_at, input_at, "
        "created_at, updated_at) SELECT 'done-' || id, id, incarnation, 'done', 'observations', ?, ?, ?, ? "
        "FROM profiles", clock.now, clock.now, clock.now, clock.now)
    clock.now += 60
    await learning_profile(dreams, "busy")
    await observe(dreams, session="b1", count=8, profile="busy")
    clock.now += 3600
    found = await claim(dreams)
    assert found is not None and found.job.profile_id == "busy"
    assert [job.status for job in await dreams.list_dreams("idle0")] == ["done"]


async def test_a_model_failure_is_not_requeued_until_the_route_changes(dreams, clock):
    await learning_profile(dreams)
    await observe(dreams, count=8)
    clock.now += 3600
    found = await claim(dreams)
    await dreams.fail_dream(found, "no_route", "no run", retry=False)
    clock.now += 60
    assert await claim(dreams) is None
    assert [job.status for job in await dreams.list_dreams("p1")] == ["failed"]  # no churn of identical jobs
    await completed_run(dreams)  # a model to take a route from
    clock.now += 60
    again = await claim(dreams)
    assert (again.job.provider, again.job.model) == ("fake", "m")
    await dreams.fail_dream(again, "model_unavailable", "gone", retry=False)
    clock.now += 60
    assert await claim(dreams) is None
    retried = await dreams.retry_dream("p1", again.job.id)  # an operator retry lifts it
    assert retried.status == "pending" and (await claim(dreams)).job.id == again.job.id
