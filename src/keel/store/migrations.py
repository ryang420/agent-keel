"""Numbered schema migrations (M3 design §1). Version 1 is the 0.2.0 schema exactly as released. A released
migration is never edited; a schema change is a new migration."""

from __future__ import annotations

from dataclasses import dataclass

# Taken first in every migration's transaction, so instances starting together migrate once.
# The key must equal 0.2.0's, so that a 0.2.0 instance and this one starting together also serialise.
_PG_LOCK = "SELECT pg_advisory_xact_lock(7312100001)"
_TYPES = {"real": "DOUBLE PRECISION", "serial": "BIGSERIAL", "bigint": "BIGINT"}


class SchemaTooNew(Exception):
    """The database was migrated by a newer keel; a downgrade is not supported."""


@dataclass(frozen=True)
class Migration:
    version: int
    template: str  # statements separated by ";", with {real}, {serial} and {bigint} filled with PostgreSQL types

    def statements(self) -> list[str]:
        sql = self.template
        sql = sql.format(**_TYPES)
        return [statement.strip() for statement in sql.split(";") if statement.strip()]


_V1 = """
CREATE TABLE IF NOT EXISTS profiles (
    id TEXT PRIMARY KEY, models TEXT NOT NULL, sandbox TEXT NOT NULL, soul TEXT NOT NULL,
    skills TEXT NOT NULL, updated_at {real} NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, title TEXT, title_source TEXT,
    created_at {real} NOT NULL, last_active_at {real} NOT NULL, message_count INTEGER NOT NULL DEFAULT 0,
    system_prompt TEXT, provider TEXT, model TEXT, cwd TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS sessions_profile_title ON sessions (profile_id, title) WHERE title IS NOT NULL;
CREATE INDEX IF NOT EXISTS sessions_profile_active ON sessions (profile_id, last_active_at);
CREATE TABLE IF NOT EXISTS messages (
    id {serial} PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT, role TEXT NOT NULL,
    parts TEXT NOT NULL, tool_calls TEXT NOT NULL, tool_call_id TEXT, tool_name TEXT,
    reasoning TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at {real} NOT NULL);
CREATE INDEX IF NOT EXISTS messages_session ON messages (session_id, id);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, session_id TEXT NOT NULL, status TEXT NOT NULL,
    provider TEXT NOT NULL, model TEXT NOT NULL, reasoning_effort TEXT, input TEXT NOT NULL,
    instructions TEXT, output TEXT NOT NULL DEFAULT '', usage TEXT NOT NULL DEFAULT '{{}}',
    error_code TEXT, error TEXT, lease_owner TEXT, lease_expires_at {real},
    created_at {real} NOT NULL, started_at {real}, finished_at {real}, current_exec_id TEXT);
CREATE INDEX IF NOT EXISTS runs_status ON runs (status, created_at);
CREATE INDEX IF NOT EXISTS runs_session ON runs (session_id);
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_in_flight_per_session ON runs (session_id)
    WHERE status IN ('queued', 'running', 'stopping');
CREATE TABLE IF NOT EXISTS run_events (
    run_id TEXT NOT NULL, seq INTEGER NOT NULL, type TEXT NOT NULL, data TEXT NOT NULL,
    at {real} NOT NULL, PRIMARY KEY (run_id, seq));
CREATE TABLE IF NOT EXISTS blobs (
    id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, session_id TEXT NOT NULL, media_type TEXT NOT NULL,
    data TEXT NOT NULL, created_at {real} NOT NULL);
CREATE INDEX IF NOT EXISTS blobs_session ON blobs (session_id);
"""

_V2 = """
ALTER TABLE profiles ADD COLUMN memory INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN summary TEXT;
ALTER TABLE sessions ADD COLUMN summary_head_id {bigint};
ALTER TABLE sessions ADD COLUMN summary_tail_id {bigint};
ALTER TABLE sessions ADD COLUMN context_tokens INTEGER;
ALTER TABLE sessions ADD COLUMN context_message_id {bigint};
ALTER TABLE sessions ADD COLUMN compaction_failed_at {real};
ALTER TABLE sessions ADD COLUMN turns_since_memory INTEGER NOT NULL DEFAULT 0;
CREATE TABLE memories (
    profile_id TEXT NOT NULL, target TEXT NOT NULL, entries TEXT NOT NULL, updated_at {real} NOT NULL,
    PRIMARY KEY (profile_id, target));
CREATE INDEX runs_finished ON runs (finished_at);
"""

_V3 = """
ALTER TABLE sessions ADD COLUMN pruned_id {bigint};
"""

