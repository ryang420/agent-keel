# Coordinating sandbox lifecycles across instances

Date: 2026-10-18. Status: draft for review.

Revision: address delayed cancellation/PID reuse and durable recovery of retired creation attempts.

## 1. Intent and agreed scope

Several keel instances on one host must safely share a profile's Docker sandbox. An instance starting, reaping
idle containers, changing a policy, or recovering a lost worker must not destroy another instance's valid work.

The approved deployment is one host, one PostgreSQL database, one Docker daemon, and one shared deployment
identifier. Keep one current sandbox per profile and allow different sessions of that profile to run concurrently.
SQLite remains a single-instance development/test option. Multi-host placement and workspace replication are
outside this proposal.

The approved behavior is:

- PostgreSQL stores sandbox identity, run-wide usage leases, and all managed executions.
- A per-exec supervisor owns cancellation; delayed control requests cannot signal a different execution after
  numeric PID reuse. Lifecycle attempt records survive retirement, replacement, and coordinator restart.
- A usage starts before sandbox preparation and survives model waits and executor cleanup.
- A sandbox policy change returns `409 sandbox_busy` while the profile has queued/running/stopping runs or
  unreleased usage. Submitting an equivalent policy remains allowed.
- A lost or uncertain usage blocks new sandbox admission. Other valid users may finish. Once they release,
  remove the affected container generation, confirm removal, and only then admit replacement work.
- During recovery, new runs may queue under the existing capacity limit; blocked profiles must not consume
  execution workers or prevent healthy profiles from progressing.
- Recovery can discard files in a container's writable layer. Existing persistent mounts retain their existing
  semantics. This proposal adds no workspace snapshot service.

This document is a design artifact. It does not authorize implementation before written-spec review and an
implementation plan.

## 2. Problems in the current implementation

`DockerSandbox._locks`, `_active`, and `_last_used` are process-local. Two instances can both inspect an absent
container and try to create the same name. One instance can also reap or replace a container while another is
using it. Even one instance considers a sandbox inactive during a model call between tool calls.

Startup currently queries in-flight profiles and later removes other containers. Admission can race that query.
`reference()` and command cleanup identify a container by its profile-derived name, which can later denote a
replacement container.

`runs.current_exec_id` is not a complete execution registry. Skill discovery, file helpers, and temporary-file
cleanup can call the backend without that recorder. A worker can therefore disappear with Docker work that
another instance cannot identify from the run row.

Finally, database lease expiration does not cancel an operation already sent to Docker. Replacing an expired
lease with a new owner is insufficient to fence external side effects.

The existing cancellation script reads a PID marker and then signals a numeric process group. Even with a valid
usage, the command can exit and its PID can be reused between those steps. Exact container IDs only fence
different containers; they do not prevent a delayed signal from hitting another session in the same generation.
One mutable lifecycle-operation slot also cannot retain several retired, unresolved creation attempts.

## 3. Guarantees and explicit limits

1. At most one generation is published as the current usable sandbox for a profile.
2. Internal lifecycle actions never remove a generation with valid bound usage. An expired usage is retained
   as recovery evidence, not counted as a clean release.
3. Creation, policy replacement, reaping, and recovery share the same database coordination state.
4. Every managed Docker exec is registered before dispatch and bound to a usage, generation, and container ID.
5. A stale actor cannot publish a result, renew a revoked lease, or direct an old handle at a replacement ID.
6. A replacement is not published while executions admitted against its predecessor remain unresolved. Full
   predecessor removal is the conservative recovery boundary.
7. A timed-out Docker request is treated as uncertain unless its outcome can be verified. Connectivity failures
   never count as proof that a container is absent.
8. The atomic run-termination contract remains intact. Run termination and sandbox release are different events.
9. Cancellation and timeout cleanup cannot signal another managed exec because of PID reuse, even when the
   original usage remains valid. Losing stable process ownership causes uncertain cleanup, never PID fallback.
10. Every dispatched lifecycle attempt has durable identity and outcome evidence independent of the current
    generation. Retirement revokes authority without asserting that an external request has finished.

There may temporarily be an unpublished or retired container after a late create response. Thus the guarantee
is one *published* generation, not that Docker can never display two physical containers. Such containers are
quarantined and cannot receive managed execs.

This is coordination among cooperating keel instances, not a replacement for the sandbox security boundary.
It does not provide exactly-once shell effects, process isolation between sessions sharing a container, durable
background jobs, recovery from host storage loss, or protection against an operator manually deleting containers.
Availability during an unresolvable Docker outage is intentionally sacrificed to avoid unsafe reuse.
The absence of session process isolation does not exempt internal cancellation from guarantee 9.

## 4. Alternatives and decision

| Approach | Advantage | Cost |
|---|---|---|
| **Database coordination records and lifecycle ownership — chosen** | Reuses shared state; supports takeover and inspection | Requires a migration and explicit recovery states |
| Shared host file locks and state files | Directly fits a single host | Requires a common writable mount and a second recovery/state mechanism |
| Independent sandbox-manager service | Central execution authority; a useful future multi-host boundary | Adds deployment and communication dependencies beyond the current need |

Implement `SandboxCoordinator` inside each keel instance, using the shared store. Docker remains an execution
backend. Database transactions protect decisions; persisted operation ownership bridges external calls. Do not
hold a database transaction or row lock while waiting for Docker or a model.

