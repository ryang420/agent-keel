import asyncio
import json
import os
import shutil
import time

import pytest

from keel.config import ServiceCredential, Settings
from keel.service import build
from keel.sandbox.base import parse_policy, profile_mount_root
from keel.store import migrations, open_store
from keel.store.base import OwnershipError, UniqueViolation
from keel.store.postgres import PostgresDb
from keel.store.migrations import LATEST_VERSION, Migration, SchemaTooNew, schema_version
from keel.store.ownership import require_tenant_ownership

COORDINATION_TABLES = {"sandboxes", "sandbox_uses", "sandbox_execs", "sandbox_operations"}
ALL_TABLES = ("history_backfill, message_tokens, message_search, sandbox_execs, sandbox_operations, sandbox_uses, "
              "sandboxes, schema_version, memory_mutations, memory_changes, memory_revisions, memory_entries, "
              "run_events, runs, messages, sessions, profiles, tenants, blobs")

# keel 0.2.0's schema as released (src/keel/store/sql.py at v0.2.0). Kept apart from migration 1, which must
# reproduce it, so that an edit to migration 1 fails the schema comparison below.
V020 = """
CREATE TABLE IF NOT EXISTS profiles (
    id TEXT PRIMARY KEY, models TEXT NOT NULL, sandbox TEXT NOT NULL, soul TEXT NOT NULL,
    skills TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, title TEXT, title_source TEXT,
    created_at DOUBLE PRECISION NOT NULL, last_active_at DOUBLE PRECISION NOT NULL, message_count INTEGER NOT NULL DEFAULT 0,
    system_prompt TEXT, provider TEXT, model TEXT, cwd TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS sessions_profile_title ON sessions (profile_id, title) WHERE title IS NOT NULL;
CREATE INDEX IF NOT EXISTS sessions_profile_active ON sessions (profile_id, last_active_at);
CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT, role TEXT NOT NULL,
    parts TEXT NOT NULL, tool_calls TEXT NOT NULL, tool_call_id TEXT, tool_name TEXT,
    reasoning TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS messages_session ON messages (session_id, id);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, session_id TEXT NOT NULL, status TEXT NOT NULL,
    provider TEXT NOT NULL, model TEXT NOT NULL, reasoning_effort TEXT, input TEXT NOT NULL,
    instructions TEXT, output TEXT NOT NULL DEFAULT '', usage TEXT NOT NULL DEFAULT '{}',
    error_code TEXT, error TEXT, lease_owner TEXT, lease_expires_at DOUBLE PRECISION,
    created_at DOUBLE PRECISION NOT NULL, started_at DOUBLE PRECISION, finished_at DOUBLE PRECISION, current_exec_id TEXT);
CREATE INDEX IF NOT EXISTS runs_status ON runs (status, created_at);
CREATE INDEX IF NOT EXISTS runs_session ON runs (session_id);
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_in_flight_per_session ON runs (session_id)
    WHERE status IN ('queued', 'running', 'stopping');
CREATE TABLE IF NOT EXISTS run_events (
    run_id TEXT NOT NULL, seq INTEGER NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL,
    at DOUBLE PRECISION NOT NULL, PRIMARY KEY (run_id, seq));
CREATE TABLE IF NOT EXISTS blobs (
    id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, session_id TEXT NOT NULL, media_type TEXT NOT NULL,
    data TEXT NOT NULL, created_at DOUBLE PRECISION NOT NULL);
CREATE INDEX IF NOT EXISTS blobs_session ON blobs (session_id);
"""

SEED = """
INSERT INTO profiles (id, models, sandbox, soul, skills, updated_at) VALUES ('p1', '[]', '{}', 'You help.', '[]', 1);
INSERT INTO sessions (id, profile_id, title, title_source, created_at, last_active_at, message_count, system_prompt)
    VALUES ('s1', 'p1', 'Old chat', 'derived', 1, 2, 1, 'sys');
INSERT INTO messages (session_id, run_id, role, parts, tool_calls, reasoning, created_at)
    VALUES ('s1', 'run_1', 'user', '[{"type":"text","text":"hello"}]', '[]', '[]', 1);
INSERT INTO runs (id, profile_id, session_id, status, provider, model, input, created_at, finished_at)
    VALUES ('run_1', 'p1', 's1', 'completed', 'fake', 'm', '[]', 1, 2);
INSERT INTO run_events (run_id, seq, type, data, at) VALUES ('run_1', 1, 'run.completed', '{}', 2);
"""


