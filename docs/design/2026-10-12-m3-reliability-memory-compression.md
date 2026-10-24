# M3: reliability, titles, memory, compression and tenant isolation — design

Parent design: `docs/design/2026-10-11-architecture.md` (sections are cited as "arch §N"). Previous milestone:
`docs/design/2026-10-12-m2-agent-loop.md`. M2 made a run an agent loop in a sandbox. M3 makes long-lived
sessions and multi-instance deployments safe: faults are survived and tested, sessions get model titles and
long-term memory, and a conversation can outgrow the model's context window. Tenant ownership and authorization
are explicit, while every profile keeps its own runtime state (§12).

Status: accepted 2026-10-24, released in 0.3.0. See "Acceptance record" at the end; §12 remains the tenant
isolation contract.

## Goal and gate

M3 is done when all of these hold:

- **Fault injection passes.** A killed worker, a deleted sandbox and provider 5xx errors each leave the session
  consistent, and the next turn works (§1).
- **A long session survives.** A scripted session many times longer than the context window finishes with valid
  transcripts, compactions that fired, and a stable cached prefix between them (§5, §6).
- **The consumer's own checks still pass.** The M2 contract checks and quality eval are re-run unchanged. M3
  optional features are off or invisible to a consumer that does not use them. The consumer adapter must adopt
  tenant-scoped credentials and profile ownership (§12); authorization is mandatory, not an optional feature.
- **Tenant and profile isolation passes.** Cross-tenant access is refused, and profiles within one tenant retain
  independent sessions, memory, files and tool credentials. Tests include two tenant accounts belonging to the same
  person. These checks gate any real-user pilot (§12).

As in M2, the consumer's quality gate runs in the consumer's own repository. keel carries only the deterministic
harness and an opt-in live script (§6).

## Changes to the milestone plan (arch §13)

| Item | Arch | M3 | Why |
|---|---|---|---|
| Images | M3 | done in M2 | See the M2 design. |
| Stop kills a running command | M3 | done in M2 | See the M2 design. |
| Compression stores a child session | arch §8 | **in place on the session** | Runs, the one-in-flight index and the cache scope are all keyed by a fixed session id. A child session would change that id mid-conversation and force every client to follow it. |
| Memory is on for every profile | arch §7 | **opt-in per profile** | Existing deployments and eval profiles must not accumulate memory between rounds (M2 design, change table). |
| Memory needs a sandbox | implied | **independent** | Today a profile with no sandbox gets no tools. Memory is useful without one. |
| Postgres listener reconnect, orphan removal safe for several instances | not listed | **M3** | They are what keeps the single-instance caveat in the README. |
| Schema migrations | none (README) | **M3** | M3 changes the schema, and a consumer cannot recreate a database that holds its users' sessions. A 0.2.0 database is upgraded in place. |
| Pruning old run events | arch §3 | **M3** | Every streamed token is a `run_events` row; the table must not grow without bound. |
| Explicit tenant ownership and authorization | profile isolation only | **M3** | A tenant contains multiple independent account profiles; profile isolation alone does not authorize a caller for a tenant (§12). |
| Hermes history import | M4 | M4 | Unchanged. |

Still out of M3: parallel read-only tools, scheduled jobs, approvals, web tools and the Hermes import. (The
Anthropic provider was out of M3 too; it shipped separately, as Claude models on the `bedrock` provider:
`2026-10-23-bedrock-anthropic-messages.md`.) Tenant-level quotas, billing, and shared files or knowledge are also outside this milestone (§12).

## 1. Reliability and fault injection

### What already works

A claim is one `UPDATE … WHERE id = (SELECT … FOR UPDATE SKIP LOCKED)` (`store/sql.py`). A heartbeat renews the
lease every third of its length. A sweeper marks runs with an expired lease `failed` with `worker_lost`. A stopped
or swept run is cancelled by its executor, which kills the running command and answers open tool calls. The next
turn repairs any tool call left without a result. M3 does not redesign this; it closes the gaps found around it.

### Changes

- **Listener reconnect.** `PostgresEventBus` logs when its LISTEN connection drops and never reconnects
  (`events.py`). Claiming still polls, so runs keep starting, but stop and SSE delivery lag by up to the poll or
  keepalive interval. M3 adds a reconnect loop with backoff that re-registers the listeners and then wakes every
  waiter so each re-reads the store.
- **Lease versus database timeout.** The Postgres command timeout (30 s) equals the default lease (30 s), so one
  stalled query can use up a whole lease and get a live run swept. The command timeout is derived from the lease
  (a third of it, minimum 5 s) and documented in `docs/guide/configuration.md`.
