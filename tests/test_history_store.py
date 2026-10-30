"""The history index: written with each message, removed with its session or profile, backfilled for old rows."""

import pytest

from keel.models import Message, TextPart, ToolCall

MODELS = [{"provider": "fake", "model": "m"}]


async def profile(store, profile_id="p1", tenant_id=1):
    await store.upsert_profile(profile_id, models=MODELS, sandbox={}, soul="", skills=[], tenant_id=tenant_id)


async def postings(store, message_id):
    rows = await store.db.fetch("SELECT token FROM message_tokens WHERE message_id = ? ORDER BY token", message_id)
    return [row["token"] for row in rows]


async def test_user_and_assistant_text_is_indexed_with_the_message(store):
    await profile(store)
    await store.ensure_session("p1", "s1")
    user = await store.append_message("s1", None, Message("user", (TextPart("Use PostgreSQL 17"),)))
    tool = await store.append_message("s1", None, Message("tool", (TextPart("secret"),), tool_call_id="c",
                                                          tool_name="terminal"))
    assert await postings(store, user.id) == ["17", "postgresql", "use"]
    assert await postings(store, tool.id) == []
    row = await store.db.fetchrow("SELECT profile_id, session_id, role, text FROM message_search WHERE message_id = ?",
                                  user.id)
    assert row == {"profile_id": "p1", "session_id": "s1", "role": "user", "text": "Use PostgreSQL 17"}


async def test_an_assistant_message_with_only_tool_calls_is_not_indexed(store):
    await profile(store)
    await store.ensure_session("p1", "s1")
    stored = await store.append_message("s1", None, Message("assistant", (), tool_calls=(ToolCall("c", "x", "{}"),)))
    assert await store.db.fetchrow("SELECT 1 AS n FROM message_search WHERE message_id = ?", stored.id) is None


async def test_imported_messages_are_indexed(store):
    result = await store.import_session(profile_id="p1", tenant_id=1, session_id="s1", title="t", title_source="user",
                                        created_at=1.0, last_active_at=2.0,
                                        messages=[(Message("user", (TextPart("budget review"),)), 1.0)])
    assert await postings(store, result.message_ids[0]) == ["budget", "review"]


async def test_deleting_a_session_removes_its_index_rows(store):
    await profile(store)
    await store.ensure_session("p1", "s1")
    stored = await store.append_message("s1", None, Message("user", (TextPart("keep nothing"),)))
    assert await store.delete_session("p1", "s1")
    assert await postings(store, stored.id) == []
    assert await store.db.fetchrow("SELECT 1 AS n FROM message_search WHERE message_id = ?", stored.id) is None


async def test_the_session_tool_list_is_stored_with_the_first_prompt_and_replaced_by_a_compaction(store):
    await profile(store)
    await store.ensure_session("p1", "s1")
    assert (await store.get_session("p1", "s1")).tools is None
    first = await store.set_session_prompt("s1", "P", "fake", "m", tools=["memory", "session_search"])
    assert first.tools == ("memory", "session_search")
    again = await store.set_session_prompt("s1", "Q", "fake", "m", tools=["memory"])
    assert (again.system_prompt, again.tools) == ("P", ("memory", "session_search"))
    await store.compact_session("s1", summary="S", head_id=1, tail_id=1, system_prompt="R", tools=["terminal"])
    assert (await store.get_session("p1", "s1")).tools == ("terminal",)
    await store.compact_session("s1", summary="S", head_id=1, tail_id=1, system_prompt="R")
    assert (await store.get_session("p1", "s1")).tools == ("terminal",)


async def test_backfill_indexes_old_messages_in_batches_and_then_reports_complete(store):
    await profile(store)
    await store.ensure_session("p1", "s1")
    old = [await store.append_message("s1", None, Message("user", (TextPart(f"old note {i}"),))) for i in range(5)]
    # Simulate rows written before migration 9: drop their index rows and record a pending backfill.
    await store.db.execute("DELETE FROM message_tokens")
    await store.db.execute("DELETE FROM message_search")
    await store.db.execute("INSERT INTO history_backfill (profile_id, done_through, ceiling, updated_at) "
                           "VALUES ('p1', 0, ?, 0)", old[-1].id)
    assert not await store.history_index_complete("p1")
    assert await store.backfill_history(batch=2) == 2
    assert not await store.history_index_complete("p1")
    assert await store.backfill_history(batch=2) == 2
    assert await store.backfill_history(batch=2) == 1
    assert await store.history_index_complete("p1")
    assert await store.backfill_history(batch=2) is None
    for message in old:
        assert "old" in await postings(store, message.id)


