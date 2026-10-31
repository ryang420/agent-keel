# Operations

Commands an operator runs against a keel database directly, beside the service. They read `KEEL_DATABASE_URL` like
the service does; in a container, run them in the keel container (`docker exec -i keel python -m keel.admin …`).

## Importing conversations

`python -m keel.admin import-sessions [--dry-run]` imports conversations copied from another system. It reads one
conversation per line of JSON on stdin and writes one result per line to stdout.

Input line:

```json
{"profile": "p1", "tenant_id": 7, "session_id": "s-0001", "title": "Row count", "title_source": "llm",
 "created_at": 1789000000.0, "last_active_at": 1789000300.0,
 "messages": [
   {"source_id": 11, "role": "user", "text": "How many rows?", "created_at": 1789000000.0},
   {"source_id": 12, "role": "assistant", "text": "", "created_at": 1789000001.0,
    "tool_calls": [{"id": "c1", "name": "terminal", "arguments": "{\"command\": \"wc -l f\"}"}]},
   {"source_id": 13, "role": "tool", "text": "42 f", "tool_call_id": "c1", "tool_name": "terminal",
    "created_at": 1789000002.0},
   {"source_id": 14, "role": "assistant", "text": "42 rows.", "created_at": 1789000003.0}
 ]}
```

- `profile` and `session_id` follow the API's id rules. `tenant_id` is the tenant that owns the profile.
- `title_source` is `derived`, `llm`, `user` or null.
- Each message's `role` is `user`, `assistant` or `tool`, and a `tool` message needs the `tool_call_id` of an earlier
  call.
- `source_id` is any value the caller uses to match messages to the ids keel assigns.
- Reasoning is never imported: an imported message has none, so no provider is replayed another system's reasoning.

Result line:

```json
{"session_id": "s-0001", "status": "imported", "profile_created": true, "title": "Row count", "reason": null,
 "message_ids": {"11": 501, "12": 502, "13": 503, "14": 504}}
```

- `imported`: the session and its messages were written in one transaction. The title, times and message order are
  kept, and a title the profile already uses becomes `Title (2)`, `Title (3)`, ….
- `exists`: a session with this id exists already, under any profile. Nothing is changed, so a re-run is safe.
- `invalid`: the line breaks one of the rules above, or the profile belongs to another tenant. `reason` says which, and
  nothing of that conversation is written.
- `valid`: the `--dry-run` result for a line that would be imported.

A missing profile is created empty for `tenant_id`: no models, no skills, an empty sandbox and soul. The next profile
update through the API (`PUT /v1/profiles/{id}`) fills it in before its first run. The command exits 1 when any line is
`invalid`.

## Upgrading to atomic run termination

This release commits a run's terminal state and its terminal event in one transaction. It needs no schema migration,
but the guarantee holds only while every process writing the database runs this version. An old writer can still
leave a terminal run without its event; an old event writer can still append after a new-style terminal event.

To cut over, stop admitting new runs, let in-flight runs drain, and stop the old instances before starting the new
version. A run left in flight when its worker stops is failed as `worker_lost` by the new version's lease sweep. Total
downtime can be as short as the drain window. Rolling back is safe — the new version writes ordinary rows — but the
atomicity guarantee ends while old writers run.

## Upgrading to the coordinated sandbox lifecycle

This version replaces the name-based sandbox lifecycle (recreate-on-use, PID-file cancellation, startup orphan
removal) with coordinated exact-ID generations. Migration 5 adds the `sandboxes`, `sandbox_uses`, `sandbox_execs` and
`sandbox_operations` tables; it runs automatically at start and is not reversible in place.

Requirements:

- Every instance of one deployment uses the same PostgreSQL database, the same Docker daemon and the same
  `KEEL_DEPLOYMENT` label.
- The installed package carries the sandbox supervisor for amd64 and arm64 (the wheel, the source distribution and
  the container image do). Building from a source tree needs `scripts/build_supervisor.py --arch all` once; it is
  never downloaded at run time.
