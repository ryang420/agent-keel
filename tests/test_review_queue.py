"""The review queue: eligibility, one running review per profile, leases on database time, bounded retries and
operator retry (cross-session memory design §4, §5)."""

import pytest

from keel.models import Message, TextPart
from keel.store.base import ReviewNotRetryable, ReviewSourceGone

LEASE = 30.0
RULES = {"turns": 10, "idle_seconds": 60.0, "max_age_seconds": 600.0}


async def memory_profile(store, profile="p1", memory=True):
    return await store.upsert_profile(profile, models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="",
                                      skills=[], memory=memory)


async def completed(store, session="s1", text="I prefer tea", profile="p1"):
    await store.ensure_session(profile, session)
    run = await store.create_run(profile_id=profile, session_id=session, provider="fake", model="m",
                                 reasoning_effort=None, input_parts=[{"type": "text", "text": text}],
                                 instructions=None)
    user = await store.append_message(session, run.id, Message("user", (TextPart(text),)))
    reply = await store.append_message(session, run.id, Message("assistant", (TextPart("Noted."),)))
    await store.finish_run(run.id, "completed", output="Noted.")
    return user, reply


@pytest.fixture
def queue(store, clock):
    store.coordination_clock = clock
    return store


async def claim(store, owner="w1", **rules):
    return await store.claim_review(owner, LEASE, **{**RULES, **rules})


async def test_a_session_becomes_eligible_after_an_idle_minute(queue, clock):
    await memory_profile(queue)
    user, reply = await completed(queue)
    clock.now += 59
    assert await claim(queue) is None
    clock.now += 1
    job = (await claim(queue)).job
    assert (job.status, job.attempts) == ("running", 1)
    assert (job.source_from, job.source_through, job.cursor_message, job.cursor_offset) == (
        user.id - 1, reply.id, user.id - 1, 0)


async def test_enough_turns_are_eligible_at_once(queue):
    await memory_profile(queue)
    await completed(queue)
    assert await claim(queue, turns=2) is None
    await completed(queue)
    assert await claim(queue, turns=2) is not None


async def test_a_continuously_active_session_is_eligible_after_ten_minutes(queue, clock):
    await memory_profile(queue)
    for _ in range(20):
        await completed(queue)
        clock.now += 30
        if clock.now - 1_000.0 < 600:
            assert await claim(queue, turns=100) is None
    assert await claim(queue, turns=100) is not None


async def test_new_completions_extend_the_state_not_the_claimed_job(queue, clock):
    await memory_profile(queue)
    await completed(queue)
    clock.now += 60
    job = (await claim(queue)).job
    _, later = await completed(queue)
    row = await queue.db.fetchrow("SELECT * FROM memory_reviews WHERE session_id = 's1'")
    assert row["queued_through"] == job.source_through < later.id == row["dirty_through"]
    stored = await queue.db.fetchrow("SELECT source_through FROM memory_review_jobs WHERE id = ?", job.id)
    assert stored["source_through"] == job.source_through


async def test_one_running_review_per_profile_and_others_proceed(queue, clock):
    await memory_profile(queue)
    await memory_profile(queue, "p2")
    await completed(queue, "s1")
    await completed(queue, "s2")
    await completed(queue, "t1", profile="p2")
    clock.now += 60
    first = await claim(queue, "w1")
    second = await claim(queue, "w2")
    assert first.job.profile_id != second.job.profile_id
    assert await claim(queue, "w3") is None


async def test_the_oldest_eligible_session_goes_first(queue, clock):
    await memory_profile(queue)
    await completed(queue, "old")
    clock.now += 10
    await completed(queue, "new")
    clock.now += 100
    assert (await claim(queue)).job.session_id == "old"


async def test_an_expired_lease_is_retried_and_fails_after_three_attempts(queue, clock):
    await memory_profile(queue)
    await completed(queue)
    clock.now += 60
    first = await claim(queue, "w1")
    assert await queue.renew_review(first, LEASE)
    for attempt in (2, 3):
        clock.now += LEASE + 1
        again = await claim(queue, "w2")
        assert again.job.id == first.job.id and again.job.attempts == attempt
        assert not await queue.renew_review(first, LEASE)
        first = again
    clock.now += LEASE + 1
    assert await claim(queue) is None
    [job] = await queue.list_reviews("p1")
    assert (job.status, job.error_code, job.attempts) == ("failed", "lease_expired", 3)