async def test_backfill_is_idempotent_with_live_rows(store):
    await profile(store)
    await store.ensure_session("p1", "s1")
    stored = await store.append_message("s1", None, Message("user", (TextPart("already indexed"),)))
    await store.db.execute("INSERT INTO history_backfill (profile_id, done_through, ceiling, updated_at) "
                           "VALUES ('p1', 0, ?, 0)", stored.id)
    assert await store.backfill_history() == 1
    assert await postings(store, stored.id) == ["already", "indexed"]


async def test_backfill_does_not_index_a_deleted_session(store):
    await profile(store)
    await store.ensure_session("p1", "s1")
    stored = await store.append_message("s1", None, Message("user", (TextPart("gone soon"),)))
    await store.db.execute("DELETE FROM message_tokens")
    await store.db.execute("DELETE FROM message_search")
    await store.db.execute("INSERT INTO history_backfill (profile_id, done_through, ceiling, updated_at) "
                           "VALUES ('p1', 0, ?, 0)", stored.id)
    assert await store.delete_session("p1", "s1")
    assert await store.backfill_history() == 0
    assert await postings(store, stored.id) == []


async def test_a_profile_without_a_backfill_row_is_complete(store):
    await profile(store)
    assert await store.history_index_complete("p1")


async def seed_two_profiles(store):
    await profile(store, "p1", 1)
    await profile(store, "p2", 1)
    for profile_id, session_id in (("p1", "a"), ("p1", "b"), ("p2", "x")):
        await store.ensure_session(profile_id, session_id)
    first = await store.append_message("a", None, Message("user", (TextPart("we chose postgres for billing"),)))
    store.clock.now += 10  # the store fixture's clock is tests/conftest.py's Clock
    second = await store.append_message("b", None, Message("assistant", (TextPart("postgres again"),)))
    foreign = await store.append_message("x", None, Message("user", (TextPart("postgres billing secret plan"),)))
    return first, second, foreign


async def test_search_ranks_by_coverage_and_never_crosses_profiles(store):
    first, second, foreign = await seed_two_profiles(store)
    rows = await store.search_history("p1", ["postgres", "billing"])
    assert [(row.message_id, row.hits) for row in rows] == [(first.id, 2), (second.id, 1)]
    assert rows[0].session_id == "a" and rows[0].role == "user" and rows[0].run_status is None
    assert [row.message_id for row in await store.search_history("p2", ["postgres"])] == [foreign.id]
    assert foreign.id not in [row.message_id for row in rows]


async def test_search_filters_by_session_and_time_before_choosing_candidates(store, clock):
    first, second, _ = await seed_two_profiles(store)
    assert [r.message_id for r in await store.search_history("p1", ["postgres"], session_id="b")] == [second.id]
    assert [r.message_id for r in await store.search_history("p1", ["postgres"], exclude_session_id="b")] \
        == [first.id]
    assert await store.search_history("p1", ["postgres"], session_id="x") == []
    assert await store.search_history("p1", ["postgres"], before=first.created_at) == []
    assert [r.message_id for r in await store.search_history("p1", ["postgres"], after=second.created_at)] \
        == [second.id]


async def test_context_returns_neighbours_and_continuation_anchors(store):
    await profile(store)
    await store.ensure_session("p1", "s1")
    ids = [(await store.append_message("s1", None, Message("user", (TextPart(f"m{i}"),)))).id for i in range(9)]
    messages, earlier, later = await store.history_context("p1", "s1", ids[4], 2)
    assert [m.id for m in messages] == ids[2:7]
    assert (earlier, later) == (ids[1], ids[7])
    messages, earlier, later = await store.history_context("p1", "s1", ids[0], 2)
    assert [m.id for m in messages] == ids[0:3] and earlier is None
    latest, earlier, later = await store.history_context("p1", "s1", None, 2)
    assert [m.id for m in latest] == ids[4:9] and (earlier, later) == (ids[3], None)