- All instances run this version. An older instance and this one must not coordinate the same deployment: the older
  one creates and removes containers by profile-derived name, outside the ledger.

Procedure:

1. Back up the database.
2. Stop admitting runs, let in-flight runs finish (or let their leases lapse), and stop every old instance. Confirm
   no old process can still create or remove sandbox containers.
3. Start this version and wait for the log line that sandbox admission is open. Startup classifies every container
   of the deployment in bounded batches, spending up to 30 seconds before workers start and retrying every 15
   seconds; sandbox runs stay queued meanwhile, and chat-only runs proceed.
4. Inspect the sandbox containers by exact ID:
   `docker ps -a --no-trunc --filter label=keel.managed=1 --filter label=keel.deployment=<deployment>`.
   A container without a `keel.generation` label (or whose labels disagree with the ledger) is a legacy leftover.
   It is kept as evidence and blocks new sandbox runs for its profile (`sandbox_inventory_conflict`); it is never
   removed automatically. After step 2 is complete, remove the ones you intend to retire with
   `docker rm -f <exact-id>`. The next reconciliation pass proves that exact ID absent and clears the block; the
   removed container's writable layer is lost, so copy anything needed out first. A legacy container you keep is
   not adopted.
5. Policies still describe the same bind sources; the profile-specific mount rules are unchanged. A generation
   carrying an incompatible policy or helper is replaced by recovery once its users finish.
6. No manual row maintenance is needed. Resolved coordination records older than `KEEL_EVENT_RETENTION_DAYS` are
   pruned in bounded batches; unresolved creates, unreleased usage, unresolved execs and current references are
   retained. Do not delete coordination rows by hand: they are the evidence recovery relies on.
7. To roll back, stop this version and restore the database backup taken in step 1, then start the older version.
   A newer schema is refused by an older keel, and the older name-based lifecycle must not run beside this one.

Verification after cutover:

- `schema_version` is at least 5, and every profile has an explicit tenant owner (see the tenant cutover above).
- The service log reports sandbox admission open and, in each maintenance pass, removed/released/blocked counts and
  any bounded diagnostics; unresolved create tombstones are reported with their oldest age.
- A sandbox run starts, runs a tool and stops; changing its policy while it has work in progress returns 409
  `sandbox_busy`.
- The daemon ID reported by `docker info` matches the one recorded in the sandbox and operation rows;
  changing it closes admission until the recorded daemon is restored and the service restarted.

## History search index

Migration 9 creates the index behind `session_search`. Every instance indexes new messages as they are written and
backfills older ones in the background, 200 messages per batch, so no operator step is needed. Until the backfill
finishes for a profile, `session_search` reports `index_complete: false` for it. Watch progress with:

```sql
SELECT profile_id, done_through, ceiling FROM history_backfill WHERE done_through < ceiling;
```

Every instance must run this release: an older instance writes messages the index never sees.

## Memory reviews

Memory-enabled profiles review new conversation text in the background (cross-session memory design §4). The work is
in PostgreSQL: `memory_reviews` keeps each session's reviewed and unreviewed range, and `memory_review_jobs` holds the
jobs. Each instance runs `KEEL_MEMORY_REVIEW_WORKERS` review loops (default 1) beside its run workers. A job holds a
lease on database time, renewed every third of `KEEL_LEASE_SECONDS`; a worker that stops renewing loses the job to
another instance and cannot write afterwards. At most one review runs per profile across all instances.

A job tries at most three times; failed attempts back off 30 s, then 60 s, while a job whose lease expired is
retried at once with no backoff. A model that is no longer enabled for the profile
(`model_unavailable`) and provider errors that are not transient fail at once. A review whose input would exceed its
budget ends the attempt with `budget` and is retried, each retry of the same chunk reviewing half as much new text
(down to 250 tokens). Inspect jobs with `GET /v1/profiles/{p}/memory/reviews` and requeue a failed one with
`POST …/reviews/{id}/retry`. A failed job holds back its session's later reviews until it is retried; other sessions
are unaffected. Each attempt's token usage is on the job.

