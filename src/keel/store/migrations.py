"""Numbered schema migrations (M3 design §1). Version 1 is the 0.2.0 schema exactly as released. A released
migration is never edited; a schema change is a new migration."""

from __future__ import annotations

from dataclasses import dataclass

# Taken first in every migration's transaction, so instances starting together migrate once.
# The key must equal 0.2.0's, so that a 0.2.0 instance and this one starting together also serialise.
_PG_LOCK = "SELECT pg_advisory_xact_lock(7312100001)"
_TYPES = {
    "sqlite": {"real": "REAL", "serial": "INTEGER", "bigint": "INTEGER"},
    "postgres": {"real": "DOUBLE PRECISION", "serial": "BIGSERIAL", "bigint": "BIGINT"},
}


class SchemaTooNew(Exception):
    """The database was migrated by a newer keel; a downgrade is not supported."""


@dataclass(frozen=True)
class Migration:
    version: int
    template: str  # statements separated by ";", with {real}, {serial} and {bigint} filled per dialect

    def statements(self, dialect: str) -> list[str]:
        sql = self.template
        if dialect == "sqlite":
            # SQLite only auto-increments an INTEGER PRIMARY KEY when it is declared exactly that way.
            # Tenant ids are assigned by the operator, so only {serial} columns get AUTOINCREMENT.
            sql = sql.replace("id {serial} PRIMARY KEY", "id INTEGER PRIMARY KEY AUTOINCREMENT")
        sql = sql.format(**_TYPES[dialect])
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

MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, _V1), Migration(2, _V2), Migration(3, _V3), Migration(4, _V4))
LATEST_VERSION = MIGRATIONS[-1].version


async def _table_exists(db, name: str) -> bool:
    if db.dialect == "sqlite":
        row = await db.fetchrow("SELECT 1 AS ok FROM sqlite_master WHERE type = 'table' AND name = ?", name)
    else:
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
    statements run one at a time because SQLite's executescript would commit the transaction."""
    for migration in MIGRATIONS:
        async with db.transaction():
            if db.dialect == "postgres":
                await db.execute(_PG_LOCK)
            current = await schema_version(db)
            if current > LATEST_VERSION:
                raise SchemaTooNew(f"The database schema is version {current}; this keel knows versions up to "
                                   f"{LATEST_VERSION}. A downgrade is not supported.")
            if current >= migration.version:
                if await _stamped_version(db) is None:
                    # A 0.2.0 database, maybe partly created: 0.2.0 made its tables outside a transaction.
                    for statement in migration.statements(db.dialect):
                        await db.execute(statement)
                    await _set_version(db, current)
                continue
            for statement in migration.statements(db.dialect):
                await db.execute(statement)
            await _set_version(db, migration.version)
    return LATEST_VERSION
