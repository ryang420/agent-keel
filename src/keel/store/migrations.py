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

# Memory entries (cross-session memory design §2): each JSON list item becomes a stable row in its old order,
# duplicates included, with legacy provenance and no invented source. The old table goes in the same transaction.
_V8 = """
CREATE TABLE memory_entries (
    id TEXT PRIMARY KEY, profile_id TEXT NOT NULL,
    target TEXT NOT NULL CHECK (target IN ('memory', 'user')),
    position INTEGER NOT NULL, text TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'removed')),
    created_at {real} NOT NULL, updated_at {real} NOT NULL);
CREATE INDEX memory_entries_live ON memory_entries (profile_id, target, state, position);
CREATE TABLE memory_revisions (
    profile_id TEXT NOT NULL, target TEXT NOT NULL, revision {bigint} NOT NULL,
    PRIMARY KEY (profile_id, target));
CREATE TABLE memory_changes (
    id {serial} PRIMARY KEY, profile_id TEXT NOT NULL, target TEXT NOT NULL, entry_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('add', 'replace', 'remove')), old_text TEXT, new_text TEXT,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('foreground', 'review', 'consumer', 'legacy')),
    run_id TEXT, session_id TEXT, message_id {bigint}, tool_call_id TEXT, mutation_key TEXT,
    revision {bigint} NOT NULL, created_at {real} NOT NULL);
CREATE INDEX memory_changes_entry ON memory_changes (profile_id, entry_id, id);
CREATE INDEX memory_changes_session ON memory_changes (profile_id, session_id);
CREATE TABLE memory_mutations (
    profile_id TEXT NOT NULL, mutation_key TEXT NOT NULL, session_id TEXT, result TEXT NOT NULL,
    created_at {real} NOT NULL,
    PRIMARY KEY (profile_id, mutation_key));
INSERT INTO memory_entries (id, profile_id, target, position, text, version, state, created_at, updated_at)
    SELECT md5(m.profile_id || '/' || m.target || '/' || e.ord::text), m.profile_id, m.target, e.ord::integer - 1,
           e.value, 1, 'active', m.updated_at, m.updated_at
    FROM memories m CROSS JOIN LATERAL jsonb_array_elements_text(m.entries::jsonb) WITH ORDINALITY AS e(value, ord);
INSERT INTO memory_revisions (profile_id, target, revision)
    SELECT DISTINCT profile_id, target, 1 FROM memory_entries;
INSERT INTO memory_changes (profile_id, target, entry_id, action, new_text, source_kind, revision, created_at)
    SELECT profile_id, target, id, 'add', text, 'legacy', 1, created_at FROM memory_entries ORDER BY position;
DROP TABLE memories
"""

# History search (cross-session memory design §3): a redacted text row per user/assistant message and unique
# postings per profile. Existing history is indexed later by a resumable backfill up to each profile's ceiling;
# newer messages are indexed as they are written. Sessions record the tools their prompt was built with.
_V9 = """
ALTER TABLE sessions ADD COLUMN tools TEXT;
CREATE TABLE message_search (
    message_id {bigint} PRIMARY KEY, profile_id TEXT NOT NULL, session_id TEXT NOT NULL, role TEXT NOT NULL,
    text TEXT NOT NULL, created_at {real} NOT NULL);
CREATE INDEX message_search_session ON message_search (profile_id, session_id, message_id);
CREATE INDEX message_search_time ON message_search (profile_id, created_at);
CREATE TABLE message_tokens (
    profile_id TEXT NOT NULL, token TEXT NOT NULL, message_id {bigint} NOT NULL,
    PRIMARY KEY (profile_id, token, message_id));
CREATE INDEX message_tokens_message ON message_tokens (message_id);
CREATE TABLE history_backfill (
    profile_id TEXT PRIMARY KEY, done_through {bigint} NOT NULL, ceiling {bigint} NOT NULL,
    updated_at {real} NOT NULL);
INSERT INTO history_backfill (profile_id, done_through, ceiling, updated_at)
    SELECT s.profile_id, 0, MAX(m.id), EXTRACT(EPOCH FROM now())
    FROM messages m JOIN sessions s ON s.id = m.session_id GROUP BY s.profile_id
"""

