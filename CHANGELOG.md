# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) (pre-1.0: minor releases may change the API).

## [Unreleased]

## [0.3.2] - 2026-10-31

A patch on 0.3.1: command runs, one command in a profile's sandbox without a model
(`docs/design/2026-10-31-command-runs.md`).

### Added

- Command runs: `POST /v1/profiles/{p}/runs` with `"kind": "command"` and `command: {argv, cwd, timeout_s, env}`
  runs one argv in the profile's sandbox without a model (a profile with no models can start one), under the same
  supervisor, leases, session rule and stop as a model run. Its `output` is the last 50,000 characters of the
  command's output and its usage is zero. Exit 0 completes the run; a non-zero exit, a timeout, an unconfirmed
  cleanup or a missing sandbox fails it as `command_failed`, `command_timed_out`, `sandbox_uncertain` or
  `sandbox_unavailable`. New events `command.started` and `command.completed`; runs report `kind`, and command runs
  their `command`. `KEEL_COMMAND_TIMEOUT_MAX_S` (default 3600) caps `timeout_s`. `argv[0]` may not start with `-`, and a command
  run in a session does not count as a completed run when a later model run's title is decided.

### Changed

- Schema version 8: `runs.kind` (`model` or `command`) and `runs.command`; `runs.provider` and `runs.model` may be
  null for a command run. PostgreSQL alters `runs` in place; SQLite rebuilds it. A database at this version is refused
  by 0.3.1.

## [0.3.1] - 2026-10-31

### Fixed

- Every sandbox command took at least the 3 s kill grace period: after a command ended on its own, the supervisor
  waited the whole grace before its final `KILL` even when the command's process group was already empty. It now
  ends the grace as soon as no live member of the group is left. A member that is still handling `TERM` gets the
  grace as before, and completion is still proven by the child wait, not by the group check.

## [0.3.0] - 2026-10-24

M3 is accepted (`docs/design/2026-10-12-m3-reliability-memory-compression.md`, acceptance record at the end). No
code changes since 0.3.0-rc.3; the entries for 0.3.0-rc.1 to 0.3.0-rc.3 below are the changes since 0.2.0. Highlights:

- Long sessions: numbered schema migrations (a 0.2.0 database upgrades in place), context compression in place on
  the session, run-event pruning, model-generated titles, the opt-in `memory` tool and background review.
- Multi-tenant safety: explicit tenant ownership with tenant-scoped credentials and independent profiles, complete
  profile deletion, and the coordinated sandbox lifecycle.
- Reliability: Postgres listener reconnect, lease recovery, stall limits between model events, fault-injection
  tests.
- Models: `POST /v1/models/verify`, and Claude models on the `bedrock` provider over
  Bedrock Mantle's Anthropic Messages API.

Upgrading: back up the database first; it is migrated at start. Running more than one instance against one Docker
daemon needs PostgreSQL and the stopped-service cutover in the operations guide.

## [0.3.0-rc.3] - 2026-10-24

### Added
- The `bedrock` provider serves Claude models: an id starting with `anthropic.` (for example
  `anthropic.claude-opus-5-5`) goes to Bedrock Mantle's Anthropic Messages API at `/anthropic/v1/messages`, with
  the same region and SigV4 credentials. It caches at `cache_control` breakpoints, replays each turn's original
  content blocks so thinking stays valid, and sends `drop_block` for thinking whose conversation changed (compression,
  compaction, new run instructions). Claude 4.6+ models get adaptive thinking and `reasoning_effort` as effort
  (`none` is sent as `low`). Design: `docs/design/2026-10-23-bedrock-anthropic-messages.md`.
- Complete profile deletion: authenticated `DELETE /v1/profiles/{profile_id}` fences a tenant-owned profile,
  cancels its queued and active runs, retires its sandboxes through verified exact container identities, and purges
  its sessions, messages, runs, events, blobs, memory and profile configuration. A same-operation retry resumes
  persisted progress, and `POST /v1/profiles/{profile_id}/deletion/{operation_id}/complete` releases only that
  operation's fence, with an idempotent completion receipt.
- Coordinator recovery APIs reconcile orphaned usage, stopped and delayed retired creates, and idle generations by
  exact container ID. Recovery preserves valid users and their execution authority, retains unresolved create
  evidence, blocks conflicting inventory until the recorded conflict is resolved, and safely replaces incompatible
  policy/helper generations after their users finish.
- The sandbox coordinator records commands, file/skill helpers and cleanup before dispatch, verifies supervisor
  receipts before finishing executions, and gates cancellation on the original execution and current usage lease.