async def run_script(url: str, script: str) -> None:
    import asyncpg

    conn = await asyncpg.connect(url)
    try:
        await conn.execute(script)
    finally:
        await conn.close()


async def connect(url: str):
    """A connection that, unlike open_store, does not migrate."""
    return await PostgresDb.connect(url)


async def empty_database(url: str) -> str:
    """Reset this test's isolated PostgreSQL database for fresh-schema comparison."""
    await run_script(url, f"DROP TABLE IF EXISTS {ALL_TABLES}")
    return url


async def restore_legacy_memories(db) -> None:
    """Undo migration 8 on a database that is being rewound to an older schema version."""
    for table in ("memory_mutations", "memory_changes", "memory_revisions", "memory_entries"):
        await db.execute(f"DROP TABLE {table}")
    await db.execute("CREATE TABLE memories (profile_id TEXT NOT NULL, target TEXT NOT NULL, "
                     "entries TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL, PRIMARY KEY (profile_id, target))")


async def restore_pre_history_index(db) -> None:
    """Undo migration 9 on a database that is being rewound to an older schema version."""
    for table in ("history_backfill", "message_tokens", "message_search"):
        await db.execute(f"DROP TABLE {table}")
    await db.execute("ALTER TABLE sessions DROP COLUMN tools")


async def database_table_names(db) -> set[str]:
    """Every table in the current PostgreSQL schema."""
    rows = await db.fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")
    return {row["table_name"] for row in rows}


async def insert_sandbox(db, deployment="keel", profile_id="p1", daemon_id="daemon-1", token="gen-token-1",
                         container_id=None):
    await db.execute(
        "INSERT INTO sandboxes (deployment, profile_id, daemon_id, generation, generation_token, container_id, "
        "state, transitioned_at) VALUES (?, ?, ?, 1, ?, ?, 'creating', 1)",
        deployment, profile_id, daemon_id, token, container_id)


async def insert_usage(db, usage_id="u1", token="token-1", run_id="run_1", session_id="s1"):
    await db.execute(
        "INSERT INTO sandbox_uses (id, token, deployment, profile_id, daemon_id, run_id, session_id, owner, "
        "state, heartbeat_at, lease_expires_at) "
        "VALUES (?, ?, 'keel', 'p1', 'daemon-1', ?, ?, 'w1', 'preparing', 1, 2)",
        usage_id, token, run_id, session_id)


async def insert_exec(db, exec_id="e1", control_token="control-1", usage_id="u1", usage_token="token-1",
                      run_id="run_1", session_id="s1"):
    await db.execute(
        "INSERT INTO sandbox_execs (id, control_token, usage_id, usage_token, deployment, profile_id, daemon_id, "
        "run_id, session_id, generation, generation_token, container_id, state, created_at) "
        "VALUES (?, ?, ?, ?, 'keel', 'p1', 'daemon-1', ?, ?, 1, 'gen-1', 'c1', 'pending', 1)",
        exec_id, control_token, usage_id, usage_token, run_id, session_id)


async def insert_operation(db, operation_id="op1", kind="create", intended_name="name-1", authority="current",
                           outcome="planned", container_id=None, daemon_id="daemon-1", run_id="run_1",
                           session_id="s1", retired_at=None):
    await db.execute(
        "INSERT INTO sandbox_operations (id, kind, deployment, profile_id, daemon_id, run_id, session_id, "
        "generation, generation_token, intended_name, container_id, owner, owner_token, authority, retired_at, "
        "outcome, created_at, updated_at) "
        "VALUES (?, ?, 'keel', 'p1', ?, ?, ?, 1, 'gen-1', ?, ?, 'w1', ?, ?, ?, ?, 1, 1)",
        operation_id, kind, daemon_id, run_id, session_id, intended_name, container_id,
        "owner-" + operation_id, authority, retired_at, outcome)


