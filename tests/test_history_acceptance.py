"""Cross-session memory acceptance, history rows (design §7): recall in English and Chinese, honest absence,
compacted history, exact long reads, isolation across tenants and incarnations, and an interrupted backfill."""

import json

from keel.models import Message, TextPart
from keel.store import open_store
from keel.tools import ToolContext, run_tool

MODELS = [{"provider": "fake", "model": "m"}]


async def profile(store, profile_id="p1", tenant_id=1):
    return await store.upsert_profile(profile_id, models=MODELS, sandbox={}, soul="", skills=[], memory=True,
                                      tenant_id=tenant_id)


async def context(store, profile_id="p1", session_id="now"):
    current = await store.get_profile(profile_id)
    await store.ensure_session(profile_id, session_id)
    return ToolContext(store=store, profile_id=profile_id, profile_incarnation=current.incarnation,
                       session_id=session_id, allowed=frozenset({"session_search"}))


async def search(ctx, **args):
    outcome = await run_tool("session_search", json.dumps(args, ensure_ascii=False), ctx)
    return outcome.error, json.loads(outcome.content)


async def say(store, session, text, role="user", profile_id="p1"):
    await store.ensure_session(profile_id, session)
    return await store.append_message(session, None, Message(role, (TextPart(text),)))


async def test_a_chinese_decision_is_found_with_the_context_that_explains_it(store):
    """§7 row 5 (Chinese): the original message is found and the surrounding text supports the answer."""
    await profile(store)
    decision = await say(store, "old", "账单服务我们最后选了 PostgreSQL，没选 MongoDB。")
    reason = await say(store, "old", "原因是需要多行事务。", role="assistant")
    ctx = await context(store)
    error, result = await search(ctx, query="账单服务 数据库")
    assert not error and result["index_complete"] is True
    assert [hit["message_id"] for hit in result["hits"]] == [decision.id]
    error, around = await search(ctx, session_id="old", around_message_id=decision.id)
    assert not error and "多行事务" in json.dumps(around, ensure_ascii=False)
    assert reason.id in [message["message_id"] for message in around["messages"]]


async def test_no_match_in_a_complete_index_is_an_honest_empty_answer(store):
    """§7 row 6: no matching history gives no hits and says the index is complete."""
    await profile(store)
    await say(store, "old", "We talked about sourdough.")
    error, result = await search(await context(store), query="rate limits")
    assert not error and result["hits"] == [] and result["index_complete"] is True


async def test_hits_from_an_incomplete_index_say_so(store):
    """§7 row 6: while the backfill has not finished, hits come back marked incomplete."""
    await profile(store)
    hit = await say(store, "old", "We chose Redis for caching.")
    await store.db.execute("INSERT INTO history_backfill (profile_id, done_through, ceiling, updated_at) "
                           "VALUES ('p1', 0, ?, 0)", hit.id + 1000)
    error, result = await search(await context(store), query="redis caching")
    assert not error and result["index_complete"] is False
    assert [h["message_id"] for h in result["hits"]] == [hit.id]


async def test_compacted_messages_stay_searchable_and_search_leaves_the_session_alone(store):
    """§7 row 7: messages hidden from the live context by a compaction are still found, and searching does not
    change the session's compaction state."""
    await profile(store)
    old = await say(store, "long", "The codename is BLUE HERON.")
    await say(store, "long", "Noted.", role="assistant")
    await store.db.execute("UPDATE messages SET active = 0 WHERE session_id = 'long'")
    await store.db.execute("UPDATE sessions SET summary = 'earlier work', summary_head_id = ?, summary_tail_id = ? "
                           "WHERE id = 'long'", old.id, old.id + 1)
    before = await store.get_session("p1", "long")
    error, result = await search(await context(store), query="codename heron")
    assert not error and [h["message_id"] for h in result["hits"]] == [old.id]
    assert await store.get_session("p1", "long") == before
    rows = await store.db.fetch("SELECT active FROM messages WHERE session_id = 'long'")
    assert {row["active"] for row in rows} == {0}


