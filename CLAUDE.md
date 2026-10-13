# agent-keel

A server-side, multi-tenant agent runtime: an HTTP + SSE API, runs queued in Postgres (SQLite for local use and
tests), an agent loop with eight tools in a per-profile Docker sandbox, skills, and pluggable model providers
(Bedrock Mantle and OpenAI-compatible, both over the OpenAI Responses API). Public, MIT-licensed open-source project
at https://github.com/ryang420/agent-keel.

Start with `README.md`, then `docs/design/2026-10-11-architecture.md` (the whole design; its §13 lists the
milestones) and `docs/design/2026-10-12-m2-agent-loop.md` (the agent loop, tools and sandbox as built).

## This is a public repository

- Write nothing about any organisation, employer, customer or deployment: no names, hostnames, account ids, regions
  tied to a deployment, internal paths, production data or usage figures, and no company email addresses. Refer to
  a client as "a consumer" or "the first consumer", described only by what it needs from keel.
- Commits are authored `Yang Ren <ryang420@gmail.com>` (set in this clone's local git config). Check
  `git config user.email` before the first commit of a session.
- Test fixtures that look like credentials are built from pieces at runtime (`"hunter" + "2"`, `"AKIA" + "…"`), so
  secret scanners stay quiet. Never commit a real credential, and never print credentials or environment dumps.
- Pushing, tagging and anything that publishes (GitHub releases, the GHCR image) needs the owner's go-ahead. The
  owner pushes; this machine has no GitHub credentials (`! git push` from the prompt).

## Develop

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

The default run needs no Docker, database or network. Opt-in suites:

- `KEEL_TEST_DOCKER=1` — Docker sandbox tests (image `KEEL_TEST_DOCKER_IMAGE`, default `python:3.12-slim`). Leave no
  containers labelled `keel.managed=1` behind.
- `KEEL_TEST_POSTGRES_DSN=postgresql://…` — store and event-bus tests on Postgres; drops and recreates keel's tables,
  so use a scratch database (e.g. a throwaway `postgres:16` container).
- `KEEL_LIVE_BEDROCK=1`, `KEEL_LIVE_OPENAI=1` — call a real model. Use only the model ids the tests name
  (`KEEL_LIVE_MODEL`, `KEEL_LIVE_OPENAI_MODEL`); never probe other Bedrock model ids or regions.

CI (`.github/workflows/ci.yml`) runs the default suite, Postgres, Docker and an image build. A `v*` tag runs
`release.yml`: tests, a multi-arch image to GHCR and a GitHub release from `CHANGELOG.md`.

## How work is done here

- **Design first.** A change to behaviour, the API, the schema or the sandbox starts as a design doc in
  `docs/design/YYYY-MM-DD-topic.md` (public). Each milestone gets its own design doc.
- **Plans stay local.** Implementation plans and subagent working notes go in `.superpowers/` (gitignored) and are
  never committed: they are working material, not documentation.
- **Branches.** Never commit on `main`; work on a branch (or a worktree under `.worktrees/`, add it to
  `.gitignore` first) and merge or open a PR when the owner says so.
- **Tests with every change**; a bug fix starts with a failing test. Match the surrounding code (naming, typing,
  comment density, ~120-column lines). Keep modules small and focused.
- **Docs and changelog.** Update `docs/guide/` when user-visible behaviour changes and add a line under
  `[Unreleased]` in `CHANGELOG.md`. Keep `keel.__version__` equal to `pyproject.toml` (a test checks it).
- **Hermes-derived code.** Some files are adapted from hermes-agent (MIT, v2026.9.24 `f97608f178`). Keep their
  header comments and keep `THIRD_PARTY_NOTICES.md` accurate when adapting more.

## Invariants worth protecting

- `RunExecutor.execute` never raises; every run ends in exactly one terminal state and one terminal event.
- Every stored assistant tool call has exactly one tool result before the next user message (cancel cleanup
  reconciles against stored messages; turn start repairs orphans). Providers reject unpaired calls.
- A session's system prompt is reused byte for byte between compactions (a compaction rebuilds it); tools are a
  fixed list. Both keep the prompt cache warm.
- A released schema migration is never edited; a schema change is a new migration.
- The sandbox never touches host paths from keel's side; policy values reach `docker` as separate arguments, env
  values through a private `--env-file`, never on argv. Keep the container hardening defaults.
- Regexes that run on model-controlled text (redaction, front matter, skill parsing) must stay linear-time; add a
  long-input timing test with any new pattern.
- Usage counts: `input_tokens` is the whole prompt including cached parts; an unknown count is `None`, never `0`.

## Status and next milestone

- 0.2.0 (M1 + M2) is released. M3 so far (unreleased): numbered schema migrations, context compression, and
  reliability (LISTEN reconnect, lease-derived database timeout, orphan removal that spares runs in flight, a lost
  worker's command killed, run-event pruning, fault-injection tests). Model-generated titles, the `memory` tool and
  background review are done on this branch.
- **M3** (`docs/design/2026-10-12-m3-reliability-memory-compression.md`) is still open: numbered schema migrations
  (a 0.2.0 database upgrades in place), context compression (at ~75% of the window or on `context_overflow`; the
  summary is kept in place on the same session), run-event pruning, model-generated titles (precedence user > llm >
  derived, `#N` on collision), the `memory` tool with per-session snapshots and background review (this slice is
  done on the branch), plus fault-injection tests (kill a worker, delete a sandbox, provider 5xx). The long-session
  live eval stays opt-in and already exists. Stop and image input were already done in M2.
- Deferred items worth picking up with M3: images are replayed in full on later turns; the final iteration-cap call
  sends no tools (one cache miss); a unique index on (session, tool_call_id) for tool rows; shutdown
  hands a running command's cleanup to the executor's cancel path.
