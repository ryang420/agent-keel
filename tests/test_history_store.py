"""The history index: written with each message, removed with its session or profile, backfilled for old rows."""

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