Deleting a session removes its review state and jobs; deleting a profile removes all of them and fences any review
still running. Turning memory off and on again discards the profile's queued, failed and running reviews (a running
one cannot write afterwards) and starts again from the next completed run. Migration 10 queues nothing for existing
conversations. It rewrites the `sessions` table (each row gets its own generation) under an exclusive lock, so
expect it to take time on large tables. Like migration 9, it needs every instance on this version before runs
start again (no mixed-version rollout): an older binary still counts turns in a column that no longer exists.

## Learned skills

Profiles with `learning` on keep learned skills in PostgreSQL (migration 11: `learned_skills`, their immutable
`learned_skill_versions` and `learned_skill_evidence`, `lesson_observations` and `skill_loads`). Migration 11
adds a column and tables only, and needs no backfill. Inspect a profile's skills with `GET /v1/profiles/{p}/skills/learned` (state, version, origin and 30-day
load counts) and one skill's full history with `GET …/skills/learned/{id}`: each version has its source (the run,
session and message, or `consumer`), evidence and rationale. Act on a bad skill through the same API:

- `POST …/{id}/suspend` takes it out of the prompt index, `skills_list` and `skill_view` at once for new sessions
  and for `skill_view` in open ones, keeping it for `…/resume`.
- `POST …/{id}/rollback` with a `version` writes a new version equal to the old one; nothing is rewritten.
- `DELETE …/{id}?expected_version=n` retires it, which frees its name and its place in the limit of 50. Versions
  stay.

Each call needs the skill's current `expected_version`; a 409 `version_conflict` means it changed, so re-read it.
Sessions already open keep the skills index in their stored prompt until they compact.

Turning `learning` off removes `skill_manage` and hides every learned skill from `skills_list` and `skill_view`
(for new sessions, and for open ones at their next call); nothing is deleted, and turning it on again restores the
active skills. The routes keep working while learning is off. Deleting a profile deletes its skills, versions and
load counts. Deleting a session removes that session's skill loads, observations and evidence rows, and its version
rows lose their run and session provenance; the skills and their text stay.

Before enabling `learning` on any profile (and before deleting a profile that has learned skills), upgrade every
serving and worker instance to this version, as for migrations 9 and 10. An instance still on migration 10 deletes
sessions and profiles without touching the learned tables.

## Complete profile deletion

A consumer may ask the service to remove one profile completely through the delete/complete contract in
`docs/guide/api.md`. Operate it as follows:

1. Upgrade every serving and worker keel instance before enabling profile deletion. Migration 7 adds durable
   profile-incarnation authority to profiles and runs; an older instance cannot attach that authority to its
   background callbacks. Confirm `schema_version` is at least 7 before enabling the consumer flow.
2. Deploy this release before the consumer release that calls it. A consumer that requires profile deletion must
   not report success against an older runtime that lacks the endpoints; it keeps its own operation pending and
   reports that a compatible runtime is required. The minimum compatible release is the first release whose
   changelog lists complete profile deletion.
3. Call `DELETE /v1/profiles/{profile_id}` with a new UUID operation id. On `409` (work still draining, or another
   operation owns the profile) or `503` (store or sandbox dependency unavailable), retry the same operation id; do
   not start a second operation. A retry resumes persisted progress and never purges twice.
4. After the consumer has removed its own data, call
   `POST /v1/profiles/{profile_id}/deletion/{operation_id}/complete`. Retry with the same id after a lost response;
   the receipt makes the retry harmless.
5. If teardown cannot be proven (a container removal is uncertain, another Docker daemon owns the recorded
   sandbox, data is being written by an unresolved generation), deletion stays incomplete and the fence remains.
   Resolve the sandbox problem with the recovery procedure above, then retry the same operation.
6. Do not delete rows from `profile_deletions`, `profile_deletion_receipts` or the sandbox ledger by hand: they
   are the evidence a retry relies on. A receipt may be retained after completion; it contains no conversation,
   file, memory or user email.
7. PostgreSQL databases are upgraded in place by numbered migrations. A newer schema is refused by an older
   keel, so roll back by restoring the database backup, as with any migration.
