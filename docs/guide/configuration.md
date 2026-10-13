# Configuration

keel reads its settings from environment variables at start. `KEEL_API_KEY` and `KEEL_DATABASE_URL` are required;
an invalid value stops the service at start with a message naming the variable.

## Service

| Variable | Default | Meaning |
|---|---|---|
| `KEEL_API_KEY` | required | The bearer key every caller sends (`Authorization: Bearer <key>`). |
| `KEEL_DATABASE_URL` | required | `postgresql://…` (or `postgres://…`) or `sqlite:///path`. Postgres wakes workers through LISTEN/NOTIFY, which needs direct connections (not a transaction-mode pooler). A lost LISTEN connection is reconnected with backoff; several instances can share one database. |
| `KEEL_HOST` | `0.0.0.0` | Listen address. |
| `KEEL_PORT` | `8700` | Listen port. If you change it, change the image's `HEALTHCHECK` too. |
| `KEEL_WORKERS` | `4` | Runs executed at the same time by this instance. |
| `KEEL_MAX_RUNNING` | `10` | Queued plus running runs allowed across all instances. A new run over the cap gets 429 `at_capacity`. |
| `KEEL_LEASE_SECONDS` | `30` | Run lease. A worker renews it while it runs; a run whose lease lapses fails as `worker_lost`. Postgres queries time out after a third of the lease (at least 5 s), so one stalled query cannot outlast a renewal. |
| `KEEL_MAX_ITERATIONS` | `60` | Tool iterations one run may take. After that, one final model call without tools asks for a summary (so at most N + 1 model calls). |
| `KEEL_COMPRESSION_THRESHOLD` | `0.75` | Fraction of the model's context window at which a session is compacted before the next model call: old tool results and images are replaced by placeholders, and if that is not enough the middle of the conversation is summarized. Above 0, at most 1. |
| `KEEL_EVENT_RETENTION_DAYS` | `7` | Days a finished run's events are kept, a positive integer. An hourly task deletes older ones (runs, their output and messages are kept). |
| `KEEL_BACKGROUND_LIMIT` | `4` | Concurrent best-effort tasks this instance runs outside a run (a title, a memory review). Past the limit the new task is dropped and logged. They are cancelled only when the service stops. |
| `KEEL_MEMORY_REVIEW_TURNS` | `10` | Completed runs between memory reviews. A positive integer. |
| `KEEL_MEMORY_CHARS` | `2200` | Character limit for the `memory` target. A positive integer. |
| `KEEL_USER_CHARS` | `1375` | Character limit for the `user` target. A positive integer. |

## Sandbox

| Variable | Default | Meaning |
|---|---|---|
| `KEEL_SANDBOX` | `docker` | `docker`, or `none` to run without tools (the model only answers). |
| `KEEL_SANDBOX_IDLE_SECONDS` | `1800` | A sandbox container idle this long is removed (checked about once a minute). |
| `KEEL_SANDBOX_MOUNT_ROOT` | unset | Absolute path prefix. When set, every `mounts[].source` in a profile policy must be under it. When unset, any host path is allowed. |
| `KEEL_DOCKER` | `docker` | The docker CLI to run. |
| `KEEL_DEPLOYMENT` | `keel` | A label (1-40 lowercase letters, digits or `-`, starting with a letter or digit) put on this deployment's containers. At start, containers carrying this label are removed (except those of profiles with a run in flight), so it must be unique per deployment that shares a Docker daemon. |

See [sandbox.md](sandbox.md) for the profile's sandbox policy.

## Providers

| Variable | Default | Meaning |
|---|---|---|
| `AWS_REGION` | `us-east-1` | Bedrock Mantle region (`AWS_DEFAULT_REGION` is used if `AWS_REGION` is unset). Credentials come from the standard AWS chain, for example `AWS_PROFILE` or an instance role. The `bedrock` provider is always registered. |
| `KEEL_OPENAI_API_KEY` | unset | Registers the `openai` provider. Falls back to `OPENAI_API_KEY`. Without a key the provider is not registered. |
| `KEEL_OPENAI_BASE_URL` | `https://api.openai.com/v1` | Base URL of the OpenAI-compatible Responses server (`/responses` is appended). Must start with `http://` or `https://`; plain `http://` logs a warning because the key travels in clear text. |
| `KEEL_OPENAI_COMPAT` | unset | `1`, `true` or `yes` omits `include`, `prompt_cache_key` and `reasoning.summary` from requests, for strict servers such as vLLM, LiteLLM or Ollama. |
| `KEEL_OPENAI_CONTEXT_WINDOW` | `128000` | Context window the `openai` provider reports. |

See [providers.md](providers.md).

## Tests

These are read by the test suite, not the service: `KEEL_TEST_DOCKER=1` (with `KEEL_TEST_DOCKER_IMAGE`),
`KEEL_TEST_POSTGRES_DSN`, `KEEL_TEST_SUBPROCESS=1` (starts and kills real server processes), `KEEL_LIVE_BEDROCK=1`,
`KEEL_LIVE_OPENAI=1`.
