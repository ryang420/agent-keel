# agent-keel

[![CI](https://github.com/ryang420/agent-keel/actions/workflows/ci.yml/badge.svg)](https://github.com/ryang420/agent-keel/actions/workflows/ci.yml)

A server-side, multi-tenant agent runtime. You send it a message over HTTP; it runs a model-and-tools loop in a
Docker sandbox and streams the result back as server-sent events.

It is **not** a command-line coding agent and **not** a chat UI. It is the service you put behind your own
product, where many users (profiles) each get their own sandbox, sessions and skills.

## Why

- **One HTTP + SSE API.** Create a profile, start a run, follow its events, stop it, read the session history.
- **Durable runs.** Runs are rows in a Postgres (or SQLite) queue. Events are stored, so a client that drops can
  reconnect and replay from the last sequence number it saw.
- **Isolation per profile.** Tools run in a hardened Docker container per profile, described by a policy you set
  on the profile.
- **Provider-neutral.** Bedrock Mantle and any OpenAI-compatible Responses endpoint are built in; adding another
  provider is one small class.

## Quickstart

You need Docker and either an OpenAI-compatible API key or AWS credentials with Bedrock access.

```bash
# from a clone of this repository
KEEL_API_KEY=dev KEEL_API_KEY_TENANT=1 docker compose -f docker-compose.dev.yml up --build
```

The dev compose file starts Postgres and keel (port 8700, local only). It calls Bedrock with your `~/.aws`
profile (the mount is only needed for Bedrock). To use the OpenAI provider instead, export `KEEL_OPENAI_API_KEY`
before `up` (and optionally `KEEL_OPENAI_BASE_URL`, `KEEL_OPENAI_COMPAT`). Use a model id your account or
endpoint exposes; `openai.gpt-6-luna` below is only an example.

Then, from another terminal:

```bash
curl -s -X PUT localhost:8700/v1/profiles/demo -H 'Authorization: Bearer dev' -H 'content-type: application/json' \
  -d '{"models": [{"provider": "bedrock", "model": "openai.gpt-6-luna"}],
       "sandbox": {"image": "python:3.12-slim", "workdir": "/work"}}'
curl -s -X POST localhost:8700/v1/profiles/demo/runs -H 'Authorization: Bearer dev' -H 'content-type: application/json' \
  -d '{"input": "Write and run a script that prints the first 10 squares.", "provider": "bedrock",
       "model": "openai.gpt-6-luna", "session_id": "chat-1"}'
curl -N localhost:8700/v1/profiles/demo/runs/<run_id>/events -H 'Authorization: Bearer dev'
```

For the OpenAI provider use `"provider": "openai"` and your model name. The full walk-through is in
[docs/guide/quickstart.md](docs/guide/quickstart.md).

## Features

- Agent loop with eight tools: `terminal`, `execute_code`, `read_file`, `write_file`, `patch`, `search_files`,
  `skills_list`, `skill_view`.
- Docker sandbox per profile: dropped capabilities, `no-new-privileges`, optional CPU, memory and PID limits,
  mounts, a network, and the `runsc` or `kata` runtime.
- Skills: directories with a `SKILL.md`, indexed into the prompt and loaded on demand.
- Sessions with stored messages, titles, and a persistent working directory.
- Image input (PNG, JPEG, GIF, WebP) for vision-capable models.
- Replayable SSE events and a stop endpoint that kills the running command.
- Postgres (workers with LISTEN/NOTIFY wake-ups; several instances can share one database) or SQLite (one instance).
- Per-run limits: iteration cap, run lease, and an in-flight cap that answers 429.

## Documentation

- [Quickstart](docs/guide/quickstart.md)
- [Configuration](docs/guide/configuration.md)
- [HTTP API](docs/guide/api.md)
- [Sandbox](docs/guide/sandbox.md)
- [Providers](docs/guide/providers.md)
- [Operations](docs/guide/operations.md)
- [Changelog](CHANGELOG.md)
- Architecture: [English](docs/architecture.md), [中文](docs/architecture_zh.md)
- Design rationale: [architecture](docs/design/2026-10-11-architecture.md), [agent loop](docs/design/2026-10-12-m2-agent-loop.md)

## Status

Pre-1.0 (0.3.0-rc.2). The API and the database schema may change between releases. A database is upgraded in place at
start (numbered migrations); a database written by a newer keel is refused. Back up the database before upgrading.

## Development

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

Opt-in suites: `KEEL_TEST_DOCKER=1` (Docker sandbox, image from `KEEL_TEST_DOCKER_IMAGE`, default
`python:3.12-slim`), `KEEL_TEST_POSTGRES_DSN=postgresql://…` (drops and recreates keel's tables),
`KEEL_LIVE_BEDROCK=1` and `KEEL_LIVE_OPENAI=1` (call a real model).

## Relationship to hermes-agent

Parts of keel are adapted from [hermes-agent](https://github.com/NousResearch/hermes-agent) (MIT, Nous Research):
the Responses wire format, the tool result shapes, the skills index and the sandbox hardening. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

MIT. See [LICENSE](LICENSE).
