# Architecture

> 中文版：[architecture_zh.md](architecture_zh.md)。The two documents say the same thing; update both together.

agent-keel as it is built today: a stateless HTTP service that runs agent loops. A client starts a **run**; a
worker claims it, calls a model, runs the tools the model asks for inside a per-profile Docker sandbox, and commits
every step to a database; the client follows the run as a replayable stream of events. Any number of identical
instances can share one Postgres database.

This page explains how the parts fit together. For how to use them, read the [guides](guide/quickstart.md); for the
reasoning behind the design, read [design/2026-10-11-architecture.md](design/2026-10-11-architecture.md) and
[design/2026-10-12-m2-agent-loop.md](design/2026-10-12-m2-agent-loop.md).

---

## 1. System view

```mermaid
flowchart TB
    CL(["Client — your product's backend<br/>tenant-bound service key"])

    subgraph inst["keel instance (identical, stateless)"]
        direction TB
        API["HTTP API · api.py<br/>bearer key check, validation,<br/>starts runs, streams SSE"]
        WP["WorkerPool · worker.py<br/>claims queued runs, renews leases,<br/>honours stop, sweeps dead leases"]
        EX["RunExecutor · runner.py<br/>the agent loop, one per run"]
        SB["DockerSandbox · sandbox/docker.py<br/>one container per profile"]
        PR["Providers · providers/<br/>Bedrock Mantle, OpenAI-compatible"]
    end

    subgraph shared["shared state"]
        direction LR
        DB[("Store<br/>Postgres<br/>profiles, sessions, messages,<br/>runs, run_events, blobs")]
        BUS{{"EventBus<br/>Postgres LISTEN/NOTIFY"}}
    end

    DK["Docker daemon<br/>/var/run/docker.sock"]
    SBX["sandbox containers<br/>keel-sbx-&lt;hash&gt;, one per profile<br/>terminal, files, execute_code"]
    LLM["model endpoints<br/>bedrock-mantle.&lt;region&gt;.api.aws<br/>api.openai.com or compatible"]

    CL -->|"HTTP + SSE · tenant-bound bearer key"| API
    API -->|"create_run, events_after"| DB
    API -->|"notify / wait"| BUS
    WP -->|"claim_run, renew_lease"| DB
    WP --> EX
    EX -->|"append_message, append_event"| DB
    EX -->|"notify"| BUS
    EX --> PR
    EX --> SB
    PR -->|"HTTPS, streaming"| LLM
    SB -->|"docker run / exec / rm"| DK
    DK --> SBX
```

Three things to hold on to:

- **The database is the only shared state.** Every run, message and event is a row. An instance holds nothing a
  restart would lose that a client could see.
- **The bus carries no data.** It only says "run X has something new" or "stop run X", so a reader knows when to look
  at the store again.