- **Orphan removal at start.** *(Superseded by the coordinated sandbox lifecycle,
  [2026-10-18](2026-10-18-sandbox-lifecycle-coordination.md), which replaces name-based removal with exact-ID
  inventory classification and a per-profile admission block.)* `remove_orphans` removes every container carrying
  this deployment's label, whether or
  not a run uses it. M3 asks the store for profiles with an in-flight run and leaves those containers alone. With the
  reconnect, this removes the single-instance caveat from the README. A race is accepted: a run claimed on another
  instance between the in-flight query and `docker rm` can lose its container, and the exec path already recreates a
  vanished container once; removing an idle profile's container is harmless (it is recreated on next use).
- **A dead worker's command.** *(Superseded by the coordinated sandbox lifecycle,
  [2026-10-18](2026-10-18-sandbox-lifecycle-coordination.md): the sweeper hands a lost run to coordinator recovery,
  and cancellation goes to the original supervisor by execution ticket, never by `reference` or a numeric PID.)*
  `runs.current_exec_id` is written and never read. When the sweeper marks a run
  `worker_lost`, it makes a best-effort `kill_tree` on that exec (on the handle from `reference`, which creates
  nothing), so a command started by a dead worker does not run on. The kill reaches the command only when the
  sweeping instance shares the lost worker's Docker daemon (or host, for the local sandbox); otherwise the command
  runs until its own timeout. The column is cleared when a command ends. Failure to reach the sandbox is
  logged and does not block the sweep. The sweeper emits `run.failed` for every expired run first and only then
  kills the commands, so a slow or unreachable sandbox never delays the terminal events; `kill_orphaned_command`
  never raises and clears `current_exec_id` whether or not the kill worked (the run is terminal). The stop path
  clears the column too, once it has killed the command.
- **Schema migrations.** M3 adds columns and tables, and `CREATE TABLE IF NOT EXISTS` does not add columns to an old
  table. M3 replaces "create if missing" with numbered migrations (below), so a 0.2.0 database is upgraded in place
  and later releases can change the schema without a fresh database.
- **Pruning old run events.** A maintenance task, started by the worker pool next to the sweeper, runs hourly and
  deletes the `run_events` of runs that finished more than `KEEL_EVENT_RETENTION_DAYS` (default 7) ago, in batches
  of 10,000 rows so one pass never holds a long transaction. The run row, its output and usage, and the session's
  messages are kept. Streaming a pruned run sends no events and closes; a client reads the run with
  `GET …/runs/{id}`. The first pass after start is unbounded; later ones only look at runs finished since the
  previous cutoff less one interval, so a pass does not rescan all history. On several instances the passes may
  overlap; deleting rows that are already gone is harmless.
  The stream closes at once, without the 5 s wait for a late terminal event, when the run is terminal, nothing
  follows the cursor and the run finished more than 60 s ago by the store's clock (a margin well above the grace,
  for clock skew between instances). That covers a pruned run and a client reconnecting past the terminal event.
  The retention is a positive number of days; pruning cannot be switched off.
  *Superseded by `2026-10-17-run-terminal-consistency.md`: the terminal state and its terminal event now commit in
  one transaction, so the 5 s grace and the 60 s age check are gone. A terminal run with nothing after the cursor
  closes at once.*

### Schema migrations

New `src/keel/store/migrations.py` holds an ordered list of migrations. Each has a version number and its SQL for
SQLite and for Postgres. Version 1 is the 0.2.0 schema exactly as released; version 2 is M3's changes (§7).

- **Version table.** `schema_version (version INTEGER NOT NULL)`, one row.
- **At start.** `open_store` reads the version and applies every newer migration in order, each in its own
  transaction that also writes the new version. Service startup wraps the migration pass and tenant ownership
  backfill in one outer transaction, so invalid cutover input rolls all of it back. On Postgres the pass runs under
  the advisory lock the schema already uses, so instances starting together migrate once. On SQLite the transaction
  is `BEGIN IMMEDIATE`.
- **Three starting states.** An empty database applies every migration from version 1, so a fresh database and an
  upgraded one go through the same code. A database with keel's tables but no `schema_version` is a 0.2.0 database
  and is treated as version 1. A version newer than the code knows makes the service refuse to start: a downgrade
  is not supported.
- **Rules.** A released migration is never edited; a change is a new migration. Migrations 2 and 3 only add tables and
  columns with defaults (`ALTER TABLE … ADD COLUMN`, which both databases run inside a transaction), so it is quick
  on a large table, and an older instance still running during a rolling upgrade keeps working, because it reads
  row values by column name and ignores columns it does not know. This rolling-upgrade compatibility does not
  apply to enabling tenant authorization: old instances must be drained and stopped before that cutover (§12).
- **Tests.** A frozen copy of the 0.2.0 DDL builds an old database with a few rows. The test upgrades it on SQLite
  and on Postgres and checks the rows survive and the resulting schema equals a fresh one.

