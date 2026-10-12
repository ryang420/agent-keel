# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) (pre-1.0: minor releases may change the API).

## [Unreleased]

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

[Unreleased]: https://github.com/ryang420/agent-keel/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/ryang420/agent-keel/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/ryang420/agent-keel/releases/tag/v0.1.0
