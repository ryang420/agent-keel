# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) (pre-1.0: minor releases may change the API).

## [Unreleased]

## [0.3.0-rc.2] - 2026-10-18

### Added
- Coordinator recovery APIs reconcile orphaned usage, stopped and delayed retired creates, and idle generations by
  exact container ID. Recovery preserves valid users and their execution authority, retains unresolved create
  evidence, blocks conflicting inventory until the recorded conflict is resolved, and safely replaces incompatible
  policy/helper generations after their users finish.
- The sandbox coordinator records commands, file/skill helpers and cleanup before dispatch, verifies supervisor
  receipts before finishing executions, and gates cancellation on the original execution and current usage lease.
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

[Unreleased]: https://github.com/ryang420/agent-keel/compare/v0.3.0-rc.2...HEAD
[0.3.0-rc.2]: https://github.com/ryang420/agent-keel/compare/v0.3.0-rc.1...v0.3.0-rc.2
[0.3.0-rc.1]: https://github.com/ryang420/agent-keel/compare/v0.2.0...v0.3.0-rc.1
[0.2.0]: https://github.com/ryang420/agent-keel/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/ryang420/agent-keel/releases/tag/v0.1.0
