# agent-keel

A server-side, multi-tenant agent runtime: an HTTP + SSE API, runs queued in PostgreSQL in every environment, an agent loop with eight tools in a per-profile Docker sandbox, skills, and pluggable model providers
(Bedrock Mantle over the OpenAI Responses API, and for `anthropic.` ids the Anthropic Messages API;
OpenAI-compatible over the Responses API). Public, MIT-licensed open-source project
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
- Pushing, opening or merging a PR, tagging and anything that publishes (GitHub releases, the GHCR image) needs
  the owner's explicit go-ahead for that change. With it, use `git push` and `gh`; merge a PR only after its CI
  passes, with a merge commit (`gh pr merge --merge`).

## Develop

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
docker compose -f docker-compose.test.yml up -d --wait
KEEL_TEST_POSTGRES_DSN=postgresql://keel:keel_test@127.0.0.1:55437/keel_test .venv/bin/python -m pytest -q
docker compose -f docker-compose.test.yml down -v
```

Integration tests require PostgreSQL and `KEEL_TEST_POSTGRES_DSN`; missing infrastructure fails them. Each test
creates/drops a private database (role needs CREATEDB), leaving the base database untouched. For pure unit tests
without PostgreSQL, run `.venv/bin/python -m pytest -q -m 'not integration'`. Opt-in suites:

- `KEEL_TEST_DOCKER=1` — Docker sandbox tests (image `KEEL_TEST_DOCKER_IMAGE`, default `python:3.12-slim`). Leave no
  containers labelled `keel.managed=1` behind.
- `KEEL_TEST_SUBPROCESS=1` — server crash/restart fault injection against PostgreSQL.
- `KEEL_LIVE_BEDROCK=1`, `KEEL_LIVE_OPENAI=1` — call a real model. Use only the model ids the tests name
  (`KEEL_LIVE_MODEL`, `KEEL_LIVE_CLAUDE_MODEL`, `KEEL_LIVE_OPENAI_MODEL`); never probe other Bedrock model ids or regions.

CI (`.github/workflows/ci.yml`) runs the full suite against PostgreSQL 17, Docker and an image build. A `v*` tag runs
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

- 0.3.0 is released: M3 (`docs/design/2026-10-12-m3-reliability-memory-compression.md`, accepted 2026-10-24, see
  its acceptance record) on top of M1 + M2. It adds numbered schema migrations (older databases upgrade in place),
  context compression in place, run-event pruning, model titles, the opt-in `memory` tool and background review,
  reliability and fault-injection tests, explicit tenant ownership with independent profiles (§12), complete
  profile deletion, the coordinated sandbox lifecycle, and Claude models on the `bedrock` provider
  (`docs/design/2026-10-23-bedrock-anthropic-messages.md`).
- Open work: cross-session memory (`docs/design/2026-10-24-cross-session-memory.md`) and, on top of it,
  procedural learning (`docs/design/2026-10-29-procedural-learning.md`), both approved 2026-10-29. Memory
  entries, the change ledger, memory tool v2 and the memory API (migration 8), the history index,
  `session_search` and per-session tool lists (migration 9), and the durable review queue (migration 10: leased
  background memory reviews, review API) and learned skills (store, `skill_manage`, the `learning` flag and the
  learned-skill API, migration 11) and observations, run feedback and automatic suspension (migration 12) and the
  dream (migration 13: leased profile-level dream jobs with commit gates and the dream routes) are merged
  (`[Unreleased]`), and cross-session memory was accepted 2026-10-30 (see its acceptance record); next is learning
  acceptance.
- M4 (history import and cutover) is consumer-specific.
- Deferred items worth picking up next: images are replayed in full on later turns; the final iteration-cap call
  sends no tools (one cache miss); a unique index on (session, tool_call_id) for tool rows; shutdown
  hands a running command's cleanup to the executor's cancel path; finished memory review jobs are never pruned, and
  the review queue scans `memory_reviews` without a dirty-row index.
