# Atomic run termination and terminal events

Date: 2026-10-17. Status: draft for review.

## 1. Intent and agreed scope

A consumer must be able to observe a run's final outcome through both the run API and its persisted event stream.
A process crash, concurrent stop requests, or competing workers must not commit a terminal run state without its
matching terminal event, or produce multiple terminal events for one transition.

The agreed scope covers terminal transitions performed after the upgrade, including transitions of runs created
before the upgrade that are still active. Historical terminal runs and their events are left untouched. There is
no backfill, reconstruction, or validation requirement for historical terminal events.

Keep the existing HTTP routes, response bodies, SSE event types, payloads, and cursor format. Support SQLite and
PostgreSQL using the existing store and database adapters. Keep the current event retention and session deletion
behavior. This is a design proposal; implementation requires a separate review and implementation plan.

## 2. Current behavior and failure modes

`SqlStore.finish_run` conditionally updates an active run, so competing finishers already have one state winner.
However, terminal events are written in a separate operation:

| Path | State writer | Event writer |
|---|---|---|
| Model completion, model failure, internal failure, executor cancellation | `finish_run` | `RunExecutor._finish` |
| Stop before claim | `request_stop` | HTTP stop handler |
| Expired lease | `expire_leases` | `WorkerPool.sweep_once` |
| Shutdown after the drain period | `finish_run` | `WorkerPool.close` |

Each path has a commit window between the state update and event insertion. If the process exits in that window,
the run remains terminal but the event is permanently missing. An event insertion failure has the same effect.
The lease sweeper cannot recover it because the run is no longer active.

The queued-stop path also infers transition ownership from a before/after read. Two requests can both read
`queued`, both later observe `cancelled`, and both emit `run.cancelled`.

The SSE handler waits up to five seconds for a terminal event after seeing a terminal run. This accommodates a
late write but cannot repair a missing one. Ordinary events can also currently be appended after a terminal
event, for example by an executor that has not yet noticed a lease sweep.

## 3. Guarantees and limits

For transitions covered by this design:

1. The terminal state, final run fields, and exactly one matching terminal event commit in one transaction.
2. A failed or cancelled transaction commits neither the terminal transition nor its event.
3. A terminal run is immutable through lifecycle operations. Retrying a transition cannot overwrite its outcome
   or append another terminal event.
4. The terminal event is the last accepted event for that run. Ordinary event writers serialize with termination
   and decline late events after observing a terminal state.
5. Notifications happen after commit and remain advisory. A missed notification may delay delivery until the
   next database read; it cannot remove or invalidate a committed event.
6. Event retention and session deletion may subsequently remove events. The guarantee is about committing the
   transition, not retaining its event forever.

Exactly-once persistence does not imply exactly-once delivery to clients. A client may receive an event again
when replaying an overlapping cursor and must identify it by `(run_id, seq)`.

This design does not provide tool side-effect deduplication, execution resumption, durable sandbox cleanup,
lease fencing for all transcript writes, or atomicity between transcript messages and run termination. It does
not redefine the winner when completion races a stop: the first successful terminal transaction wins, including
the existing possibility that a `stopping` run completes normally.

The guarantee assumes the database becomes available again and the lifecycle machinery can make progress. A
database outage may leave a run active until a later successful transition or lease sweep.

## 4. Alternatives and decision

| Approach | Benefits | Costs and limitations |
|---|---|---|
| **Atomic store operations with per-run event serialization — recommended** | Closes the commit window, gives one transition winner, and makes the terminal event final; uses existing infrastructure | Changes internal store return types and adds a row lock to each event write |
| Atomic terminal writes only | Smallest change; closes the primary state/event commit window | Ordinary writers can still append after termination; SSE and event consumers have a less precise ordering contract |
| Separate transactional outbox and publisher | Useful when events must be delivered to an external broker | Adds a table, publisher, delivery deduplication, and an eventual publication interval; the current SSE reader already consumes the database |

Choose the first approach. `run_events` is already the durable event log consumed by SSE. Store the terminal
event there in the state transaction and keep LISTEN/NOTIFY as a wake-up mechanism. A separate outbox is not
needed for this contract.