For cancellation, choose an exec-scoped supervisor with a pinned child identity. Keeping the PID-marker path
would weaken the safety contract; draining the whole generation on every cancellation would avoid PID signals
but could delay cancellation indefinitely while another valid user remains. The supervisor preserves targeted
TERM/grace/KILL behavior, with conservative draining when its result is uncertain. It is a container-local
execution helper, not a new independently deployed manager service.

For recovery evidence, choose a persistent lifecycle-attempt ledger. Inventory-only classification could avoid
another table, but would require proving retirement from the absence of current references under every
transition. Explicit attempts make revocation, late discovery, and retention independently inspectable.

## 5. Persistent records

Add four tables in a new numbered migration. A profile's ownership still comes from `profiles`; internal
coordination records do not introduce another tenant authorization model.

### 5.1 `sandboxes`

One row per `(deployment, profile_id)`, retained even while there is no container:

| Field group | Contents |
|---|---|
| Identity | deployment, profile ID, Docker daemon ID |
| Current generation | monotonic generation number, unique generation token, container ID/name, applied policy digest, helper protocol/artifact digest |
| State | `absent`, `creating`, `ready`, `draining`, `removing`, or `blocked` |
| Lifecycle operation | current foreground operation ID referencing `sandbox_operations` |
| Idle and diagnostics | `idle_since`, last transition time, bounded redacted error code/message |

The generation counter never resets during idle reaping. A generation token is unique even if a database is
restored. A destructive operation stores its target ID in the ledger before dispatch. Creating/removing
ownership is distinct from usage ownership and can be renewed during long Docker calls. Replacing the current
operation pointer never overwrites or deletes an older attempt.

`blocked` retains the operation reference, target, and recovery evidence. It is not an alternative spelling of
`absent`. The daemon ID is checked before managing a record; a different daemon is a configuration/migration
error and must not be treated as deletion of the original container.

### 5.2 `sandbox_uses`

One usage per sandbox-enabled run, with a unique `run_id`:

| Field group | Contents |
|---|---|
| Identity | usage ID, deployment/profile key, run ID, owner instance, opaque usage token |
| Binding | generation/token and exact container ID; null until preparation has published a container |
| State | `preparing`, `active`, `cleaning`, `orphaned`, or `released` |
| Lease | heartbeat time and expiry |
| Completion | release time, recovery reason |

`preparing`, `active`, and `cleaning` are valid only while their lease is unexpired. `orphaned` cannot renew or
become active again. `released` is terminal. An expired row continues to block reaping/policy changes until
reconciliation releases it.

Usage metadata must survive deletion of a terminal run/session during cleanup. `run_id` is a logical reference;
session deletion must not cascade-delete usage or execution evidence. Existing session-deletion responses stay
unchanged. Cleanup authority comes from usage records, not from continued existence of the transcript.

### 5.3 `sandbox_execs`

One row per managed exec ID: usage ID/token, generation/token, exact container ID, state, dispatch/start/finish
times, supervisor protocol version, unique control identity, and cleanup outcome. States are `pending`,
`running`, `finished`, `uncertain`, and `retired`. Numeric PIDs/PGIDs, if present in diagnostics, are not authority
for cancellation and cannot be reconstructed into a control handle after a restart.

`pending` is committed before starting the Docker client. A lost response leaves the record pending/uncertain;
it does not justify a new dispatch. `retired` means its target container was confirmed removed. Store IDs and
bounded diagnostics, not argv, source code, environment values, or credentials.

The registry includes terminal/execute-code commands, file helpers, skill discovery and reads, and temporary-file
cleanup. Lifecycle inspections, signals, and removal are coordinator maintenance operations with an explicit
target; they do not masquerade as model-authorized executions.

### 5.4 `sandbox_operations`

One row per external lifecycle attempt, inserted before dispatch. Identity is immutable; ownership, observed
outcome, and cleanup progress advance conditionally. Retain separate rows for create, helper installation,
start, and remove; link preparation steps to their original create attempt.

| Field group | Contents |
|---|---|
| Identity | operation ID, kind, deployment/profile key, daemon ID, generation number/token, unique intended name, original create-operation ID |
| Target | exact container ID when known; null only before a create result is discovered |
| Ownership | owner instance, renewable owner token, lease deadline |
| Authority | `current` or irrevocably `retired`, retirement time/reason |
| External outcome | `planned`, `dispatched`, `succeeded`, `definite_failure`, or `uncertain`; bounded result evidence |
| Cleanup | discovered container ID, cleanup-operation reference, confirmed-absence time, resolution time |

An attempt's operation ID is stable across reconciliation ownership changes; the renewable owner token is not
its identity. Container creation labels carry the original create-operation ID as well as the generation token.
Create names and operation/generation tokens are never reused. Each attempt permits at most one external
dispatch, including across takeover; disable automatic create/start retries below the coordinator. A fresh
attempt needs a fresh row. A committed `dispatched` row may represent a call paused before reaching Docker.
Once an exact target is attached it cannot be rebound to a different ID; conflicting evidence blocks recovery.

`retired + uncertain` is an intentional combination. Retired create A and B can remain unresolved while C is
current, provided neither A nor B has an authorized start or managed exec. Historical cleanup has ownership in
its own operation row; it does not replace C's foreground operation pointer. Serialize destructive actions per
exact target and recheck current usage/bindings before recording each remove intent.