async def test_a_long_message_reads_back_exactly_in_bounded_chunks(store):
    """§7 row 8: a long hit is bounded, its continuation works, and the chunks join to the exact text."""
    await profile(store)
    text = "".join(f"第{i}段 notes ✓ " for i in range(1500))
    message = await say(store, "old", text)
    ctx = await context(store)
    pieces, offset = [], 0
    while offset is not None:
        error, chunk = await search(ctx, message_id=message.id, offset=offset)
        assert not error and len(chunk["text"]) <= 4000
        pieces.append(chunk["text"])
        offset = chunk["next_offset"]
    assert "".join(pieces) == text


async def test_no_owner_crossing_through_query_anchor_offset_or_listing(store):
    """§7 row 9: two tenants and two profiles in one tenant see nothing of each other through a query, an anchor, an
    offset read or the session listing; a foreign id answers like a missing one."""
    await profile(store, "p1", 1)
    await profile(store, "p2", 1)
    await profile(store, "p3", 2)
    mine = await say(store, "mine", "Shared marker word zebracorn.")
    same_tenant = await say(store, "theirs", "Shared marker word zebracorn.", profile_id="p2")
    other_tenant = await say(store, "far", "Shared marker word zebracorn.", profile_id="p3")
    ctx = await context(store)
    error, result = await search(ctx, query="zebracorn")
    assert [h["message_id"] for h in result["hits"]] == [mine.id]
    missing = await search(ctx, message_id=10**12, offset=10)
    for foreign in (same_tenant, other_tenant):
        assert await search(ctx, message_id=foreign.id, offset=10) == missing
        _, around = await search(ctx, session_id="far", around_message_id=foreign.id)
        assert "zebracorn" not in json.dumps(around)
    _, listing = await search(ctx)
    assert {s["session_id"] for s in listing["sessions"]} <= {"mine", "now"}


async def test_a_recreated_profile_finds_nothing_of_its_previous_incarnation(store):
    """§7 row 12: after a profile is deleted and created again, search finds none of the old history."""
    await profile(store)
    await say(store, "old", "The codename is BLUE HERON.")
    record = await store.begin_profile_deletion("p1", 1, "op-1")
    record = await store.advance_profile_deletion(record, "draining")
    await store.purge_profile_data(record)
    await store.complete_profile_deletion("p1", 1, "op-1")
    await profile(store)
    error, result = await search(await context(store), query="codename heron")
    assert not error and result["hits"] == []


async def test_an_interrupted_backfill_resumes_without_duplicate_or_missing_postings(pg_dsn, clock):
    """§7 row 14: a backfill stopped part-way and resumed by another process ends with exactly the postings live
    indexing produces."""
    first = await open_store(pg_dsn, clock=clock)
    try:
        await profile(first)
        for i in range(30):
            await say(first, f"s{i % 3}", f"Decision {i}: 选用方案{i} because of reason {i}.")
        expected = await first.db.fetch("SELECT token, message_id FROM message_tokens WHERE profile_id = 'p1' "
                                        "ORDER BY token, message_id")
        texts = await first.db.fetch("SELECT message_id, text FROM message_search ORDER BY message_id")
        top = texts[-1]["message_id"]
        await first.db.execute("DELETE FROM message_tokens WHERE profile_id = 'p1'")
        await first.db.execute("DELETE FROM message_search WHERE profile_id = 'p1'")
        await first.db.execute("INSERT INTO history_backfill (profile_id, done_through, ceiling, updated_at) "
                               "VALUES ('p1', 0, ?, 0)", top)
        assert await first.backfill_history(7) is not None  # one batch, then the process "dies"
    finally:
        await first.close()
    second = await open_store(pg_dsn, clock=clock)
    try:
        for _ in range(20):
            if await second.backfill_history(7) is None:
                break
        assert await second.history_index_complete("p1")
        actual = await second.db.fetch("SELECT token, message_id FROM message_tokens WHERE profile_id = 'p1' "
                                       "ORDER BY token, message_id")
        assert actual == expected
        assert await second.db.fetch("SELECT message_id, text FROM message_search ORDER BY message_id") == texts
    finally:
        await second.close()