Do not add a terminal-event unique index in this change. Such an index could fail to build on historical
duplicate terminal events, which the agreed scope explicitly leaves untouched. Enforce the invariant through
the shared run lock, conditional state update, and exclusive terminal-event insertion path. This is an
application/store invariant; direct SQL writers are outside the contract.

## 5. Store contract

### 5.1 Transition results

Introduce the internal immutable result `RunTransition`:

```python
@dataclass(frozen=True)
class RunTransition:
    run: Run
    event: Event | None
```

An event is present only when this invocation committed a terminal transition. A repeated stop returns the
current run with no new event. A transition that loses to an existing terminal state returns that state with no
event. A missing run returns `None`; profile-scoped operations also return `None` for a profile mismatch.

Change the internal contracts to:

```python
finish_run(...) -> RunTransition | None
request_stop(profile_id, run_id) -> RunTransition | None
expire_leases(*, batch=100) -> list[RunTransition]
append_event(run_id, type_, data) -> Event | None
```

Every public store lifecycle method owns its transaction. Callers must not wrap these methods in an outer
transaction and notify before that outer transaction commits. Private helpers may compose operations inside
the lifecycle method's transaction.

### 5.2 Canonical terminal payloads

The store derives the terminal event type and payload from the accepted outcome. Callers do not supply an
independent event type that could disagree with the final status.

| Status | Type | Existing payload to preserve |
|---|---|---|
| `completed` | `run.completed` | `output`, `usage`, and `incomplete: true` only when applicable |
| `failed` | `run.failed` | `error_code`, `message` |
| `cancelled` | `run.cancelled` | `{}` |

`finish_run` accepts the final output, usage, error fields, and an optional `incomplete` flag. Validate the
requested terminal status and outcome-specific arguments before mutation. Reject an invalid status, a failed
outcome without an error code/message, or an `incomplete` flag on a non-completed outcome. Preserve usage values
of `None` as unknown; do not coerce them to zero. Use one timestamp for `finished_at` and the terminal event's
`at` field. Preserve the current final run-field behavior of each path, including queued-stop defaults and the
existing shutdown/lease-loss error codes and messages.

### 5.3 One lock for lifecycle and event writes

In PostgreSQL, obtain `SELECT ... FOR UPDATE` on the run row before inspecting state, allocating an event
sequence number, or making a terminal change. A profile-scoped operation includes the profile in its lookup.
SQLite uses the existing `BEGIN IMMEDIATE` transaction and connection serialization.

All event writers take this same lock before computing `MAX(seq) + 1`. This makes sequence allocation serial
for one run while independent runs remain concurrent on PostgreSQL. Validate that the run exists before
appending; unknown run IDs are rejected rather than creating orphan events.

The ordinary `append_event` entry point rejects terminal event types with `ValueError`. An unknown run ID also
raises `ValueError`; neither case writes anything. After locking the
run, it returns `None` if the run is terminal. This is an expected late-writer outcome, not a run failure.
It continues to accept nonterminal events while the run is `queued`, `running`, or `stopping`.

A private insertion helper allocates the sequence number and inserts an event while the caller already holds
the transaction and run lock. Terminal transitions use this helper directly after their state update.

Do not put a retry loop around a statement inside an already-aborted PostgreSQL transaction. The shared lock
removes the normal sequence collision; unexpected SQL errors roll back the entire operation. A caller may retry
a lifecycle operation in a fresh transaction, subject to its existing failure handling.

### 5.4 Atomic finish

Within one transaction:

1. Lock and read the run.
2. If it is missing, return `None`; if terminal, return the existing run with no event.
3. Conditionally update the active row, final outcome fields, finish time, and cleared lease fields.
4. Insert the canonical terminal event through the private locked helper.
5. Commit, then return the updated run and inserted event.

Keep a conditional active-state predicate on the update as defense in depth. No notification, provider request,
or sandbox operation runs inside this transaction.

### 5.5 Stop

Lock the profile-scoped run and handle its actual state in that transaction:

- `queued`: change to `cancelled`, record its finish time, clear lease fields, and insert `run.cancelled`.
- `running`: change to `stopping`; no terminal event yet.
- `stopping` or terminal: return the current state without a new event.

This eliminates the API's separate before-state inference. If claiming wins before stop takes the lock, stop
observes `running` and requests executor cancellation. If stop wins, claiming cannot select that run anymore.

### 5.6 Expired leases

Replace the unbounded bulk update followed by external event insertion with one bounded transaction per sweep
batch. Select up to 100 expired active leases, ordered by run ID, using `FOR UPDATE SKIP LOCKED` on PostgreSQL.
SQLite uses the same expiry predicate and limit without that clause.

For every selected run, commit its `failed` state with `worker_lost` and its canonical event in that same batch
transaction. Recheck the active-state and expiry predicates under the lock before updating. Lease renewal and
termination then serialize on the run row: a lease renewed before selection is not failed; a sweep that wins
first prevents a later renewal.

Return transition results only after the whole batch commits. If any event insertion fails, the batch rolls
back, so no committed run disappears from the sweeper's retry set without a terminal event. Do not commit
individual rows and then lose their cleanup/notification results when a later row raises.

`sweep_once` processes one bounded batch; remaining expired runs are handled by following sweep passes. This
bounds transaction size and per-pass work. After commit, notify all transitioned runs before attempting any
sandbox cleanup, preserving the current behavior that slow cleanup cannot delay terminal visibility. Cleanup
uses the returned run's existing `current_exec_id`; it is not cleared by termination itself.

## 6. Runner, worker, and API integration

`RunExecutor._finish` delegates state and event persistence to the store and only notifies when the returned
transition includes an event. Remove its independently supplied event type and payload. Run completion
background work only when this executor actually wins the completed transition; losing to cancellation or
lease expiry must not schedule completion-only work.

`RunExecutor.emit` handles `Event | None`, notifying only for a newly persisted ordinary event. Suppression of a
late event does not interrupt cleanup. During normal user cancellation, tool cleanup still precedes terminal
commit. If shutdown or lease expiry has already terminated the run, later cleanup may update the transcript or
kill a command, but cannot append another event. Consumers treat a terminal event as closing any unfinished tool
display; a matching `tool.completed` is not guaranteed after forced termination.

`WorkerPool.close` and `sweep_once` use transition results and notify, without synthesizing or inserting terminal
events. Keep the existing drain period, cancellation handling, and best-effort command cleanup behavior.

The HTTP stop handler keeps tenant/profile ownership checks, returns the same run JSON, and removes the
before/after event inference. Notify the run-event key if a cancellation event was committed, and notify the
existing stop key to wake an executor when appropriate. Repeated stop calls remain safe.

No change is needed to notification payloads or EventBus interfaces. A notification failure must not cause a
second event insertion. The existing bus implementations already treat send failures as advisory.

## 7. SSE reading and retention

Atomic writes remove the need to wait for a terminal event to be inserted after its state. However, they do not
make two separate reads share one snapshot. A termination can commit between `events_after` and `get_run`.

When the first event read is exhausted and the next state read is terminal, perform one final
`events_after(run_id, cursor)` read before closing. Yield any newly committed events, stopping at the terminal
event. The final read sees a termination that raced the earlier event read. Since terminal runs accept no later
events, no grace-period polling loop is required.

Retain the existing waiter-before-read pattern and timeout-based polling for nonterminal runs. Keep the SSE
frame format, cursor rules, keepalives, disconnect behavior, and close-on-terminal behavior.

If the final read finds no events, close immediately. This covers a cursor at or beyond the terminal event,
events removed by retention, and historical terminal runs with missing events. Do not synthesize events or
attempt repair in the SSE endpoint. Remove the five-second terminal grace loop and the associated age-based
workaround; their removal changes waiting time, not response structure.

## 8. Failure behavior

