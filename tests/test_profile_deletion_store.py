"""Durable, tenant-owned profile deletion fences and completion receipts."""

import asyncio

import pytest

from keel.store.base import (
    MemorySource, ProfileDeletionBusy, ProfileDeletionConflict, ProfileIncarnationMismatch, ProfileTenantMismatch,
)

MODELS = [{"provider": "fake", "model": "m"}]
OP_A = "11111111-1111-4111-8111-111111111111"
OP_B = "22222222-2222-4222-8222-222222222222"


async def put_profile(store, profile_id="p1", tenant_id=1):
    return await store.upsert_profile(profile_id, models=MODELS, sandbox={}, soul="s", skills=[],
                                      tenant_id=tenant_id)


async def test_delete_fence_survives_reopen(tmp_path, clock, pg_dsn):
    from keel.store import open_store

    url = pg_dsn
    first = await open_store(url, clock=clock)
    await put_profile(first)
    record = await first.begin_profile_deletion("p1", 1, OP_A)
    await first.close()

    reopened = await open_store(url, clock=clock)
    try:
        found = await reopened.get_profile_deletion("p1")
        assert found == record
        assert found.operation_id == OP_A
        assert found.phase == "blocked"
        with pytest.raises(ProfileDeletionBusy):
            await reopened.upsert_profile("p1", models=MODELS, sandbox={}, soul="s", skills=[], tenant_id=1)
    finally:
        await reopened.close()


async def test_foreign_tenant_cannot_claim_profile(store):
    await put_profile(store, tenant_id=1)

    with pytest.raises(ProfileTenantMismatch):
        await store.begin_profile_deletion("p1", 2, OP_A)

    assert await store.get_profile_deletion("p1") is None


async def test_old_completion_does_not_release_new_operation(store):
    await put_profile(store)
    old = await store.begin_profile_deletion("p1", 1, OP_A)
    old = await store.advance_profile_deletion(old, "draining")
    await store.purge_profile_data(old)
    await store.complete_profile_deletion("p1", 1, OP_A)

    # The profile is reprovisioned and a second, independent deletion starts.
    await put_profile(store, "p1")
    new = await store.begin_profile_deletion("p1", 1, OP_B)
    assert new.phase == "blocked"

    # A late retry of the old operation may acknowledge its receipt, but it
    # must leave the new fence and the new profile data untouched.
    await store.complete_profile_deletion("p1", 1, OP_A)
    assert (await store.get_profile_deletion("p1")).operation_id == OP_B
    assert (await store.get_profile("p1")) is not None

    # It also cannot claim the new operation's work.
    with pytest.raises(ProfileDeletionBusy):
        await store.begin_profile_deletion("p1", 1, OP_A)


async def test_unknown_profile_can_be_fenced(store):
    record = await store.begin_profile_deletion("never-seen", 7, OP_A)

    assert record.phase == "blocked"
    assert record.tenant_id == 7
    with pytest.raises(ProfileDeletionBusy):
        await store.upsert_profile("never-seen", models=[], sandbox={}, soul="", skills=[], tenant_id=7)

    # No runtime data exists, so purging is a no-op and completion is allowed.
    record = await store.advance_profile_deletion(record, "draining")
    await store.purge_profile_data(record)
    await store.complete_profile_deletion("never-seen", 7, OP_A)
    assert await store.get_profile_deletion("never-seen") is None
    assert await store.get_profile("never-seen") is None


async def test_duplicate_begin_and_completion_are_idempotent(store):
    await put_profile(store)
    first = await store.begin_profile_deletion("p1", 1, OP_A)
    assert await store.begin_profile_deletion("p1", 1, OP_A) == first

    with pytest.raises(ProfileDeletionBusy):
        await store.begin_profile_deletion("p1", 1, OP_B)

    first = await store.advance_profile_deletion(first, "draining")
    await store.purge_profile_data(first)
    await store.complete_profile_deletion("p1", 1, OP_A)
    # A lost response may retry completion: the receipt acknowledges it.
    await store.complete_profile_deletion("p1", 1, OP_A)


async def test_completion_requires_purged_phase_and_matching_operation(store):
    await put_profile(store)
    record = await store.begin_profile_deletion("p1", 1, OP_A)

    with pytest.raises(ProfileDeletionConflict):
        await store.complete_profile_deletion("p1", 1, OP_A)
    record = await store.advance_profile_deletion(record, "draining")
    # The purge has not committed yet: completion must not release the fence.
    with pytest.raises(ProfileDeletionConflict):
        await store.complete_profile_deletion("p1", 1, OP_A)
    await store.purge_profile_data(record)
    with pytest.raises(ProfileDeletionConflict):
        await store.complete_profile_deletion("p1", 1, OP_B)
    with pytest.raises(ProfileTenantMismatch):
        await store.complete_profile_deletion("p1", 2, OP_A)
    assert (await store.get_profile_deletion("p1")).phase == "purged"