# Durable memory review (cross-session memory design §4): per-session review state written with a run's completion,
# jobs that claim an immutable source range, and a generation per session so a deleted and recreated session id
# never inherits old work. The old per-session turn counter is retired; existing history is not queued.
_V10 = """
ALTER TABLE sessions ADD COLUMN generation TEXT NOT NULL DEFAULT md5(random()::text || clock_timestamp()::text);
ALTER TABLE sessions DROP COLUMN turns_since_memory;
CREATE TABLE memory_reviews (
    profile_id TEXT NOT NULL, session_id TEXT NOT NULL, generation TEXT NOT NULL, profile_incarnation TEXT NOT NULL,
    reviewed_through {bigint} NOT NULL, queued_through {bigint} NOT NULL, dirty_through {bigint} NOT NULL,
    pending_turns INTEGER NOT NULL, first_dirty_at {real}, last_completed_at {real} NOT NULL,
    provider TEXT NOT NULL, model TEXT NOT NULL,
    PRIMARY KEY (profile_id, session_id));
CREATE TABLE memory_review_jobs (
    id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, session_id TEXT NOT NULL, generation TEXT NOT NULL,
    profile_incarnation TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'failed', 'discarded')),
    provider TEXT NOT NULL, model TEXT NOT NULL, source_from {bigint} NOT NULL, source_through {bigint} NOT NULL,
    cursor_message {bigint} NOT NULL, cursor_offset INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at {real} NOT NULL, lease_owner TEXT, lease_token TEXT, lease_expires_at {real},
    error_code TEXT, error TEXT, usage TEXT NOT NULL DEFAULT '[]',
    created_at {real} NOT NULL, updated_at {real} NOT NULL, finished_at {real});
CREATE UNIQUE INDEX memory_review_jobs_open ON memory_review_jobs (profile_id, session_id)
    WHERE status IN ('pending', 'running', 'failed');
CREATE UNIQUE INDEX memory_review_jobs_running ON memory_review_jobs (profile_id) WHERE status = 'running';
CREATE INDEX memory_review_jobs_due ON memory_review_jobs (status, next_attempt_at);
CREATE INDEX memory_review_jobs_profile ON memory_review_jobs (profile_id, created_at)
"""

# Procedural learning (learning design §2, §3): the profile switch, profile-private learned skills with an immutable
# version per change, the evidence each version cites, lesson observations (written by skill_manage now, by review
# and feedback later) and the learned skills each run loaded.
_V11 = """
ALTER TABLE profiles ADD COLUMN learning INTEGER NOT NULL DEFAULT 0;
CREATE TABLE learned_skills (
    id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL, category TEXT,
    body TEXT NOT NULL, version INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('active', 'suspended', 'retired')),
    origin TEXT NOT NULL CHECK (origin IN ('foreground', 'dream', 'consumer')),
    consumer_changed_at {real}, created_at {real} NOT NULL, updated_at {real} NOT NULL);
CREATE UNIQUE INDEX learned_skills_name ON learned_skills (profile_id, name) WHERE state <> 'retired';
CREATE INDEX learned_skills_profile ON learned_skills (profile_id, state);
CREATE TABLE learned_skill_versions (
    profile_id TEXT NOT NULL, skill_id TEXT NOT NULL, version INTEGER NOT NULL, name TEXT NOT NULL,
    description TEXT NOT NULL, category TEXT, body TEXT NOT NULL,
    change TEXT NOT NULL CHECK (change IN ('create', 'revise', 'merge', 'retire', 'rollback', 'suspend', 'resume')),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('foreground', 'dream', 'consumer', 'automatic')),
    run_id TEXT, session_id TEXT, tool_call_id TEXT, job_id TEXT, rationale TEXT, created_at {real} NOT NULL,
    PRIMARY KEY (profile_id, skill_id, version));
CREATE INDEX learned_skill_versions_session ON learned_skill_versions (profile_id, session_id);
CREATE TABLE learned_skill_evidence (
    profile_id TEXT NOT NULL, skill_id TEXT NOT NULL, version INTEGER NOT NULL, session_id TEXT NOT NULL,
    message_id {bigint}, observation_id TEXT);
CREATE INDEX learned_skill_evidence_version ON learned_skill_evidence (profile_id, skill_id, version);
CREATE INDEX learned_skill_evidence_session ON learned_skill_evidence (profile_id, session_id);
CREATE TABLE lesson_observations (
    id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, session_id TEXT NOT NULL, generation TEXT NOT NULL,
    topic TEXT NOT NULL, situation TEXT NOT NULL, approach TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('worked', 'failed', 'corrected', 'requested', 'feedback_up',
                                             'feedback_down')),
    evidence TEXT NOT NULL DEFAULT '[]', loaded TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL CHECK (state IN ('pending', 'consumed', 'dismissed', 'expired')),
    consumed_by TEXT, created_at {real} NOT NULL);
CREATE INDEX lesson_observations_profile ON lesson_observations (profile_id, state, created_at);
CREATE INDEX lesson_observations_session ON lesson_observations (profile_id, session_id);
CREATE TABLE skill_loads (
    profile_id TEXT NOT NULL, run_id TEXT NOT NULL, session_id TEXT NOT NULL, skill_id TEXT NOT NULL,
    version INTEGER NOT NULL, loaded_at {real} NOT NULL,
    PRIMARY KEY (profile_id, run_id, skill_id));
CREATE INDEX skill_loads_skill ON skill_loads (profile_id, skill_id, loaded_at);
CREATE INDEX skill_loads_session ON skill_loads (profile_id, session_id)
"""

