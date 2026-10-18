# Configuration

keel reads its settings from environment variables at start. `KEEL_DATABASE_URL` and at least one tenant-bound
service key are required; an invalid value stops the service at start with a message naming the variable.

## Service

| Variable | Default | Meaning |
|---|---|---|
| `KEEL_API_KEYS` | unset | Comma-separated `tenant-id:secret` bindings, for example `1:key-a,2:key-b`. Each tenant ID is a signed 32-bit integer (`0` and negatives such as `-5` are valid; no leading zeros or `+`) and has its own key; multiple keys may name one tenant. A key cannot name two tenants. |
| `KEEL_API_KEY`, `KEEL_API_KEY_TENANT` | unset | Single-key form of the binding. Both must be set together; `KEEL_API_KEY_TENANT` is a signed 32-bit integer. A bare key does not start the service. |
| `KEEL_TENANT_BACKFILL` | unset | Path to a JSON object mapping existing profile IDs to integer tenant IDs, for example `{"p1":1}`. Required while any existing profile lacks an owner. The named tenants must have configured keys; an incomplete, unknown or conflicting assignment stops startup without changing ownership. |
| `KEEL_DATABASE_URL` | required | `postgresql://…` (or `postgres://…`) or `sqlite:///path`. Postgres wakes workers through LISTEN/NOTIFY, which needs direct connections (not a transaction-mode pooler). A lost LISTEN connection is reconnected with backoff; several instances can share one database. |
| `KEEL_HOST` | `0.0.0.0` | Listen address. |
| `KEEL_PORT` | `8700` | Listen port. If you change it, change the image's `HEALTHCHECK` too. |
| `KEEL_WORKERS` | `4` | Runs executed at the same time by this instance. |
| `KEEL_MAX_RUNNING` | `10` | Queued plus running runs allowed across all instances. A new run over the cap gets 429 `at_capacity`. |
| `KEEL_LEASE_SECONDS` | `30` | Run lease, also used for sandbox usage and lifecycle-operation leases. A worker renews it every third of the lease while it runs; a run whose lease lapses fails as `worker_lost`. Postgres queries time out after a third of the lease (at least 5 s), so one stalled query cannot outlast a renewal. |
| `KEEL_MAX_ITERATIONS` | `60` | Tool iterations one run may take. After that, one final model call without tools asks for a summary (so at most N + 1 model calls). |
| `KEEL_COMPRESSION_THRESHOLD` | `0.75` | Fraction of the model's context window at which a session is compacted before the next model call: old tool results and images are replaced by placeholders, and if that is not enough the middle of the conversation is summarized. Above 0, at most 1. |
| `KEEL_EVENT_RETENTION_DAYS` | `7` | Days a finished run's events are kept, a positive integer. An hourly task deletes older ones (runs, their output and messages are kept). |
| `KEEL_BACKGROUND_LIMIT` | `4` | How many best-effort tasks (a title, a memory review) this instance may run at once outside a run, a positive integer; past the limit the new task is dropped and logged, and they are cancelled only when the service stops. |
| `KEEL_MEMORY_REVIEW_TURNS` | `10` | Completed runs of a memory-enabled profile between background memory reviews, a positive integer. |
| `KEEL_MEMORY_CHARS` | `2200` | Character limit for the `memory` target, a positive integer. |
| `KEEL_USER_CHARS` | `1375` | Character limit for the `user` target, a positive integer. |

## Sandbox

