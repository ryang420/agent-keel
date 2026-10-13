# HTTP API

Base path `/v1`. Every request except `GET /healthz` needs `Authorization: Bearer <KEEL_API_KEY>`. Bodies and
responses are JSON, except the event stream. IDs (profile, session) are 1-128 characters: letters, digits, `.`,
`_`, `-`, starting with a letter or digit.

Timestamps (`created_at`, `updated_at`, `at`, …) are numbers.

Errors have one shape, with a machine-readable code:

```json
{"error": {"code": "profile_not_found", "message": "No such profile."}}
```

## Endpoints

| Method and path | Purpose |
|---|---|
| `GET /healthz` | `{"status": "ok"}`, or 503 `unavailable` when the store does not answer within 3 s. No key needed. |
| `PUT /v1/profiles/{profile_id}` | Create or replace a profile. |
| `POST /v1/profiles/{profile_id}/runs` | Start a run (202). |
| `GET /v1/profiles/{profile_id}/runs?status=` | List runs. `status` is `active` (default: queued, running, stopping) or a comma-separated list of statuses. Returns `{"data": [run, …]}`. |
| `GET /v1/profiles/{profile_id}/runs/{run_id}` | One run. |
| `POST /v1/profiles/{profile_id}/runs/{run_id}/stop` | Stop a run. Returns the run. |
| `GET /v1/profiles/{profile_id}/runs/{run_id}/events?after=` | Server-sent events of a run. |
| `GET /v1/profiles/{profile_id}/sessions?limit=` | List sessions, most recent first. `limit` 1-500, default 100. |
| `GET /v1/profiles/{profile_id}/sessions/{session_id}/messages` | Messages of a session. |
| `GET /v1/profiles/{profile_id}/sessions/{session_id}/boundary` | `{"last_message_id": …}`. |
| `PATCH /v1/profiles/{profile_id}/sessions/{session_id}` | Rename: `{"title": "…"}`. Returns the session. |
| `DELETE /v1/profiles/{profile_id}/sessions/{session_id}` | Delete a session: `{"deleted": true}`. |

## Profile

`PUT /v1/profiles/{profile_id}` body (all fields optional):

```json
{
  "models": [{"provider": "bedrock", "model": "openai.gpt-6-luna"}],
  "sandbox": {"image": "python:3.12-slim", "workdir": "/work"},
  "soul": "You are a careful data assistant.",
  "skills": ["my-skill"],
  "memory": false
}
```

- `models`: the provider and model pairs runs may use.
- `sandbox`: the policy; see [sandbox.md](sandbox.md). Empty or missing means the sandbox tools are not offered.
- `soul`: text that replaces the default identity ("You are a helpful assistant.") in the system prompt. A session builds its system prompt once, at its first run, so a changed `soul` or `skills` list applies to new sessions.
- `skills`: the names of the skills to enable.
- `memory`: when true, the model is offered a `memory` tool (with or without a sandbox) and the system prompt
  includes a snapshot of stored facts, frozen until that prompt is rebuilt. Omitted, it is false, including when
  replacing a profile that had it on. Anything other than a boolean is 400 `invalid_input`.

The response echoes `id`, `models`, `sandbox`, `soul`, `skills`, `memory` and `updated_at`.

## Starting a run

`POST /v1/profiles/{profile_id}/runs`:

```json
{
  "input": "Summarize the files in /work.",
  "provider": "bedrock",
  "model": "openai.gpt-6-luna",
  "session_id": "chat-1",
  "reasoning_effort": "low",
  "instructions": "Answer in one paragraph.",
  "file_context": "Text appended to the input as one more text part."
}
```

- `provider` and `model` are required and must be one of the profile's `models`.
- `input` is a string, or a list of parts: `{"type": "text", "text": "…"}` and
  `{"type": "image", "media_type": "image/png", "data": "<base64>"}`. At most 4 images; each non-empty and at most
  5 MB decoded; `media_type` is `image/png`, `image/jpeg`, `image/gif` or `image/webp`; `data` is raw standard
  base64 (no `data:` prefix, no line breaks). An image-only input is accepted.
- `session_id` continues a session. If omitted, a new session `ses_<random>` is created; a new id creates the
  session. A session takes one run at a time.
- `reasoning_effort` is passed to the provider as is (for example `low`, `medium`, `high`, or `none`).
- `instructions` apply to this run only.

The response (202): `{"run_id": "…", "status": "queued", "session_id": "…"}`.

## Run

`run_id`, `status` (`queued`, `running`, `stopping`, `completed`, `failed`, `cancelled`), `session_id`,
`provider`, `model`, `reasoning_effort`, `output` (the final text), `usage`
(`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`; null when unknown), `error_code`,
`error`, `created_at`, `started_at`, `finished_at`.