Retiring a generation irrevocably retires all its preparation authority before clearing the current pointer.
An observed successful create remains protected while it is the current preparation/current generation; its
success alone does not authorize another start or a second dispatch.
Reconciliation may lease a retired row to observe or clean its result; it cannot restore preparation authority.

Do not cascade-delete operation evidence when a run, session, or profile is deleted. Retain the coordination
guard for a deleted profile while recovery is unresolved. Store identity and bounded diagnostics, not command
arguments, environment contents, or credentials.

Released usage and finished/retired exec records may be pruned after the existing event-retention interval, but
never while referenced by unresolved recovery. Lifecycle-attempt retention follows section 14, not event age.
Retention does not delete containers.

## 6. Transaction boundaries and lock order

Use the profile row as the first guard for admission and policy updates, ensuring first-use row creation cannot
escape coordination. PostgreSQL takes row locks; SQLite uses its existing write transaction serialization.
Insert the sandbox row idempotently under that guard.

Operations that need several locks use this order:

1. Existing global mount-policy lock, only for profile updates that already need it.
2. Profile row.
3. Sandbox row.
4. Lifecycle-operation rows in ID order, if needed.
5. Run row, if needed.
6. Usage rows in ID order.
7. Exec rows in ID order.

Run termination and run-lease sweeping may continue to lock run rows alone, as in the first P0. They must not
then acquire a profile/sandbox lock in the same transaction. Reconciliation follows after that transaction
commits. Lease-only updates can lock a usage or operation row alone, but must not subsequently seek earlier
locks. This avoids reversing the order introduced by claim/admission.

Within the profile guard, serialize enqueue, sandbox policy comparison/update, claim-and-reserve, execution
admission, usage release, and decisions to begin lifecycle work. Recheck predicates after acquiring locks.
Use conditional token/state updates when applying external results.

