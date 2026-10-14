# agent-keel architecture — design

Date: 2026-10-11. Status: approved.

## Why

agent-keel is a server-side, multi-tenant agent runtime. Its first consumer is a chat product
whose answers come from tools run in a sandbox, and which today runs its agent on Hermes (Nous
Research hermes-agent, MIT, pinned at v2026.9.24 `f97608f178`). That consumer uses a small part
of Hermes, and the rest gets in the way of scaling out:

- every in-flight run lives in one single-process gateway with a small concurrency cap;
- all state (profiles, config, sessions, memory) is on one host's disk, and per-profile config
  is kept in on-disk files;
- local source patches and plugins have to be re-fitted on every upgrade;
- the gateway cannot list runs, forgets them on restart, and detaches a run's event queue on
  any client disconnect.

agent-keel keeps what the first consumer uses and is built to run as several stateless
instances.

The first consumer already talks to its agent only through a narrow runtime contract. agent-keel
is a second implementation of that contract, reached through an HTTP client adapter beside the
existing one. Every milestone below is gated on an eval of a consumer's workload against its
previous runtime's recorded baseline.

### Design rules

- **No domain words.** agent-keel knows nothing about any one product's domain. Domain
  behaviour arrives as configuration: persona (SOUL), skills, sandbox image and mounts,
  per-run instructions.
- **Stateless workers.** Every piece of state is in the store; any instance can serve any
  request and pick up any run.
- **Every step is committed.** A model response and each tool result are persisted before the
  next step, so a transcript is always consistent even if a worker dies.
- **Interfaces only where two implementations exist or are planned:** `Store`, `EventBus`,
  `ModelProvider` (Bedrock now; OpenAI, Anthropic planned), `SandboxBackend`. Everything else is concrete code.
- **Hermes is a reference, not a base.** Pieces are copied or adapted where they carry
  hard-won behaviour (see "Parts taken from Hermes"); the rest is written fresh.

## Scope

### In the first version

Runs with streaming events and stop; sessions with titles; the agent loop with nine tools; a
per-profile sandbox; skills (read-only, shipped with the deployment); per-profile long-term
memory with background review; context compression; images in input; a provider-neutral model
interface with Bedrock Mantle (OpenAI Responses API) as its first implementation; import of
Hermes history.

### Not in the first version

| Left out | Why |
|---|---|
| Scheduled jobs (cron) | No consumer need yet. If added later: results go to a session the user sees, per-job token budget and minimum interval, the user's chosen model (a minimal version is small). |
| Approvals | Deployments run unattended; no approval prompt is ever shown. |
| Web search / browsing | No consumer need yet; the tools can be added later behind the same tool interface. |
| MCP servers, agent-authored skills | No consumer need yet; skills are shipped with the deployment and read-only. |
| OpenAI and Anthropic providers | Not needed by the first consumer; the provider interface (§5) is designed for them so they plug in without changes elsewhere. |

The first version keeps the tools most server-side agent work needs: a shell, files and skills.
The others can be added behind the same tool interface when a consumer needs them.

## 1. Process model

One `agent-keel` service (one container) holds an HTTP API and a pool of worker tasks in the
same asyncio process; API and workers can be split into separate processes later without an
interface change.

- **Runs are the queue.** `POST runs` inserts a `queued` row. Workers claim with
  `SELECT … FOR UPDATE SKIP LOCKED`, set a lease (`lease_owner`, `lease_expires_at`) and
  renew it by heartbeat while the run is active.
- **Several instances** can run against one database; nothing is held in process memory that
  another instance needs.
- **Concurrency.** A per-instance cap (worker count) and a global cap (count of runs in
  `running`); over the global cap `POST runs` answers 429, and a client can queue and retry.
- **A dead worker.** When a lease expires, any instance marks the run `failed` with
  `error_code = worker_lost`. A run is not resumed mid-turn in the first version; because
  every step is committed, the session stays consistent and the user can send again.

## 2. HTTP API

Bearer service key on every route. All routes are under `/v1/profiles/{profile}`; `profile`
is the unit of isolation for sessions, memory, sandbox and config.

