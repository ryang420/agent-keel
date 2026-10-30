"""A review commits its staged edits, its job and the session cursor in one transaction, only while its lease and
owner are current and the targets it touched have not moved (cross-session memory design §4)."""

from keel.memory import Change, Slot
from keel.models import Message, TextPart
from keel.store.base import MemorySource

LEASE = 30.0
RULES = {"turns": 1, "idle_seconds": 60.0, "max_age_seconds": 600.0}


async def setup(store, clock, text="I prefer tea"):
    store.coordination_clock = clock
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="", skills=[],
                               memory=True)
    await store.ensure_session("p1", "s1")
    run = await store.create_run(profile_id="p1", session_id="s1", provider="fake", model="m",
                                 reasoning_effort=None, input_parts=[{"type": "text", "text": text}],
                                 instructions=None)
    user = await store.append_message("s1", run.id, Message("user", (TextPart(text),)))
    reply = await store.append_message("s1", run.id, Message("assistant", (TextPart("Noted."),)))
    await store.finish_run(run.id, "completed", output="Noted.")
    claim = await store.claim_review("w1", LEASE, **RULES)
    return claim, user, reply


def add(text, revision=0, entry="new1"):
    return {"user": (revision, [], [Slot(entry, text, 1)], [Change("add", entry, None, text)])}


async def test_rows_and_context_are_indexed_text_of_this_session_only(store, clock):
    claim, user, reply = await setup(store, clock)
    await store.ensure_session("p1", "other")
    await store.append_message("other", None, Message("user", (TextPart("not mine"),)))
    rows, complete = await store.review_rows(claim)
    assert [(row.id, row.role, row.text) for row in rows] == [(user.id, "user", "I prefer tea"),
                                                              (reply.id, "assistant", "Noted.")]
    assert complete
    assert await store.review_context(claim) == []


async def test_a_commit_applies_edits_finishes_the_job_and_moves_the_cursor(store, clock):
    claim, user, reply = await setup(store, clock)
    events = (await store.db.fetchrow("SELECT COUNT(*) AS n FROM run_events"))["n"]
    outcome = await store.commit_review(claim, add("likes tea"), end=(reply.id, 0), done=True, evidence=user.id,
                                        usage={"attempt": 1, "outcome": "committed"})
    assert outcome == "committed"
    [item] = await store.memory_items("p1")
    assert (item.text, item.source_kind, item.session_id, item.message_id) == ("likes tea", "review", "s1", user.id)
    [job] = await store.list_reviews("p1")
    assert (job.status, job.cursor_message, job.usage) == ("done", reply.id, [{"attempt": 1,
                                                                               "outcome": "committed"}])
    state = await store.db.fetchrow("SELECT reviewed_through FROM memory_reviews WHERE session_id = 's1'")
    assert state["reviewed_through"] == reply.id
    assert [stored.message.role for stored in await store.messages("s1")] == ["user", "assistant"]
    assert (await store.db.fetchrow("SELECT COUNT(*) AS n FROM run_events"))["n"] == events


async def test_a_partial_chunk_checkpoints_and_requeues(store, clock):
    claim, user, _ = await setup(store, clock)
    assert await store.commit_review(claim, {}, end=(user.id - 1, 5), done=False, evidence=None,
                                     usage=None) == "committed"
    [job] = await store.list_reviews("p1")
    assert (job.status, job.cursor_message, job.cursor_offset, job.attempts) == ("pending", user.id - 1, 5, 0)
    again = await store.claim_review("w1", LEASE, **RULES)
    assert again.job.id == job.id and again.job.cursor_offset == 5


async def test_a_foreground_write_to_a_touched_target_is_a_conflict(store, clock):
    claim, user, reply = await setup(store, clock)
    await store.apply_memory("p1", "user", 1375, [{"action": "add", "content": "likes coffee"}])
    assert await store.commit_review(claim, add("likes tea"), end=(reply.id, 0), done=True, evidence=user.id,
                                     usage=None) == "conflict"
    assert [item.text for item in await store.memory_items("p1")] == ["likes coffee"]
    [job] = await store.list_reviews("p1")
    assert (job.status, job.error_code) == ("pending", "conflict")


