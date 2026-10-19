"""Durable, tenant-owned profile deletion fences and completion receipts."""

import pytest

from keel.store.base import ProfileDeletionBusy, ProfileDeletionConflict, ProfileTenantMismatch

MODELS = [{"provider": "fake", "model": "m"}]
OP_A = "11111111-1111-4111-8111-111111111111"
OP_B = "22222222-2222-4222-8222-222222222222"


async def put_profile(store, profile_id="p1", tenant_id=1):
    return await store.upsert_profile(profile_id, models=MODELS, sandbox={}, soul="s", skills=[],
                                      tenant_id=tenant_id)


async def test_delete_fence_survives_reopen(tmp_path, clock):
    from keel.store import open_store

    url = f"sqlite:///{tmp_path / 'keel.sqlite3'}"
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
    old = await store.advance_profile_deletion(old, "purged")
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
    record = await store.advance_profile_deletion(record, "purged")
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
    first = await store.advance_profile_deletion(first, "purged")
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
    record = await store.advance_profile_deletion(record, "purged")
    with pytest.raises(ProfileDeletionConflict):
        await store.complete_profile_deletion("p1", 1, OP_B)
    with pytest.raises(ProfileTenantMismatch):
        await store.complete_profile_deletion("p1", 2, OP_A)
    assert (await store.get_profile_deletion("p1")).phase == "purged"


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
        await store.apply_memory(profile, "user", 10, [{"action": "add", "content": "likes tea"}])

    record = await store.begin_profile_deletion("p1", 1, OP_A)
    record = await store.advance_profile_deletion(record, "draining")
    record = await store.advance_profile_deletion(record, "purged")
    await store.purge_profile_data(record)

    assert await store.get_profile("p1") is None
    assert await store.list_sessions("p1") == []
    assert await store.memory_entries("p1") == {"memory": [], "user": []}

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
