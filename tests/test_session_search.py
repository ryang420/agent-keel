"""session_search: bounded, owned, plain-data reads of a profile's earlier conversations."""

import json

from keel.models import Message, TextPart
from keel.tools import ToolContext, run_tool

MODELS = [{"provider": "fake", "model": "m"}]


async def ready(store, memory=True):
    for profile_id in ("p1", "p2"):
        await store.upsert_profile(profile_id, models=MODELS, sandbox={}, soul="", skills=[], memory=memory,
                                   tenant_id=1)
    for profile_id, session_id in (("p1", "now"), ("p1", "old"), ("p2", "other")):
        await store.ensure_session(profile_id, session_id)
    profile = await store.get_profile("p1")
    return ToolContext(store=store, profile_id="p1", profile_incarnation=profile.incarnation, session_id="now",
                       allowed=frozenset({"session_search"}))


async def call(ctx, **args):
    outcome = await run_tool("session_search", json.dumps(args), ctx)
    return outcome.error, json.loads(outcome.content)


async def test_search_finds_other_sessions_with_excerpts_and_reports_index_state(store):
    ctx = await ready(store)
    old = await store.append_message("old", None, Message("user", (TextPart("We picked Postgres for billing."),)))
    await store.append_message("now", None, Message("user", (TextPart("Why Postgres?"),)))
    await store.append_message("other", None, Message("user", (TextPart("Postgres in another profile"),)))
    error, result = await call(ctx, query="postgres billing")
    assert not error and result["index_complete"] is True
    assert [(h["session_id"], h["message_id"], h["run_status"]) for h in result["hits"]] == [
        ("old", old.id, "unknown")]
    assert result["hits"][0]["excerpt"] == "We picked Postgres for billing." and not result["hits"][0]["truncated"]
    error, result = await call(ctx, query="postgres", session_id="now")
    assert [h["session_id"] for h in result["hits"]] == ["now"]


async def test_hits_keep_two_per_session_and_respect_the_limit(store):
    ctx = await ready(store)
    for i in range(4):
        await store.append_message("old", None, Message("user", (TextPart(f"deploy note {i}"),)))
    _, result = await call(ctx, query="deploy")
    assert len(result["hits"]) == 2
    _, result = await call(ctx, query="deploy", session_id="old", limit=3)
    assert len(result["hits"]) == 3
    error, result = await call(ctx, query="deploy", limit=11)
    assert not error and len(result["hits"]) <= 10


async def test_an_exact_phrase_outranks_scattered_words_with_the_same_coverage(store):
    ctx = await ready(store)
    scattered = await store.append_message("old", None, Message("user", (TextPart("plan the data review base"),)))
    exact = await store.append_message("old", None, Message("assistant", (TextPart("data base"),)))
    _, result = await call(ctx, query="data base")
    assert [h["message_id"] for h in result["hits"]] == [exact.id, scattered.id]


async def test_context_and_chunks_are_bounded_and_label_tool_content(store):
    ctx = await ready(store)
    long = "x" * 9_000
    first = await store.append_message("old", None, Message("user", (TextPart(long),)))
    await store.append_message("old", None, Message("tool", (TextPart("raw"),), tool_call_id="c",
                                                    tool_name="terminal"))
    _, result = await call(ctx, session_id="old", around_message_id=first.id)
    entry = result["messages"][0]
    assert (entry["chars"], len(entry["text"]), entry["next_offset"]) == (9_000, 4_000, 4_000)
    assert result["messages"][1]["text"] == "[result of tool terminal omitted]"
    _, chunk = await call(ctx, message_id=first.id, offset=8_000)
    assert (len(chunk["text"]), chunk["next_offset"]) == (1_000, None)


async def test_a_response_stays_under_sixteen_thousand_characters(store):
    ctx = await ready(store)
    ids = [(await store.append_message("old", None, Message("user", (TextPart("y" * 4_000),)))).id
           for _ in range(9)]
    _, result = await call(ctx, session_id="old", around_message_id=ids[4], window=4)
    assert sum(len(m["text"]) for m in result["messages"]) <= 16_000
    assert ids[4] in [m["message_id"] for m in result["messages"]]
    assert result["earlier_message_id"] is not None and result["later_message_id"] is not None


async def test_foreign_and_missing_ids_get_the_same_answer(store):
    ctx = await ready(store)
    foreign = await store.append_message("other", None, Message("user", (TextPart("hidden"),)))
    missing = await call(ctx, message_id=10_000_000)
    assert await call(ctx, message_id=foreign.id) == missing
    assert (await call(ctx, session_id="other", around_message_id=foreign.id))[1] \
        == (await call(ctx, session_id="nope", around_message_id=1))[1]


async def test_browse_lists_this_profiles_sessions_and_marks_the_current_one(store):
    ctx = await ready(store)
    _, result = await call(ctx)
    assert {s["session_id"] for s in result["sessions"]} == {"now", "old"}
    assert [s["current"] for s in result["sessions"] if s["session_id"] == "now"] == [True]


async def test_bad_arguments_and_a_disabled_profile_are_refused(store):
    ctx = await ready(store)
    assert (await call(ctx, query="x", after="2026-10-01T00:00:00"))[0]
    assert (await call(ctx, query="!!!"))[0]
    assert (await call(ctx, around_message_id=1))[0]  # needs session_id
    await store.upsert_profile("p1", models=MODELS, sandbox={}, soul="", skills=[], memory=False, tenant_id=1)
    error, result = await call(ctx, query="postgres")
    assert error and result["error"] == "History search is not available."


async def test_an_incomplete_index_is_reported(store):
    ctx = await ready(store)
    await store.db.execute("INSERT INTO history_backfill (profile_id, done_through, ceiling, updated_at) "
                           "VALUES ('p1', 0, 99, 0)")
    _, result = await call(ctx, query="anything")
    assert result == {"hits": [], "index_complete": False}


async def test_a_stale_profile_incarnation_is_refused(store):
    ctx = await ready(store)
    ctx.profile_incarnation = "stale-" + str(ctx.profile_incarnation)
    error, result = await call(ctx, query="postgres")
    assert error and result["error"] == "History search is not available."


async def test_titles_and_excerpts_are_redacted(store):
    ctx = await ready(store)
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    await store.db.execute("UPDATE sessions SET title = ? WHERE id = 'old'", f"key {secret}")
    stored = await store.append_message("old", None, Message("user", (TextPart("plain note"),)))
    await store.db.execute("UPDATE message_search SET text = ? WHERE message_id = ?", f"note {secret}", stored.id)
    _, browsed = await call(ctx)
    assert secret not in json.dumps(browsed)
    _, found = await call(ctx, query="note")
    assert found["hits"] and secret not in json.dumps(found)