# Procedural learning, observations and feedback (learning design §4, §6): whether a run ended at the iteration cap,
# which review job or run produced an observation, and one feedback row per run.
_V12 = """
ALTER TABLE runs ADD COLUMN capped INTEGER NOT NULL DEFAULT 0;
ALTER TABLE lesson_observations ADD COLUMN review_job_id TEXT;
ALTER TABLE lesson_observations ADD COLUMN run_id TEXT;
CREATE INDEX lesson_observations_job ON lesson_observations (profile_id, review_job_id);
CREATE TABLE run_feedback (
    profile_id TEXT NOT NULL, run_id TEXT NOT NULL, session_id TEXT NOT NULL,
    rating TEXT NOT NULL CHECK (rating IN ('up', 'down')), comment TEXT, observation_id TEXT,
    created_at {real} NOT NULL, updated_at {real} NOT NULL,
    PRIMARY KEY (profile_id, run_id));
CREATE INDEX run_feedback_session ON run_feedback (profile_id, session_id)
"""

# Procedural learning, the dream (learning design §5, §7): profile-level dream jobs run on the memory review worker,
# the start of every dream attempt (a few per profile per day) and the reason a dream dismissed an observation.
_V13 = """
CREATE TABLE dream_jobs (
    id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, profile_incarnation TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'failed', 'discarded')),
    trigger TEXT NOT NULL, provider TEXT, model TEXT, attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at {real} NOT NULL, lease_owner TEXT, lease_token TEXT, lease_expires_at {real}, input_at {real},
    error_code TEXT, error TEXT, usage TEXT NOT NULL DEFAULT '[]', changes TEXT NOT NULL DEFAULT '{{}}',
    created_at {real} NOT NULL, updated_at {real} NOT NULL, finished_at {real});
CREATE UNIQUE INDEX dream_jobs_open ON dream_jobs (profile_id) WHERE status IN ('pending', 'running');
CREATE INDEX dream_jobs_due ON dream_jobs (status, next_attempt_at);
CREATE INDEX dream_jobs_profile ON dream_jobs (profile_id, created_at);
CREATE TABLE dream_attempts (profile_id TEXT NOT NULL, job_id TEXT NOT NULL, started_at {real} NOT NULL);
CREATE INDEX dream_attempts_profile ON dream_attempts (profile_id, started_at);
CREATE INDEX lesson_observations_pending ON lesson_observations (profile_id, created_at) WHERE state = 'pending';
ALTER TABLE lesson_observations ADD COLUMN note TEXT
"""

MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, _V1), Migration(2, _V2), Migration(3, _V3), Migration(4, _V4), Migration(5, _V5),
    Migration(6, _V6), Migration(7, _V7), Migration(8, _V8), Migration(9, _V9),
    Migration(10, _V10), Migration(11, _V11), Migration(12, _V12), Migration(13, _V13))
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