- **The Docker socket is root-equivalent on the host.** keel starts and removes sandbox containers through it. See
  [§14](#14-security-model).

---

## 2. Code map

Everything is in `src/keel/`.

| Module | Role |
|---|---|
| `service.py` | Builds the service from `Settings`: store, bus, providers, sandbox coordinator, executor, worker pool, API. Owns start-up (classifies the deployment's sandbox inventory and opens admission), maintenance and shutdown. |
| `config.py` | `Settings.from_env()`; validates every `KEEL_*` variable and fails fast on a bad one. |
| `api.py` | FastAPI app: auth middleware, routes, error envelope, the SSE stream. |
| `worker.py` | `WorkerPool`: claim, lease renewal, stop handling, lease sweeper, drain on shutdown. |
| `runner.py` | `RunExecutor`: the agent loop and the cancel / failure clean-up paths. |
| `compress.py`, `compactor.py` | Context compression: the pure estimate, boundaries, request assembly and summary input; when a run compacts and what it stores. |
| `models.py` | Provider-neutral types: `Message`, parts, `ToolCall`, `ModelRequest`, `Usage`, model events. |
| `store/` | `Store` over PostgreSQL: `sql.py` (queries), `postgres.py` (connection pool and transactions). |
| `events.py` | `InMemoryEventBus`, `PostgresEventBus`. |
| `providers/` | `ModelProvider` interface, registry, retry policy, the Responses-API transport, Bedrock (SigV4), OpenAI. |
| `sandbox/` | Policy parsing, the coordinated exact-ID Docker backend and its supervisor, recovery, and a host-process backend for tests. |
| `tools/` | The sandbox tools, the `memory` tool and `run_tool`, the single entry point the loop calls. |
| `prompt.py`, `prompts/` | The session system prompt and the prompt / tool-definition texts. |
| `skills.py` | Skill discovery through the sandbox and the prompt index. |
| `titles.py` | Session titles, derived and model-generated. |
| `background.py` | Best-effort tasks outside a run: a model title, a memory review. |
| `memory.py` | Curated entries for the two memory targets, and the checks on a write. |
| `review.py` | A background memory review. It writes memory only. |

`Store` uses PostgreSQL in every environment. Interfaces also separate `EventBus` (a local test double and
Postgres), `ModelProvider` (Bedrock, OpenAI, a fake for tests), `SandboxExecutor` (coordinated tool facade, local
host-process backend for tests). Everything else is concrete code.

---

## 3. Data model

```mermaid
erDiagram
    profiles ||--o{ sessions : "has"
    profiles ||--o{ memories : "facts"
    sessions ||--o{ messages : "transcript"
    sessions ||--o{ runs : "turns"
    sessions ||--o{ blobs : "images"
    runs ||--o{ run_events : "event log"
    runs ||--o{ messages : "wrote"

    profiles {
        text id PK
        text models "allow-list: provider + model"
        text sandbox "policy JSON, empty = no sandbox tools; memory is separate and follows the flag"
        text soul "the persona"
        text skills "enabled skill names"
        bool memory "off when omitted"
    }
    memories {
        text profile_id PK
        text target PK "memory or user"
        text entries "JSON list"
    }
    sessions {
        text id PK
        text profile_id
        text title "unique per profile"
        text title_source "derived, llm, user"
        text system_prompt "rebuilt only at a compaction"
        text provider
        text model
        text cwd "working directory carried between turns"
        text summary "the latest compaction"
        bigint summary_head_id "last head message"
        bigint summary_tail_id "first tail message"
        bigint pruned_id "placeholder boundary"
        int context_tokens "last reported input tokens"
        bigint context_message_id "newest message that count included"
        real compaction_failed_at "last failed summary, pauses retries"
    }
    messages {
        bigint id PK "orders the transcript"
        text session_id
        text run_id
        text role "user, assistant, tool"
        text parts "text and image parts"
        text tool_calls
        text reasoning "provider reasoning items, for replay"
    }
    runs {
        text id PK
        text status "queued, running, stopping, completed, failed, cancelled"
        text provider
        text model
        text input
        text instructions "per-run, not stored in the session"
        text usage
        text error_code
        text lease_owner "which instance holds it"
        real lease_expires_at
        text current_exec_id "the command now running"
    }
    run_events {
        text run_id PK
        int seq PK "1, 2, 3 ... per run"
        text type
        text data
    }
    blobs {
        text id PK
        text media_type
        text data "base64"
    }
```

Rules the schema enforces:

- **One run in flight per session.** A partial unique index on `runs(session_id)` where `status` is `queued`,
  `running` or `stopping`. A second start on a busy session is refused (`409 session_busy`), and the index makes the
  rule hold even if two requests race.
- **Titles are unique per profile.** A partial unique index on `sessions(profile_id, title)` where the title is set.
- **A session belongs to one profile.** `ensure_session` answers `404 session_not_found` when the id exists under a
  different profile.
- **Events are numbered per run.** `(run_id, seq)` is the primary key. `append_event` locks the run row, takes
  `MAX(seq) + 1` and inserts in the same transaction, so sequence allocation serialises without a retry loop. It
  rejects a terminal event type (the run transition writes that one) and an unknown run, and returns `None` for a
  terminal run so a late writer can stop quietly.

The store uses PostgreSQL, with `?` query placeholders translated by its adapter. The schema is created and upgraded
by numbered migrations (`store/migrations.py`) at start: each runs in its own transaction under a Postgres advisory lock, so
several instances starting together migrate once. A database from 0.2.0 is upgraded in place; one written by a newer
keel is refused.

---

## 4. Run lifecycle

```mermaid
stateDiagram-v2
    [*] --> queued: POST /runs (202)
    queued --> running: a worker claims it, takes a lease
    queued --> cancelled: POST /stop before any worker has it
    running --> completed: the model answered in text
    running --> failed: model error, sandbox_unavailable, internal_error
    running --> stopping: POST /stop
    stopping --> cancelled: the command is killed, tool calls answered
    running --> failed: lease expired, error_code worker_lost
    stopping --> failed: lease expired, error_code worker_lost
    completed --> [*]
    failed --> [*]
    cancelled --> [*]
```

`finish_run` returns a `RunTransition`: `None` when the run does not exist, the run unchanged with no event when it is
already terminal, and otherwise the updated run with the terminal event inserted in the same transaction. Only one
transition can commit, so a run that completes at the same instant it is stopped, or swept, ends exactly once, and
only the winner emits the terminal event.

### Starting a run

```mermaid
sequenceDiagram
    participant C as Client
    participant A as API
    participant S as Store
    participant W as Worker
    participant X as RunExecutor
    C->>A: POST /v1/profiles/{p}/runs
    A->>A: validate model is in the profile, provider configured, images allowed
    A->>S: session busy? in-flight count >= KEEL_MAX_RUNNING?
    A->>S: ensure_session, store image blobs, create_run (queued)
    A-->>C: 202 run_id, session_id
    W->>S: claim_run (queued to running, lease)
    W->>X: execute(run)
    X->>S: run.started, then the loop (see section 6)
    C->>A: GET /runs/{id}/events
    A->>S: events_after(cursor), wait on the bus when empty
    A-->>C: SSE frames until the terminal event
```

The request is refused before anything is written when it cannot run: unknown model, unconfigured provider, an image
sent to a model without vision, a busy session, or the in-flight cap (`429 at_capacity` with `Retry-After: 1`). A
refused run leaves no empty session and no orphan blob behind.

---

## 5. Workers, leases and recovery

`WorkerPool` starts `KEEL_WORKERS` worker loops, one sweeper and one maintenance task.

**Claiming.** `claim_run` is one `UPDATE … WHERE id = (SELECT … WHERE status = 'queued' ORDER BY created_at LIMIT 1
FOR UPDATE SKIP LOCKED)`. On Postgres, instances never claim the same run. A worker that finds nothing sleeps
0.5 s (or until the bus wakes it); a failing database backs the loop off up to 10 s instead of logging every poll.

**Leases.** The claim sets `lease_expires_at` to now + `KEEL_LEASE_SECONDS` (30 s). While the run executes, the
worker wakes every third of the lease, re-reads the run, and renews the lease. The renew only succeeds for the owner
and for a run still `running` or `stopping`. Every Postgres query times out after a third of the lease (at least
5 s; 10 s by default), so one stalled query cannot use up the lease of the run waiting on it.

**Stop.** `POST …/stop` cancels a `queued` run on the spot (and emits `run.cancelled`), or moves a `running` run to
`stopping` and notifies the key `stop:<run_id>`. The worker sees it through that wake-up or on its next renewal,
cancels the executor task, and the executor's cancel path does the rest: kill the running command in the sandbox,
close the open tool call, store a "cancelled" result for each tool call that has none, finish the run `cancelled`.

**Dead workers.** The sweeper runs every lease interval and marks any `running` or `stopping` run whose lease has
lapsed as `failed` with `error_code: worker_lost`, on whichever instance sweeps first. It emits `run.failed` for every
expired run first, so a slow sandbox cannot delay the terminal events, and then kills the command each dead worker
left running (it reaches the profile's container and kills the process group of `runs.current_exec_id`; it never
creates a container). The kill reaches the command only when the sweeping instance shares the lost worker's Docker
daemon (or host, for the local sandbox); otherwise the command runs until its own timeout. The column is cleared
whether or not the kill worked. A worker whose renewal fails until its own deadline gives the run up to
the sweeper.

**Old events.** Every hour the maintenance task deletes the events of runs finished more than
`KEEL_EVENT_RETENTION_DAYS` (7) days ago, 10,000 rows per statement so no pass holds a long transaction. Runs, their
output and the sessions' messages are kept. The first pass after start scans all finished runs; later ones only
those finished since the previous cutoff (less one hour of overlap). Passes on several instances may overlap; that is
harmless.

**Shutdown.** The pool stops claiming, gives runs in flight 4 s to finish, then fails what remains as `worker_lost`
(not `cancelled`, which is what a user's stop looks like). The service then removes nothing: leftover sandbox
containers are cleaned at the next start.

**A transcript that is always valid.** A run that was swept, crashed or cut short may leave an assistant message whose
tool calls have no results, which a model API would reject. At the start of every turn the executor finds such calls
and stores a "cancelled" result for each, so the next request always pairs every call with a result.

---

## 6. The agent loop

`RunExecutor._loop` (in `runner.py`) is the whole agent:

```mermaid
flowchart TB
    A["load profile and session<br/>parse the sandbox policy"] --> B{"policy set and<br/>KEEL_SANDBOX=docker?"}
    B -->|no| C["no sandbox tools<br/>memory still offered when on"]
    B -->|yes| D["coordinator.prepare(use)<br/>discover skills through it"]
    C --> E
    D --> E["system prompt:<br/>reuse the session's, or build and freeze it"]
    E --> F["repair unanswered tool calls<br/>append the user message, derive a title"]
    F --> G["call the model, stream the answer<br/>emit message.delta"]
    G --> H["commit the assistant message"]
    H --> I{"tool calls?"}
    I -->|no| J["run.completed"]
    I -->|yes| K["for each call:<br/>tool.started, run it in the sandbox,<br/>commit the result, tool.completed"]
    K --> L{"iteration cap?"}
    L -->|no| G
    L -->|yes| M["last call without tools,<br/>asking the model to wrap up"]
    M --> J
```

Properties worth knowing:

- **Every step is committed before the next.** The assistant message is stored when the model finishes; each tool
  result is stored before the next call. If a worker dies, the transcript is consistent up to the last step.
- **Tool failures never kill a run.** Bad arguments, a vanished sandbox or an exception inside a tool become an error
  result the model reads. Only a cancellation passes through.
- **The iteration cap is a soft landing.** At `KEEL_MAX_ITERATIONS` (60) the loop makes one more call with no tools
  and an added request to summarise, so the user gets an answer instead of an error. That request is not stored.
- **Reasoning is replayed.** The provider's reasoning items are stored on the assistant message and sent back in the
  order the provider expects. A provider that rejects them (`invalid_reasoning`) is retried once without them.
- **Long sessions are compacted in place.** Before each model call the loop estimates the request: the input tokens
  the provider reported for the last call, plus a heuristic for newer messages (a quarter of a token per ASCII
  character, one per other character). At `KEEL_COMPRESSION_THRESHOLD` (0.75) of the context window, tool results
  over 200 characters and images before a recent tail are replaced by one-line placeholders, and the reasoning items
  there are dropped. If that is not enough (judged by the heuristic, scaled up when the provider counts more),
  one summary call condenses the middle, and the request becomes the first messages (each cut to about 4,000
  tokens), the summary and the tail. The same happens once when a call fails with `context_overflow`, and the call
  is retried. Nothing stored is deleted: `GET …/messages` returns the whole transcript, and the session records only
  the summary and its boundaries. The system prompt is rebuilt at a compaction and reused byte for byte until the
  next one. A summary call that fails, or is cut off at the output limit, is not stored, and the threshold retries
  it only after 10 minutes.
- **A working directory follows the session.** `terminal` reports the directory it ended in; the executor saves it on
  the session and starts the next turn there.
- **The skills list is cached** for 60 s per (profile, policy digest, enabled skills), and an empty result is not
  cached, because it may be a failed listing.

### Failure mapping

| What happened | Run ends | `error_code` |
|---|---|---|
| The provider is not configured | `failed` | `bad_request` |
| The model call failed (after retries) | `failed` | the provider's code: `auth`, `rate_limited`, `server_error`, `timeout`, `context_overflow`, `invalid_reasoning`, `bad_request` |
| The sandbox policy is invalid, or the container cannot be created or reached | `failed` | `sandbox_unavailable` |
| An unexpected exception | `failed` | `internal_error` (the cleanup still kills the command and answers pending tool calls) |
| A stop | `cancelled` | none |
| The worker died or shut down | `failed` | `worker_lost` |

---

## 7. Events and streaming

Every state change of a run is a row in `run_events`. The types are `run.started`, `message.delta`, `tool.started`,
`tool.completed`, `context.compacted`, `run.completed`, `run.failed` and `run.cancelled`; the last three are
terminal.

```mermaid
sequenceDiagram
    participant X as RunExecutor
    participant S as Store
    participant B as EventBus
    participant H as SSE handler
    participant C as Client
    X->>S: append_event (seq n)
    X->>B: notify(run_id)
    B-->>H: wake the waiter
    H->>S: events_after(cursor)
    H-->>C: id: n / event: type / data: JSON
    Note over H,C: nothing new for 15 s, a keepalive comment is sent
```

- **Replay is free.** A client reconnects with `?after=<last seq>`; the handler reads from the store, so no event is
  lost and no queue is detached by a disconnect.
- **No race between reading and waiting.** The handler takes the waiter *before* reading the store, so a notify that
  lands in between is not lost.
- **The terminal event commits with the terminal state.** A run's terminal state and its terminal event are one
  transaction, and the terminal event is the last event the run accepts. When an empty read is followed by a
  terminal state, the handler makes one final read for the event that raced the state read, then closes. A terminal
  run with nothing after the cursor closes at once: its events were pruned, it predates the guarantee, or the
  client is already past the terminal event.
- **A wake-up is advisory.** A notify that fails is logged and never raised, and readers also re-read the store on a
  timeout, so the worst case of a lost notify is latency.

### The bus

`InMemoryEventBus` is a dictionary of `asyncio.Event` keyed by run id (and `stop:<run id>`); it is a test double
and the local base of `PostgresEventBus`. The service always uses `PostgresEventBus`, which adds `LISTEN`/`pg_notify`
on one channel (`keel_events`, the key as payload), so a notify on one instance wakes a reader on another. It needs two direct connections (one to listen, one to send); a
transaction-mode pooler such as RDS Proxy pins or drops `LISTEN`. If the listen connection is lost, cross-instance
wake-ups fall back to polling while it is reconnected with backoff (0.5 s, doubling up to 30 s). Once it is back,
every waiter is woken, because a notify sent meanwhile was lost; each reader then re-reads the store.

---

## 8. Sandboxes

A profile gets sandbox tools only if its `sandbox` policy is non-empty **and** the service runs with
`KEEL_SANDBOX=docker`. With an empty policy, or `KEEL_SANDBOX=none`, there are no sandbox tools. The `memory` tool
is still offered when the profile's `memory` flag is on.

### The policy

Set on `PUT /v1/profiles/{id}` and checked there, then checked again when a run starts. Only these keys are accepted;
any other key is a `400 invalid_input`.

| Key | Meaning |
|---|---|
| `image`, `workdir` | Required. The image to run and the starting directory. |
| `runtime` | `runc` (default), `runsc` (gVisor) or `kata`. |
| `network` | An existing Docker network to join; none given means Docker's default. |
| `mounts` | `{source, target, read_only}`. With `KEEL_SANDBOX_MOUNT_ROOT` set, every `source` must be under it. |
| `env` | Variables for the container. |
| `user` | `uid[:gid]` to run as. |
| `cpus`, `memory_mb`, `pids_limit` | Resource limits. |
| `skills_path` | A directory in the container that holds skills. |

### One container per profile

```mermaid
flowchart LR
    R["run starts"] --> I["docker inspect keel-sbx-hash"]
    I -->|"missing"| N["docker run -d --init ... sleep infinity"]
    I -->|"policy digest differs, nothing running"| RM["docker rm -f, then docker run"]
    I -->|"policy digest differs, a command is running"| W["keep it; warn; new policy applies when idle"]
    I -->|"same digest, stopped"| ST["docker start"]
    I -->|"same digest, running"| U["use it"]
    N --> U
    RM --> U
    ST --> U
    W --> U
    U --> EXEC["each tool call: docker exec"]
    EXEC --> IDLE["no use for KEEL_SANDBOX_IDLE_SECONDS<br/>and nothing running: the reaper removes it"]
```

- **Name and labels.** A generation's container name starts with `keel-sbx-` plus the profile; a token makes it
  unique per generation, and the ledger records its exact container ID. Labels `keel.managed=1`, `keel.deployment`,
  `keel.profile`, `keel.policy` (the policy digest), `keel.generation` and the create operation let keel compare its
  own containers and nobody else's. Every later operation uses the recorded exact ID, never the name.
- **A profile's sessions share its generation.** A container is per profile, not per session or per run. keel keeps
  a working directory per session but does not isolate one session from another inside the container; if that
  matters, use a separate profile for each independent account, including accounts in the same tenant.
- **Idle reaping.** A minute-by-minute maintenance pass removes a generation idle for
  `KEEL_SANDBOX_IDLE_SECONDS` (default 30 min), by exact ID, once its usage is released. The next run gets a new
  generation.
- **Inventory and leftovers.** At start and in each maintenance pass, the coordinator classifies every container of
  the deployment in bounded batches. An unknown, legacy or conflicting container is kept as evidence and blocks only
  its profile's sandbox admission; it is never removed by profile name or by an in-flight query. A generation with an
  incompatible policy or helper is replaced by recovery after its users finish. An unresolved create is retained as
  a tombstone so it is never created twice.
- **Killing a command.** Stopping a run resolves the recorded execution ticket and asks the supervisor that started
  the command to cancel it: `TERM`, the grace period, then `KILL`, followed by a receipt proving the leader and its
  descendants are gone. No host-side numeric PID or process-group lookup is involved. The current command's id is
  kept in `runs.current_exec_id`; the lease sweeper hands a lost run to coordinator recovery instead of signalling
  the command itself ([§5](#5-workers-leases-and-recovery)).
- **Secrets stay off the command line.** Policy `env` goes through a private temporary `--env-file` (mode 0600, in a
  0700 directory, deleted right after), so values never appear in `ps`.

### Hardening

Every container is started with `--cap-drop ALL` plus only `DAC_OVERRIDE`, `CHOWN` and `FOWNER`;
`--security-opt no-new-privileges`; `--init`; and tmpfs for `/tmp` (512 MB, `nosuid`), `/var/tmp` and `/run`. CPU,
memory and PID limits and the OCI runtime come from the policy.

`sandbox/local.py` runs commands as plain host processes. It isolates nothing and exists for tests and local
development.

---

## 9. Tools

Eight tools are offered when a profile has a sandbox; their schemas live in `prompts/tools/*.json`. The `memory`
tool is separate: it is offered when the profile's `memory` flag is on, with or without a sandbox.

| Tool | What it does | Limits |
|---|---|---|
| `terminal` | Runs a shell command; reports exit code and the directory it ended in. | Timeout 180 s by default, 600 s at most. |
| `execute_code` | Runs a Python script. | 300 s. |
| `read_file` | Reads with line numbers, `offset` and `limit`; refuses binary files. | 10 MiB read, 2000 lines, 2000 characters per line. |
| `write_file` | Writes a file, creating parent directories. | |
| `patch` | Replaces `old_string` with `new_string`; the match must be exact and unique. | |
| `search_files` | Content search (modes: content, files only, count; optional context lines and file glob) or file search, with `rg` when the image has it and `grep`/`find` otherwise. Hidden directories are skipped, and with `rg` gitignored files too; a search with no matches says when hidden or ignored files would match. | 50 results by default. |
| `skills_list`, `skill_view` | List the enabled skills; read one. | |

Cross-cutting behaviour (`tools/base.py`):

- **One entry point.** `run_tool` looks the tool up, parses the JSON arguments, runs it, and turns every failure into
  an error result. An unknown tool, bad JSON, a sandbox error and an unexpected exception all become text the model
  can act on.
- **Secrets are redacted.** Output is scrubbed for common secret shapes (private-key blocks, provider tokens,
  `user:password@` URLs) before the model sees it.
- **Results are capped.** A result over 100 000 characters is shortened while staying valid JSON.
- **Previews.** Each call has a short preview (`tool.started` / `tool.completed`) so a client can show progress
  without the full arguments.

### Memory

The `memory` tool adds, replaces or removes entries, and several changes can go in one batch. A write does not
change the session's stored system prompt.

Two targets, each a list of short strings: `user` is who the user is, and `memory` is notes about the environment.
The joined text of each is limited by `KEEL_USER_CHARS` (1375) and `KEEL_MEMORY_CHARS` (2200).

The system prompt includes a snapshot of both, taken when the prompt is built. It stays frozen for that session. A
write appears in the next session's prompt, and in this session when the prompt is rebuilt at a compaction.

Every `KEEL_MEMORY_REVIEW_TURNS` (10) completed runs of a memory-enabled profile, a background review may save
facts. It does not add messages to the session, and a review already running for that profile is skipped.

### Skills

A skill is a directory with a `SKILL.md` whose front matter has a name and a description. keel reads them **through
the sandbox** from the policy's `skills_path` (the deployment ships them in the image or a mount; they are read-only),
keeps those the profile enables, and puts an index (categories, names, descriptions cut to 60 characters) in the
system prompt. The model loads a skill's full text with `skill_view` only when it needs it.

---

## 10. The system prompt

Built **once per session** (and again only at a compaction, §6) and stored in `sessions.system_prompt`, so every
later turn sends a byte-identical prefix, which is what makes provider-side prompt caching work. In order, stable first:

1. **Identity** — the profile's `soul`, or `You are a helpful assistant.`
2. **Guidance**, then memory guidance when memory is on, then the **platform** text (tool use and environment rules).
3. **Skills** header, index and footer, when there are skills.
4. **Volatile context** — the current date (as of the build, so refreshed at a compaction), the model and the provider.
5. **Memory snapshot** — both targets, when memory is on.
6. **Environment** — the working directory.

Without tools the prompt is the identity alone. Per-run `instructions` (text a client sends with each run) are a
separate field of the model request and never change the stored prompt; the provider appends them after the system
prompt. The prompt-cache key is a hash of the session id, the system prompt and the tool definitions, and leaves the
per-run instructions out, so instructions that change from run to run do not break the cached prefix.

---

## 11. Model providers

```mermaid
flowchart LR
    X["RunExecutor"] --> RT["stream_with_retries"]
    RT --> P{"ProviderRegistry.get(run.provider)"}
    P --> BR["bedrock<br/>BedrockMantleProvider"]
    P --> OA["openai<br/>OpenAIProvider"]
    BR --> TR["ResponsesTransport<br/>OpenAI Responses API over SSE"]
    OA --> TR
    BR -.->|"SigV4 signing, AWS credentials"| MANTLE["bedrock-mantle.&lt;region&gt;.api.aws"]
    OA -.->|"bearer key, KEEL_OPENAI_BASE_URL"| OPENAI["api.openai.com or a compatible server"]
```

A provider implements two things: `capabilities(model)` (context window, vision, reasoning replay, parallel tool
calls, prompt-cache style) and `stream(request)`, which yields `TextDelta`, `ToolCallReady`, `ReasoningReady` and a
final `Completed` with `Usage`. Provider failures leave only as `ModelError` with one of seven codes, so nothing above
the provider depends on a vendor's error types.

- **`bedrock`** is always registered: OpenAI-family models on Bedrock Mantle, signed with SigV4 from the usual AWS
  credential chain; 272k context, vision, reasoning replay, prompt cache by key.
- **`openai`** is registered when `KEEL_OPENAI_API_KEY` is set: bearer auth, `KEEL_OPENAI_BASE_URL` for a compatible
  server, `KEEL_OPENAI_COMPAT=1` to leave out request fields strict servers reject, and
  `KEEL_OPENAI_CONTEXT_WINDOW` (default 128 000). The key is held with `repr=False`, scrubbed from error text, and
  authentication errors get a fixed message.
- **Retries** (`providers/retry.py`): `rate_limited`, `server_error` and `timeout` are retried up to three attempts
  with exponential backoff (5 s, doubling to 120 s, with jitter; a `Retry-After` is honoured up to 600 s) — but **only
  while the call has produced nothing**, because output already sent to a client cannot be taken back.
- **Adding a provider** means implementing the protocol and registering it in `service.default_providers`; nothing
  else changes.

Which models a profile may use is its allow-list (`models`); a run naming a model outside it is refused
(`400 model_not_allowed`).

---

## 12. HTTP API

All under `/v1/profiles/{profile_id}` except `/healthz`. Details are in the [API guide](guide/api.md).

| Route | Purpose |
|---|---|
| `PUT /` | Create or replace a profile: `models`, `sandbox`, `soul`, `skills`, `memory`. |
| `POST /runs` | Start a run; `202` with `run_id`, `status`, `session_id`. |
| `GET /runs`, `GET /runs/{id}` | List runs (`status=active` by default) or read one. |
| `POST /runs/{id}/stop` | Stop a run. |
| `GET /runs/{id}/events?after=` | The SSE stream. |
| `GET /sessions`, `GET /sessions/{id}/messages`, `GET /sessions/{id}/boundary` | List sessions; read the transcript; the newest message id. |
| `PATCH /sessions/{id}`, `DELETE /sessions/{id}` | Rename; delete (refused while a run is in flight). |

**Authentication** uses tenant-bound service keys: `Authorization: Bearer <key>`, compared in constant time.
`/healthz` is open. Tenant IDs are signed 32-bit integers (0 and negatives included); each tenant owns multiple
independent profiles. The product authenticates end users and maps each tenant account to its profile. keel validates
the key's tenant and the profile's ownership, while sessions, memory and private sandbox state stay per profile.

**Errors** are always `{"error": {"code", "message"}}`. The codes a client sees: `unauthorized`, `unavailable`,
`invalid_input`, `run_not_found`, `session_not_found`, `session_busy`, `at_capacity` (429), `model_not_allowed`,
`provider_unavailable`, `model_not_vision_capable`, `title_in_use`.

### Sessions, titles and images

- **Sessions** are created by the first run that names them (or get a generated id). They persist the transcript,
  the system prompt (fixed between compactions) and the working directory.
- **Titles** come from the first user message (cleaned and bounded), then from a model call after the first
  completed run (`title_source` `llm`, between `derived` and `user`). Collisions get `#2`, `#3`, … with no cap.
  Rank is `user` > `llm` > `derived`, and a user's title is never overwritten by a lower-ranked one. There is no
  new event; a client reads the title from the session.
- **Images** (PNG, JPEG, GIF, WebP; at most 4 per run, 5 MiB each, length checked before decoding) are stored as
  blobs; messages keep only a reference, and the bytes are loaded when the model request is built. A missing blob
  becomes the text `[image no longer available]`. A model without vision refuses them.

---

## 13. Deployment shapes

```mermaid
flowchart LR
    subgraph one["Single host"]
        direction TB
        K1["keel"] --- PG1[("Postgres")]
        K1 -->|"docker.sock"| D1["Docker daemon + sandboxes"]
    end
    subgraph dev["Development"]
        direction TB
        K2["keel, KEEL_SANDBOX=none or docker"] --- PG2[("Postgres")]
    end
```

- **Production shape today:** one keel container with Postgres, and the host's Docker socket mounted for sandboxes.
  Each sandbox joins a network you choose; keel's database should sit on a network sandboxes cannot reach.
- **Development:** PostgreSQL, with `KEEL_SANDBOX=none` for chat only or the host-process backend in tests.
- **Scaling out** is what the design is for (stateless workers, claims with `SKIP LOCKED`, cross-instance wake-ups),
  and several instances can share one Postgres database.

Configuration is entirely environment variables, validated at start (`config.py`); the list is in the
[configuration guide](guide/configuration.md).

---

## 14. Security model

- **The service key** protects the whole API. Whoever has it can read every profile's transcripts and start runs.
  Keep it in the product's backend, never in a browser.
- **The Docker socket is root on the host.** keel can start any image with any mount the policy allows. Limit that
  with `KEEL_SANDBOX_MOUNT_ROOT` (mounts must be under it), a policy that comes only from your own backend, and a
  hardened daemon (rootless Docker or a dedicated host).
- **Sandboxes are the boundary for model-run code.** Dropped capabilities, `no-new-privileges`, resource limits, an
  optional gVisor or Kata runtime, a network you choose (or none), and read-only mounts where you want them.
  Sandboxes do not share keel's database network.
- **Secrets** are redacted from tool output, never put on a command line, and the OpenAI key is scrubbed from errors.
- **Tenancy** is by profile: every route is scoped to the profile in its path, and a session id under another
  profile answers 404. It is not a defence against a client that holds the service key.

The reporting policy for vulnerabilities is in [SECURITY.md](../SECURITY.md).

---

## 15. Known limits

- **Not built:** scheduled jobs, approvals, web search or browsing, MCP servers, agent-written skills, a provider
  for Anthropic's own API.
- **Postgres needs direct connections** for `LISTEN`; a transaction-mode pooler will not do.
- **Pre-1.0:** the API and schema may change between releases (see the [changelog](../CHANGELOG.md)).

---

## 16. Testing

```bash
docker compose -f docker-compose.test.yml up -d --wait
KEEL_TEST_POSTGRES_DSN=postgresql://keel:keel_test@127.0.0.1:55437/keel_test .venv/bin/python -m pytest -q
docker compose -f docker-compose.test.yml down -v
```

The test compose file runs PostgreSQL 17 on `127.0.0.1:55437`. Integration tests require
`KEEL_TEST_POSTGRES_DSN`; a missing DSN fails those tests. Each test creates and drops its own unique database,
so the configured user needs `CREATEDB`; the supplied base database is never cleared. To run only unit tests
without a database, use `.venv/bin/python -m pytest -q -m 'not integration'`.

The loop uses a fake provider and the host-process sandbox. Optional suites switch on with environment variables:

| Variable | Adds |
|---|---|
| `KEEL_TEST_DOCKER=1` | the Docker sandbox against a real daemon (`KEEL_TEST_DOCKER_IMAGE` picks the image) |
| `KEEL_TEST_SUBPROCESS=1` | a server process killed with `SIGKILL` and restarted (fault injection) |
| `KEEL_LIVE_BEDROCK=1` with `KEEL_TEST_DOCKER=1` | one real model run that calls `terminal` in a sandbox |

CI runs the unit and PostgreSQL integration suites, the Docker sandbox tests and an image build on every push.