| Variable | Default | Meaning |
|---|---|---|
| `KEEL_SANDBOX` | `docker` | `docker`, or `none` to run without tools (the model only answers). With `docker`, an unreachable daemon or missing sandbox helper keeps sandbox runs queued while chat-only profiles still run. |
| `KEEL_SANDBOX_IDLE_SECONDS` | `1800` | A sandbox container idle this long is removed (checked about once a minute). |
| `KEEL_SANDBOX_REAP_SECONDS` | `5` | Maximum time to prove sandbox process descendants have exited after the termination grace period; an unproven cleanup leaves the generation blocked for recovery. Must be above 0 and at most 86400 (the supervisor's one-day bound). |
| `KEEL_SANDBOX_MOUNT_ROOT` | unset | Deployment-owned host directory for profile bind mounts. A profile may mount only paths under `<root>/<sha256(profile_id)>`; the operator must create and protect that directory. Without this setting, bind mounts are refused. |
| `KEEL_DOCKER` | `docker` | The docker CLI to run. |
| `KEEL_DEPLOYMENT` | `keel` | A label (1-40 lowercase letters, digits or `-`, starting with a letter or digit) put on this deployment's containers. Sandbox records and reconciliation are scoped to it, so it must be unique per deployment that shares a Docker daemon. |

See [sandbox.md](sandbox.md) for the profile's sandbox policy.

## Providers

| Variable | Default | Meaning |
|---|---|---|
| `AWS_REGION` | `us-east-1` | Bedrock Mantle region (`AWS_DEFAULT_REGION` is used if `AWS_REGION` is unset). Credentials come from the standard AWS chain, for example `AWS_PROFILE` or an instance role. The `bedrock` provider is always registered. |
| `KEEL_OPENAI_API_KEY` | unset | Registers the `openai` provider. Falls back to `OPENAI_API_KEY`. Without a key the provider is not registered. |
| `KEEL_OPENAI_BASE_URL` | `https://api.openai.com/v1` | Base URL of the OpenAI-compatible Responses server (`/responses` is appended). Must start with `http://` or `https://`; plain `http://` logs a warning because the key travels in clear text. |
| `KEEL_OPENAI_COMPAT` | unset | `1`, `true` or `yes` omits `include`, `prompt_cache_key` and `reasoning.summary` from requests, for strict servers such as vLLM, LiteLLM or Ollama. |
| `KEEL_OPENAI_CONTEXT_WINDOW` | `128000` | Context window the `openai` provider reports. |
| `KEEL_MODEL_FIRST_EVENT_SECONDS` | `120` | Seconds a model call may wait for its first response event (`response.*` or `error`). Keep-alive traffic does not count. Past that the call fails with `timeout`; a call that has streamed nothing is retried, up to three attempts in all. Applies to every provider. Positive. |
| `KEEL_MODEL_STALL_SECONDS` | `60` | Seconds allowed between response events once the stream has started. Raised to 120 s for requests above about 50,000 estimated tokens and 180 s above about 100,000 (estimated as request bytes / 4), since a large prompt can be silent while it is prefilled. Positive. |
| `KEEL_MODEL_HIGH_EFFORT_STALL_SECONDS` | `300` | At `high`, `xhigh` or `max` reasoning effort both limits above are at least this, since the model may think for minutes before its next event. Kept well under ten minutes so a retried stall still finishes inside a client's wait. Positive. The byte-level read timeout (600 s, connect 10 s) remains a backstop for a dead connection. |

See [providers.md](providers.md).

## Existing database cutover

Drain runs, stop older keel instances, back up the database, prepare the tenant-bound keys and an explicit
`KEEL_TENANT_BACKFILL` assignment for every existing profile, and start only this version. Check that the consumer
maps its authorized tenant account to the correct profile. Old builds with a global key must not run alongside this
version. If existing profiles use bind mounts, move their data into their profile-specific directories and update
their stored policies as a reviewed database migration during the stopped-service cutover; the runtime API refuses
to change an existing mount list. Restoring an older version requires restoring the database backup and reconciling
any newer state.

For each profile with a legacy bind mount, use this stopped-service procedure before starting tenant-aware keel:

1. Read the profile's `id` and `sandbox` JSON from the database backup. Compute
   `<KEEL_SANDBOX_MOUNT_ROOT>/<sha256(profile_id)>` as described in [sandbox.md](sandbox.md). Give each old source a
   distinct destination under that directory; sources for one profile must not be nested.
2. Copy the old directory and its permissions to the destination. Keep the original in place. Verify the copied
   files (for example, compare file counts and checksums) and ensure the destination does not resolve outside the
   profile directory. If two old profiles used the same source, make independent copies.
3. In a database transaction, update only that profile's `sandbox` JSON `mounts[].source` fields to the new paths.
   Preserve every target, `read_only` flag and other policy field. Use the old JSON as an optimistic condition on
   the update, check that exactly one row changed, and roll back if any profile cannot be updated. Perform this
   after all destinations have been verified.
4. Start keel with the tenant keys, explicit ownership backfill and the mount root. Check each migrated profile's
   session history and a file in its sandbox. Delete old copies only after the cutover and rollback window.

The frozen 0.2.0 migration test exercises this copy, policy update, backfill and history-preservation sequence on
SQLite and PostgreSQL. A failed ownership mapping leaves the old schema and data unchanged.

## Tests

These are read by the test suite, not the service: `KEEL_TEST_DOCKER=1` (with `KEEL_TEST_DOCKER_IMAGE`),
`KEEL_TEST_POSTGRES_DSN`, `KEEL_TEST_SUBPROCESS=1` (starts and kills real server processes), `KEEL_LIVE_BEDROCK=1`,
`KEEL_LIVE_OPENAI=1`.