| Failure or race | Required result |
|---|---|
| Exit or cancellation before transaction commit | Both writes roll back; the run may later terminate through retry or lease expiry |
| Terminal event insertion fails | State update rolls back with it |
| Commit succeeds but the caller loses the acknowledgement | State and event both exist; retry observes terminal and creates nothing |
| Notification is lost after commit | SSE eventually reads the persisted event through polling or reconnect |
| Two queued stop requests | One cancellation transition and one terminal event |
| Completion competes with cancellation, shutdown, or expiry | First successful terminal transaction wins; all later attempts return no new event |
| Ordinary event competes with termination | It commits before the terminal event or is suppressed after terminal state is observed |
| Database unavailable during finish or cleanup | No promise of immediate termination; existing failure handling and later lease sweeping remain responsible |
| Process dies after a lease-loss commit but before command cleanup | Terminal state/event remain durable; command cleanup remains best effort |

Preserve the executor's goal of absorbing failures and recording a final outcome. This work strengthens the
database transition contract; it does not claim that a permanently unavailable database can record a failure.

## 9. Compatibility and rollout

No schema migration is required. Use the existing `runs` and `run_events` tables. Update every internal caller
and test fixture for the changed store contracts; direct test construction of impossible post-upgrade states
must be explicitly labeled as historical data.

Deploy with a coordinated writer cutover: stop admitting new work, drain/stop old instances, and start the new
version. Old and new writers must not share the database during the guaranteed period, because old code can
still update a terminal state and append events independently. Existing active rows processed by new workers
use the new contract. Existing terminal rows are not changed.

The revised implementation does not provide an automatic capability fence against an old binary. Operators must
enforce the cutover. A later rollback to an old binary ends the new guarantee; it does not require undoing data
or a schema change. Preserve the existing retention settings.

Update both architecture documents, the relevant API/operations guidance, and the changelog during
implementation to describe atomic termination, final event ordering, forced-cleanup behavior, and the coordinated
cutover requirement.

## 10. Verification and acceptance criteria

Use fake providers and controlled database failures; no real model calls are needed. Run store-level cases on
both SQLite and PostgreSQL. For PostgreSQL races, use independent connections or store instances so a single
client's serialization cannot hide a concurrency bug.

Required checks:

- Every terminal path persists exactly one event matching the final status and existing payload shape.
- Inject an exception between the state update and terminal insertion; verify both are rolled back from a
  separate observer, then verify a retry succeeds.
- Inject cancellation before commit, and simulate a successful commit followed by acknowledgement loss; verify
  rollback or idempotent retry respectively.
- Race two finishes with different outcomes, two queued stops, stop against claim, and completion against stop,
  shutdown, and expiry. Verify one matching terminal event and unchanged winning final fields.
- Race lease renewal with expiry and verify the winner is determined under the run lock.
- Race ordinary event append with termination; verify no accepted event has a sequence after the terminal event.
- Reject direct terminal-event append and unknown-run event append through the ordinary store entry point.
- Inject one event insertion failure in a multi-run expiry batch; verify the whole batch rolls back and a later
  sweep can finish all candidates. Verify batches are bounded and subsequent passes process the remainder.
- Drop notification delivery; verify SSE still finds the event by polling or reconnect, without duplicate rows.
- Force termination between SSE's first event read and state read; verify the final reread delivers the event.
- Verify SSE closes for a cursor at/past terminal, retained historical missing-event fixtures, and pruned runs,
  without inserting or reconstructing events.
- Verify late cleanup still kills commands and repairs tool results while its post-terminal events are suppressed.
- Preserve tenant ownership checks, output/usage fields, the `incomplete` flag, and event retention/deletion tests.

Acceptance requires passing the default suite, PostgreSQL suite, and affected Docker/fault-injection tests, plus
explicit inspection that no production terminal-state writer or terminal-event producer bypasses the store's
atomic lifecycle operations. Review per-run event-write latency under representative streaming load before
increasing concurrency; a database round trip is added to ordinary PostgreSQL event writes.

## 11. Follow-up work outside this proposal

Lease-generation fencing for all writes, a durable sandbox cleanup queue, event batching for streaming throughput,
and schema-level enforcement for future data may be evaluated separately. None is required to implement the
transition and event contract described here.