_V4 = """
CREATE TABLE IF NOT EXISTS tenants (id INTEGER PRIMARY KEY CHECK (id BETWEEN -2147483648 AND 2147483647));
ALTER TABLE profiles ADD COLUMN tenant_id INTEGER;
CREATE INDEX IF NOT EXISTS profiles_tenant ON profiles (tenant_id);
"""

# Coordination identities are independent of transcript rows: run/profile/session columns are logical links, not
# foreign keys, so evidence survives deletion of a terminal run or session. Authority and external outcome are
# separate columns: retired + uncertain is an intentional, unresolved combination. There is no FK cycle between
# sandboxes.current_operation_id and sandbox_operations; guarded transitions validate the pointer instead.
_V5 = """
CREATE TABLE sandboxes (
    deployment TEXT NOT NULL, profile_id TEXT NOT NULL, daemon_id TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0), generation_token TEXT,
    container_id TEXT, container_name TEXT, policy_digest TEXT,
    helper_protocol INTEGER, helper_digest TEXT,
    state TEXT NOT NULL DEFAULT 'absent'
        CHECK (state IN ('absent', 'creating', 'ready', 'draining', 'removing', 'blocked')),
    current_operation_id TEXT, idle_since {real}, transitioned_at {real} NOT NULL,
    error_code TEXT, error_message TEXT,
    PRIMARY KEY (deployment, profile_id));
CREATE UNIQUE INDEX sandboxes_generation_token ON sandboxes (generation_token)
    WHERE generation_token IS NOT NULL;
CREATE UNIQUE INDEX sandboxes_container ON sandboxes (daemon_id, container_id)
    WHERE container_id IS NOT NULL;
CREATE INDEX sandboxes_idle ON sandboxes (state, idle_since);
CREATE TABLE sandbox_uses (
    id TEXT PRIMARY KEY, token TEXT NOT NULL,
    deployment TEXT NOT NULL, profile_id TEXT NOT NULL, daemon_id TEXT NOT NULL,
    run_id TEXT NOT NULL, session_id TEXT, owner TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('preparing', 'active', 'cleaning', 'orphaned', 'released')),
    generation INTEGER, generation_token TEXT, container_id TEXT, container_name TEXT,
    create_operation_id TEXT, heartbeat_at {real} NOT NULL, lease_expires_at {real} NOT NULL,
    released_at {real}, release_reason TEXT,
    CHECK (state <> 'released' OR released_at IS NOT NULL),
    CHECK (state = 'released' OR released_at IS NULL));
CREATE UNIQUE INDEX sandbox_uses_token ON sandbox_uses (token);
CREATE UNIQUE INDEX sandbox_uses_run ON sandbox_uses (run_id);
CREATE INDEX sandbox_uses_profile ON sandbox_uses (deployment, profile_id, state);
CREATE INDEX sandbox_uses_lease ON sandbox_uses (lease_expires_at);
CREATE INDEX sandbox_uses_create ON sandbox_uses (create_operation_id);
CREATE TABLE sandbox_execs (
    id TEXT PRIMARY KEY, control_token TEXT NOT NULL, usage_id TEXT NOT NULL, usage_token TEXT NOT NULL,
    deployment TEXT NOT NULL, profile_id TEXT NOT NULL, daemon_id TEXT NOT NULL,
    run_id TEXT, session_id TEXT, generation INTEGER NOT NULL, generation_token TEXT NOT NULL,
    container_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'running', 'finished', 'uncertain', 'retired')),
    protocol INTEGER, dispatched_at {real}, started_at {real}, finished_at {real},
    cleanup_outcome TEXT, uncertainty_reason TEXT, diagnostic TEXT, created_at {real} NOT NULL);
CREATE UNIQUE INDEX sandbox_execs_control_token ON sandbox_execs (control_token);
CREATE INDEX sandbox_execs_usage ON sandbox_execs (usage_id, state);
CREATE INDEX sandbox_execs_state ON sandbox_execs (state, created_at);
CREATE TABLE sandbox_operations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('create', 'install', 'start', 'remove')),
    deployment TEXT NOT NULL, profile_id TEXT NOT NULL, daemon_id TEXT NOT NULL,
    run_id TEXT, session_id TEXT, generation INTEGER NOT NULL CHECK (generation >= 0),
    generation_token TEXT NOT NULL, intended_name TEXT, original_create_operation_id TEXT,
    usage_id TEXT, usage_token TEXT, container_id TEXT, owner TEXT NOT NULL, owner_token TEXT NOT NULL,
    lease_expires_at {real},
    authority TEXT NOT NULL DEFAULT 'current' CHECK (authority IN ('current', 'retired')),
    retired_at {real}, retirement_reason TEXT,
    outcome TEXT NOT NULL DEFAULT 'planned'
        CHECK (outcome IN ('planned', 'dispatched', 'succeeded', 'definite_failure', 'uncertain')),
    result_code TEXT, result_message TEXT, discovered_container_id TEXT, cleanup_operation_id TEXT,
    confirmed_absent_at {real}, resolved_at {real}, created_at {real} NOT NULL, updated_at {real} NOT NULL,
    CHECK (kind <> 'create' OR intended_name IS NOT NULL),
    CHECK (authority <> 'retired' OR retired_at IS NOT NULL));
CREATE UNIQUE INDEX sandbox_operations_owner_token ON sandbox_operations (owner_token);
CREATE UNIQUE INDEX sandbox_operations_create_name ON sandbox_operations (daemon_id, intended_name)
    WHERE kind = 'create' AND intended_name IS NOT NULL;
CREATE UNIQUE INDEX sandbox_operations_remove_target ON sandbox_operations (daemon_id, container_id)
    WHERE kind = 'remove' AND authority = 'current' AND container_id IS NOT NULL;
CREATE INDEX sandbox_operations_profile ON sandbox_operations (deployment, profile_id, authority, outcome);
CREATE INDEX sandbox_operations_lease ON sandbox_operations (lease_expires_at);
CREATE INDEX sandbox_operations_retired ON sandbox_operations (authority, outcome, retired_at);
CREATE INDEX sandbox_operations_create ON sandbox_operations (original_create_operation_id);
CREATE INDEX sandbox_operations_target ON sandbox_operations (daemon_id, container_id);
"""