async def test_old_run_authority_cannot_write_into_a_reprovisioned_profile(store):
    await put_profile(store)
    await store.ensure_session("p1", "reused-session")
    old_run = await store.create_run(
        profile_id="p1", session_id="reused-session", provider="fake", model="m",
        reasoning_effort=None, input_parts=[{"type": "text", "text": "old"}], instructions=None)

    old = await store.begin_profile_deletion("p1", 1, OP_A)
    old = await store.advance_profile_deletion(old, "draining")
    await store.purge_profile_data(old)
    await store.complete_profile_deletion("p1", 1, OP_A)

    new_profile = await put_profile(store)
    await store.ensure_session("p1", "reused-session")
    assert new_profile.incarnation != old_run.profile_incarnation

    with pytest.raises(ProfileIncarnationMismatch):
        await store.apply_memory("p1", "user", 2200, [{"action": "add", "content": "stale"}],
                                 profile_incarnation=old_run.profile_incarnation)
    with pytest.raises(ProfileIncarnationMismatch):
        await store.set_title("p1", "reused-session", "stale", "llm",
                              profile_incarnation=old_run.profile_incarnation)

    saved = await store.apply_memory("p1", "user", 2200, [{"action": "add", "content": "fresh"}],
                                     profile_incarnation=new_profile.incarnation)
    assert saved["success"]
    assert (await store.set_title("p1", "reused-session", "fresh", "llm",
                                  profile_incarnation=new_profile.incarnation)).title == "fresh"


async def test_old_operation_retry_does_not_change_current_background_block(store):
    from keel.background import Background
    from keel.deletion import ProfileDeletionService

    await put_profile(store)
    background = Background()
    service = ProfileDeletionService(store, background=background)
    try:
        await service.delete("p1", 1, OP_A)
        await service.complete("p1", 1, OP_A)
        new_profile = await put_profile(store)

        # A retry of a completed receipt must not block the new incarnation.
        await service.delete("p1", 1, OP_A)
        accepted = background.spawn(asyncio.sleep(0), "new-incarnation", profile_id="p1",
                                    profile_incarnation=new_profile.incarnation)
        assert accepted
        await asyncio.sleep(0)

        # A newer deletion owns the local block; the old receipt cannot lift it.
        await service.delete("p1", 1, OP_B, timeout_seconds=0)
        await service.complete("p1", 1, OP_A)
        assert not background.spawn(asyncio.sleep(0), "during-new-delete", profile_id="p1",
                                    profile_incarnation=new_profile.incarnation)
    finally:
        await background.close()


async def test_completion_on_another_instance_does_not_leave_old_tracker_blocking_new_profile(store):
    from keel.background import Background
    from keel.deletion import ProfileDeletionService

    await put_profile(store)
    background_a, background_b = Background(), Background()
    service_a = ProfileDeletionService(store, background=background_a)
    service_b = ProfileDeletionService(store, background=background_b)
    try:
        await service_a.delete("p1", 1, OP_A)
        await service_b.complete("p1", 1, OP_A)

        current = await put_profile(store)
        accepted = background_a.spawn(asyncio.sleep(0), "new-incarnation", profile_id="p1",
                                      profile_incarnation=current.incarnation)
        assert accepted
        await asyncio.sleep(0)
    finally:
        await background_a.close()
        await background_b.close()


async def test_mismatched_background_authority_cannot_clear_a_newer_local_block():
    from keel.background import Background

    background = Background()
    background.block_profile("p1", OP_B, "incarnation-b")
    try:
        # A delayed callback carrying A may be admitted to reach its final durable check, but it cannot
        # remove B's process-local block. Unknown authority remains denied outright.
        assert background.spawn(asyncio.sleep(0), "stale", profile_id="p1", profile_incarnation="incarnation-a")
        assert not background.spawn(asyncio.sleep(0), "unknown", profile_id="p1")
        assert not background.spawn(asyncio.sleep(0), "current", profile_id="p1",
                                    profile_incarnation="incarnation-b")
        await asyncio.sleep(0)
    finally:
        await background.close()