async def test_a_retryable_failure_backs_off_and_a_final_one_blocks_only_its_session(queue, clock):
    await memory_profile(queue)
    await completed(queue, "s1")
    clock.now += 60
    first = await claim(queue)
    assert await queue.fail_review(first, "deadline", "too slow", retry=True,
                                   usage={"attempt": 1, "outcome": "deadline"}) == "pending"
    clock.now += 29
    assert await claim(queue) is None
    clock.now += 1
    again = await claim(queue)
    assert await queue.fail_review(again, "model_unavailable", "gone", retry=False) == "failed"
    await completed(queue, "s1")
    await completed(queue, "s2")
    clock.now += 60
    assert (await claim(queue)).job.session_id == "s2"
    [failed] = [job for job in await queue.list_reviews("p1") if job.session_id == "s1"]
    assert failed.usage == [{"attempt": 1, "outcome": "deadline"}] and failed.error == "gone"


async def test_release_returns_the_job_without_using_an_attempt(queue, clock):
    await memory_profile(queue)
    await completed(queue)
    clock.now += 60
    first = await claim(queue)
    await queue.release_review(first)
    again = await claim(queue)
    assert again.job.id == first.job.id and again.job.attempts == 1


async def test_disabled_or_fenced_profiles_are_not_queued(queue, clock):
    await memory_profile(queue)
    await completed(queue)
    await memory_profile(queue, memory=False)
    clock.now += 60
    assert await claim(queue) is None
    await memory_profile(queue, "p2")
    await completed(queue, "t1", profile="p2")
    clock.now += 60
    await queue.begin_profile_deletion("p2", 1, "op-1")
    assert await claim(queue) is None


async def test_owner_checks_see_disabled_memory_deleted_sessions_and_restarts(queue, clock):
    await memory_profile(queue)
    await completed(queue)
    clock.now += 60
    claimed = await claim(queue)
    assert await queue.review_owner_gone(claimed) is None
    await queue.db.execute("UPDATE sessions SET generation = 'other' WHERE id = 's1'")
    assert await queue.review_owner_gone(claimed) == "session_gone"
    await queue.db.execute("UPDATE profiles SET memory = 0 WHERE id = 'p1'")
    assert await queue.review_owner_gone(claimed) == "memory_disabled"
    await queue.db.execute("UPDATE profiles SET incarnation = 'new' WHERE id = 'p1'")
    assert await queue.review_owner_gone(claimed) == "profile_gone"


async def test_retry_requeues_a_failed_job_once_and_refuses_finished_ones(queue, clock):
    await memory_profile(queue)
    await memory_profile(queue, "p2")
    await completed(queue)
    clock.now += 60
    claimed = await claim(queue)
    await queue.fail_review(claimed, "budget", "too big", retry=False)
    assert await queue.retry_review("p2", claimed.job.id) is None
    assert await queue.retry_review("p1", "missing") is None
    job = await queue.retry_review("p1", claimed.job.id)
    assert (job.status, job.attempts, job.error_code) == ("pending", 0, None)
    assert (await queue.retry_review("p1", claimed.job.id)).status == "pending"
    again = await claim(queue)
    await queue.discard_review(again, "session_gone")
    with pytest.raises(ReviewNotRetryable):
        await queue.retry_review("p1", claimed.job.id)


async def test_retry_refuses_a_job_whose_session_was_replaced(queue, clock):
    await memory_profile(queue)
    await completed(queue)
    clock.now += 60
    claimed = await claim(queue)
    await queue.fail_review(claimed, "budget", "too big", retry=False)
    await queue.db.execute("UPDATE sessions SET generation = 'other' WHERE id = 's1'")
    with pytest.raises(ReviewSourceGone):
        await queue.retry_review("p1", claimed.job.id)


async def test_list_is_newest_first_bounded_and_filterable(queue, clock):
    await memory_profile(queue)
    for session in ("a", "b", "c"):
        await completed(queue, session)
        clock.now += 1
    clock.now += 60
    claimed = await claim(queue)
    await queue.fail_review(claimed, "budget", "too big", retry=False)
    await claim(queue)  # queues the rest
    jobs = await queue.list_reviews("p1")
    assert [job.created_at for job in jobs] == sorted((job.created_at for job in jobs), reverse=True)
    assert len(await queue.list_reviews("p1", limit=1)) == 1
    assert [job.status for job in await queue.list_reviews("p1", status="failed")] == ["failed"]
    assert await queue.list_reviews("p2") == []