_V6 = """
CREATE TABLE IF NOT EXISTS profile_deletions (
    profile_id TEXT PRIMARY KEY,
    tenant_id INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    phase TEXT NOT NULL CHECK (phase IN ('blocked', 'draining', 'purged')),
    created_at {real} NOT NULL,
    updated_at {real} NOT NULL);
CREATE INDEX IF NOT EXISTS profile_deletions_operation ON profile_deletions (operation_id);
CREATE INDEX IF NOT EXISTS profile_deletions_tenant ON profile_deletions (tenant_id);
CREATE TABLE IF NOT EXISTS profile_deletion_receipts (
    profile_id TEXT NOT NULL,
    tenant_id INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    completed_at {real} NOT NULL,
    PRIMARY KEY (profile_id, operation_id));
CREATE INDEX IF NOT EXISTS profile_deletion_receipts_operation
    ON profile_deletion_receipts (operation_id);
"""

_V7 = """
ALTER TABLE profiles ADD COLUMN incarnation TEXT;
ALTER TABLE runs ADD COLUMN profile_incarnation TEXT;
"""

MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, _V1), Migration(2, _V2), Migration(3, _V3), Migration(4, _V4), Migration(5, _V5),
    Migration(6, _V6), Migration(7, _V7))
LATEST_VERSION = MIGRATIONS[-1].version


async def _table_exists(db, name: str) -> bool:
    row = await db.fetchrow("SELECT 1 AS ok FROM information_schema.tables "
                            "WHERE table_schema = current_schema() AND table_name = ?", name)
    return row is not None


async def _stamped_version(db) -> int | None:
    if not await _table_exists(db, "schema_version"):
        return None
    row = await db.fetchrow("SELECT version FROM schema_version")
    return int(row["version"]) if row else None


async def schema_version(db) -> int:
    """The database's version: 0 when empty, 1 for a 0.2.0 database (keel's tables and no version recorded)."""
    stamped = await _stamped_version(db)
    if stamped is not None:
        return stamped
    return 1 if await _table_exists(db, "profiles") else 0


async def _set_version(db, version: int) -> None:
    await db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    await db.execute("DELETE FROM schema_version")
    await db.execute("INSERT INTO schema_version (version) VALUES (?)", version)


async def migrate(db) -> int:
    """Bring the schema to LATEST_VERSION, one transaction per migration, each re-reading the version under the lock;
    each version and its stamp commit atomically."""
    for migration in MIGRATIONS:
        async with db.transaction():
            await db.execute(_PG_LOCK)
            current = await schema_version(db)
            if current > LATEST_VERSION:
                raise SchemaTooNew(f"The database schema is version {current}; this keel knows versions up to "
                                   f"{LATEST_VERSION}. A downgrade is not supported.")
            if current >= migration.version:
                if await _stamped_version(db) is None:
                    # A 0.2.0 database, maybe partly created: 0.2.0 made its tables outside a transaction.
                    for statement in migration.statements():
                        await db.execute(statement)
                    await _set_version(db, current)
                continue
            for statement in migration.statements():
                await db.execute(statement)
            await _set_version(db, migration.version)
    return LATEST_VERSION
