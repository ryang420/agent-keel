# Command runs

Status: accepted for 0.3.2 (2026-10-31).

## Goal

A consumer sometimes needs to run a known program in a profile's sandbox with no model involved, for example
re-running a script that an earlier model run wrote and a person checked. Today the only way is a model run told to
call `terminal`: it costs tokens, depends on the model doing exactly that, and is capped at 600 s per call. A command
run executes one argv in the sandbox and reports its exit code and output.

## API

`POST /v1/profiles/{p}/runs` takes `kind`: `"model"` (the default; requests without `kind` are unchanged) or
`"command"`:

```json
{"kind": "command", "session_id": "job-1",
 "command": {"argv": ["python3", "job.py"], "cwd": "/work/jobs/1", "timeout_s": 1800, "env": {"MODE": "full"}}}
```

- A command run takes no `provider`, `model`, `input`, `instructions`, `reasoning_effort` or `file_context`; sending
  any of them is 400 `invalid_input`, and so is `command` on a model run.
- The profile needs a sandbox. Its `models` may be empty: a profile used only for command runs needs no model.
- `argv`: 1 to 64 strings, at most 16,384 UTF-8 bytes in total, the first not empty, no NUL.
- `cwd`: absolute, at or under the sandbox `workdir` after normalisation.
- `timeout_s`: a number from 1 to `KEEL_COMMAND_TIMEOUT_MAX_S` (default 3600).
- `env`: optional, the same rules as `sandbox.env`; set over the policy env for this command only.
- Text that cannot be encoded as UTF-8 is refused: it could not be handed to `docker` and would leave the exec
  without a cleanup receipt.
- Tenant and profile checks, one in-flight run per session, `KEEL_MAX_RUNNING` and stop are those of a model run.

`GET …/runs/{id}` and the run list report `kind` for every run and `command` for a command run.

## Execution

The worker claims a command run like any other run: the run lease, the sandbox usage, heartbeats and stop supervision
do not change. `RunExecutor.execute` branches on `run.kind`:

1. `run.started` with `{"kind": "command"}`.
2. Prepare the bound sandbox from the claim's usage, as a model run does before its first tool.
3. `command.started` with `argv` and `cwd`.
4. One coordinated exec with a fresh exec id, the run's `timeout_s` and its `env`.
5. `command.completed` with `exit_code`, `timed_out`, `duration_s` and `output`: the last 50,000 characters of the
   merged stdout and stderr (the sandbox already caps the stream at 8 MiB).
6. Exit 0 completes the run with the output tail as its output. A non-zero exit fails it with `command_failed`, a
   timeout with `command_timed_out`, and an exec whose cleanup receipt could not be verified with
   `sandbox_uncertain` (its usage stays protective for recovery, as for a tool). A failed run keeps the output tail
   as its output too. Usage is all zero.

The exec starts in `/`, and a fixed `sh` prologue enters `cwd` and then `exec`s the argv. A `docker exec -w` into a
directory that does not exist fails before the sandbox supervisor starts, which would leave the exec without a
cleanup receipt; with the prologue a missing directory is an ordinary exit with code 125. The argv items are
positional parameters of the prologue and are never parsed by a shell.

A stop cancels the execution and the existing `_kill_command` path sends the exec's one supervisor control; the run
ends `cancelled` without `command.completed`. Provider lookup, the system prompt, compaction, titles, memory review
and the iteration cap do not apply. A command run writes no messages to its session.

## Schema version 8

`runs.kind` (`model` or `command`, default `model`) and `runs.command` (JSON text) are added, and `runs.provider` and
`runs.model` become nullable, under a named check `runs_kind_fields`: a model run has a provider and a model, and a
command run has a command. PostgreSQL alters the table in place with idempotent statements; SQLite cannot drop
`NOT NULL`, so it rebuilds `runs` with the same columns and indexes. A 0.3.1 instance refuses a database at schema
version 8.

## Security

A command run gives a caller nothing it lacks today: any holder of the tenant key can already have a model run execute
arbitrary commands through `terminal`, in the same sandbox under the same policy. The command runs under the sandbox
supervisor, so timeouts, cancellation and the receipt checks apply. The `cwd` rule catches mistakes; it is not a
boundary (the sandbox policy is).

## Not included

- Streaming output while the command runs; a client reads `command.completed` or the run's output.
- A message in the session transcript: a later model run in the same session does not see a command run.
- Saving the full output to a file, as `terminal` does for long output: the command can write its own files.
