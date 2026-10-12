# M2: agent loop, tools and sandbox — design

Parent design: `docs/design/2026-10-11-architecture.md` (sections are cited as "arch §N"). M1 serves tool-less single-turn runs. M2 turns a run into an agent: the model calls tools that
run in a per-profile sandbox until it answers in text.

## Goal and gate

M2 is done when a consumer's own checks pass against agent-keel deployed beside its previous runtime,
with users still served by the previous runtime:

- **Contract checks.** The consumer's behaviour checks all pass: run lifecycle, stop during a command,
  multi-turn, image input, attachments, sessions, stored messages and unknown run.
- **Quality eval.** A quality eval against the previous runtime's recorded baseline stays within agreed
  bounds on correctness, failures, latency, tokens and cache hit rate.

The baseline is recorded where the consumer's data lives, so the gate is measured there.

## Changes to the milestone plan (arch §13)

| Item | Arch | M2 | Why |
|---|---|---|---|
| Image input | M3 | **M2** | A consumer's image-input check runs whenever the model has vision. |
| Stop kills a running command | M3 | **M2** | A check that stops a run mid-command expects it to end well within the client's stop timeout. Fault injection (worker kill, sandbox loss, provider 5xx) stays in M3. |
| `memory` tool and background review | M2 tool list | **M3** | Memory is an M3 feature (arch §7). The eval profile must not accumulate memory between rounds anyway. |
| Parallel read-only tools | M2 | **later** | Tools run one at a time. Latency is dominated by model calls and `terminal`, and parallelism can come later behind the same loop. |

M2 therefore ships eight tools: `terminal`, `execute_code`, `read_file`, `write_file`, `patch`,
`search_files`, `skill_view` and `skills_list`.

Still out of M2: compression, model-generated titles, memory, lease-recovery fault tests and the Hermes
import (all M3/M4).

## 1. The agent loop

A run executes in `RunExecutor` (M1), now as a loop.

1. **Build the request.** Use the session's stored system prompt (§2) plus the run's `instructions`, the
   session history, and the tool definitions (§3).
2. **Stream the model call.**
   - Text deltas become `message.delta`.
   - Reasoning items and tool calls are collected.
   - Usage is summed over all calls of the run.
3. **Commit the assistant message.** It holds the text, the reasoning items and the tool calls, in the
   order of arch §5's contract.
4. **Answer in text?** If the response has no tool calls, the run completes. `run.output` is this final
   message's text.