| Route | Purpose |
|---|---|
| `PUT /v1/profiles/{p}` | Create or update a profile: allowed models (provider, model id), sandbox policy, SOUL, enabled skills. Idempotent. |
| `POST …/runs` | Start a run: `input` (text and image parts), `session_id`, `instructions`, `provider`, `model`, `reasoning_effort`, optional `file_context`. Returns `run_id`. |
| `GET …/runs/{id}` | Status, error, `output`, `usage`. |
| `GET …/runs?status=active` | Runs in flight (Hermes has no equivalent). |
| `GET …/runs/{id}/events?after=<seq>` | SSE stream; replays events after `seq`, then follows live. Disconnecting never affects the run. |
| `POST …/runs/{id}/stop` | Stop a run. |
| `GET …/sessions`, `GET …/sessions/{id}/messages?limit&offset&order=latest`, `PATCH …/sessions/{id}` (title), `DELETE …/sessions/{id}` | Session list, transcript pages, rename, delete. |
| `GET …/sessions/{id}/boundary`, `GET …/sessions/{id}/messages?after_id=` | The newest message id, and every message after one: what a client needs to reconstruct a run's tool calls. |
| `GET /healthz` | Liveness and store reachability. |

Errors are JSON `{"error": {"code", "message"}}`: 404 `run_not_found` / `session_not_found`
/ `profile_not_found`, 409 `title_in_use`, 429 `at_capacity`, 400 for invalid input.

### Events

Every event is `{"seq", "type", "run_id", "at", …}`, stored in `run_events` and published on
the bus. `seq` increases per run.

| Type | Fields |
|---|---|
| `run.started` | `provider`, `model`, `reasoning_effort` |
| `message.delta` | `text` |
| `tool.started` | `tool_call_id`, `tool`, `preview` (redacted, ≤ 500 chars) |
| `tool.completed` | `tool_call_id`, `tool`, `duration_s`, `error` (bool), `preview` |
| `run.completed` | `output`, `usage` |
| `run.failed` | `error_code`, `message` |
| `run.cancelled` | — |

`usage` is `{input_tokens, output_tokens, cache_read_tokens, cache_write_tokens}` summed over
the run's model calls, where `input_tokens` is the whole prompt including cached parts — the
semantics the first consumer's usage contract and eval gate rely on.

## 3. Data model

Postgres in deployment; SQLite for local development and tests, behind the same `Store`
interface. The event bus is Postgres `LISTEN/NOTIFY` in deployment and in-memory in tests,
behind `EventBus`.

| Table | Columns (essentials) |
|---|---|
| `profiles` | `id`, `models` (json: provider, model), `sandbox` (json), `soul`, `skills` (enabled names), `updated_at` |
| `sessions` | `id`, `profile_id`, `title`, `title_source` (user/llm/derived), `provider`, `model`, `system_prompt`, `parent_session_id` (compression lineage), `created_at`, `last_active_at`, `message_count` |
| `messages` | `id` (bigserial), `session_id`, `role`, `content` (json parts; images stored as references, never flattened), `tool_calls` (json, OpenAI shape), `tool_call_id`, `tool_name`, `reasoning_items` (json, each stamped with provider and model), `active`, `created_at`, `run_id` |
| `runs` | `id`, `profile_id`, `session_id`, `status`, `provider`, `model`, `reasoning_effort`, `input`, `instructions`, `output`, `usage` (json), `error_code`, `error`, `lease_owner`, `lease_expires_at`, `created_at`, `started_at`, `finished_at` |
| `run_events` | `run_id`, `seq`, `type`, `data` (json), `at`; pruned some days after the run ends |
| `memories` | `profile_id`, `target` (memory/user), `entries` (json list), `updated_at` |

- Message ids increase within a session and a run stores exactly one user message, so a
  client can reconstruct a run's tool calls from the stored messages.
- Titles are unique per profile (`unique (profile_id, title)` on non-null titles).
- Skills are not in the store: the deployment ships them read-only in the image (or a mounted
  directory), and a profile's config names which are enabled. No per-profile seeding.

## 4. Agent loop and prompt

**Loop.** Each iteration calls the model with streaming; text deltas become `message.delta`.
If the response has tool calls, the assistant message is committed first, then the tools run:
read-only tools (`read_file`, `search_files`, `skill_view`, `skills_list`) in parallel when
their paths do not overlap, everything else in order. Each result is committed, then the next
iteration starts. The loop ends on a text-only response; at the iteration cap (default 60) it
appends Hermes's "reached the maximum number of tool-calling iterations… provide a final
response… without calling any more tools" request and makes one last tool-less call. Stop
ends it at the next boundary (see §10).