### Fault-injection tests

New `tests/test_fault_injection.py`. It reuses the injectable clocks, `FakeProvider` scripts and `LocalSandbox`
subclasses the existing tests use.

| Scenario | Assertions |
|---|---|
| Worker dies mid-tool-call: pool A claims a run and is abandoned without cleanup, the store clock moves past the lease, pool B sweeps | Run is `failed` with `worker_lost`; its command was killed; the next turn answers the orphaned call and completes; the transcript pairs every call with a result |
| Provider 5xx three times | Run fails `server_error`; the transcript is consistent; the next turn works |
| Provider 5xx twice, then success | Run completes; usage is counted once |
| Provider fails after streaming text | Run fails without a retry (a retry after output is forbidden); the next turn works |
| Sandbox call fails once (`SandboxError`-raising `LocalSandbox` subclass) | The tool result carries the error; the run continues |
| Container removed between two tool calls (`KEEL_TEST_DOCKER=1`) | The next call recreates the container and succeeds |
| Server process killed with `SIGKILL` on a SQLite file, restarted (opt-in) | The run ends `worker_lost` after the lease; a new run in the same session succeeds |

The container case is opt-in with `KEEL_TEST_DOCKER=1`. The `SIGKILL` case starts real server processes, so it is
opt-in with `KEEL_TEST_SUBPROCESS=1`, which CI's default test job sets. The Postgres CI job gains these files, and the
table drop list in `tests/conftest.py` gains the new tables.

## 2. Background tasks

Titles (§3) and memory review (§4) are model calls that must not belong to a run: a run has a lease, a worker slot
and the one-in-flight-per-session index, and a failed side call must never fail the user's answer.

New `src/keel/background.py` owns them:

- `spawn(coro, name)` starts a task and keeps a reference. A limit on concurrent background tasks
  (`KEEL_BACKGROUND_LIMIT`, default 4) drops the new task, logs it and moves on. Titles and reviews are best-effort.
- Every exception is caught and logged. Nothing a background task does can change a run's status.
- `close()` cancels all tasks and waits for them, bounded by a short timeout. `service.py` calls it in the lifespan
  `finally`, next to `pool.close()`.
- A background task is not cancelled when its run finishes. It is cancelled only on shutdown.

As built (2026-10-13): `Background.close` waits 5 seconds.

## 3. Model-generated titles

The first turn already sets a derived title (arch §9, `titles.py`). M3 adds the model title.

- **When.** After the first run of a session completes, `RunExecutor` hands a title task to the background owner.
  It uses the run's provider and model, no tools, low reasoning effort and a small output cap. A session that
  already has a `user` or `llm` title is skipped.
- **Prompt.** Adapted from Hermes `agent/title_generator.py` (pinned commit): 3 to 7 words, answered as JSON, with
  its guards against vague titles. The text lives under `src/keel/prompts/` and in `THIRD_PARTY_NOTICES.md`.
- **Result.** Parse, then `sanitize_title` (100 characters), then `store.set_title(..., "llm")`. Rank precedence
  `user > llm > derived` is already enforced by the store in one statement (`TITLE_RANK`), so a user rename that
  lands during the call wins.
- **Collisions.** `title_candidates` appends `#2`, `#3`, … The cap of `#9` is removed, because a session must not be
  left untitled (the case that `runner.py` currently leaves open). The derived title keeps its 48-character limit
  and the model title its own 100.
- **Failure.** Bad JSON, an empty title or any provider error keeps the derived title.
- **Event.** No new event. A client reads the title from `GET …/sessions`.

As built (2026-10-13): guards reject a title over 12 words, an echo of an example, or the friendly greeting.

## 4. Memory and background review

### Data

```sql
memories (profile_id TEXT, target TEXT, entries TEXT, updated_at REAL, PRIMARY KEY (profile_id, target))
```