async def test_context_and_message_reads_refuse_foreign_and_mismatched_ids(store):
    first, _, foreign = await seed_two_profiles(store)
    assert await store.history_context("p1", "x", foreign.id, 2) is None
    assert await store.history_context("p1", "b", first.id, 2) is None  # the anchor is in another session
    assert await store.history_message("p1", foreign.id) is None
    assert (await store.history_message("p1", first.id)).message.text == "we chose postgres for billing"


async def test_sessions_are_browsed_newest_first_with_time_bounds(store, clock):
    await profile(store)
    for name in ("old", "mid", "new"):
        await store.ensure_session("p1", name)
        await store.append_message(name, None, Message("user", (TextPart(name),)))
        clock.now += 10
    sessions = await store.history_sessions("p1", limit=2)
    assert [s.id for s in sessions] == ["new", "mid"]
    mid = sessions[1].last_active_at
    assert [s.id for s in await store.history_sessions("p1", before=mid, limit=5)] == ["old"]
    assert [s.id for s in await store.history_sessions("p1", after=mid, limit=5)] == ["new", "mid"]


async def test_a_nul_in_a_message_is_stripped_from_the_index_not_the_message(store):
    await profile(store)
    await store.ensure_session("p1", "s1")
    stored = await store.append_message("s1", None, Message("user", (TextPart("a\x00b note"),)))
    row = await store.db.fetchrow("SELECT text FROM message_search WHERE message_id = ?", stored.id)
    assert row["text"] == "ab note"
    assert "ab" in await postings(store, stored.id)
    found = await store.search_history("p1", ["ab"])
    assert [hit.message_id for hit in found] == [stored.id]


async def test_backfill_indexes_a_message_containing_a_nul(store):
    await profile(store)
    await store.ensure_session("p1", "s1")
    stored = await store.append_message("s1", None, Message("user", (TextPart("old\x00word"),)))
    await store.db.execute("DELETE FROM message_tokens")
    await store.db.execute("DELETE FROM message_search")
    await store.db.execute("INSERT INTO history_backfill (profile_id, done_through, ceiling, updated_at) "
                           "VALUES ('p1', 0, ?, 0)", stored.id)
    assert await store.backfill_history() == 1
    assert await postings(store, stored.id) == ["oldword"]


async def test_importing_a_message_containing_a_nul_succeeds(store):
    result = await store.import_session(profile_id="p1", tenant_id=1, session_id="s1", title="t", title_source="user",
                                        created_at=1.0, last_active_at=2.0,
                                        messages=[(Message("user", (TextPart("bud\x00get"),)), 1.0)])
    assert await postings(store, result.message_ids[0]) == ["budget"]


async def test_a_failing_backfill_batch_does_not_starve_other_profiles(store, monkeypatch):
    for profile_id in ("p1", "p2"):
        await profile(store, profile_id)
        await store.ensure_session(profile_id, f"s-{profile_id}")
    ids = {}
    for profile_id in ("p1", "p2"):
        ids[profile_id] = (await store.append_message(f"s-{profile_id}", None,
                                                      Message("user", (TextPart("old note"),)))).id
    await store.db.execute("DELETE FROM message_tokens")
    await store.db.execute("DELETE FROM message_search")
    for profile_id in ("p1", "p2"):
        await store.db.execute("INSERT INTO history_backfill (profile_id, done_through, ceiling, updated_at) "
                               "VALUES (?, 0, ?, 0)", profile_id, ids[profile_id])
    real = store._index_message

    async def flaky(profile_id, *args):
        if profile_id == "p1":
            raise RuntimeError("bad row")
        return await real(profile_id, *args)

    monkeypatch.setattr(store, "_index_message", flaky)
    store.clock.now += 5
    with pytest.raises(RuntimeError):
        await store.backfill_history()
    assert await store.backfill_history() == 1
    assert await postings(store, ids["p2"]) == ["note", "old"]