Stopping a queued run cancels it at once. Stopping a running run moves it to `stopping`; the worker ends it as
`cancelled` and kills its running command. Stopping a finished run changes nothing.

## Events

`GET .../events` returns `text/event-stream`. Each frame is:

```
id: 7
event: tool.completed
data: {"tool_call_id": "…", "tool": "terminal", …, "seq": 7, "type": "tool.completed", "run_id": "…", "at": …}
```

Every `data` object carries `seq` (also the SSE `id`), `type`, `run_id` and `at` (a number: when the event was recorded), plus the fields
below. Events are stored: pass `after=<last seq>` to resume without losing or repeating any. The stream sends a
`: keepalive` comment every 15 s and ends after the terminal event.

| Event | Fields |
|---|---|
| `run.started` | `provider`, `model`, `reasoning_effort` |
| `message.delta` | `text`: a piece of the model's answer |
| `tool.started` | `tool_call_id`, `tool`, `preview` |
| `tool.completed` | `tool_call_id`, `tool`, `duration_s`, `error` (boolean), `preview` |
| `context.compacted` | `messages_before`, `messages_after`, `tokens_before`, `tokens_after` (estimates), `usage` (the summary call's; it is also counted in the run's `usage`) |
| `run.completed` | `output`, `usage`, and `incomplete: true` when the model reported an incomplete response |
| `run.failed` | `error_code`, `message` |
| `run.cancelled` | none |

`run.completed`, `run.failed` and `run.cancelled` are terminal.

The events of a run finished more than `KEEL_EVENT_RETENTION_DAYS` ago are deleted. Streaming such a run sends
nothing and closes; read the run itself with `GET /v1/profiles/{p}/runs/{id}`.

`context.compacted` means a long session's earlier turns were summarized before a model call; the transcript itself
is unchanged. A client should ignore event types it does not know.

## Sessions and messages

A session is `id`, `title`, `title_source` (`derived` from the first message, `llm` after the first completed run, or
`user` after a rename), `created_at`, `last_active_at`, `message_count`.

`GET .../messages` returns `{"data": [message, …], "pagination": {"limit", "offset", "returned"}}` with `limit`
(1-500, default 200) and `offset`. With `after_id=<message id>` it returns only the later messages, without
`pagination`; use it with `boundary` to read what a run added.

A message has `id`, `role` (`user`, `assistant`, `tool`), `content` (the text), `parts`, `tool_calls`
(`[{"id", "type": "function", "function": {"name", "arguments"}}]` or null), `tool_call_id`, `tool_name`,
`run_id`, `created_at`. Image parts appear as `{"type": "image", "media_type", "blob_id"}`; the data is not
returned. If a stored image is gone later, the model sees "[image no longer available]".

A turn cut short (cancelled, crashed or swept) can leave tool calls without results. They are answered with
"The command was cancelled." when the run ends or at the start of the next turn.

## Error codes

| HTTP | Code | When |
|---|---|---|
| 400 | `invalid_input` | A malformed body, id, policy, image or parameter. |
| 400 | `model_not_allowed` | The provider and model are not in the profile's `models`. |
| 400 | `provider_unavailable` | The provider is not configured on this service. |
| 400 | `model_not_vision_capable` | The input has images and the model cannot read them. |
| 401 | `unauthorized` | Missing or wrong key. |
| 404 | `profile_not_found`, `session_not_found`, `run_not_found` | No such object. |
| 409 | `session_busy` | The session already has a run in progress (also when deleting it). |
| 409 | `title_in_use` | Another session already has that title. |
| 429 | `at_capacity` | `KEEL_MAX_RUNNING` runs are in flight. Has a `Retry-After: 1` header. |
| 500 | `internal_error` | An unexpected error. |
| 503 | `unavailable` | The service is not ready, or the store does not answer. |

A failed run reports its cause in `error_code` (on the run and in `run.failed`):

| Code | Meaning |
|---|---|
| `auth` | The provider rejected the credentials. |
| `rate_limited`, `server_error`, `timeout` | Transient provider failures. keel retries a call up to 3 times with backoff while it has produced no output. |
| `context_overflow` | The conversation no longer fits the model's context. |
| `invalid_reasoning` | A provider refused replayed reasoning. keel retries once without it. |
| `bad_request` | The provider refused the request. |
| `sandbox_unavailable` | The sandbox policy is invalid, or its container could not be created or reached. |
| `worker_lost` | The worker stopped renewing the run's lease, or the service shut down while it ran. |
| `internal_error` | An unexpected failure inside the run. |