5. **Run the tools.** Otherwise run each call in order:
   - emit `tool.started`;
   - execute the call (§3);
   - commit a `tool` message (`tool_call_id`, `tool_name`, content = the tool's result string);
   - emit `tool.completed`.

   Then go to 1.
6. **Iteration cap.** The default is 60, a per-deployment setting. When the cap is reached, the loop
   appends Hermes's final-answer request (arch §4) as a user-role message that is not stored, and makes
   one last call with no tools offered.

Rules:

- **A tool failure is a result, not a run failure.**
  - Bad arguments, a non-zero exit code, a missing file and a sandbox timeout all go back to the model
    as the tool's result, with `tool.completed.error = true`.
  - Only provider errors (arch §5), a sandbox that cannot be created at all, and internal errors fail
    the run.
  - A sandbox that cannot be created gives `error_code: sandbox_unavailable`.
- **Unknown tools and malformed JSON arguments** are answered with an error result naming the problem,
  as Hermes does. The model then corrects itself.
- **Reasoning replay.** Reasoning items from earlier iterations of the same run, and from earlier turns,
  are replayed under arch §5's rule: same provider and model only. Mantle requires this for multi-step
  tool use.
- **Retries.** `stream_with_retries` (M1) applies per model call. A retry after output has started is
  still forbidden.
- **What a message commit holds.** Every commit appends to `messages` in one transaction. A run
  therefore stores one user message, then alternating assistant and tool messages. This is the shape
  clients expect: assistant `tool_calls` in OpenAI shape, and tool results with
  `tool_call_id` and `tool_name`.

## 2. System prompt

Arch §4's three tiers, built **once per session** when its first run starts, stored in
`sessions.system_prompt`, and reused for every later turn. This keeps the cached prefix byte-identical
across turns.

| Tier | Content | Source |
|---|---|---|
| Stable | the profile's `soul`, then Hermes's task-completion, tool-use and skills guidance | the profile, and text taken from Hermes |
| Context | Hermes's `api_server` platform hint | text taken from Hermes |
| Volatile | the skills index (§4) and a date-only timestamp | built at session start |

- **Where the Hermes text comes from.** The texts are taken from Hermes `f97608f178`. The authority is
  the system prompt Hermes actually stored for a session (its `sessions.system_prompt`), not a
  reading of the builder code. The plan's first task captures that prompt and keeps the reusable parts
  as files under `src/keel/prompts/`, with the MIT notice.
- **No memory tier.** Memory guidance and the memory snapshot are absent in M2; they arrive in M3.
- **Where `instructions` go.** A run's `instructions` are appended at call time as
  `system + "\n\n" + instructions`, unchanged from M1.
- **The cache key.** `prompt_cache_key` stays the M1 hash over scope, system and tools. A test asserts
  that the system text and the tool list are identical across two turns of a session.

The M1 schema has no `sessions.system_prompt`. M2 adds it, along with the other columns below. M1
databases are disposable (README), so M2 needs a fresh database rather than a migration.

## 3. Tools

The schemas and descriptions are Hermes's own:
- they are captured from the tool definitions Hermes sends at `f97608f178`, for the toolsets keel
  needs;
- they are stored as JSON under `src/keel/prompts/tools/`;
- they are changed only where a feature is absent in keel, as listed below.

Consumers' prompts are tuned against these texts.

| Tool | Behaviour in keel | Differences from Hermes |
|---|---|---|
| `terminal` | Runs `command` in the sandbox through `bash -lc`, in the session's working directory (§5). Returns combined output and the exit code in Hermes's result shape. `timeout` defaults to 180 s, with a maximum of 600 s. On timeout the process tree is killed and the result says so. | No background processes, PTY or sudo. The parameters for those are removed from the schema, and the timeout text no longer mentions promotion to a background process. |
| `execute_code` | Writes the script to a temporary file in the sandbox, runs `python3` on it with the same timeout rules, then deletes the file. | No persistent kernel and no RPC bridge to tools (arch §6). |
| `read_file` | Reads with line numbers, `offset`/`limit`, Hermes's size cap and its binary-file refusal. | — |
| `write_file` | Creates parent directories and writes the content. | No write approval (the deployment runs unattended). |
| `patch` | The modes in Hermes's schema, implemented as a read, an edit in keel, then a write. Hermes's matching rule is kept: an exact unique match for replace. Hermes's V4A patch parser is not taken. | — |
| `search_files` | `content` search uses `grep -rn` in the sandbox, with `rg` when the image has it; `files` search uses `find`. Hermes's limits and output format apply. | — |
| `skills_list` | The enabled skills' names, descriptions and categories (§4). | — |
| `skill_view` | A skill's `SKILL.md`, or a file inside the skill's directory, together with the directory's path in the sandbox. | — |

**Result handling.**
- Results are strings, using Hermes's formats; JSON results keep Hermes's keys.
- Output is truncated at 50,000 characters: 40% head, 60% tail, with Hermes's marker (arch §4).
- Output over 100,000 characters is written to `<workdir>/.keel/outputs/<tool_call_id>.txt` in the
  sandbox and replaced with a preview and that path.

**Events.**
- `tool.started`: `tool_call_id`, `tool`, `preview`.
- `tool.completed`: `tool_call_id`, `tool`, `duration_s`, `error`, `preview`.
- Previews are at most 500 characters. For `terminal` the preview is the command; for file tools, the
  path; for `execute_code`, the script's first line.
- Values matching secret patterns are redacted before a preview is stored or sent. The patterns are
  AWS keys, bearer tokens, `*_TOKEN=`/`*_KEY=`/`*_SECRET=` assignments, and private key blocks.

## 4. Skills

- **Where skills live.** Skills are read through the sandbox, never from a keel host path. The profile's
  sandbox policy names `skills_path`, a directory inside the sandbox. The deployment mounts it read-only.
- **What a skill is.** Each `skills_path/<category>/<name>/SKILL.md`, or `skills_path/<name>/SKILL.md`,
  whose YAML front matter has `name` and `description`.
- **Which are visible.** Only names listed in the profile's `skills` are visible to the model. An empty
  list means none are visible.
- **The skills index.** It goes into the volatile prompt tier at session start, formatted as Hermes
  formats it. `skill_view` reports the skill's directory as an absolute sandbox path, so instructions
  such as "run `scripts/x.py`" resolve inside the sandbox.

## 5. Sandbox

### Interface

Arch §6's `SandboxBackend`, with M2's signatures:

```
ensure(profile_id, policy) -> Sandbox
exec(sandbox, command: list[str], *, cwd, timeout_s, env=None, stdin=None) -> ExecResult
    # ExecResult: exit_code, output (stdout+stderr interleaved), timed_out, duration_s
read_file(sandbox, path, max_bytes) -> bytes
write_file(sandbox, path, data: bytes) -> None
kill_tree(sandbox, exec_id) -> None
destroy(sandbox) -> None
```

- **Operation ids.** Every `exec` gets an `exec_id`. The worker keeps the run's current `exec_id`, so
  stop can kill it (§7).
- **File operations.** `read_file`, `write_file` and listing go through `exec` with fixed helper
  commands (`cat`, `base64`, `find`). That way a backend needs only exec, and paths are never
  interpreted on the keel host.

### The sandbox policy

The policy is the profile's `sandbox` object, set with `PUT /v1/profiles/{p}`. keel validates it; the
policy names nothing about any consumer.

```json
{
  "image": "registry/exec-image:tag",
  "network": "sandbox-net",
  "mounts": [{"source": "/host/path", "target": "/in/sandbox", "read_only": false}],
  "env": {"NAME": "value"},
  "user": "1000:1000",
  "cpus": 2,
  "memory_mb": 1024,
  "pids_limit": 256,
  "workdir": "/in/sandbox",
  "skills_path": "/in/sandbox/skills",
  "runtime": "runc"
}
```

- **Required fields:** `image`, `workdir` and `runtime`. `runtime` is one of `runc`, `runsc`, `kata`
  (arch §6); M2 is verified on `runc`.
- **Host paths.** `mounts[].source` is a path on the Docker host, and keel never opens it. The consumer
  provides it because it owns the host layout. A deployment can restrict sources to a prefix
  (`KEEL_SANDBOX_MOUNT_ROOT`); a policy outside it is refused with 400.
- **Values in env.** `env` values are stored in the profile and passed to the container. They are never
  echoed in events or logs.
- **Policy changes.** A changed policy is detected by a hash label on the container. The container is
  recreated on next use, once no command is running in it.

### Docker backend

The backend uses the `docker` CLI, through `asyncio.create_subprocess_exec` with argument lists and no
shell on the keel side. Hermes drives Docker the same way. The keel image therefore ships the static
`docker` client.

- **One container per profile.** It is named `keel-sbx-<profile>-<hash8>`, with labels for the keel
  deployment, the profile and the policy hash. It is started with `docker run -d --init` and the
  policy's limits, network, mounts, env, user and `--runtime`, and runs `sleep infinity`. Sessions of
  one profile share it.
- **Commands.** `docker exec` runs `setsid bash -lc '<command>'` with a PID-file marker, so the command
  is its own process group. `kill_tree` sends `TERM` to the group, then `KILL` after 3 s, through a
  second `docker exec`.
- **Working directory.** The working directory is per session, `<workdir>`, unless the consumer's
  instructions say otherwise. It is carried between commands by an in-band marker: the wrapper prints
  `pwd` after the command, keel strips it from the output, and the next command starts there. The value
  is stored on the session (`sessions.cwd`).
- **Recovery.** A missing container is recreated, and the call retried once.
- **Idle and orphaned containers.** Containers idle for `KEEL_SANDBOX_IDLE_SECONDS` (default 1800)
  are removed by the sweeper. At start, containers with this deployment's label that no live run uses
  are removed. This is safe in M2 because it runs a single instance (M1 README).

### Security

- **Docker socket.** keel needs the Docker socket, which is equivalent to root on the host. It runs
  under the same exposure as Hermes: one service, not reachable beyond a private network,
  service-key auth.
- **Isolation.** The sandbox image runs as the policy's non-root user. `runsc` is the next step (arch
  §6).
