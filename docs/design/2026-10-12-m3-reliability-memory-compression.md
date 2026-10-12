# M3: reliability, titles, memory and compression — design

Parent design: `docs/design/2026-10-11-architecture.md` (sections are cited as "arch §N"). Previous milestone:
`docs/design/2026-10-12-m2-agent-loop.md`. M2 made a run an agent loop in a sandbox. M3 makes long-lived
sessions and multi-instance deployments safe: faults are survived and tested, sessions get model titles and
long-term memory, and a conversation can outgrow the model's context window.

Status: draft.

## Goal and gate

M3 is done when all of these hold:

- **Fault injection passes.** A killed worker, a deleted sandbox and provider 5xx errors each leave the session
  consistent, and the next turn works (§1).
- **A long session survives.** A scripted session many times longer than the context window finishes with valid
  transcripts, compactions that fired, and a stable cached prefix between them (§5, §6).
- **The consumer's own checks still pass.** The M2 contract checks and quality eval are re-run unchanged. M3
  features are off or invisible to a consumer that does not use them.

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
| Hermes history import | M4 | M4 | Unchanged. |

Still out of M3: parallel read-only tools, scheduled jobs, approvals, web tools, the Anthropic provider and the
Hermes import.

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
- **Orphan removal at start.** `remove_orphans` removes every container carrying this deployment's label, whether or
  not a run uses it. M3 asks the store for profiles with an in-flight run and leaves those containers alone. With the
  reconnect, this removes the single-instance caveat from the README.
- **A dead worker's command.** `runs.current_exec_id` is written and never read. When the sweeper marks a run
  `worker_lost`, it makes a best-effort `kill_tree` on that exec (`ensure`, then `kill_tree`), so a command started
  by a dead worker does not run on. The column is cleared when a command ends. Failure to reach the sandbox is
  logged and does not block the sweep.
- **Schema guard.** M3 adds columns and a table, and `CREATE TABLE IF NOT EXISTS` does not add columns to an old
  table. A one-row `schema_version` table is written on first start. A database with tables but no version, or an
  older one, makes the service refuse to start with a message that says to recreate it. There are still no
  migrations (README).

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

The Postgres CI job gains these files, and the table drop list in `tests/conftest.py` gains the new tables.

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

### New `src/keel/compress.py`

Pure functions, unit-tested without a model: token estimate, boundary choice, history assembly and the
placeholder pass.

- **Boundaries.** Keep the first 3 messages and a recent tail of about 2.5% of the window, clamped to 10k–25k
  tokens. A boundary never separates an assistant tool call from its results, and the tail starts at a user or
  assistant message.
- **Estimate.** The last call's `input_tokens` when the provider reported it. Otherwise characters divided by 4 plus
  a fixed cost per image. `state.usage` is summed over a run, so the loop also keeps the last call's value.
- **Placeholder pass.** Old tool outputs are replaced by a short placeholder in the request only, with no model
  call. Nothing stored changes.

### When it runs

In `RunExecutor._loop`, before the model call, when the estimate reaches `KEEL_COMPRESSION_THRESHOLD` (default
0.75) of `Capabilities.context_window`. Also when a call fails with `context_overflow` before any output, once per
call. The steps:

1. Apply the placeholder pass. If the estimate is now below the threshold, stop here.
2. Otherwise make one summary call, with the same provider and model, no tools, and Hermes's checkpoint prompt and
   sections (the user's latest unfulfilled request verbatim, goal, completed actions, active state, decisions,
   errors and fixes). The call runs inside the run, so stop and shutdown already cancel it.
3. Store `summary`, `summary_head_id`, `summary_tail_id`.
4. Rebuild the system prompt, so the skills index, date and memory snapshot are fresh, with a new store method
   that overwrites `sessions.system_prompt`. `set_session_prompt` stays first-writer-wins.
5. Drop reasoning items from messages before the tail when assembling history.
6. Emit `context.compacted` with message counts and token estimates before and after.

If the summary call fails, the failure is logged, the run continues with the placeholders, and a persisting
overflow fails the run with `context_overflow` as it does today.

### Cache

The cache key is the hash of scope, system and tools. It changes at a compaction, because the system prompt is
rebuilt, and is stable between compactions. A test asserts both.

## 6. Long-session harness

- **`tests/test_long_session.py`**, in normal CI. A scripted `FakeProvider` with a small `context_window` drives
  many turns through the HTTP API. It asserts compactions fired, every transcript pairs calls with results, the
  system prompt and tool list are identical between compactions, the memory snapshot refreshes after a compaction,
  and the full transcript is still readable.
- **`scripts/long_session_live.py`**, opt-in with the existing `KEEL_LIVE_*` variables. It plants facts early in a
  long session, forces compactions, and asks for the facts back.

## 7. Data model changes

M3 needs a fresh database (§1, schema guard).

| Table | Change |
|---|---|
| `schema_version` | new, one row |
| `profiles` | adds `memory` |
| `sessions` | adds `summary`, `summary_head_id`, `summary_tail_id`, `turns_since_memory` |
| `memories` | new (§4) |
| `runs` | `current_exec_id` is now read and cleared |

## 8. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `KEEL_COMPRESSION_THRESHOLD` | 0.75 | Fraction of the context window at which a session is compacted |
| `KEEL_MEMORY_REVIEW_TURNS` | 10 | Completed runs between background memory reviews |
| `KEEL_MEMORY_CHARS` | 2200 | Limit for the `memory` target |
| `KEEL_USER_CHARS` | 1375 | Limit for the `user` target |
| `KEEL_BACKGROUND_LIMIT` | 4 | Concurrent background model calls per instance |

## 9. API changes

- `PUT /v1/profiles/{p}` accepts `memory` (boolean).
- `title_source` on sessions can now be `llm`.
- New event `context.compacted`: `messages_before`, `messages_after`, `tokens_before`, `tokens_after`.
- No new routes. A read-only `GET /v1/profiles/{p}/memory` is easy to add if wanted and is left out of M3.

## 10. Testing

- **Unit tests** cover `compress.py`, the memory store and tool, the background owner, and title parsing, all
  against the fake provider and in-memory store.
- **Fault injection** is §1. The Docker and subprocess cases are opt-in.
- **Postgres** (`KEEL_TEST_POSTGRES_DSN`) covers the listener reconnect by terminating the listening backend, and
  concurrent memory writers.
- **Long session** is §6.

## 11. Risks

- **Summary quality.** A weak summary loses facts. Mitigation: Hermes's checkpoint prompt is taken as written, the
  latest user request is kept verbatim, and the live script checks recall.
- **Token estimates.** Providers may omit usage and images are not counted by characters. Mitigation: the
  `context_overflow` path is a second trigger, and the threshold leaves 25% of the window as margin.
- **Memory and prompt injection.** A page the model reads could try to write instructions into memory. Mitigation:
  the injection scan, opt-in per profile, and the volatile tier labelling memory as data about the user.
- **Cost.** Titles, reviews and summaries are extra model calls. Mitigation: titles run once per session, reviews
  every 10 runs, the concurrency limit, and usage of background calls is logged separately.
- **A fresh database again.** Existing deployments must recreate theirs. The release note says so and the schema
  guard turns a silent failure into a clear one.