**System prompt**, three tiers in Hermes's order so the cached prefix stays stable:

1. Stable: SOUL; task-completion and tool-use guidance; the skills rule ("if a skill matches or
   is even partially relevant… load it with skill_view"); memory guidance.
2. Context: the platform hint (Hermes's `api_server` hint).
3. Volatile: the skills index; the session's frozen memory snapshot; a date-only timestamp.

The prompt is built once per session, stored on the session, and rebuilt only after
compression. A run's `instructions` are not part of it: they are appended to the system
message at call time (`cached prompt + "\n\n" + instructions`), exactly as Hermes does, which keeps
a client's cache layout stable.

**Tool output.** Hermes's head/tail truncation at 50,000 characters (40% head, 60% tail, with
an "[OUTPUT TRUNCATED - N chars omitted out of M total]" marker); output beyond 100,000
characters is written to a file in the sandbox and replaced with a preview and its path.

## 5. Model providers

The agent loop and the store speak one provider-neutral format; each provider translates it to
and from its own API. Bedrock Mantle is the only provider built in the first version; OpenAI
and Anthropic plug in later by implementing the same interface, with no change to the loop,
store or API.

### The neutral format

- **Request:** system text (the cached prompt) plus per-run instructions, input items (user
  text and image parts, assistant text, tool calls, tool results), tool schemas (JSON Schema),
  reasoning effort, a cache scope (session lineage id).
- **Response stream:** text deltas, tool calls (`id`, `name`, `arguments`), reasoning items,
  and a final `usage` with the semantics in §2 (`input_tokens` includes cached parts).
- **Reasoning items** are opaque to everything but their provider: stored as
  `{provider, model, kind, payload}` and replayed only to the same provider and model.
- **Errors** are raised as one of the neutral codes below; nothing provider-specific leaves a
  provider.

### The interface

```python
class ModelProvider(Protocol):
    name: str                                   # "bedrock", "openai", "anthropic"
    def capabilities(self, model: str) -> Capabilities: ...
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...

@dataclass
class Capabilities:
    context_window: int
    vision: bool
    reasoning_replay: bool          # opaque reasoning must be sent back on later turns
    parallel_tool_calls: bool
    prompt_cache: Literal["key", "breakpoints", "none"]
```

The loop reads capabilities instead of assuming Mantle: compression thresholds use
`context_window`; images are refused before the call when `vision` is false; reasoning items
are kept and replayed only when `reasoning_replay` is true; the cache hint is a key
(`prompt_cache_key`) or explicit breakpoints (`cache_control` on the system prompt, tools and
the last stable message) depending on `prompt_cache`.

A registry maps a provider name to an implementation, configured per deployment (credentials,
region, base URL). Each run names `provider` and `model`; a profile lists the allowed pairs.
There is no profile default model to go stale.

### Implementations: protocol × auth

Providers share code by wire protocol, with authentication as a separate, pluggable part:

| Provider | Protocol | Auth | First version |
|---|---|---|---|
| `bedrock` (Mantle) | OpenAI Responses | SigV4 (`bedrock` service), ambient AWS credentials or a named profile | built |
| `openai` | OpenAI Responses | bearer API key | interface ready — a new auth on the same transport |
| `anthropic` | Anthropic Messages | API key | interface ready — a new transport |
| Claude on Bedrock | Anthropic Messages (Bedrock invoke) | SigV4 | interface ready — the Messages transport with SigV4 |

### The Responses transport (Mantle now, OpenAI later)

- **Request:** `store: false`, `include: ["reasoning.encrypted_content"]`,
  `reasoning: {effort, summary: "auto"}`, `parallel_tool_calls: true`,
  `prompt_cache_key` = hash of system prompt (without per-run instructions) and tool schemas.
- **Streaming:** the output is assembled from `response.output_item.done` events, never from
  `response.completed.output`; announced but unconfirmed function calls are settled; empty
  arguments become `{}`.
- **Encrypted reasoning:** captured as reasoning items stamped with provider and model; replay
  sends only the run's own provider-and-model items, without ids. Switching models therefore
  never sends foreign reasoning (the cause of Hermes's 400s and 500 loops).

What the Anthropic transport will have to cover, so the neutral format is checked against it
now: thinking blocks with signatures (its reasoning items, replayed verbatim and in place),
`cache_control` breakpoints, tool use / tool result content blocks, and image blocks.

### Errors and retries

Every provider maps its failures to: `auth`, `rate_limited`, `server_error`, `timeout`,
`context_overflow` (including 413 payload too large → compress), `invalid_reasoning` (strip
reasoning items, retry once; Mantle's `invalid_encrypted_content`, Anthropic's invalid thinking
signature), `bad_request`. Retries are shared: Retry-After when given (≤ 600 s), otherwise 5 s
base doubling to 120 s with jitter, at most 3 attempts per call.

## 6. Tools and sandbox

Nine tools, with Hermes's schemas and descriptions kept as close as possible (consumers'
prompts are tuned against these texts): `terminal`, `execute_code`, `read_file`, `write_file`,
`patch`, `search_files`, `skill_view`, `skills_list`, `memory`. `execute_code` runs a Python
script in the sandbox (no persistent kernel or RPC bridge). The first version of the loop (M2) ships eight of the nine; `memory` arrives in M3 (§7).

**`SandboxBackend`** — the tools never touch host paths; every file and process operation goes
through the backend, so a backend without a shared host filesystem works unchanged:

```
ensure(profile, policy) -> handle         # create or reuse the profile's sandbox
exec(handle, command, cwd, timeout, env)  # streaming stdout/stderr, exit code
read_file / write_file / list(handle, path, ...)
kill_tree(handle, exec_id)                # the command and all its children
destroy(handle)
```

**Docker backend (first version).** One long-lived container per profile, configured by the
profile's sandbox policy: image, network, mounts, CPU, memory and pid limits, user, and
**`runtime`: `runc` (default) | `runsc` (gVisor) | `kata` (Kata Containers, microVM)**.
Commands run through `docker exec`, with the working directory carried by an in-band marker.
`kill_tree` kills the whole process group (built in, where Hermes needs a patch). A vanished
container is recreated on the next call (Hermes needs a restart). Idle containers are reaped;
orphans are removed at start.

Isolation path: ship M2 on `runc`; then run the eval and contract checks on `runsc` and switch
the default when they pass the gate. `kata` (with Cloud Hypervisor, which supports virtio-fs
so shared workspaces still mount) needs KVM on the host — a nested-virtualization-capable
instance type or bare metal — and is evaluated separately. A raw Firecracker backend is not
planned: it has no shared directories, and Kata gives the same isolation behind this interface.

## 7. Memory

Two targets per profile, `memory` and `user`, each a list of entries, capped at 2,200 and
1,375 characters (Hermes's limits). The `memory` tool adds, replaces and removes entries;
writes are scanned for secrets and injected instructions. Each session freezes a snapshot at
its start into the volatile prompt tier, so writes during a session never change its cached
prompt. Background review: every 10 user turns (reset when the model calls `memory` itself),
after a completed turn, a review call runs with the same model and the same conversation prefix
(so it hits the cache), Hermes's memory-review prompt, and only the `memory` tool allowed.

## 8. Context compression

Triggered when a call's input reaches 75% of the model's context window (taken from the provider's
capabilities), or when the provider answers `context_overflow`. Old tool outputs are first
replaced by placeholders without a model call; then the first 3 messages and a recent tail
(~2.5% of the window, 10k–25k tokens) are kept and the middle is summarized with Hermes's
checkpoint prompt and sections (the user's latest unfulfilled request verbatim; goal;
completed actions; active state; decisions; errors and fixes). The summary continues in a child
session (`parent_session_id`); a client can show one conversation. The system prompt is
rebuilt, reasoning items before the compaction point are dropped, and the cache key is scoped
to the session lineage.

Superseded by the M3 design (`2026-10-12-m3-reliability-memory-compression.md` §5): the summary
is kept on the same session rather than in a child session, so the session id never changes.

## 9. Titles

The first turn sets a derived title (first line, ≤ 48 characters). A background call asks the
model for 3–7 words as JSON with Hermes's prompt and guards. Precedence user > llm > derived; a
user title is never overwritten. Titles are unique per profile; an automatic title that
collides gets `#N`. Titles are sanitized (control, zero-width and bidi characters stripped,
≤ 100 characters).

## 10. Stop and failure

`POST stop` sets `stopping`; the worker cancels an in-flight model stream at once and calls
`kill_tree` on a running command, then ends the run `cancelled`. Messages already committed
stay; an assistant tool call without a result gets a synthetic "cancelled" tool result so the
next turn's request is valid. Lease expiry is covered in §1.

## 11. Importing Hermes history

Optional and consumer-specific; not part of the core service.

`keel-import-hermes` reads each profile's `state.db` and memory files and writes sessions,
titles (with their source), messages and tool calls, and memory entries. Encrypted reasoning is
dropped (not portable); images stay as Hermes stored them (`[screenshot]` text), which a client can already handle. Message ids are renumbered in order. Imported sessions can
be continued. Hermes cron jobs and per-profile skill copies are not imported.

## 12. Security

- Sandbox isolation is configurable per profile (§6).
- The sandbox's network must not reach the instance metadata service; the sandbox gets its own
  narrowly scoped cloud role. Tracked as its own task: agent-keel's sandbox policy carries the
  network name and env, the deployment enforces the block.
- Service-key auth; the service is not exposed beyond a private network; secrets
  are redacted from logs, event previews and stored tool output.

## 13. Testing and milestones

Unit tests run with a fake `ModelProvider` and the in-memory store/bus; integration tests use
SQLite and a real Docker daemon; a consumer's evals reach agent-keel through the same HTTP API as any client.

| Milestone | Scope | Gate |
|---|---|---|
| M1 | Service skeleton, store (Postgres + SQLite), event bus, provider interface and the Mantle provider, tool-less single-turn runs, events and run API; consumer HTTP client adapter | A consumer's tool-less generation jobs run on agent-keel with results equivalent to its previous runtime |
| M2 | Agent loop, eight tools (memory in M3), Docker sandbox, sessions API, skills | An eval of a consumer's workload against its previous runtime's recorded baseline: contract checks all pass; correctness, latency, tokens and cache hit rate within agreed bounds |
| M3 | Stop and lease recovery, compression, titles, images, memory and background review; explicit tenant ownership and authorization with independent profiles | Fault injection (kill a worker, delete a sandbox, provider 5xx) passes; a long-session eval; tenant/profile isolation and consumer account-authorization checks pass before a real-user pilot |
| M4 | Optional, consumer-specific: import of another runtime's history and a staged cutover; not planned for keel itself | — |

Each milestone gets its own spec and plan.

The M3 design §12 extends the original profile-only isolation and service-key model: a tenant contains multiple
independent account profiles, and runtime credentials are tenant-scoped. Profiles never share private files or
knowledge, even within one tenant. This is required M3 work, not part of the optional M4 history import/cutover.

## Parts taken from Hermes

Hermes is MIT-licensed; copied or adapted files point to `THIRD_PARTY_NOTICES.md`, which carries its copyright notice and
`THIRD_PARTY_NOTICES.md` records the source and version (v2026.9.24, `f97608f178`).

| Piece | Hermes source | Treatment |
|---|---|---|
| SigV4 auth for Mantle, URL helpers | `agent/bedrock_adapter.py` (~190 of 1,391 lines) | copy |
| Responses request building, stream assembler, reasoning capture and replay | `agent/transports/codex.py`, `codex_responses_adapter.py`, `codex_runtime.py` | adapt |
| Head/tail truncation | `tools/tool_output_truncate.py` | copy |
| Prompt tier order and key constants (skills rule, memory guidance, api_server hint, max-iterations request) | `agent/system_prompt.py`, `prompt_builder.py` | adapt |
| Tool schemas and descriptions (terminal, file tools, execute_code, skills, memory) | `tools/*.py` | copy text, rewrite code |
| Memory store and review prompt | `tools/memory_tool*.py`, `agent/background_review.py` | adapt |
| Compression thresholds, summary prompt and template | `agent/context_compressor.py` | adapt |
| Title prompt, guards, sanitizer | `agent/title_generator.py`, `hermes_state_titles.py` | adapt |
| Backoff | `agent/retry_utils.py` | copy |
| Event names and run terminal statuses | `gateway/platforms/api_server_runs.py` | adapt |

## Stack

Python 3.12, asyncio; FastAPI (HTTP and SSE); asyncpg for Postgres and aiosqlite for SQLite
behind `Store`; httpx for providers (no vendor SDKs, so each transport controls its own wire
format and streaming); botocore for SigV4; the Docker Engine API for the
sandbox; pytest with pytest-asyncio. Packaged with `pyproject.toml`; one container image.