- **Instance metadata.** Blocking the instance metadata service from the sandbox network stays its own
  deployment task (arch §12). M2 leaves network policy to the deployment.

## 6. Images in input

- **API.** `POST …/runs` accepts parts `{"type": "image", "media_type": "image/png|jpeg|gif|webp",
  "data": "<base64>"}`. Limits: 4 images per run and 5 MB decoded each. Larger or unknown types give
  400 `invalid_input`.
- **Storage.** Image bytes are stored once in a new `blobs` table (`id`, `profile_id`, `media_type`,
  `data`, `created_at`). The message part keeps `{"type": "image", "blob_id", "media_type"}`, so images
  are referenced and never flattened (arch §3).
- **Provider side.** The Responses transport sends a stored image as an `input_image` data URL. Later
  turns replay it, so the model can still refer to it. Providers without vision (`Capabilities.vision`)
  get a 400 `model_not_vision_capable` at run start.
- **Messages API.** `GET …/messages` returns image parts as `{"type": "image", "media_type",
  "blob_id"}`, without the bytes.

## 7. Stop during tools

Arch §10 for M2:

- **`POST stop` on a running run** sets `stopping`, as in M1. The worker's stop watcher then does
  three things:
  - cancels the model stream, if one is open;
  - calls `kill_tree` on the run's current `exec_id`, if a command is running;
  - lets the executor's cancel path finish the run `cancelled`.