async def test_purge_removes_only_the_target_profiles_rows(store):
    await put_profile(store, "p1")
    await put_profile(store, "p2", tenant_id=2)
    for profile in ("p1", "p2"):
        await store.ensure_session(profile, f"{profile}-s")
        run = await store.create_run(profile_id=profile, session_id=f"{profile}-s", provider="fake",
                                     model="m", reasoning_effort=None,
                                     input_parts=[{"type": "text", "text": "hi"}], instructions=None)
        await store.append_message(f"{profile}-s", run.id, _message())
        await store.put_blob(profile, f"{profile}-s", "text/plain", "aGk=")
        await store.apply_memory(profile, "user", 10, [{"action": "add", "content": "likes tea"}],
                                 source=MemorySource("foreground", run_id=run.id, session_id=f"{profile}-s"),
                                 mutation_key=f"foreground:{run.id}:c1")

    for profile in ("p1", "p2"):
        await store.db.execute("INSERT INTO history_backfill (profile_id, done_through, ceiling, updated_at) "
                               "VALUES (?, 0, 1, 0)", profile)

    for profile in ("p1", "p2"):
        await store.db.execute(
            "INSERT INTO memory_reviews (profile_id, session_id, generation, profile_incarnation, reviewed_through, "
            "queued_through, dirty_through, pending_turns, last_completed_at, provider, model) "
            "VALUES (?, ?, 'g', 'i', 0, 0, 1, 1, 0, 'fake', 'm')", profile, f"{profile}-s")
        await store.db.execute(
            "INSERT INTO memory_review_jobs (id, profile_id, session_id, generation, profile_incarnation, status, "
            "provider, model, source_from, source_through, cursor_message, next_attempt_at, created_at, updated_at) "
            "VALUES (?, ?, ?, 'g', 'i', 'failed', 'fake', 'm', 0, 1, 0, 0, 0, 0)", f"j-{profile}", profile,
            f"{profile}-s")

    record = await store.begin_profile_deletion("p1", 1, OP_A)
    record = await store.advance_profile_deletion(record, "draining")
    await store.purge_profile_data(record)

    assert await store.get_profile("p1") is None
    assert await store.list_sessions("p1") == []
    assert await store.memory_entries("p1") == {"memory": [], "user": []}
    for table in ("memory_entries", "memory_changes", "memory_revisions", "memory_mutations",
                  "message_search", "message_tokens", "history_backfill", "memory_reviews", "memory_review_jobs"):
        assert await store.db.fetchrow(f"SELECT 1 AS n FROM {table} WHERE profile_id = ?", "p1") is None
        assert await store.db.fetchrow(f"SELECT 1 AS n FROM {table} WHERE profile_id = ?", "p2") is not None

    assert await store.get_profile("p2") is not None
    assert len(await store.list_sessions("p2")) == 1
    assert (await store.memory_entries("p2"))["user"] == ["likes tea"]


async def test_a_fenced_profile_cannot_start_session_run_or_memory_write(store):
    await put_profile(store)
    record = await store.begin_profile_deletion("p1", 1, OP_A)

    with pytest.raises(ProfileDeletionBusy):
        await store.ensure_session("p1", "s1")
    with pytest.raises(ProfileDeletionBusy):
        await store.create_run(profile_id="p1", session_id="s1", provider="fake", model="m",
                               reasoning_effort=None, input_parts=[{"type": "text", "text": "hi"}],
                               instructions=None)
    with pytest.raises(ProfileDeletionBusy):
        await store.apply_memory("p1", "user", 10, [{"action": "add", "content": "x"}])

    assert record.phase == "blocked"


def _message():
    from keel.models import Message, TextPart

    return Message("user", (TextPart("hi"),))


async def test_purge_removes_learned_skill_rows(store):
    from keel.store.base import SkillSource

    await store.upsert_profile("p1", models=MODELS, sandbox={}, soul="s", skills=[], tenant_id=1, learning=True)
    await store.ensure_session("p1", "s1")
    source = SkillSource("foreground", run_id="r1", session_id="s1", message_id=7, tool_call_id="c1")
    created = await store.change_learned_skill(
        "p1", {"action": "create", "name": "csv-report", "description": "d", "body": "1. Step."}, source=source)
    await store.record_skill_load("p1", "s1", "r1", created["skill"]["id"], 1)
    record = await store.begin_profile_deletion("p1", 1, OP_A)
    record = await store.advance_profile_deletion(record, "draining")
    await store.purge_profile_data(record)
    for table in ("learned_skills", "learned_skill_versions", "learned_skill_evidence", "lesson_observations",
                  "skill_loads"):
        row = await store.db.fetchrow(f"SELECT COUNT(*) AS n FROM {table} WHERE profile_id = ?", "p1")
        assert row["n"] == 0, table
