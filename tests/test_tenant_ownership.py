import pytest

from keel.store import open_store
from keel.store.base import OwnershipError
from keel.store.ownership import require_tenant_ownership


async def _profile(store, profile_id, tenant_id=None):
    await store.upsert_profile(profile_id, models=[], sandbox={}, soul=profile_id, skills=[])
    if tenant_id is not None:
        await store.db.execute("UPDATE profiles SET tenant_id = ? WHERE id = ?", tenant_id, profile_id)


async def test_a_fresh_database_rejects_a_null_or_unknown_owner(store):
    await require_tenant_ownership(store.db, (1,), {})
    with pytest.raises(Exception):
        await store.db.execute(
            "INSERT INTO profiles (id, models, sandbox, soul, skills, updated_at, memory, tenant_id) "
            "VALUES ('bare', '[]', '{}', '', '[]', 0, 0, NULL)")
    with pytest.raises(Exception):
        await store.db.execute(
            "INSERT INTO profiles (id, models, sandbox, soul, skills, updated_at, memory, tenant_id) "
            "VALUES ('bare', '[]', '{}', '', '[]', 0, 0, 999999)")
    for invalid in (2_147_483_648, -2_147_483_649):
        with pytest.raises(Exception):
            await store.db.execute("INSERT INTO tenants (id) VALUES (?)", invalid)
    for valid in (0, -1, 2_147_483_647, -2_147_483_648):
        await store.db.execute("INSERT INTO tenants (id) VALUES (?)", valid)
    assert await store.get_profile("bare") is None


async def test_backfill_assigns_every_profile_or_changes_nothing(store):
    await _profile(store, "p1")
    await _profile(store, "p2")
    before = {profile_id: (await store.get_profile(profile_id)).incarnation for profile_id in ("p1", "p2")}
    await store.ensure_session("p1", "p1-session")
    run = await store.create_run(profile_id="p1", session_id="p1-session", provider="fake", model="m",
                                 reasoning_effort=None, input_parts=[], instructions=None)
    with pytest.raises(OwnershipError, match="KEEL_TENANT_BACKFILL"):
        await require_tenant_ownership(store.db, (1,), {})
    assert (await store.get_profile("p1")).tenant_id is None
    assert await store.db.fetchrow("SELECT id FROM tenants") is None

    with pytest.raises(OwnershipError, match="every profile"):
        await require_tenant_ownership(store.db, (1,), {"p1": 1})
    assert (await store.get_profile("p1")).tenant_id is None

    with pytest.raises(OwnershipError, match="unknown profile"):
        await require_tenant_ownership(store.db, (1,), {"p1": 1, "p2": 1, "nope": 1})
    with pytest.raises(OwnershipError, match="not provisioned"):
        await require_tenant_ownership(store.db, (1,), {"p1": 1, "p2": 999999})
    assert (await store.get_profile("p2")).tenant_id is None

    await require_tenant_ownership(store.db, (1, 2), {"p1": 1, "p2": 2})
    assert (await store.get_profile("p1")).tenant_id == 1
    assert (await store.get_profile("p2")).tenant_id == 2
    assert {profile_id: (await store.get_profile(profile_id)).incarnation for profile_id in ("p1", "p2")} == before
    assert (await store.get_run_by_id(run.id)).profile_incarnation == before["p1"]
    await require_tenant_ownership(store.db, (1, 2), {})  # already complete

    with pytest.raises(OwnershipError, match="conflicts"):
        await require_tenant_ownership(store.db, (1, 2), {"p1": 2, "p2": 2})
    assert (await store.get_profile("p1")).tenant_id == 1


async def test_backfill_rejects_a_persisted_tenant_without_a_current_credential(store):
    await store.db.execute("INSERT INTO tenants (id) VALUES (?)", 2)
    await _profile(store, "p1")
    with pytest.raises(OwnershipError, match="not provisioned"):
        await require_tenant_ownership(store.db, (1,), {"p1": 2})
    assert (await store.get_profile("p1")).tenant_id is None