`target` is `memory` or `user`. `entries` is a JSON list of strings. Limits are 2,200 and 1,375 characters of
joined entries (Hermes's limits, arch §7), configurable in `KEEL_MEMORY_CHARS` and `KEEL_USER_CHARS`. A write is
a read-modify-write in one transaction with a row lock on Postgres, because two sessions of one profile can write
at once. `sessions` gains `turns_since_memory INTEGER NOT NULL DEFAULT 0`.

### Opt-in

`profiles` gains `memory` (boolean, default false), set by `PUT /v1/profiles/{p}`. When false there is no memory
tool, no guidance, no snapshot and no review.

### The `memory` tool

Hermes's schema from `tools/memory_tool.py` (pinned commit), stored as `src/keel/prompts/tools/memory.json`, with
actions add, replace and remove. A write is refused, as an error result, when it matches the secret patterns already
used for previews (`tools/base.py`), matches an injection pattern adapted from Hermes, or would pass the limit. The
result reports the current usage so the model can make room.

### Offering tools without a sandbox

Today `tools = prompts.tool_definitions() if ctx is not None else ()`. M3 changes `tool_definitions` to take names.
The sandbox tools are offered when the profile has a sandbox. `memory` is offered when `profile.memory` is true.
`ToolContext` gains `store` and `profile_id`, used only by `memory`. A run with neither gets no tools, as today.

### Prompt

Memory guidance joins the stable tier (`prompts/guidance.md`, Hermes text). The volatile tier gets a snapshot of
both targets taken when the session's prompt is built. The prompt is stored on the session, so a write during the
session never changes its cached prefix. Writes appear in the next session or after compaction (§5).

### Background review

- **Trigger.** After a run completes, `turns_since_memory` is incremented. The `memory` tool resets it to 0. At 10
  (`KEEL_MEMORY_REVIEW_TURNS`) a review task is spawned (§2) and the counter is reset.
- **Request.** The same provider, model and system prompt, and the session's messages as the prefix so the call
  hits the cache. Hermes's memory-review prompt (`agent/background_review.py`) is the final user message. Only the
  `memory` tool is offered, with an iteration cap of 4.
- **Isolation.** Nothing is written to the session's messages. The review only changes `memories`. A review is
  skipped when one is already running for that profile.

As built (2026-10-13): memory guidance is its own file (`prompts/memory.md`), included only when memory is on, and
the prompt rebuild is async. Omitting `memory` on `PUT` stores false (`null` is 400). A write may be a batch of
operations. Five injection patterns are refused.

## 5. Context compression, in place

### Why not a child session

Arch §8 puts the summary in a child session. Here a run is tied to `session_id` at creation, the in-flight index
is keyed on it, the cache scope is the session, and clients hold the id. Switching sessions mid-conversation would
break all four. M3 keeps one session and records the compaction on it.

### Stored state

`sessions` gains `summary TEXT`, `summary_head_id` and `summary_tail_id`. No message is deleted or deactivated, so
`GET …/messages` still returns the whole transcript and its paging is unchanged. History sent to the model is:

1. stored messages with id up to `summary_head_id`;
2. one user message holding the summary (the prefix from Hermes's `SUMMARY_PREFIX`);
3. stored messages with id from `summary_tail_id`.

With no summary, history is every message as today. A second compaction summarizes the previous summary together
with the new middle and moves `summary_tail_id` forward.

`sessions` also gains `context_tokens` and `context_message_id`: the `input_tokens` the provider reported for the
session's last model call, and the id of the newest stored message that call included. They anchor the estimate
below across turns, and are cleared at a compaction. `compaction_failed_at` records the last failed summary call.

`pruned_id` (migration 3) is the placeholder boundary: stored messages below it, other than the head, are sent with
the placeholder pass. It moves only when a trigger fires, so what is sent, and its cached prefix, stay the same
between triggers. A compaction sets it to the tail start.

### New `src/keel/compress.py`

Pure functions, unit-tested without a model: token estimate, boundary choice, history assembly, the placeholder
pass and the summary input.

- **Estimate.** `context_tokens` plus a heuristic count of the messages newer than `context_message_id`, so a large
  tool result appended since the last call is counted before the next one. The heuristic counts an ASCII character
  as a quarter of a token and any other character as one token (CJK text is close to one token per character, and
  dividing all text by 4 would undercount it several times), plus a fixed cost per image. With no anchor (a new
  session, a provider that reports no usage, or just after a compaction) the whole request is counted this way. The
  loop updates the anchor after every main call, except one whose newest round was cut to the tail budget (later
  calls send that round whole, so the provider's count would be too low); a summary call never updates it.
- **Head.** The first 3 messages, extended forward until every tool call among them has its results. The head is
  sent on every call and never summarized, so in the request each head message over 4k tokens (a client may paste a
  whole file into the first message) is cut head-and-tail with the truncation marker once the session has a summary
  (a short session is sent whole). The same pass caps each tool call's arguments at about 4k tokens, kept valid
  JSON, replaces images by `[image omitted from context]` and drops reasoning. Nothing stored changes. The head is
  never given the placeholder pass.
- **Tail.** A recent tail of about 2.5% of the window, clamped to 10k–25k tokens, starting at a user or assistant
  message. A boundary never separates an assistant tool call from its results. The tail always holds at least the
  newest assistant message with all its results (or the newest user message when that is last). Whenever the
  session has a summary or a placeholder boundary, the newest round's tool results are cut head-and-tail in the
  request (the existing truncation marker) until the round fits the tail budget; nothing stored changes.
- **Placeholder pass.** Outside the tail, in the request only and with no model call, each tool result over 200
  characters is replaced by a one-line placeholder naming the tool and the size it had, and each image part becomes
  the text `[image omitted from context]`. Nothing stored changes.
- **Summary input.** The previous summary in full except its "Files referenced" section (keel wrote it and carries
  it over itself, below), then the middle (messages after the head and before the tail)
  after the placeholder pass, serialized as text. When that is over 40k tokens, or half the context window if that
  is smaller, the middle is cut head-and-tail to fit, with a marker saying how much was left out, so the summary
  call itself always fits the window.
- **Files referenced.** File paths are collected from the middle without a model: the `path` argument of
  `read_file`, `write_file` and `patch` calls, and absolute paths that appear in user messages (an attachment's
  location usually arrives this way) and in `terminal` and `execute_code` arguments. A path with a newline or over
  1,024 characters is skipped, and each path is redacted like the summary input. The list carries over the previous
  summary's list, is deduplicated with a path named again moving to the newest end, and is capped at 50 entries
  (the most recently named are kept). It
  is appended to the model's summary verbatim under a "Files referenced" heading, so a long path is never shortened
  or misspelt by the summary call and the model can still reopen an attachment or a file it wrote. The path pattern
  runs on model-controlled text, so it is linear-time and gets a long-input timing test.

### When it runs

In `RunExecutor._loop`, before each model call, when the estimate reaches `KEEL_COMPRESSION_THRESHOLD` (default
0.75) of `Capabilities.context_window`. Also when a call fails with `context_overflow` before any output, once per
call. The steps:

1. Apply the placeholder pass. If the estimate is now below the threshold, send the request and stop here (this
   stores `pruned_id` when it moved). The heuristic estimate after the pass is scaled by how much more the provider
   counted than the heuristic for the same request before it (never scaled down), so a provider that counts code
   or JSON high does not prune on every call without ever summarizing. After a `context_overflow` this shortcut is
   skipped: the estimate was wrong, so it cannot show the pass was enough.
2. Otherwise make one summary call, with the same provider and model, no tools, and Hermes's checkpoint prompt and
   sections (the user's latest unfulfilled request verbatim, goal, completed actions, active state, decisions,
   errors and fixes) applied to the summary input. The call runs inside the run, so stop and shutdown already
   cancel it.
3. Append the files referenced to the summary, then store `summary`, `summary_head_id`, `summary_tail_id`, and
   clear the estimate anchor.
4. Rebuild the system prompt, so the skills index, date and memory snapshot are fresh, with a new store method
   that overwrites `sessions.system_prompt`. `set_session_prompt` stays first-writer-wins.
5. Reassemble the run's in-memory conversation from the store, so the rest of the run (and a compaction in the
   middle of a run) sends head, summary and tail. Reasoning items on messages before the tail are dropped.
6. Emit `context.compacted` with message counts and token estimates before and after, and the summary call's usage.

**Usage.** The summary call's usage is added to the run's usage, so `run.completed` reports what the run cost. The
`context.compacted` event carries it separately, so a client can tell the two apart.

**Failure.** If the summary call fails, or its summary is cut off at the output limit (a truncated summary is never
stored), the failure is logged, `compaction_failed_at` is set, and the run continues with the placeholders
(`pruned_id` is stored). For 10 minutes after a failure the threshold trigger applies only the placeholder pass and
makes no summary call, so a broken summary does not cost every turn a failed call. The `context_overflow` trigger
ignores this pause and tries once, since the alternative is failing the run. A persisting overflow fails the run with
`context_overflow` as it does today.

### Cache

The cache key is the hash of scope, system and tools. It changes at a compaction, because the system prompt is
rebuilt, and is stable between compactions. A test asserts both. The invariant "a session's system prompt is built
once and reused byte for byte" becomes "reused byte for byte between compactions".

## 6. Long-session harness

- **`tests/test_long_session.py`**, in normal CI. A scripted `FakeProvider` with a small `context_window` drives
  many turns through the HTTP API. It asserts compactions fired, every transcript pairs calls with results, the
  system prompt and tool list are identical between compactions, the memory snapshot refreshes after a compaction (added with memory),
  and the full transcript is still readable. Separate cases cover the edges of §5:
  - a compaction in the middle of a run, between two tool calls;
  - a newest tool round larger than the tail budget, which is cut in the request and left whole in the store;
  - a middle larger than the summary input limit;
  - a compaction triggered by `context_overflow` rather than the estimate;
  - a session with images outside the tail;
  - an attachment path given in an early user message and a file written by a tool, both summarized away, still
    listed verbatim under "Files referenced" after two compactions;
  - a first user message far over the head limit, cut in the request and left whole in the store;
  - mostly-CJK text, so the estimate triggers before the provider overflows;
  - a failing summary call, then the 10-minute pause, then a successful compaction;
  - the run's usage including the summary call.
- **`tests/test_long_session_live.py`**, skipped unless `KEEL_LIVE_BEDROCK=1`, like the other live tests. It plants facts early in a
  long session, forces compactions, and asks for the facts back.

## 7. Data model changes

The reliability, memory and compression changes are migrations 2 and 3 (§1). Tenant ownership uses a subsequent
numbered migration and explicit backfill (§12); existing migrations are not rewritten. No data is discarded.

| Table | Change |
|---|---|
| `schema_version` | new, one row |
| `profiles` | adds `memory` (default false) |
| `tenants` | new tenant identity table (§12); no natural-person identity or membership directory |
| `profiles.tenant_id` | required, indexed tenant ownership after the explicit backfill (§12) |
| `sessions` | adds `summary`, `summary_head_id`, `summary_tail_id`, `pruned_id`, `context_tokens`, `context_message_id`, `compaction_failed_at` (all nullable), `turns_since_memory` (default 0) |
| `memories` | new (§4) |
| `runs` | `current_exec_id` is now read and cleared |
| `run_events` | rows of runs finished more than `KEEL_EVENT_RETENTION_DAYS` ago are deleted (§1) |

## 8. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `KEEL_COMPRESSION_THRESHOLD` | 0.75 | Fraction of the context window at which a session is compacted |
| `KEEL_MEMORY_REVIEW_TURNS` | 10 | Completed runs between background memory reviews |
| `KEEL_MEMORY_CHARS` | 2200 | Limit for the `memory` target |
| `KEEL_USER_CHARS` | 1375 | Limit for the `user` target |
| `KEEL_BACKGROUND_LIMIT` | 4 | Concurrent background model calls per instance |
| `KEEL_EVENT_RETENTION_DAYS` | 7 | Days a finished run's events are kept |

## 9. API changes

- `PUT /v1/profiles/{p}` accepts `memory` (boolean).
- `title_source` on sessions can now be `llm`.
- New event `context.compacted`: `messages_before`, `messages_after`, `tokens_before`, `tokens_after`, `usage` (the
  summary call's). It is not terminal; a client that does not know it ignores it.
- The events of a run finished more than `KEEL_EVENT_RETENTION_DAYS` ago are gone; its stream sends nothing.
- Existing profile route paths remain; their authentication and ownership checks change as specified in §12.
  A read-only `GET /v1/profiles/{p}/memory` is easy to add if wanted and is left out of M3.

## 10. Testing

- **Unit tests** cover `compress.py`, the memory store and tool, the background owner, and title parsing, all
  against the fake provider and in-memory store.
- **Fault injection** is §1. The Docker and subprocess cases are opt-in.
- **Postgres** (`KEEL_TEST_POSTGRES_DSN`) covers the listener reconnect by terminating the listening backend,
  concurrent memory writers, and two instances migrating one database at once.
- **Migrations** upgrade a frozen 0.2.0 database on both databases (§1); a database newer than the code is refused.
- **Event pruning** deletes only events of runs finished before the cutoff, in batches, and leaves runs and
  messages alone.
- **Long session** is §6.
- **Tenant and profile isolation** is §12, including API authorization, background work, real sandbox isolation,
  consumer account authorization, and migration/cutover checks.

## 11. Risks

- **Summary quality.** A weak summary loses facts. Mitigation: Hermes's checkpoint prompt is taken as written, the
  latest user request is kept verbatim, and the live script checks recall.
- **Token estimates.** Providers may omit usage, images are not counted by characters, and the heuristic is rough.
  Mitigation: the estimate is anchored to the provider's last reported count, CJK text is counted per character, the
  `context_overflow` path is a second trigger, and the threshold leaves 25% of the window as margin.
- **Memory and prompt injection.** A page the model reads could try to write instructions into memory. Mitigation:
  the injection scan, opt-in per profile, and the volatile tier labelling memory as data about the user.
- **Cost.** Titles, reviews and summaries are extra model calls. Mitigation: titles run once per session, reviews
  every 10 runs, the concurrency limit, and usage of background calls is logged separately.
- **A migration that fails halfway.** Each migration runs in one transaction with its version bump, so a failure
  leaves the database at the previous version and the service refuses to start with the error. The release note
  still advises a database backup before upgrading.
- **An incorrect ownership mapping or an old privileged caller.** An inferred tenant assignment, an unrestricted
  service key, or an old instance can bypass the intended boundary. Mitigation: explicit backfill, scoped
  credentials, refusal to serve incomplete ownership, and a coordinated cutover (§12).

## 12. Tenant ownership and independent profiles

### Identity and scope

A tenant is the ownership and authorization boundary. A profile is the independent account's runtime and data
boundary within that tenant. One tenant contains many profiles; each profile belongs to exactly one tenant.
The same natural person may hold accounts in several tenants, but those accounts have different profiles and
identities. keel does not store or infer a link between them, including from a shared email address or external
user identifier.

```text
tenant A -> account profile A -> sessions, runs, memory, sandbox and private files
tenant B -> account profile B -> sessions, runs, memory, sandbox and private files
```

Profiles in the same tenant are independent too. There is no tenant-wide memory, shared knowledge store or shared
user-file directory. Public model configuration, base sandbox images and read-only deployment skills packaged in
those images may be reused; they must contain no profile's private state. Profile ownership is immutable through
the runtime API.
Changing tenant means using another profile; it never continues the old identity's session or reassigns its data.

### Consumer and runtime responsibilities

The consumer authenticates the end user, manages tenant membership and account permissions, and resolves the
authorized `(tenant, profile)` before calling keel. It must not accept an arbitrary profile from a client without
checking that account's access. Switching tenants selects that tenant's account profile.

keel authenticates its service caller and enforces the tenant ownership of every requested profile, followed by
the profile ownership of each session or run. Authentication to a tenant does not by itself identify an end user:
the trusted consumer remains responsible for account authorization inside that tenant. Its adapter checks are
part of the integration gate; a tenant-scoped service caller is deliberately trusted to serve that tenant's
profiles. End users do not receive these service credentials.

For M3, deployment-managed service credentials each resolve to one tenant. The deployment provisions the tenant
and credential binding; no tenant-management HTTP API or end-user login system is required. The existing Bearer
transport and `/v1/profiles/{p}/...` paths remain. Tenant context comes from the verified credential binding,
not from an unsigned tenant header, request-body field, profile prefix or model-generated tool argument.
There is no unrestricted all-tenant credential on these runtime routes. Profile creation assigns the caller's
tenant; updating an existing profile cannot change its tenant.

Missing or invalid credentials return 401. A valid caller requesting a profile outside its tenant, or a session
or run outside the selected profile, receives the same 404 as a missing resource, with no resource data or side
effects. Apply these checks to profile updates, lists, reads, message boundaries, history pagination, rename,
delete, stop, and SSE replay before any event is sent. Referenced blobs must belong to the authorized session and
profile. IDs are locators, never evidence of permission.

### Stored ownership and execution

Add `tenants` with a signed 32-bit integer primary key (any value from −2³¹ to 2³¹−1, including 0, so a consumer's own
tenant IDs are used unchanged) and `profiles.tenant_id` of the same integer type referencing it, with an index for
tenant-scoped lookups. Retain globally unique internal profile and session IDs; adding tenant ownership does not
silently make existing IDs tenant-local. If a consumer has tenant-local account or conversation IDs, its adapter maps
the full scoped identity to distinct keel IDs. A naming convention alone is not authorization.

Sessions and runs retain their profile ownership; messages, events and blobs are reached through that ownership
chain. Memory remains keyed by profile. Every API-to-store operation uses validated ownership, and all resource
references must agree with the parent profile. Jobs load their scope from persisted ownership rather than an
unverified caller field. The executor, title generation, memory review, compression and recovery must preserve
that scope after the HTTP request ends. Internal maintenance can scan multiple tenants, but each action must use
the persisted resource's own profile and sandbox.

Keep containers, writable directories, mounts, tool credentials and any private caches separate per profile, even
within one tenant. Provisioning must not bind two profiles to the same host directory, including read-only mounts,
or give one profile credentials capable of reading another's private data. Container naming alone does not enforce
this. A bind mount is permitted only under the deployment-owned `KEEL_SANDBOX_MOUNT_ROOT/<sha256(profile_id)>`;
without the root setting, bind mounts are refused. The operator creates and protects each profile directory. A
profile cannot mount another profile's directory or both a host directory and its descendant. The source is checked
again when a container is created, so a path retargeted after `PUT` cannot expose another profile. Once a profile
has mounts, its mount list cannot be changed through `PUT`: an older live container could
otherwise retain a removed mount while another profile claims the path. Public skills may be packaged into the
shared image instead of a shared host mount. Infrastructure model
provider credentials may remain service-wide; they are not exposed as sandbox tool credentials. Tenant ownership
does not change the existing sandbox hardening or the requirement for deployment-enforced network access policy.

### Migration and compatibility

Use the next numbered migration after those already present. Preserve existing profile and session IDs, messages,
runs, blobs, memory, and their sandbox/file associations. Do not edit migrations 1–3 or recreate the database.

For an existing database, require an explicit operator-provided assignment of every existing profile to an integer tenant ID.
A single-tenant installation may explicitly assign all profiles to one named tenant; never infer assignments from
an email, an ID prefix or the natural person behind an account. Validate coverage, tenant references and conflicting
assignments before applying the backfill transaction. An incomplete or conflicting mapping leaves the database
unchanged and the service refuses to serve it. Fresh databases require ownership from the first profile creation.
Enforce non-null ownership and referential integrity on both SQLite and Postgres after backfill.

Drain in-flight runs and background work, stop old instances, back up the database, apply the migration and
credential bindings, update the consumer adapter, then start only tenant-aware instances. Old instances with a
global service key must not run alongside the tenant-aware service. A legacy service key may be retained only when
explicitly bound to one tenant, with the same checks as every other scoped credential. Omitting scope never grants
access to all tenants. A binary downgrade is not a rollback procedure; use the tested backup/restore procedure
and reconcile any new state before reverting traffic.

### Acceptance gate

The following are required M3 checks, not optional follow-up work:

| Scenario | Required result |
|---|---|
| Tenant A credential names tenant B's profile, session, run or event stream | 404, no data, events or mutation; profile upsert cannot overwrite B's profile |
| Missing/invalid credential or a forged tenant field/header | No tenant impersonation; only a valid credential binding determines scope |
| One person has accounts in tenants A and B, with matching consumer-local identifiers | Distinct keel identities; no shared state and no automatic cross-tenant session continuation |
| Two profiles in the same tenant use the same titles, filenames and memory facts | Each sees only its own session state, memory, private files and tool credentials |
| A session/run/blob ID from another profile is supplied under an authorized profile | Rejected without reading, changing, stopping or streaming the other resource |
| Consumer user A selects user B's profile in the same tenant | The consumer rejects the request before calling keel; integration checks cover every user-facing entry point |
| Background review, titles, compaction and recovery run concurrently for several profiles | Each reads and writes only its persisted profile's resources, including after restart |
| Real Docker sandboxes for two profiles, including profiles in one tenant | Private file markers and tool credentials are inaccessible from the other profile; private mounts do not overlap |
| An old database is upgraded on SQLite and Postgres | Explicit ownership is complete; all prior data and IDs survive; fresh and upgraded schemas agree |
| Backfill is incomplete/conflicting, or a scoped caller attempts to reassign a profile | No partial assignment or ownership transfer; invalid migration input prevents service startup |

API/store checks belong in normal CI using the fake provider; database cases also run in the Postgres suite and
the real sandbox cases in the Docker suite. The consumer owns account/membership integration checks, just as it
owns the quality gate (§6). Release validation includes these integration and Docker checks even though local
default tests do not require those services.

### Rollout and deferred work

Tenant isolation is part of M3, completed before the first real-user pilot. After M3 acceptance, start with one
profile in one tenant, keep that profile on keel for complete conversations and subsequent sessions, and expand
by profile. Isolation tests still require at least two tenants and multiple profiles; one pilot account cannot
demonstrate the boundary. Do not randomly split an account's requests between runtimes with divergent state.

Tenant-level quotas, fair scheduling, usage reporting and billing can follow based on operational needs. M3 keeps
the existing global capacity controls and per-sandbox limits and does not claim tenant-level resource fairness.
Shared files, shared knowledge, identity federation and cross-tenant account linking are not part of this design.

## Acceptance record

Accepted 2026-10-24 against the gate at the top of this document.

| Gate | Evidence |
|---|---|
| Fault injection | `tests/test_fault_injection.py` (killed worker, deleted sandbox, provider 5xx; the next turn works) in CI on SQLite and Postgres. The first consumer also killed keel mid-command against a live model: the run failed with a user-facing message, the session stayed usable and the command was killed. |
| Long session, scripted | `tests/test_long_session.py` in CI, covering every edge case listed in §6. |
| Long session, live | `tests/test_long_session_live.py` with `openai.gpt-6-luna` passed: a summary compaction fired (19 → 10 messages, about 13.2k → 9.2k tokens) and the facts planted in turns 3 and 4 were recalled after it. Per turn, the prompt cache read the whole previous prefix before the compaction (2.3k → 8.5k tokens read) and 99% of the prompt on the first turn after it (6.9k of 7.0k), so the rebuilt prefix is stable. |
| Consumer checks | The first consumer re-ran its contract checks and quality eval as same-window pairs against its previous runtime and passed its gate before and after its cutover. |
| Tenant and profile isolation | The §12 API and store checks run in CI with the fake provider, on SQLite and Postgres; the real-sandbox checks run in the Docker suite. The first consumer's account-authorization checks pass in its own repository. |

The deferred items in `CLAUDE.md` (full image replay on later turns, the tool-less final call's cache miss, a
unique index on tool rows, command cleanup at shutdown) are not part of this gate.