- Sandboxed runs execute only under a coordinated claim's usage. The worker renews the run lease until the run is
  terminal and the usage lease until the executor's cleanup (including release) returns, both on database time,
  and cancels a run by its last confirmed usage deadline. A failed sandbox use ends the run as
  `sandbox_unavailable` after its tool result is stored, instead of recreating the container mid-run; recovery
  replaces the generation before the next run. Cancellation sends one bounded supervisor control request, with no
  hard-kill retry, and the lease sweeper hands lost runs to coordinator recovery instead of signalling a command.
- The service runs one sandbox coordinator per instance for claims, startup inventory reconciliation and
  minute-by-minute bounded recovery/idle passes, replacing the startup removal of containers by in-flight profile
  list and the process-local idle reaper. `PUT /v1/profiles/{id}` returns 409 `sandbox_busy` when the effective
  sandbox policy would change while the profile has runs in progress or unreleased sandbox work; run enqueue and
  policy updates serialize on the profile row. When Docker or the sandbox helper is unavailable, sandbox runs stay
  queued and chat-only runs proceed. Sandbox admission opens only after the instance has classified every
  container of the deployment (bounded at startup, retried in the background), claims never wait on Docker, and
  the Docker daemon ID is read afresh for every ownership or absence decision: a changed daemon closes admission
  instead of making recorded containers look absent.