async def schema_shape(db) -> dict:
    """Every table's columns and every index, in a form that compares equal for equal schemas."""
    columns: dict[str, list] = {}
    for row in await db.fetch(
            "SELECT table_name, column_name, data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = current_schema() ORDER BY table_name, ordinal_position"):
        columns.setdefault(row["table_name"], []).append(
            (row["column_name"], row["data_type"], row["is_nullable"], row["column_default"]))
    indexes = {row["indexname"]: row["indexdef"] for row in await db.fetch(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema()")}
    return {"columns": columns, "indexes": indexes}


@pytest.fixture
async def db_url(pg_dsn):
    return pg_dsn


async def test_a_fresh_database_reaches_the_latest_version(db_url):
    store = await open_store(db_url)
    try:
        assert await schema_version(store.db) == LATEST_VERSION
        await store.upsert_profile("p1", models=[], sandbox={}, soul="", skills=[])
        assert (await store.ensure_session("p1", "s1")).message_count == 0
    finally:
        await store.close()


async def test_a_0_2_0_database_is_upgraded_in_place_and_matches_a_fresh_one(db_url):
    await run_script(db_url, V020 + SEED)
    store = await open_store(db_url)
    try:
        assert await schema_version(store.db) == LATEST_VERSION
        assert await database_table_names(store.db) >= COORDINATION_TABLES
        stamped = await store.db.fetch("SELECT version FROM schema_version")
        assert [row["version"] for row in stamped] == [LATEST_VERSION]
        assert (await store.get_profile("p1")).soul == "You help."
        assert (await store.get_profile("p1")).incarnation
        session = await store.get_session("p1", "s1")
        assert (session.title, session.title_source, session.system_prompt) == ("Old chat", "derived", "sys")
        assert [stored.message.text for stored in await store.messages("s1")] == ["hello"]
        assert (await store.get_run_by_id("run_1")).status == "completed"
        assert (await store.get_run_by_id("run_1")).profile_incarnation == (await store.get_profile("p1")).incarnation
        assert [event.type for event in await store.events_after("run_1", 0)] == ["run.completed"]
        upgraded = await schema_shape(store.db)
    finally:
        await store.close()
    fresh = await open_store(await empty_database(db_url))
    try:
        assert await schema_shape(fresh.db) == upgraded
    finally:
        await fresh.close()


async def test_invalid_tenant_cutover_does_not_change_the_old_database(db_url):
    await run_script(db_url, V020 + SEED)
    db = await connect(db_url)
    try:
        before = await schema_shape(db)
    finally:
        await db.close()
    settings = Settings(credentials=(ServiceCredential(1, "k"),), database_url=db_url, sandbox="none")
    with pytest.raises(OwnershipError, match="BACKFILL"):
        await build(settings)
    db = await connect(db_url)
    try:
        assert await schema_version(db) == 1
        assert await schema_shape(db) == before
        assert (await db.fetchrow("SELECT soul FROM profiles WHERE id = ?", "p1"))["soul"] == "You help."
    finally:
        await db.close()


async def test_complete_tenant_cutover_preserves_old_data_and_matches_fresh_schema(db_url):
    await run_script(db_url, V020 + SEED + """
        INSERT INTO blobs (id, profile_id, session_id, media_type, data, created_at)
        VALUES ('b1', 'p1', 's1', 'image/png', 'aGVsbG8=', 1);
    """)
    settings = Settings(credentials=(ServiceCredential(1, "k"),), database_url=db_url, sandbox="none",
                        tenant_backfill={"p1": 1})
    keel, _pool, closables, _backend = await build(settings)
    try:
        store = keel.store
        assert (await store.get_profile("p1")).tenant_id == 1
        assert (await store.get_session("p1", "s1")).title == "Old chat"
        assert [message.message.text for message in await store.messages("s1")] == ["hello"]
        assert (await store.get_run_by_id("run_1")).status == "completed"
        assert [event.type for event in await store.events_after("run_1", 0)] == ["run.completed"]
        assert (await store.db.fetchrow("SELECT data FROM blobs WHERE id = ?", "b1"))["data"] == "aGVsbG8="
        upgraded = await schema_shape(store.db)
    finally:
        for item in closables:
            await (getattr(item, "aclose", None) or getattr(item, "close"))()
    fresh_url = await empty_database(db_url)
    fresh_settings = Settings(credentials=(ServiceCredential(1, "k"),), database_url=fresh_url, sandbox="none")
    fresh, _pool, closables, _backend = await build(fresh_settings)
    try:
        assert await schema_shape(fresh.store.db) == upgraded
    finally:
        for item in closables:
            await (getattr(item, "aclose", None) or getattr(item, "close"))()


async def test_stopped_service_cutover_preserves_a_legacy_mount_and_its_file(db_url, tmp_path):
    await run_script(db_url, V020 + SEED)
    old = tmp_path / "legacy-p1"
    old.mkdir()
    (old / "marker.txt").write_text("kept")
    root = tmp_path / "mounts"
    root.mkdir()
    new = tmp_path / "mounts" / os.path.basename(profile_mount_root(str(root), "p1"))
    old_policy = {"image": "python:3.12-slim", "workdir": "/work", "mounts": [
        {"source": str(old), "target": "/private"}]}
    db = await connect(db_url)
    try:
        await db.execute("UPDATE profiles SET sandbox = ? WHERE id = ?", json.dumps(old_policy), "p1")
    finally:
        await db.close()

    # With old instances stopped, the operator copies and verifies files before changing the stored source.
    shutil.copytree(old, new)
    assert (new / "marker.txt").read_bytes() == (old / "marker.txt").read_bytes()
    new_policy = {**old_policy, "mounts": [{"source": str(new), "target": "/private"}]}
    db = await connect(db_url)
    try:
        async with db.transaction():
            await db.execute("UPDATE profiles SET sandbox = ? WHERE id = ? AND sandbox = ?",
                             json.dumps(new_policy), "p1", json.dumps(old_policy))
    finally:
        await db.close()

    settings = Settings(credentials=(ServiceCredential(1, "k"),), database_url=db_url, sandbox="none",
                        sandbox_mount_root=str(root), tenant_backfill={"p1": 1})
    keel, _pool, closables, _backend = await build(settings)
    try:
        profile = await keel.store.get_profile("p1")
        policy = parse_policy(profile.sandbox, profile_mount_root(str(root), "p1"))
        assert policy.mounts[0].source == str(new)
        assert (new / "marker.txt").read_text() == "kept"
        assert (old / "marker.txt").read_text() == "kept"  # leave the original until cutover is verified
        assert (await keel.store.get_session("p1", "s1")).title == "Old chat"
        assert [message.message.text for message in await keel.store.messages("s1")] == ["hello"]
    finally:
        for item in closables:
            await (getattr(item, "aclose", None) or getattr(item, "close"))()


async def test_opening_again_changes_nothing(db_url):
    first = await open_store(db_url)
    await first.ensure_session("p1", "s1")
    await first.close()
    again = await open_store(db_url)
    try:
        assert await schema_version(again.db) == LATEST_VERSION
        assert await again.get_session("p1", "s1") is not None
    finally:
        await again.close()


async def test_a_database_newer_than_the_code_is_refused(db_url):
    store = await open_store(db_url)
    await store.db.execute("UPDATE schema_version SET version = ?", LATEST_VERSION + 1)
    await store.close()
    with pytest.raises(SchemaTooNew):
        await open_store(db_url)
    db = await connect(db_url)
    try:
        assert await schema_version(db) == LATEST_VERSION + 1
    finally:
        await db.close()


async def test_a_failed_migration_leaves_the_database_at_the_version_before(db_url, monkeypatch):
    await (await open_store(db_url)).close()
    failing = Migration(LATEST_VERSION + 1, """
ALTER TABLE profiles ADD COLUMN doomed INTEGER;
CREATE TABLE doomed (id TEXT PRIMARY KEY);
INSERT INTO no_such_table (id) VALUES ('x');
""")
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS + (failing,))
    monkeypatch.setattr(migrations, "LATEST_VERSION", failing.version)
    with pytest.raises(Exception, match="no_such_table"):
        await open_store(db_url)
    db = await connect(db_url)
    try:
        assert await schema_version(db) == failing.version - 1
        shape = await schema_shape(db)
        assert "doomed" not in shape["columns"]
        assert "doomed" not in [column[0] for column in shape["columns"]["profiles"]]
    finally:
        await db.close()


async def test_a_0_2_0_database_whose_first_start_crashed_is_completed(db_url):
    partial = [s for s in V020.split(";") if "TABLE IF NOT EXISTS profiles" in s or "blobs" in s]
    await run_script(db_url, ";".join(partial))
    store = await open_store(db_url)
    try:
        assert await schema_version(store.db) == LATEST_VERSION
        upgraded = await schema_shape(store.db)
    finally:
        await store.close()
    fresh = await open_store(await empty_database(db_url))
    try:
        assert await schema_shape(fresh.db) == upgraded
    finally:
        await fresh.close()


async def test_an_empty_version_table_reads_like_no_version_table(db_url):
    await run_script(db_url, V020 + "CREATE TABLE schema_version (version INTEGER NOT NULL);")
    db = await connect(db_url)
    try:
        assert await schema_version(db) == 1
    finally:
        await db.close()


async def test_instances_starting_together_upgrade_an_old_database_once(db_url):
    url = db_url
    await run_script(url, V020 + SEED)
    stores = await asyncio.gather(*(open_store(url) for _ in range(6)))
    try:
        for store in stores:
            assert await schema_version(store.db) == LATEST_VERSION
        assert await stores[0].get_session("p1", "s1") is not None
    finally:
        for store in stores:
            await store.close()


async def test_m3_fields_read_their_defaults_on_a_fresh_database(db_url):
    store = await open_store(db_url)
    try:
        assert (await store.upsert_profile("p1", models=[], sandbox={}, soul="", skills=[])).memory is False
        session = await store.ensure_session("p1", "s1")
        assert (session.summary, session.summary_head_id, session.summary_tail_id) == (None, None, None)
        assert (session.context_tokens, session.context_message_id, session.compaction_failed_at) == (None, None, None)
        assert session.turns_since_memory == 0
        assert session.pruned_id is None
    finally:
        await store.close()


async def test_an_upgraded_0_2_0_database_gets_the_m3_defaults(db_url):
    await run_script(db_url, V020 + SEED)
    store = await open_store(db_url)
    try:
        assert (await store.get_profile("p1")).memory is False
        session = await store.get_session("p1", "s1")
        assert session.summary is None and session.turns_since_memory == 0 and session.title == "Old chat"
        assert session.pruned_id is None
    finally:
        await store.close()


async def test_migration_4_keeps_existing_profiles_unowned(db_url):
    store = await open_store(db_url)
    await store.upsert_profile("p1", models=[], sandbox={}, soul="kept", skills=[])
    await store.db.execute("UPDATE schema_version SET version = 3")
    await restore_pre_history_index(store.db)
    await restore_legacy_memories(store.db)
    await store.db.execute("ALTER TABLE runs DROP COLUMN profile_incarnation")
    await store.db.execute("ALTER TABLE profiles DROP COLUMN incarnation")
    await store.db.execute("DROP INDEX IF EXISTS profiles_tenant")
    await store.db.execute("ALTER TABLE profiles DROP COLUMN tenant_id")
    # A v3 database predates migration 5; drop its tables so the reopen exercises both migrations.
    for table in ("sandbox_execs", "sandbox_operations", "sandbox_uses", "sandboxes"):
        await store.db.execute(f"DROP TABLE IF EXISTS {table}")
    await store.close()
    upgraded = await open_store(db_url)
    try:
        assert await schema_version(upgraded.db) == LATEST_VERSION
        profile = await upgraded.get_profile("p1")
        assert profile.soul == "kept" and profile.tenant_id is None
        assert await upgraded.db.fetchrow("SELECT id FROM tenants") is None
        columns = await schema_shape(upgraded.db)
        assert "tenants" in columns["columns"]
        assert "profiles_tenant" in columns["indexes"]
        tenant_type = await upgraded.db.fetchrow(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'tenants' AND column_name = 'id'")
        owner_type = await upgraded.db.fetchrow(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'profiles' AND column_name = 'tenant_id'")
        assert tenant_type["data_type"] == owner_type["data_type"] == "integer"
    finally:
        await upgraded.close()


async def test_open_backfills_v7_incarnation_tokens_for_existing_profiles_and_runs(db_url):
    store = await open_store(db_url)
    await store.upsert_profile("p1", models=[], sandbox={}, soul="old", skills=[], tenant_id=1)
    await store.ensure_session("p1", "s1")
    run = await store.create_run(profile_id="p1", session_id="s1", provider="fake", model="m",
                                 reasoning_effort=None, input_parts=[], instructions=None)
    await store.db.execute("UPDATE profiles SET incarnation = NULL WHERE id = ?", "p1")
    await store.db.execute("UPDATE runs SET profile_incarnation = NULL WHERE id = ?", run.id)
    await store.close()

    upgraded = await open_store(db_url)
    try:
        profile = await upgraded.get_profile("p1")
        saved_run = await upgraded.get_run_by_id(run.id)
        assert profile.incarnation
        assert saved_run.profile_incarnation == profile.incarnation
    finally:
        await upgraded.close()


async def test_a_v4_database_gains_the_coordination_tables(db_url):
    store = await open_store(db_url)
    await store.upsert_profile("p1", models=[], sandbox={}, soul="kept-v4", skills=[])
    for table in ("sandbox_execs", "sandbox_operations", "sandbox_uses", "sandboxes"):
        await store.db.execute(f"DROP TABLE {table}")
    await store.db.execute("UPDATE schema_version SET version = 4")
    await restore_pre_history_index(store.db)
    await restore_legacy_memories(store.db)
    await store.db.execute("ALTER TABLE runs DROP COLUMN profile_incarnation")
    await store.db.execute("ALTER TABLE profiles DROP COLUMN incarnation")
    await store.close()

    upgraded = await open_store(db_url)
    try:
        assert await schema_version(upgraded.db) == LATEST_VERSION
        assert await database_table_names(upgraded.db) >= COORDINATION_TABLES
        assert (await upgraded.get_profile("p1")).soul == "kept-v4"
        await insert_usage(upgraded.db, "u1", "token-1", "run_1")
        with pytest.raises(UniqueViolation):
            await insert_usage(upgraded.db, "u2", "token-2", "run_1")
    finally:
        await upgraded.close()


async def test_coordination_identities_are_unique(db_url):
    store = await open_store(db_url)
    try:
        db = store.db
        await insert_usage(db, "u1", "token-1", "run_1")
        with pytest.raises(UniqueViolation):
            await insert_usage(db, "u2", "token-2", "run_1")  # one usage per run
        with pytest.raises(UniqueViolation):
            await insert_usage(db, "u3", "token-1", "run_3")  # unique usage token
        await insert_exec(db, "e1", "control-1")
        with pytest.raises(UniqueViolation):
            await insert_exec(db, "e2", "control-1", usage_id="u2", usage_token="token-2")  # unique control token
        await insert_operation(db, "op1", intended_name="create-name-1")
        with pytest.raises(UniqueViolation):
            await insert_operation(db, "op1", intended_name="create-name-2")  # unique operation id
        with pytest.raises(UniqueViolation):
            await insert_operation(db, "op2", intended_name="create-name-1")  # unique create name per daemon
        await insert_operation(db, "op3", kind="install", intended_name="create-name-1")  # linked steps share it
        await insert_operation(db, "op4", intended_name="create-name-1", daemon_id="daemon-2")
        await insert_operation(db, "remove-1", kind="remove", intended_name=None, container_id="c9")
        with pytest.raises(UniqueViolation):
            await insert_operation(db, "remove-2", kind="remove", intended_name=None, container_id="c9")
        await insert_operation(db, "remove-3", kind="remove", intended_name=None, container_id="c9",
                               authority="retired", outcome="uncertain", retired_at=3)
    finally:
        await store.close()


async def test_operation_authority_and_outcome_are_independent(db_url):
    store = await open_store(db_url)
    try:
        await insert_operation(store.db, "op-retired-uncertain", authority="retired", outcome="uncertain",
                               retired_at=2)
        await insert_operation(store.db, "op-current-succeeded", intended_name="name-2", outcome="succeeded")
        saved = await store.db.fetchrow(
            "SELECT authority, outcome FROM sandbox_operations WHERE id = ?", "op-retired-uncertain")
        assert (saved["authority"], saved["outcome"]) == ("retired", "uncertain")
        saved = await store.db.fetchrow(
            "SELECT authority, outcome FROM sandbox_operations WHERE id = ?", "op-current-succeeded")
        assert (saved["authority"], saved["outcome"]) == ("current", "succeeded")
        with pytest.raises(Exception):
            await insert_operation(store.db, "op-bad-authority", intended_name="name-3", authority="revoked")
        with pytest.raises(Exception):
            await insert_operation(store.db, "op-bad-outcome", intended_name="name-4", outcome="failed")
    finally:
        await store.close()


async def test_coordination_constraints_survive_tenant_ownership_enforcement(db_url):
    store = await open_store(db_url)
    try:
        await store.upsert_profile("p1", models=[], sandbox={}, soul="", skills=[])
        await insert_sandbox(store.db, token="gen-token-before", container_id="c-before")
        await require_tenant_ownership(store.db, (1,), {"p1": 1})
        assert await database_table_names(store.db) >= COORDINATION_TABLES
        row = await store.db.fetchrow(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'profiles' AND column_name = 'tenant_id'")
        assert row["is_nullable"] == "NO"
        survivor = await store.db.fetchrow(
            "SELECT generation_token, container_id FROM sandboxes WHERE profile_id = ?", "p1")
        assert (survivor["generation_token"], survivor["container_id"]) == ("gen-token-before", "c-before")
        with pytest.raises(UniqueViolation):
            await insert_sandbox(store.db, deployment="other", token="gen-token-before")
        with pytest.raises(UniqueViolation):
            await insert_sandbox(store.db, deployment="other", token="gen-token-2", container_id="c-before")
        await insert_usage(store.db, "u1", "token-1", "run_1")
        with pytest.raises(UniqueViolation):
            await insert_usage(store.db, "u2", "token-2", "run_1")
        await insert_operation(store.db, "op1", intended_name="name-1")
        with pytest.raises(UniqueViolation):
            await insert_operation(store.db, "op2", intended_name="name-1")
    finally:
        await store.close()


async def test_coordination_evidence_survives_terminal_session_deletion(db_url):
    store = await open_store(db_url)
    try:
        await store.upsert_profile("p1", models=[], sandbox={}, soul="", skills=[])
        await store.ensure_session("p1", "s1")
        run = await store.create_run(profile_id="p1", session_id="s1", provider="fake", model="m",
                                     reasoning_effort=None, input_parts=[], instructions=None)
        await store.finish_run(run.id, "completed", output="done")
        await insert_usage(store.db, "u1", "token-1", run.id, "s1")
        await insert_exec(store.db, "e1", "control-1", run_id=run.id, session_id="s1")
        await insert_operation(store.db, "op1", run_id=run.id, session_id="s1")

        assert await store.delete_session("p1", "s1") is True
        assert await store.get_session("p1", "s1") is None
        assert await store.get_run_by_id(run.id) is None
        assert await store.db.fetchrow("SELECT id FROM sandbox_uses WHERE id = ?", "u1") is not None
        assert await store.db.fetchrow("SELECT id FROM sandbox_execs WHERE id = ?", "e1") is not None
        assert await store.db.fetchrow("SELECT id FROM sandbox_operations WHERE id = ?", "op1") is not None
    finally:
        await store.close()


async def test_coordination_time_is_database_time_unless_injected(db_url):
    store = await open_store(db_url)
    try:
        first = await store.coordination_now()
        second = await store.coordination_now()
        assert isinstance(first, float) and isinstance(second, float)
        assert 0 <= second - first < 60
    finally:
        await store.close()
    injected = await open_store(db_url, coordination_clock=lambda: 1234.5)
    try:
        assert await injected.coordination_now() == 1234.5
    finally:
        await injected.close()


async def test_coordination_time_is_read_at_call_time_inside_a_transaction(db_url):
    store = await open_store(db_url)
    try:
        async with store.db.transaction():
            before = await store.coordination_now()
            started = time.monotonic()
            await asyncio.sleep(0.06)
            after = await store.coordination_now()
            elapsed = time.monotonic() - started
        # Transaction-start timestamps (not transaction_timestamp/now()) would show no advance at all.
        assert elapsed >= 0.05
        assert after - before >= 0.03
    finally:
        await store.close()


async def test_migration_8_copies_legacy_memory_lists_into_entries(db_url, monkeypatch):
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:7])
    monkeypatch.setattr(migrations, "LATEST_VERSION", 7)
    store = await open_store(db_url)
    await store.upsert_profile("p1", models=[], sandbox={}, soul="", skills=[], tenant_id=1)
    await store.db.execute("INSERT INTO memories (profile_id, target, entries, updated_at) VALUES (?, ?, ?, ?)",
                           "p1", "user", json.dumps(["likes tea", "likes tea", "works late"]), 5.0)
    await store.db.execute("INSERT INTO memories (profile_id, target, entries, updated_at) VALUES (?, ?, ?, ?)",
                           "p1", "memory", "[]", 5.0)
    await store.close()
    monkeypatch.undo()

    upgraded = await open_store(db_url)
    try:
        assert await schema_version(upgraded.db) == 9
        # Order and duplicates survive exactly; the rendered snapshot is unchanged.
        assert await upgraded.memory_entries("p1") == {"memory": [], "user": ["likes tea", "likes tea", "works late"]}
        items = await upgraded.memory_items("p1")
        assert [(item.target, item.version, item.source_kind, item.run_id) for item in items] == [
            ("user", 1, "legacy", None)] * 3
        assert len({item.id for item in items}) == 3
        revision = await upgraded.db.fetchrow(
            "SELECT revision FROM memory_revisions WHERE profile_id = ? AND target = ?", "p1", "user")
        assert revision["revision"] == 1
        assert "memories" not in await database_table_names(upgraded.db)
    finally:
        await upgraded.close()


async def test_migration_9_records_a_backfill_ceiling_per_profile_and_adds_the_tool_list(db_url, monkeypatch):
    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS[:8])
    monkeypatch.setattr(migrations, "LATEST_VERSION", 8)
    store = await open_store(db_url)
    for profile in ("p1", "p2"):
        await store.upsert_profile(profile, models=[], sandbox={}, soul="", skills=[], tenant_id=1)
        await store.ensure_session(profile, f"{profile}-s")
    ids = {}
    for profile in ("p1", "p2"):
        for text in ("one", "two"):
            row = await store.db.fetchrow(
                "INSERT INTO messages (session_id, run_id, role, parts, tool_calls, tool_call_id, tool_name, "
                "reasoning, active, created_at) VALUES (?, NULL, 'user', ?, '[]', NULL, NULL, '[]', 1, 1.0) "
                "RETURNING id", f"{profile}-s", json.dumps([{"type": "text", "text": text}]))
            ids[profile] = row["id"]
    await store.close()
    monkeypatch.undo()

    upgraded = await open_store(db_url)
    try:
        assert await schema_version(upgraded.db) == 9
        rows = await upgraded.db.fetch(
            "SELECT profile_id, done_through, ceiling FROM history_backfill ORDER BY profile_id")
        assert [(r["profile_id"], r["done_through"], r["ceiling"]) for r in rows] == [
            ("p1", 0, ids["p1"]), ("p2", 0, ids["p2"])]
        assert (await upgraded.get_session("p1", "p1-s")).tools is None
        assert not await upgraded.history_index_complete("p1")
        while await upgraded.backfill_history() is not None:
            pass
        assert await upgraded.history_index_complete("p1")
        assert (await upgraded.db.fetchrow("SELECT COUNT(*) AS n FROM message_search"))["n"] == 4
    finally:
        await upgraded.close()
