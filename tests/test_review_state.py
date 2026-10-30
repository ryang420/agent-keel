"""A completed run marks its session's new text for memory review in the completion transaction (cross-session
memory design §4). Nothing else counts, and nothing older than the first marked run is ever reviewed."""

from keel.models import Message, TextPart


async def memory_profile(store, profile="p1", memory=True, tenant_id=None):
    return await store.upsert_profile(profile, models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="",
                                      skills=[], memory=memory, tenant_id=tenant_id)


async def finished_run(store, status="completed", session="s1", text="hello", profile="p1", incomplete=False):
    """A run with one user and one assistant message, ended with `status`. finish_run accepts a queued run."""
    await store.ensure_session(profile, session)
    run = await store.create_run(profile_id=profile, session_id=session, provider="fake", model="m",
                                 reasoning_effort=None, input_parts=[{"type": "text", "text": text}],
                                 instructions=None)
    user = await store.append_message(session, run.id, Message("user", (TextPart(text),)))
    reply = await store.append_message(session, run.id, Message("assistant", (TextPart("ok"),)))
    if status == "failed":
        await store.finish_run(run.id, "failed", error_code="internal", error="boom")
    else:
        await store.finish_run(run.id, status, output="ok", incomplete=incomplete)
    return run, user, reply


async def state(store, session="s1"):
    return await store.db.fetchrow("SELECT * FROM memory_reviews WHERE session_id = ?", session)


async def test_a_completed_run_marks_its_range_from_its_own_first_message(store, clock):
    store.coordination_clock = clock
    await memory_profile(store)
    await store.ensure_session("p1", "s1")
    await store.append_message("s1", None, Message("user", (TextPart("older history"),)))
    _, user, reply = await finished_run(store)
    row = await state(store)
    assert (row["reviewed_through"], row["queued_through"], row["dirty_through"]) == (user.id - 1, user.id - 1,
                                                                                      reply.id)
    assert row["pending_turns"] == 1 and row["first_dirty_at"] == row["last_completed_at"] == clock.now
    assert (row["provider"], row["model"]) == ("fake", "m")
    session = await store.get_session("p1", "s1")
    assert row["generation"] == session.generation and row["profile_incarnation"]


async def test_later_completions_extend_the_range_and_keep_the_first_dirty_time(store, clock):
    store.coordination_clock = clock
    await memory_profile(store)
    _, first, _ = await finished_run(store)
    clock.now += 30
    _, _, reply = await finished_run(store, text="again")
    row = await state(store)
    assert row["reviewed_through"] == first.id - 1 and row["dirty_through"] == reply.id
    assert row["pending_turns"] == 2 and row["first_dirty_at"] == clock.now - 30
    assert row["last_completed_at"] == clock.now


async def test_failed_and_cancelled_runs_do_not_mark_but_an_incomplete_completion_does(store):
    await memory_profile(store)
    await finished_run(store, status="failed")
    await finished_run(store, status="cancelled")
    assert await state(store) is None
    await finished_run(store, incomplete=True)
    assert (await state(store))["pending_turns"] == 1


async def test_a_profile_without_memory_marks_nothing(store):
    await memory_profile(store, memory=False)
    await finished_run(store)
    assert await state(store) is None


async def test_turning_memory_on_restarts_review_state(store):
    await memory_profile(store)
    await finished_run(store)
    await store.db.execute(
        "INSERT INTO memory_review_jobs (id, profile_id, session_id, generation, profile_incarnation, status, "
        "provider, model, source_from, source_through, cursor_message, next_attempt_at, created_at, updated_at) "
        "VALUES ('j1', 'p1', 's1', 'g', 'i', 'failed', 'fake', 'm', 0, 1, 0, 0, 0, 0)")
    await memory_profile(store)  # still on: nothing changes
    assert await state(store) is not None
    await memory_profile(store, memory=False)
    assert await state(store) is not None
    await memory_profile(store)  # off -> on
    assert await state(store) is None
    assert await store.db.fetchrow("SELECT id FROM memory_review_jobs") is None


async def test_deleting_a_session_removes_its_review_state_and_jobs(store):
    await memory_profile(store)
    await finished_run(store)
    await store.db.execute(
        "INSERT INTO memory_review_jobs (id, profile_id, session_id, generation, profile_incarnation, status, "
        "provider, model, source_from, source_through, cursor_message, next_attempt_at, created_at, updated_at) "
        "VALUES ('j1', 'p1', 's1', 'g', 'i', 'pending', 'fake', 'm', 0, 1, 0, 0, 0, 0)")
    assert await store.delete_session("p1", "s1")
    assert await state(store) is None
    assert await store.db.fetchrow("SELECT id FROM memory_review_jobs") is None


async def test_a_recreated_session_gets_a_new_generation(store):
    await memory_profile(store)
    first = await store.ensure_session("p1", "s1")
    assert await store.delete_session("p1", "s1")
    second = await store.ensure_session("p1", "s1")
    assert first.generation and second.generation and first.generation != second.generation