PostgreSQL row locks end with the transaction; this design does not hold session advisory locks across Docker
calls. See [PostgreSQL row-level locking](https://www.postgresql.org/docs/16/explicit-locking.html#LOCKING-ROWS).

## 7. Enqueue and claim

Creating a queued run takes the profile guard for its final profile validation and insertion. A concurrent
policy update either commits first and the run validates against that policy, or observes the queued run and
returns `409`. Existing model/input checks and capacity/session conflict behavior remain.

Claiming a sandbox-enabled run atomically changes `queued` to `running` and creates its `preparing` usage under
the same guard. Return a claim carrying the run and usage handle. Chat-only runs need no usage. No executor
may access Docker before receiving this handle.

The queue selector skips profiles in `draining`, `removing`, or `blocked`, and profiles whose creation is owned
by another claim. A queued run does not hold a container, does not start a lease heartbeat, and does not reserve
a worker. Stop can still cancel it through the atomic lifecycle method.

Do not lock a candidate run and then wait for its profile guard. Inspect candidates without write locks, try
the profile guard without waiting on a busy profile, then lock/revalidate the candidate run. Continue to other
eligible candidates when a profile is unavailable. The final conditional update still ensures one claimant.
Tests must cover a blocked profile at the head of a nonempty queue.

When no usable container exists, one claim owns preparation; other queued runs wait. When publication succeeds,
their claims may share the ready generation. A policy replacement can reserve preparation for a new run, but
its unbound `preparing` usage never grants permission to execute in the old generation.

## 8. Lease ownership and cancellation

Usage and lifecycle-operation leases use `KEEL_LEASE_SECONDS` and renew every third of that interval. The
existing 60-second maintenance interval remains appropriate for idle/recovery scans. Heartbeat work is
independent of the potentially long Docker await.

Use database time for coordination deadlines and idle times. Claim, run-lease renewal, and expiry checks must
use the same database-time basis when coupled with usage admission; pass lease durations instead of host-made
absolute deadlines. Add a database-clock helper (with an injectable test clock) for PostgreSQL and SQLite.

Usage renewal remains active through executor cleanup, even after the run row has become terminal. It requires
the matching owner/token, a renewable state, and an unexpired deadline. Never resurrect an expired or orphaned
usage. Another profile's orphan does not affect it. A valid usage in a draining profile may renew and finish.

On failed renewal, the owner stops dispatching new execs and cancels its executor no later than its last
confirmed lease deadline. Every new exec also requires a successful database admission; an unavailable database
therefore stops new launches immediately. Already-sent Docker work may continue and is reconciled separately.

Lease responses include database observation time and expiry. Calculate local cancellation deadlines using
elapsed RPC time and a monotonic clock, rather than comparing database timestamps with the host wall clock.
If the response leaves no positive lease interval, do not dispatch work.

A lease is an authority to request work, not proof of process lifetime. Run terminal transitions continue through
the atomic store operations from the first P0; no sandbox lease operation inserts terminal events independently.

## 9. Lifecycle operation protocol

Each create/helper-install/start/remove operation follows three phases:

1. In a short transaction, validate state and usage, persist operation intent and a unique owner token, and move
   the sandbox out of an admissible state where necessary. Insert a ledger row; link the foreground pointer when
   applicable. Commit `dispatched` before the one permitted external call, conditioned on current authority.
2. Outside the transaction, perform the operation against its recorded identity while renewing operation
   ownership. Distinguish a definite result from timeout, cancellation, transport failure, or lost acknowledgement.
3. In a fresh transaction, verify operation token, generation, and target before publishing the observed result.
   A stale result cannot make a generation ready or release another owner's records. It may supply immutable
   result evidence to the original attempt for reconciliation, without restoring its authority.

An expired operation can be taken over for *reconciliation*, not blindly dispatched again. Ownership in the
database does not fence the Docker daemon. Use observation, exact IDs, generation-specific names, and the
conservative recovery boundary below to handle external work that outlives its owner.

### Creation and policy replacement

Allocate a new generation and unique name, for example `keel-sbx-<profile-hash>-<generation-token>`. Preserve
managed/deployment/profile/policy labels and add generation and operation labels. Names discover an uncertain
create result; a published handle always includes the full returned container ID.

Only publish after verifying labels, policy digest, daemon identity, and running state for the intended
generation and a still-valid preparing usage. Use `docker create` followed by a separately recorded
`docker start <container-id>` operation, rather than `docker run`: creation alone must not execute the image's
entrypoint. Install the runtime helper into the stopped exact-ID target as described in section 10.1. Revalidate
operation ownership and preparing usage before recording start intent. The create attempt must still be current;
retirement and start authorization serialize under the same guard. If start is uncertain,
its exact target must be reconciled/removed before replacement publication.
Publication atomically sets the sandbox `ready`, binds the preparing usage to the verified ID/generation, and
moves that usage to `active`. Claims sharing an already-ready generation bind their usage in the claim transaction.

A lost create response is recovered by its unique name/labels and original ledger row; do not repeat creation
under a new identity until the old attempt is durably retired from admission. Retirement revokes publication,
helper installation, start, and exec authorization for that attempt, but retains its uncertain external outcome.
Before replacing it, prove under the guard that no start or managed exec was authorized. Any already-dispatched
preparation against a known target must be reconciled/removed before replacement publication. A late retired
create can be quarantined and deleted by its observed ID; it remains stopped and is never adopted by an old
handle, started by stale continuation, or given a managed exec. This separation also covers images whose
entrypoints modify mounted files before running
the supplied command. An old start already authorized before revocation is handled as uncertain work against
its recorded ID, not as a harmless late create.

The create/start distinction follows [Docker's create contract](https://docs.docker.com/reference/cli/docker/container/create/).

Policy replacement first retires/removes the old container with no valid bound usage, confirms removal, then
creates and publishes the new generation. Never run a task with a new policy object against an old-policy
container. Existing mount immutability and mount isolation checks remain enforced.

### Restart inventory and retired-attempt recovery

On startup and each bounded maintenance pass, reconcile unresolved ledger rows and inventory managed containers
for the configured deployment/daemon, including stopped containers. Match deployment, profile, generation token,
create-operation label, unique name, and any recorded exact ID. A stopped container alone is not proof of
retirement; a valid in-progress creation can legitimately be stopped.

Classify observations under the coordination guard before authorizing any destructive action:

| Observation | Classification and action |
|---|---|
| Matches the current generation or a non-retired preparation attempt | Protected; reconcile its recorded phase, even if its lease expired. Never sweep it merely because it is not yet published |
| Matches a retired create attempt with no authorized start/exec | Quarantined late create; attach the exact ID to that ledger row, record a separate cleanup remove, and confirm absence |
| Matches an unresolved start, exec, or removal against an old target | Uncertain old work; use the existing drain/removal boundary before replacement publication |
| Managed labels have no matching ledger row, or identity fields conflict | Unclassified; block new admission for the affected profile, preserve evidence, and require explicit reconciliation. Never adopt or delete it using name or age alone |

An expired owner does not itself retire an attempt. Takeover first reconciles or explicitly revokes it with a
conditional transaction. Recheck bindings under the guard before every cleanup remove, and verify the observed
exact ID outside the transaction. If identity differs from the recorded candidate, return to classification.
Container inventory can lag or fail; neither an empty scan nor a missing name settles an uncertain create.

For example, after retired creates A and B, published C, and a restart, the ledger still identifies both A and B.
Late A/B observations create independent cleanup operations. Their completions can resolve only their own rows;
they cannot clear C's identity, operation pointer, or valid usages. A running retired-create observation violates
the stopped-only premise: block new admission and reconcile its origin rather than treating it as harmless.

### Idle reaping

`idle_since` starts when the last usage releases safely, or when an unused generation first becomes ready.
Admission clears it. Under the guard, reaping rechecks the deadline, absence of unreleased usage, and absence
of unresolved operations that can affect the current generation, then records a remove intent. A queued run
alone does not keep an unused container alive, but its later claim serializes with the removal decision and
cannot execute in a removing container.
Historical stopped-only retired creates do not block reaping or admission of an otherwise safe current
generation; their cleanup proceeds separately. Uncertain start/exec work remains a blocking condition.

Run removal by exact ID, never by a reusable profile name. Confirm absence with a successful daemon response
that specifically identifies a missing container. An arbitrary nonzero `docker inspect` result is not enough.
After confirmed removal, retain the generation counter, clear the current ID, and move to `absent`.

## 10. Coordinated executions

The coordinator returns an immutable handle containing usage ID/token, generation/token, container ID, and the
applied policy. Tools and skills receive only this bound handle and the coordinated backend facade.

For each exec:

1. Under the guard, validate the usage lease/token, binding, and generation. Existing valid users may execute
   while a profile drains; no new usage is admitted there.
2. Insert a `pending` exec record with its exact target and a new exec ID, then commit.
3. Dispatch once by container ID through the exec supervisor in section 10.1. Register runtime progress/results
   against the same exec record; condition updates on its original identities.
4. On confirmed completion, record `finished`. On an ambiguous launch or cleanup outcome, atomically mark the
   exec uncertain and the generation draining under the guard. Keep evidence rather than claiming a clean
   release or replaying the shell command.

A launch admitted before expiry can reach Docker after expiry. Its pending record protects the recovery path:
new usage remains blocked and the predecessor ID must be removed before reuse. Late requests against a removed
ID cannot be redirected to the replacement. The backend must never turn a missing old ID into an automatic
`ensure(profile)` and command retry.

If an externally removed/stopped container is detected during an active run, fail the affected sandbox use
through existing run failure handling and reconcile the generation. This proposal deliberately replaces the
old transparent mid-run recreate-and-retry behavior: it could execute against missing files or a stale policy.
The next eligible queued run gets a newly published generation after recovery.

Adapt file helpers and skills to the same execution path. Preserve hardening flags, private env files, output
limits, redaction, timeouts, and cancellation signals. `runs.current_exec_id` may remain a convenience pointer,
but recovery authority comes from the complete execution registry and immutable target IDs.

### 10.1 Stable execution identity and cancellation

Replace the PID-file shell wrapper and out-of-process numeric group signaling with a versioned, statically
linked Linux supervisor/helper. Package Linux amd64/arm64 artifacts with keel; select by daemon architecture.
Copy the verified artifact into a reserved `/.keel-runtime` location in the stopped container by exact ID,
using a recorded preparation operation. This location must not overlap policy mounts or image-declared volumes.
It is runtime infrastructure, not a new user-configurable mount. Install it root-owned and executable by the
configured sandbox user; commands and their supervisor still run as that user with existing capabilities,
seccomp, no-new-privileges, and runtime settings. No Python/compiler, privileged container, writable cgroup
mount, host PID access, or Linux 6.9 pidfd group-signal support is required inside the sandbox image.
Include the helper protocol/artifact digest in internal generation compatibility checks. An unsupported
architecture/runtime or failed installation fails preparation closed; never fall back to PID-marker cleanup.
See [Docker copy semantics](https://docs.docker.com/reference/cli/docker/container/cp/).

The selected identity mechanism is exclusive parent ownership of an unreaped process-group leader:

1. The helper reserves a generation-local, never-reused exec control identity before launching the command.
   Duplicate dispatch cannot start a second command. A Unix-domain endpoint and terminal receipt use this
   identity; retain its reservation/receipt until the generation is removed. Control requests include the exec
   identity and original usage binding. A control client never accepts or signals a supplied numeric PID.
2. The supervisor directly forks the child. A private handshake establishes a fresh child session/process group
   before payload execution. The supervisor remains outside that group, records the child from `fork`, and is
   its sole reaper. It must not set `SIGCHLD` to `SIG_IGN`, use `SA_NOCLDWAIT`, or allow another thread/handler to
   reap that child. Observe leader exit with `waitid(..., WNOWAIT)` until control is closed. Cancellation during
   setup waits for the group handshake or confirmed setup failure; never signal an unverified inherited group.
3. One serialized supervisor state machine handles completion, TERM, grace expiry, KILL, and final reaping.
   It may call `killpg` only while it still owns the original unreaped leader. The live child or retained zombie
   reserves that PID, preventing reuse as another execution's group leader. The ownership invariant spans the
   signal syscall, including pauses immediately before it; reaping cannot race an in-flight signal handler.
4. A cancel request initiates TERM and the configured grace/KILL sequence inside that supervisor. Retries join
   the same cleanup; they do not restart its deadline. Before reaping the leader, transition to terminal,
   invalidate the endpoint and all queued signal work, and finish/cancel every timer that could issue a signal.
   Only then reap. A delayed control client sees the original terminal receipt or a closed endpoint; the exec
   identity is never rebound to another supervisor. Transport clients only exchange control messages.
   Closing the Docker CLI or command stdin does not give the control client signal authority or count as a
   cleanup acknowledgement. Preserve command stdin/stdout transport separately from supervisor control.
5. A successful signal delivery is not a cleanup receipt. Record `finished` only after the supervisor reports
   leader completion and a confirmed cleanup outcome for its managed group. Descendant cleanup that cannot be
   established, including observed group escape, is `uncertain`; do not claim the whole container is empty.
   Missing supervisor/receipt, helper crash, lost acknowledgement, or protocol mismatch also makes the exec
   uncertain and the generation draining. No actor reconstructs ownership from a PID file, `/proc` lookup,
   `kill -0`, or PID/start-time comparison. Full exact-ID removal remains the recovery boundary.

This pins process identity while signaling rather than checking identity and later signaling an unpinned PID.
The kernel's unreaped-child rules and process-group/session rules are described in
[wait(2)](https://man7.org/linux/man-pages/man2/wait.2.html) and
[setpgid(2)](https://man7.org/linux/man-pages/man2/setpgid.2.html).
Keeping a pidfd only for the leader is not a substitute for a defined group-cleanup protocol.

Lease validation still gates cancellation admission. A request admitted before expiry may arrive late at its
original supervisor; it can affect only that exec's pinned group, never a later exec. After expiry, recovery
does not issue new per-exec signals and follows section 11. A worker disappearing does not transfer supervisor
parenthood to another coordinator. Reconciliation can observe evidence but cannot recreate the supervisor.

Docker's CLI permits exec/removal targeting by container ID; exec requires a running target. See
[docker exec](https://docs.docker.com/reference/cli/docker/container/exec/) and
[docker rm](https://docs.docker.com/reference/cli/docker/container/rm/).

## 11. Normal release and recovery

Normal release moves usage through `cleaning`, finishes managed cleanup, and commits `released` only when no
pending/running/uncertain exec remains for that usage. A terminal run is not sufficient evidence. Completion,
cancellation, and error paths all execute this release protocol; an unreachable store leaves persistent evidence
for a later recovery pass.

Normal release requires an unexpired matching usage token. Expired/orphaned usage can only be released by
reconciliation after the generation is removed. A late completion can improve diagnostics, but cannot turn an
orphan back into a valid user or reverse draining.

On expired usage, uncertain execution, or a missing generation:

1. Under the guard and usage locks, recheck deadlines/tokens, mark affected uses `orphaned`, and mark the
   generation `draining`. This is irreversible for those tokens. New admission checks also detect expired usage
   and refuse to slip through between lease expiry and a maintenance scan.
2. Fail/cancel affected runs through existing lifecycle handling when needed. An already-terminal run is left
   unchanged. Preserve the first P0's single terminal event and late-event suppression rules.
3. Allow other valid users to finish and release. Recovery does not signal a process group using an expired
   user's PID marker while other valid users remain: stale/reused PIDs are not sufficient authority to target
   another live process. Normal cancellation and timeouts of still-valid owned executions use the stable
   supervisor protocol in section 10.1. A delayed admitted request remains bound to its original exec identity;
   uncertain control triggers draining rather than numeric-PID fallback.
4. When no valid usage remains, obtain remove ownership, remove the exact old container ID, and confirm absence.
   This also handles descendants that escaped a command's original process group inside the container.
5. Mark unresolved execs `retired`, release orphan usages, clear the current ID, and move to `absent`. Future
   claims prepare a new generation with a new token.

If another remaining user loses its lease, it also becomes orphaned. There is no forced timeout that destroys
a still-valid user's container. Therefore recovery waits for valid users to finish or lose authority; bounded
run duration is a separate budget feature. Record the blocked reason so an operator can diagnose a long drain.

Docker unavailability keeps the profile blocked. A lost acknowledgement to removal is resolved by inspecting
the recorded ID. Renewed database connectivity does not itself authorize a new generation. For an uncertain
create with no published target, only its labeled generation and durable attempt can be reconciled; no exec may
have been admitted to it. Retired attempts remain discoverable independently of the current operation pointer.
A retired late container is classified and swept by the section 9 protocol without touching the current generation.

If inspection confirms the bound container is already missing, its existing users cannot continue against a
replacement. Fail their still-active runs through the atomic lifecycle API with `sandbox_unavailable`, revoke
their usages, and reconcile the now-confirmed absence before allowing a new generation. Do not wait for those
users to discover a replacement and silently continue. Already-terminal outcomes remain unchanged.

## 12. HTTP behavior and policy updates

`PUT /v1/profiles/{id}` canonicalizes the old/new sandbox policies and compares their digests under the guard.
If the policy changes while any run is queued/running/stopping, any usage is unreleased, or an unresolved
lifecycle operation can still affect an executable generation, return HTTP 409 with error code `sandbox_busy`. Retired stopped-only
create attempts and their isolated exact-ID cleanup do not make a safe current generation busy. Do not partially
update other profile fields.

An unchanged effective sandbox policy is allowed. Changes to model lists, persona, enabled skills, and memory
retain their existing behavior when the sandbox policy is unchanged. Omitting `sandbox` still means `{}` under
the existing replace semantics; clearing or enabling a sandbox counts as a policy change. Preserve tenant checks,
mount validation, and the existing prohibition on changing established mounts.

`POST /runs` may queue for a recovering profile; the existing session and global capacity limits still apply.
`GET /runs` reports queued until a worker can claim it. Queued stop works normally. Do not introduce a new public
run status or change the SSE protocol. Internal generations, tokens, and execution records are not public API
credentials or new model-visible tools.

## 13. Service wiring and internal interfaces

| Component | Responsibility |
|---|---|
| Store | Atomic admission/claim, policy guard, usage and operation leases, durable attempt ledger, execution records, release and reconciliation transitions |
| `SandboxCoordinator` | Bound handles; preparation; operation dispatch/reconciliation; coordinated execution; idle/recovery scans |
| Docker backend | Low-level exact-ID inspect/copy/start/exec/control/remove; labeled generation creation; definite versus uncertain outcomes |
| Exec supervisor/helper | Exclusive child identity, serialized TERM/grace/KILL and reaping, immutable exec control routing and cleanup receipts |
| Worker/runner | Claim handle ownership, heartbeat through cleanup, cancellation, normal release, existing run termination |
| API | Guarded profile update/enqueue and existing authorization/response mapping |

Provide explicit internal operations for claim-and-reserve, usage renewal, preparation/publication,
begin/finish-exec, begin-cleanup/release, and bounded reconciliation. Their results carry immutable handles or
typed outcomes; do not return a profile-derived name as execution authority. Private raw Docker methods must
not be reachable through production tool/skill paths.

Replace startup `remove_orphans(in_flight_profiles())` with a coordinator reconciliation scan. Idle cleanup and
lost-worker cleanup use that same coordinator. Startup never deletes containers based on a stale profile list.
All instances may scan; persisted operation ownership and predicates select one owner for each action. Scans
cover historical unresolved attempts as well as current sandbox rows and daemon inventory.

Use bounded batches (100 records per maintenance pass) and skip locked profiles. Notifications may reduce queue
or recovery latency but remain advisory; the worker/maintenance polling paths ensure progress. Shutdown stops
claiming, drains/cancels work, keeps usage heartbeats through cleanup, and leaves uncertain work recoverable.

## 14. Migration, cutover, and retention

Add a new migration after the existing latest schema version; do not edit released migrations. Include primary
keys, unique run/exec/operation identities, and indexes for profile usage, live lease expiry, operation expiry,
unresolved retired attempts, original create identity, exact cleanup target, and exec state by usage. Enforce
unique intended create names per daemon and one non-retired destructive operation per exact target. Replacing
remove ownership retires the old attempt before authorizing a fresh exact-ID attempt; any delayed old remove
still addresses that same immutable target, and all unresolved rows retain their evidence. SQLite and
PostgreSQL must express the same state transitions and constraints.
Update scratch-database teardown and migration fixtures for these tables, in dependency order, so repeated
independent-store tests exercise clean schemas rather than inheriting coordinator state.

Use a coordinated cutover: stop admission, drain and stop every old writer/reaper, back up the database, migrate,
and start only the coordinated version. Existing queued runs may remain; their first new claim creates usage.
Old workers must be stopped before the guarantee begins because they can bypass every new guard.

Legacy labeled containers have no trustworthy generation/usage records. After old writers are stopped, inventory
them by ID and retire/remove them under coordination before allowing that profile's new sandbox admission.
Preserve bind-mounted data; do not adopt an untracked legacy container as a ready generation. This cutover can
discard legacy writable layers, so required files must already be in configured persistent mounts or separately
backed up by the operator.

Validate deployment namespace and daemon identity when opening records. Replacing the Docker daemon identity
requires an explicit stopped-service reconciliation; never automatically rebind an existing profile's metadata
to another daemon. The guarantee does not cover direct SQL or Docker mutations by unrelated processes.

Lifecycle-attempt retention is evidence-based:

- Never prune an attempt referenced by current state, unreleased usage/exec, or unresolved recovery/cleanup.
- An uncertain create with no discovered result retains a compact tombstone indefinitely. This design assumes
  no finite upper bound on delayed dispatch or Docker delivery; elapsed time, lease expiry, event retention,
  and repeated empty inventory scans cannot prove that the request will never arrive.
- A retired attempt can become resolved after authoritative failure of its sole dispatch, or discovery of its
  uniquely identified result followed by confirmed exact-ID removal and reconciliation of every linked
  preparation/start/remove operation. Authoritative failure requires a definitive daemon rejection of that
  dispatch; a CLI timeout, disconnect, missing-name inspection, or call paused before Docker does not qualify.
- Resolved, unreferenced attempts may be pruned after the existing event-retention interval measured from
  resolution, not creation or retirement. Prune dependent cleanup rows and references in a safe transaction;
  never reset generation counters or reuse operation/name/control identities. Even after pruning, conflicting
  or unknown inventory remains unclassified and is never automatically adopted/deleted.

Keep unresolved tombstones small and process them in bounded batches; expose their count and oldest age for
diagnostics. A future bounded-delivery or daemon-decommission protocol may permit additional pruning, but
neither is assumed here. Package and verify helper artifacts in wheel/container builds and Docker CI before
cutover; the new version must not run with a missing helper or mixed cancellation protocol.

Because this change adds a schema migration, rollback to an older binary follows the repository's database
backup/restore procedure; the schema is not downgraded automatically. Update both architecture documents, API,
sandbox and operations guidance, and the changelog with these compatibility changes during implementation.

## 15. Failure matrix

| Fault or race | Required behavior |
|---|---|
| Two first claims for one profile | One preparation owner; only a verified generation becomes ready |
| Reaper races admission | One guarded decision wins; no valid usage is bound to a removing generation |
| Policy update races enqueue or claim | Either the update wins first, or the busy check returns 409; no stale-policy admission |
| Model wait exceeds idle timeout | Usage remains valid; no idle removal |
| Run becomes terminal before cleanup ends | Usage remains protective through cleaning |
| Worker dies during a helper command | Registered exec and usage remain; profile drains and the generation is retired |
| Database becomes unreachable | No new exec admission; owners stop by their last confirmed deadlines; evidence remains |
| Docker call times out | Preserve uncertain intent/result; reconcile before reuse |
| Remove succeeds but acknowledgement is lost | Inspect that exact ID; do not remove by name or duplicate the state transition |
| Old exec/start/remove arrives late | It targets only its old ID; no stale result can publish or recreate a usable replacement |
| Cancellation pauses before signaling and the command exits | Supervisor retains the leader until all signal work is closed; delayed requests cannot target a reused PID |
| Supervisor dies or cancellation acknowledgement is lost | Exec becomes uncertain; drain, preserve other valid users, and recover by full exact-ID removal |
| Retired create appears late | Match its durable attempt; quarantine, forbid start/exec, and clean up by observed ID |
| Several retired creates appear after restart | Recover each from its own ledger row; never overwrite or clear the current generation |
| Inventory container has no matching attempt or conflicting labels | Block affected admission and preserve evidence; no automatic adoption or deletion |
| Retention runs while a retired create can still arrive | Keep its unresolved tombstone regardless of age or empty inventory scans |
| One use is orphaned while another remains valid | Block new users, let the valid user finish, then remove the predecessor |
| Two instances reconcile simultaneously | One operation token owner; stale database updates cannot clear the winner's state |
| Operator removes a container | Fail affected use and recover; do not transparently repeat shell effects |

## 16. Verification and acceptance

Use deterministic fake clocks/backends for the state machine, PostgreSQL tests with independent stores/processes
for contention, and two coordinator instances sharing a real Docker daemon for lifecycle acceptance. No model
API call is required.

Required tests:

- Concurrent create/claim, idle reap/admission, policy update/enqueue, and policy update/claim. Verify a single
  published generation, correct 409 behavior, and unchanged tenant/mount isolation.
- A run waiting on a fake model beyond the idle timeout; cancellation and terminal-before-cleanup cases. Verify
  protection until safe release, including all internal helpers and temporary-file cleanup.
- Independent ownership/renewal races, lease expiry at admission, and stale-token renewal/publication. Exercise
  actual database time and injected host-clock skew.
- Kill an instance during creation, after Docker success but before publication, during exec dispatch, and during
  release. Verify another instance can recover without assuming an expired lease means the command stopped.
- Pause a valid user while another becomes orphaned. Verify new claims remain queued, healthy profiles progress,
  the valid user's container survives, and old-generation removal is confirmed before replacement.
- Hold a pending exec until after revocation/removal, then deliver it. Verify it cannot target or recreate the new
  container. Deliver a late create and a stale removal completion; verify quarantine and conditional publication.
- Delay a cancel client before delivery, complete/reap its original command, and reuse that numeric PID for
  another exec in the same container; verify the old control identity cannot signal the new exec. Use a
  deterministic identity-reuse model and a controlled Linux PID-namespace integration harness, not probabilistic
  PID churn on a shared daemon. Pause the supervisor immediately before TERM and KILL: final reaping must wait
  while the original leader stays pinned. Assert that numeric signaling is unreachable after control closure.
- Exercise cancel-versus-natural-exit, duplicate cancellation, leader exit with group children still running,
  helper death, lost receipts, expired-usage recovery, and delayed control after coordinator restart. Other valid
  sessions must survive; uncertain cleanup must never release usage or fall back to PID-marker signaling.
- Retire multiple uncertain create attempts, publish a later generation, restart both coordinators, and deliver
  old containers in reverse order. Verify independent ledger discovery/removal, unchanged current identity and
  usages, and no late start/exec. Include stopped legitimate in-progress creation and conflicting/unknown labels.
- Race retirement with start authorization, late cleanup with policy replacement, and ledger takeover with stale
  results. Verify that any authorized uncertain start blocks replacement until its exact target is absent.
- Advance retention beyond event age before late-create discovery, run repeated empty scans, then restart and
  deliver the result. Keep unresolved tombstones; prune only resolved unreferenced attempts, with linked rows
  intact until safe. Test profile/session deletion and guard/evidence survival during historical cleanup.
- Inject Docker/database outages, inspection errors other than not-found, uncertain removal, and lost commit
  acknowledgements. Verify blocked state survives restart and no run/usage/operation is falsely released.
- Verify generation tokens/IDs and applied policies across changes, writable-layer loss after recovery, and
  preservation of existing bind-mounted files. Verify startup does not delete another instance's protected use.
- Delete a terminal session during cleanup and verify execution evidence remains available to recovery.
- Verify retention never prunes unresolved evidence and that maintenance batches are bounded.
- Verify helper packaging/digest, stopped-container installation, reserved-path conflicts, configured sandbox
  users, Linux amd64/arm64 builds, and supported runtimes with the existing hardening flags. Unsupported helper
  capabilities must fail closed without executing a payload through the legacy wrapper.
- Run the existing run-termination race suite to protect atomic terminal events and suppression of late events.
- Put every new PostgreSQL concurrency test in the PostgreSQL CI job and every real-container test in Docker CI.

Acceptance requires passing default, PostgreSQL, Docker, and relevant subprocess fault suites, plus an explicit
audit that every production Docker exec and destructive lifecycle path goes through the coordinator. Exercise
the deployment cutover on a scratch database with legacy containers. Record admission/heartbeat transaction
latency and connection-pool pressure before increasing configured concurrency.

## 17. Follow-up boundaries

Multi-host routing, remote sandbox services, per-session containers, filesystem snapshots, transparent mid-run
workspace restoration, background-job management, and exactly-once external effects remain separate work.
The complete execution registry may support those features later, but this proposal implements only the
single-host lifecycle and recovery contract agreed above.
