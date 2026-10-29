"""Memory rows, ledger and replay (cross-session memory design §2)."""

import asyncio

from keel.store.base import MemorySource

ADD_TEA = [{"action": "add", "content": "likes tea"}]


async def ledger(store, profile_id="p1"):
    return await store.db.fetch(
        "SELECT entry_id, action, old_text, new_text, source_kind, run_id, session_id, message_id, tool_call_id, "
        "revision FROM memory_changes WHERE profile_id = ? ORDER BY id", profile_id)


async def test_a_write_creates_a_stable_entry_a_revision_and_a_ledger_row(store):
    source = MemorySource("foreground", run_id="r1", session_id="s1", message_id=7, tool_call_id="c1")
    result = await store.apply_memory("p1", "user", 200, ADD_TEA, source=source)
    assert result["success"] is True and result["revision"] == 1
    [item] = await store.memory_items("p1")
    assert (item.target, item.text, item.version, item.source_kind) == ("user", "likes tea", 1, "foreground")
    assert (item.run_id, item.session_id, item.message_id) == ("r1", "s1", 7)
    assert result["items"] == [{"id": item.id, "text": "likes tea", "version": 1}]
    [row] = await ledger(store)
    assert (row["entry_id"], row["action"], row["new_text"], row["revision"]) == (item.id, "add", "likes tea", 1)


async def test_replace_by_id_keeps_the_id_and_records_old_and_new_text(store):
    await store.apply_memory("p1", "user", 200, ADD_TEA)
    [item] = await store.memory_items("p1")
    result = await store.apply_memory("p1", "user", 200, [
        {"action": "replace", "entry_id": item.id, "expected_version": 1, "content": "likes green tea"}],
        source=MemorySource("consumer"))
    assert result["success"] is True and result["revision"] == 2
    [after] = await store.memory_items("p1")
    assert (after.id, after.text, after.version, after.source_kind) == (item.id, "likes green tea", 2, "consumer")
    rows = await ledger(store)
    assert (rows[-1]["action"], rows[-1]["old_text"], rows[-1]["new_text"]) == (
        "replace", "likes tea", "likes green tea")


async def test_a_stale_version_changes_nothing(store):
    await store.apply_memory("p1", "user", 200, ADD_TEA)
    [item] = await store.memory_items("p1")
    result = await store.apply_memory("p1", "user", 200, [
        {"action": "remove", "entry_id": item.id, "expected_version": 9}])
    assert result["success"] is False and result["code"] == "version_conflict" and result["revision"] == 1
    assert len(await ledger(store)) == 1


async def test_a_removed_entry_leaves_the_prompt_but_keeps_its_row_and_history(store):
    await store.apply_memory("p1", "user", 200, ADD_TEA)
    [item] = await store.memory_items("p1")
    await store.apply_memory("p1", "user", 200, [{"action": "remove", "entry_id": item.id}])
    assert await store.memory_entries("p1") == {"memory": [], "user": []}
    assert await store.memory_item("p1", item.id) is None
    row = await store.db.fetchrow("SELECT state, version FROM memory_entries WHERE id = ?", item.id)
    assert (row["state"], row["version"]) == ("removed", 2)
    assert [r["action"] for r in await ledger(store)] == ["add", "remove"]


async def test_a_mutation_key_is_applied_once(store):
    first = await store.apply_memory("p1", "user", 200, ADD_TEA, mutation_key="run-1:c1")
    await store.apply_memory("p1", "user", 200, [{"action": "remove", "old_text": "tea"}])
    again = await store.apply_memory("p1", "user", 200, ADD_TEA, mutation_key="run-1:c1")
    assert again == {**first, "replayed": True}
    assert await store.memory_entries("p1") == {"memory": [], "user": []}


async def test_an_entry_id_from_another_profile_answers_like_a_missing_one(store):
    await store.apply_memory("p2", "user", 200, ADD_TEA)
    [foreign] = await store.memory_items("p2")
    result = await store.apply_memory("p1", "user", 200, [{"action": "remove", "entry_id": foreign.id}])
    assert result["code"] == "not_found"
    assert await store.memory_item("p1", foreign.id) is None


async def test_concurrent_adds_are_serialized(store):
    await asyncio.gather(*(store.apply_memory("p1", "memory", 500, [{"action": "add", "content": f"fact {i}"}])
                           for i in range(5)))
    assert sorted((await store.memory_entries("p1"))["memory"]) == [f"fact {i}" for i in range(5)]
    revision = await store.db.fetchrow("SELECT revision FROM memory_revisions WHERE profile_id = ?", "p1")
    assert revision["revision"] == 5


async def test_run_user_message_id_is_the_runs_latest_user_message(store):
    from keel.models import Message, TextPart
    await store.ensure_session("p1", "s1")
    run = await store.create_run(profile_id="p1", session_id="s1", provider="fake", model="m",
                                 reasoning_effort=None, input_parts=[], instructions=None)
    user = await store.append_message("s1", run.id, Message("user", (TextPart("remember tea"),)))
    await store.append_message("s1", run.id, Message("assistant", (TextPart("ok"),)))
    assert await store.run_user_message_id("s1", run.id) == user.id
    assert await store.run_user_message_id("s1", "other") is None


async def test_deleting_a_session_detaches_provenance_but_keeps_the_fact(store):
    await store.ensure_session("p1", "s1")
    await store.apply_memory("p1", "user", 200, ADD_TEA,
                             source=MemorySource("foreground", run_id="r1", session_id="s1", message_id=3))
    assert await store.delete_session("p1", "s1") is True
    [item] = await store.memory_items("p1")
    assert item.text == "likes tea" and (item.run_id, item.session_id, item.message_id) == (None, None, None)
    [row] = await ledger(store)
    assert row["new_text"] == "likes tea" and row["source_kind"] == "foreground"
