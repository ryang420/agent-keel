from keel.store import open_store
from keel.store.ownership import require_tenant_ownership


async def test_memory_and_titles_survive_a_restart_on_their_own_profiles(tmp_path, pg_dsn):
    url = pg_dsn
    store = await open_store(url)
    await require_tenant_ownership(store.db, (1,), {})
    for profile_id, fact in (("p1", "FACT-P1"), ("p2", "FACT-P2")):
        await store.upsert_profile(profile_id, models=[], sandbox={}, soul="", skills=[], memory=True,
                                   tenant_id=1)
        await store.ensure_session(profile_id, "s-" + profile_id)
        await store.set_title(profile_id, "s-" + profile_id, fact, "llm")
        saved = await store.apply_memory(profile_id, "memory", 2200, [{"action": "add", "content": fact}])
        assert saved["success"] is True
    await store.close()

    again = await open_store(url)
    try:
        assert (await again.memory_entries("p1"))["memory"] == ["FACT-P1"]
        assert (await again.memory_entries("p2"))["memory"] == ["FACT-P2"]
        assert (await again.get_session("p1", "s-p1")).title == "FACT-P1"
        assert await again.get_session("p1", "s-p2") is None
        assert (await again.get_profile("p1")).tenant_id == 1
        assert (await again.get_profile("p2")).tenant_id == 1
    finally:
        await again.close()