- **Unanswered tool calls.** Every tool call of the last assistant message that has no result gets a
  synthetic tool message: content `"The command was cancelled."`, `tool_call_id` set. The next turn's
  request is then valid.
- **Bound on stopping.** Kill plus finish stays well within the client's stop timeout. The kill escalates
  from `TERM` to `KILL` in 3 s.

## 8. Data model changes

M2 needs a fresh database (no migrations yet).

- **`sessions`:** adds `system_prompt` (text), `provider`, `model` and `cwd`. `cwd` is the session's
  current working directory in the sandbox.
- **`messages`:** `tool_calls`, `tool_call_id` and `tool_name` are now written. M1 already had the
  columns.
- **`blobs`:** a new table (§6).
- **`runs`:** adds `current_exec_id`. The worker writes it, and the stop path reads it from the same
  process.
- **`run_events`:** the new event types from §3.

## 9. Configuration

| Variable | Default | Meaning |
|---|---|---|
| `KEEL_MAX_ITERATIONS` | 60 | Tool-calling iterations per run before the final no-tool call |
| `KEEL_SANDBOX_IDLE_SECONDS` | 1800 | Idle sandbox containers are removed after this |
| `KEEL_SANDBOX_MOUNT_ROOT` | unset | When set, every `mounts[].source` must be under it |
| `KEEL_DOCKER` | `docker` | The docker CLI to run |

## 10. Testing

- **Unit tests** cover the loop, tools, prompt and stop paths. They run against the fake provider (M1,
  extended with scripted tool calls) and a `FakeSandbox` that records exec calls and serves files from
  memory.
- **Golden tests:**
  - the captured Hermes prompt pieces and tool schemas load unchanged;
  - the system prompt and tool list are byte-identical across two turns of one session;
  - `instructions` are appended last.
- **Docker integration tests** are opt-in (`KEEL_TEST_DOCKER=1`) and use a small public image. They
  cover:
  - `ensure`, then exec, then a carried working directory;
  - timeout kills the process tree;
  - `kill_tree` during `sleep`;
  - a vanished container is recreated;
  - a policy change recreates the container;
  - idle reaping.
- **Live run (`KEEL_LIVE_BEDROCK=1`).** One model run calls `terminal` (`echo`) in a Docker sandbox and
  answers with its output.
- **The gate itself** runs in the consumer's own repository: contract checks, then quality rounds
  against its recorded baseline.

## 11. The consumer's side

A client sets a profile up in one `PUT` (models, sandbox policy, soul, skills) and maps
`tool.started`/`tool.completed` events to its own UI.

## 12. Risks

- **Answer quality depends on exact texts.** Prompt and tool-description drift from Hermes is the most
  likely cause of a quality miss. Mitigation: the texts are captured, not rewritten, and a miss is
  diagnosed by diffing keel's request against Hermes's stored prompt and tool list for the same case.
- **Tokens and latency.** The gate bounds both. Extra tokens most likely come from tool-output formatting
  and replayed reasoning; extra latency from container start-up. Mitigation: Hermes's truncation, warm
  per-profile containers, and the cache-stability test.
- **Docker-socket exposure** is the same as with Hermes. Reducing it (`runsc`, a socket proxy
  limited to the needed endpoints) is follow-up work.