async def test_a_write_to_an_untouched_target_does_not_conflict(store, clock):
    claim, user, reply = await setup(store, clock)
    await store.apply_memory("p1", "memory", 2200, [{"action": "add", "content": "uses uv"}])
    assert await store.commit_review(claim, add("likes tea"), end=(reply.id, 0), done=True, evidence=user.id,
                                     usage=None) == "committed"


async def test_an_expired_or_foreign_lease_cannot_commit(store, clock):
    claim, user, reply = await setup(store, clock)
    clock.now += LEASE + 1
    assert await store.commit_review(claim, add("likes tea"), end=(reply.id, 0), done=True, evidence=user.id,
                                     usage=None) == "lost"
    again = await store.claim_review("w2", LEASE, **RULES)
    assert await store.commit_review(claim, add("likes tea"), end=(reply.id, 0), done=True, evidence=user.id,
                                     usage=None) == "lost"
    assert await store.commit_review(again, add("likes tea"), end=(reply.id, 0), done=True, evidence=user.id,
                                     usage=None) == "committed"
    assert await store.commit_review(again, add("x", entry="n2"), end=(reply.id, 0), done=True, evidence=user.id,
                                     usage=None) == "lost"
    assert [item.text for item in await store.memory_items("p1")] == ["likes tea"]


async def test_a_disabled_profile_or_replaced_session_discards_the_job(store, clock):
    claim, user, reply = await setup(store, clock)
    await store.db.execute("UPDATE profiles SET memory = 0 WHERE id = 'p1'")
    assert await store.commit_review(claim, add("likes tea"), end=(reply.id, 0), done=True, evidence=user.id,
                                     usage=None) == "discarded"
    assert await store.memory_items("p1") == []
    [job] = await store.list_reviews("p1")
    assert (job.status, job.error_code) == ("discarded", "memory_disabled")


async def test_a_recreated_profile_fences_an_old_claim(store, clock):
    claim, user, reply = await setup(store, clock)
    record = await store.begin_profile_deletion("p1", 1, "op-1")
    record = await store.advance_profile_deletion(record, "draining")
    await store.purge_profile_data(record)
    await store.complete_profile_deletion("p1", 1, "op-1")
    await store.upsert_profile("p1", models=[], sandbox={}, soul="", skills=[], memory=True, tenant_id=1)
    assert await store.commit_review(claim, add("stale"), end=(reply.id, 0), done=True, evidence=user.id,
                                     usage=None) == "lost"
    assert await store.memory_items("p1") == []


async def test_corrections_newer_than_the_chunk_are_reported(store, clock):
    claim, user, reply = await setup(store, clock)
    await store.apply_memory("p1", "user", 1375, [{"action": "add", "content": "likes tea"}])
    await store.apply_memory("p1", "user", 1375, [{"action": "remove", "old_text": "likes tea"}],
                             source=MemorySource("foreground", session_id="s1", message_id=reply.id + 5))
    assert await store.corrected_texts("p1", after_message=reply.id, after_time=0.0) == {"user": {"likes tea"}}
    assert await store.corrected_texts("p1", after_message=reply.id + 5, after_time=0.0) == {}
    clock.now += 10
    await store.apply_memory("p1", "user", 1375, [{"action": "add", "content": "likes milk"}])
    [milk] = await store.memory_items("p1")
    await store.apply_memory("p1", "user", 1375, [{"action": "replace", "entry_id": milk.id,
                                                    "content": "likes oat milk"}],
                             source=MemorySource("consumer"))
    texts = await store.corrected_texts("p1", after_message=reply.id + 5, after_time=clock.now - 1)
    assert texts == {"user": {"likes milk"}}


async def test_review_memory_reads_slots_and_revisions_together(store, clock):
    await setup(store, clock)
    await store.apply_memory("p1", "user", 1375, [{"action": "add", "content": "likes tea"}])
    slots, revisions = await store.review_memory("p1")
    assert [slot.text for slot in slots["user"]] == ["likes tea"] and slots["memory"] == []
    assert revisions == {"user": 1, "memory": 0}
