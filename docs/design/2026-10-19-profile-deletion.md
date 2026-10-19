# Complete profile deletion

Date: 2026-10-19
Status: implemented

## Purpose

A consumer of this service may need to remove one profile completely: its
sessions, messages, runs, events, blobs, memory, sandbox and model/persona
configuration. The platform account remains, and a later profile update may
create an empty profile again. Deletion is a resumable, tenant-owned operation
that outlives a process restart.

This is not account deletion: credentials, tenants and deployment configuration
are outside a profile.

## Contract

`DELETE /v1/profiles/{profile_id}` with `{"operation_id": "<uuid>"}`.

- `200 {"deleted": true, "blocked": true}` only after the runtime data is gone
  and no container remains. The fence stays in place.
- `404 profile_not_found` for an unknown profile or one owned by another tenant.
  An absent profile still creates a fence, then succeeds once no runtime
  resources exist.
- `409 profile_deletion_conflict` when another operation already owns the
  profile, or `409 profile_deletion_busy` while work is still draining.
- `503 unavailable` when the store or a cleanup dependency cannot answer.

The same `operation_id` may be retried; it resumes the persisted progress. A
different operation for a profile with an active fence is refused.

`POST /v1/profiles/{profile_id}/deletion/{operation_id}/complete` releases that
exact operation's fence and returns `{"completed": true}`. It is idempotent for
a retried operation and never releases a newer fence or touches a newer
profile incarnation. A minimal receipt (profile, tenant, operation, time)
survives the release so a lost-response retry can be acknowledged without
storing conversations, files, memory or an email address.

## Durable state

A migration adds two tables:

- `profile_deletions` - one row per profile: tenant, operation, phase and
  timestamps. Phases are `blocked`, `draining` and `purged`.
- `profile_deletion_receipts` - completion receipts independent of profile
  rows.

The fence is claimed in the same transaction that reads tenant ownership. Every
profile-owned write checks it inside its own transaction: profile updates,
session creation, run creation, memory writes and worker claims. On PostgreSQL
the claim, the purge, completion and every profile-owned writer also take the
same transaction-scoped advisory lock for the profile, so a writer admitted
before the fence either commits before the purge or observes the fence and
fails. SQLite's `BEGIN IMMEDIATE` provides the equivalent serialization.

## Lifecycle

1. **blocked** - the fence and its tenant are durable.
2. **draining** - queued and active runs are cancelled through the existing run
   state machine (one terminal transition and one terminal event per run);
   workers drain; the profile's background reviews and titles are cancelled and
   awaited, and new ones are refused; the profile's sandboxes are retired
   through the existing lifecycle authority using verified exact container
   identities.
3. **purged** - sessions, messages, runs, events, blobs, memory, the profile row
   and resolved, unreferenced sandbox lifecycle rows are removed in the same
   store transaction that writes this phase. Uncertain lifecycle evidence is
   retained until it is settled.

A request's total wait is bounded; a timeout is incomplete cleanup, never
success. Each step is idempotent, so a retry continues from the persisted phase.
If any step fails, both the fence and the safest available evidence remain.

## Guarantees

- Tenant isolation: a foreign tenant's profile is indistinguishable from an
  absent one.
- No container is removed by name alone; uncertain removal keeps the deletion
  incomplete.
- A profile never becomes writable during cleanup, and a completed reset cannot
  be reopened by a late request carrying an older operation.
- A completed reset leaves no background review that could write memory into a
  recreated profile.
- Completion requires durable proof that the owned-data purge committed.
- Both SQLite and PostgreSQL use the same migration and store code.