- Migration 5 adds the `sandboxes`, `sandbox_uses`, `sandbox_execs` and `sandbox_operations` tables. The maintenance
  pass prunes resolved, unreferenced coordination records older than `KEEL_EVENT_RETENTION_DAYS` in batches of 100;
  unresolved creates, unreleased usages, unresolved execs and current references are retained, and an uncertain
  create is kept as a tombstone indefinitely. The stopped-service cutover, including legacy containers, is in
  [the operations guide](docs/guide/operations.md#upgrading-to-the-coordinated-sandbox-lifecycle).

### Changed
- The Docker backend exposes only exact-ID coordinated operations. The name-based `ensure`/`reference`/`exec`,
  PID-file `kill_tree`, process-local idle reaper and startup `remove_orphans` paths (and their process-local
  state) are gone, so no caller can reach a container without a recorded attempt or ticket. The `LocalSandbox`
  test/dev backend is unchanged.

### Fixed
- Cross-instance background callbacks now carry a durable profile incarnation through memory, title and memory-review
  writes, so work from a deleted profile cannot change a recreated profile that reuses its id or session ids. Old
  deletion receipts cannot block or unblock another incarnation's background work.
- Profile guards acquire PostgreSQL's shared profile advisory lock before the profile row lock, preventing the
  profile update versus deletion purge deadlock while preserving nonblocking claim skips.
- An unknown or legacy container no longer blocks its profile's sandbox runs forever after the operator removes it,
  including on a profile that never had a coordinated sandbox. A block clears only when the container it records
  is proven absent by exact ID on the same daemon (or is still listed but stopped as of the fresh exact-ID
  inspection, where it classified as non-conflicting), and a complete, unbatched listing of the deployment's
  containers, each container of that profile classified again, finds none that conflicts: no unknown container, no
  recorded container whose labels or policy disagree, and no running retired create. A failed listing or
  inspection keeps the block; a container that starts running between the listing and the inspection keeps it too.
- A Docker warning printed on stderr (for example a platform mismatch or missing swap accounting) no longer makes a
  successful container creation, inspection, inventory or daemon identity read uncertain: results are parsed from
  stdout only, and stderr is kept as a bounded diagnostic.

## [0.3.0-rc.2] - 2026-10-18

### Added
- `POST /v1/models/verify` checks a model id with two tiny calls (text, then a 1×1 image) and reports whether
  the model reads images. Any tenant key may call it; it stores nothing ([API guide](docs/guide/api.md)).
- `python -m keel.admin import-sessions` imports conversations copied from another system: JSON lines on stdin, one
  result line per conversation on stdout ([operations guide](docs/guide/operations.md)). A conversation keeps its
  title (numbered if the profile already uses it), times and messages, and never any reasoning. An existing session
  id is skipped, and a missing profile is created empty for its tenant. Each conversation is one transaction, and
  `--dry-run` validates without writing.
- Model-call retries are logged: a warning per retry (call, provider/model, error code and message, attempt,
  delay) and an error when the last attempt fails, so a stalled or failing provider shows up in the service log.

### Fixed
- Run termination is atomic. The terminal state and its `run.completed`, `run.failed` or `run.cancelled` event commit
  in one transaction, exactly one terminal transition can win, and the terminal event is the last event the run
  accepts. Lifecycle changes and event sequence allocation serialise on the run row, so concurrent finishes, stops,
  lease expiries and ordinary event writes cannot reorder or duplicate the terminal event. SSE rereads events once
  the run is terminal instead of waiting out a grace window, so a terminal event that races the stream's first read
  is still delivered; a pruned or historical terminal run closes immediately. No schema migration: all writers must
  cut over together ([operations guide](docs/guide/operations.md)).
- The SQLite store closes every cursor it opens (`BEGIN IMMEDIATE`, the connect-time PRAGMAs, `executescript`). One
  left to the garbage collector was freed on the event loop thread while the connection's worker thread ran another
  statement, which crashed the test suite with a segmentation fault in about 1 of 40 runs. PostgreSQL is not affected.
- A session's model title is written from every user message so far, not only the first completed run's input. A
  conversation whose first turn was stopped or failed was titled from its second turn alone (a bare greeting).
- A stalled model stream now fails as `timeout` and is retried if it had streamed nothing. A stalled Bedrock Mantle
  stream sends keep-alives but no response events; they kept resetting the fixed 600 s read timeout, so a run waited
  until its client gave up. Stalls are now timed between response events (`response.*` or `error`), in two stages:
  - `KEEL_MODEL_FIRST_EVENT_SECONDS` (default 120): until the first response event.
  - `KEEL_MODEL_STALL_SECONDS` (default 60): between response events, raised to 120 s above about 50,000 estimated
    request tokens and 180 s above about 100,000.
  - At `high`, `xhigh` or `max` effort both are at least `KEEL_MODEL_HIGH_EFFORT_STALL_SECONDS` (default 300, Hermes's
    high-effort floor; 600 left no time to retry a stall inside a ten-minute client wait).
  The 600 s byte-level read timeout stays as a backstop for a dead connection.

## [0.3.0-rc.1] - 2026-10-15

### Added
- Tenant-bound service keys with signed 32-bit integer tenant IDs (0 and negatives included) (`KEEL_API_KEYS`, or
  `KEEL_API_KEY` plus `KEEL_API_KEY_TENANT`). Migration 4 records tenant ownership of profiles; an existing database
  needs an explicit `KEEL_TENANT_BACKFILL`. Other tenants' profiles and their sessions, runs and events return 404.
  Image blobs are loaded only for their profile and session; sandbox mounts cannot overlap another profile's mounts
  and cannot be changed once assigned to a profile.
- Background memory review: with memory on, every `KEEL_MEMORY_REVIEW_TURNS` completed runs of a profile, a review
  may save facts. It does not add messages or events to the session. A review already running for that profile is
  skipped, and one dropped because the background limit is full does not block the next.
- `PUT /v1/profiles/{id}` accepts `memory` (a boolean). Omitting it stores false, including on update; `null` is
  400. Memory entries for a profile are written in one transaction. With memory on, the model is offered a `memory`
  tool even when the profile has no sandbox. A write does not change the current session's stored system prompt; it
  appears in the next session's prompt, and when this session is compacted.
- A session's first completed run may replace its derived title with a short model title (`title_source` `llm`,
  between `derived` and `user`). No new event: a client reads the title from the session. A user rename still wins,
  and a reply that is empty, vague, or not a short title keeps the derived one. Numbered suffixes (`#2`, `#3`, …)
  are no longer capped.
- Best-effort background tasks for work that must not belong to a run. `KEEL_BACKGROUND_LIMIT` (4),
  `KEEL_MEMORY_REVIEW_TURNS` (10), `KEEL_MEMORY_CHARS` (2200) and `KEEL_USER_CHARS` (1375) are positive integers,
  read at start. `KEEL_BACKGROUND_LIMIT` caps how many tasks run at once; a task past the limit is dropped and
  logged, and tasks are cancelled only at shutdown.
- Numbered schema migrations: a database is upgraded in place at start, and a 0.2.0 database keeps its data. A
  database written by a newer keel is refused. Migration 2 adds the tables and columns M3 uses.
- Context compression: a session can outgrow the model's context window. At `KEEL_COMPRESSION_THRESHOLD` (0.75)
  of the window, old tool results and images are replaced by placeholders, and if that is not enough the middle of
  the conversation is summarized in place (the stored transcript is unchanged). A new `context.compacted` event
  reports it; the summary call's usage is part of the run's usage. Migration 3 adds `sessions.pruned_id`.
- Run-event pruning: an hourly task deletes the events of runs finished more than `KEEL_EVENT_RETENTION_DAYS` (7)
  days ago; runs and messages are kept. Streaming such a run closes at once.
- Fault-injection tests: a lost worker, provider 5xx, sandbox failures, a vanished container and a killed server.

### Changed
- Bind mounts require `KEEL_SANDBOX_MOUNT_ROOT` and may use only the calling profile's SHA-256-named directory
  beneath it. Sources in one profile cannot overlap, and container creation rechecks host paths. Service startup
  migrates and backfills ownership in one transaction; a rejected backfill leaves the old database unchanged.
- Listing runs or sessions for an unknown profile now returns 404 instead of an empty list.
- `search_files` uses ripgrep when the sandbox image has it, and `grep`/`find` otherwise. Both skip hidden
  directories (before, only `.git`). With ripgrep, gitignored files are skipped too, results are sorted by path, and
  a content search with no matches adds a `hint` when hidden or gitignored files would match.
- The system prompt's date line reads `Current date:` instead of `Conversation started:`; a compaction rebuilds the
  prompt, so the date is the build's.
- Several instances can share one Postgres database: the LISTEN connection reconnects with backoff, start-up
  orphan removal spares the sandboxes of profiles with a run in flight, and the sweeper kills a lost worker's
  command when it shares that worker's Docker daemon (otherwise the command runs until its own timeout).
- Postgres queries time out after a third of `KEEL_LEASE_SECONDS` (at least 5 s) instead of a fixed 30 s; with the
  default 30 s lease that is 10 s.

### Fixed
- The `memory` tool writes only when the profile's memory flag is on. A sandboxed run can still call it when it was
  not offered; that call is an error and stores nothing.
- A background memory review sends the history a model call would: the assembled head, summary and tail, with image
  bytes loaded. Reasoning stays on the review's own assistant message. The stored transcript is unchanged.

## [0.2.0] - 2026-10-12

### Added
- Agent loop: the model calls tools until it answers in text, with an iteration cap (`KEEL_MAX_ITERATIONS`) after
  which one final call without tools asks for a summary.
- Eight tools: `terminal`, `execute_code`, `read_file`, `write_file`, `patch`, `search_files`, `skills_list`,
  `skill_view`.
- Docker sandbox per profile, set by the profile's `sandbox` policy (image, workdir, runtime, network, mounts, env,
  user, CPU, memory and PID limits), with idle cleanup and removal of leftover containers at start.
- Skills: `SKILL.md` directories under the policy's `skills_path`, indexed into the system prompt.
- Image input on runs (PNG, JPEG, GIF, WebP; up to 4 images of 5 MB), stored as blobs.
- Stop: `POST .../runs/{run_id}/stop` ends the run and kills its running command.
- OpenAI-compatible provider (`openai`), configured by `KEEL_OPENAI_API_KEY` (or `OPENAI_API_KEY`),
  `KEEL_OPENAI_BASE_URL`, `KEEL_OPENAI_COMPAT` and `KEEL_OPENAI_CONTEXT_WINDOW`.
- New run error codes `sandbox_unavailable`, and API error code `model_not_vision_capable`.

## [0.1.0] - 2026-10-11

### Added
- The service: HTTP API with bearer-key authentication and a health check.
- Store on SQLite or Postgres, and an event bus (in memory, or Postgres LISTEN/NOTIFY).
- Bedrock Mantle provider (OpenAI Responses API, SigV4 signing).
- Profiles, runs with replayable SSE events, and sessions with messages, titles and deletion.

[Unreleased]: https://github.com/ryang420/agent-keel/compare/v0.3.2...HEAD
[0.3.2]: https://github.com/ryang420/agent-keel/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/ryang420/agent-keel/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/ryang420/agent-keel/compare/v0.2.0...v0.3.0
[0.3.0-rc.3]: https://github.com/ryang420/agent-keel/compare/v0.3.0-rc.2...v0.3.0-rc.3
[0.3.0-rc.2]: https://github.com/ryang420/agent-keel/compare/v0.3.0-rc.1...v0.3.0-rc.2
[0.3.0-rc.1]: https://github.com/ryang420/agent-keel/compare/v0.2.0...v0.3.0-rc.1
[0.2.0]: https://github.com/ryang420/agent-keel/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/ryang420/agent-keel/releases/tag/v0.1.0
