# HTTP API

Base path `/v1`. Every request except `GET /healthz` needs `Authorization: Bearer <tenant-bound key>`. The verified
key selects one integer tenant ID. Bodies and responses are JSON, except the event stream. IDs (profile, session)
are 1-128 characters: letters, digits, `.`, `_`, `-`, starting with a letter or digit.

Timestamps (`created_at`, `updated_at`, `at`, …) are numbers.

`PUT` creates a profile in the caller's tenant and cannot move an existing profile. A tenant value in a request
header or body does not change the key's scope. An unknown profile and one from another tenant both return 404;
sessions, runs and event streams outside the selected profile are also 404. The consumer authenticates end users
and checks which account may use a profile before calling keel. Within a tenant, keel trusts its service caller
to select the authorized profile.

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
| `GET /v1/profiles/{profile_id}/memory` | The profile's active memory entries per target, with usage and each entry's source. |
| `PATCH /v1/profiles/{profile_id}/memory/{entry_id}` | Replace an entry's text: `{"text": "…", "expected_version": n}`. Returns the entry. |
| `DELETE /v1/profiles/{profile_id}/memory/{entry_id}?expected_version=n` | Remove an entry: `{"deleted": true}`. |
| `GET /v1/profiles/{profile_id}/skills/learned?state=` | The profile's learned skills, optionally of one state. |
| `POST /v1/profiles/{profile_id}/skills/learned` | Create a learned skill (201). |
| `GET /v1/profiles/{profile_id}/skills/learned/{skill_id}` | One learned skill with its version history. |
| `PATCH /v1/profiles/{profile_id}/skills/learned/{skill_id}` | Edit a learned skill. |
| `POST /v1/profiles/{profile_id}/skills/learned/{skill_id}/{rollback,suspend,resume}` | Change a learned skill's version or state. |
| `DELETE /v1/profiles/{profile_id}/skills/learned/{skill_id}?expected_version=n` | Retire a learned skill: `{"retired": true}`. |
| `PUT`, `GET`, `DELETE /v1/profiles/{profile_id}/runs/{run_id}/feedback` | Rate a finished run, read the rating, or remove it. |
| `POST /v1/models/verify` | Check a model id: `{"provider", "model"}` → `{"supports_vision": bool}`. |

## Profile

`PUT /v1/profiles/{profile_id}` body (all fields optional):

```json
{
  "models": [{"provider": "bedrock", "model": "openai.gpt-6-luna"}],
  "sandbox": {"image": "python:3.12-slim", "workdir": "/work"},
  "soul": "You are a careful data assistant.",
  "skills": ["my-skill"],
  "memory": false,
  "learning": false
}
```

- `models`: the provider and model pairs runs may use.
- `sandbox`: the policy; see [sandbox.md](sandbox.md). Empty or missing means new sessions are not offered the
  sandbox tools. Tool availability (the sandbox tools, `memory` and `session_search`) is fixed per session when its
  prompt is built and changes only when the session compacts; open sessions keep advertising removed tools, which
  answer that they are unavailable.
- `soul`: text that replaces the default identity ("You are a helpful assistant.") in the system prompt. A session builds its system prompt once, at its first run, so a changed `soul` or `skills` list applies to new sessions. Its tool list is fixed the same way, until the session compacts.
- `skills`: the names of the skills to enable.
- `memory`: a boolean. When true, the model is offered a `memory` tool and a `session_search` tool (both with or
  without a sandbox) and the system prompt includes a snapshot of stored facts. The tool's `list` action shows entries
  with ids and versions, and a change can name an entry by `entry_id` with an `expected_version`. A write does not
  change this session's stored prompt: it is visible in the next session's prompt, and after a compaction.
  A completed run (including one cut off at the iteration cap) marks its new user and assistant text for a
  background review. A session becomes eligible after `KEEL_MEMORY_REVIEW_TURNS` completed runs (10), 60 seconds
  after its last completed run, or 10 minutes after its first unreviewed one. Failed and cancelled runs do not make
  a session eligible; their text is reviewed with the next completed run's. The review is durable: it survives a
  restart and runs on one instance at a time per profile. It reads only the new text, plus a little earlier text for
  context. It never adds messages or events to the session. Its result is eventual: a session started right after a
  run may not see it yet. Turning memory on starts reviews from the next completed run; older conversations are
  searchable but never reviewed. Turning memory off and on again discards the profile's queued, failed and running
  reviews. Omitted, the field is false, including on update. `null`, or anything other than a boolean, is
  400 `invalid_input`.

- `learning`: a boolean, default false (including on update), and it requires `memory`: `true` without `memory` is
  400 `invalid_input` ("learning requires memory."). When true, the model is offered a `skill_manage` tool, and
  `skills_list` and `skill_view` show the profile's active learned skills, and the background memory review may also record
  observations about methods (see [Learned skills](#learned-skills) and [Run feedback](#run-feedback)).
  Like `memory`, the tool is fixed per session until it compacts. `null`, or anything other than a boolean, is
  400 `invalid_input`.

The response echoes `id`, `models`, `sandbox`, `soul`, `skills`, `memory`, `learning` and `updated_at`.

A replacement that changes the effective sandbox policy is refused with 409 `sandbox_busy` while the profile has a
run queued, running or stopping, or sandbox work that has not been cleaned up yet; retry once it has finished.
Policies are compared after normalisation, so resubmitting an equivalent policy (for example with default values
spelled out) is not a change, and other fields can still be updated while runs are in progress. Omitting `sandbox`
clears it, and enabling or clearing a sandbox counts as a change.

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

The response (202): `{"run_id": "…", "status": "queued", "session_id": "…"}`. A fast worker may already have
claimed the run, in which case `status` is `running`.

## Run

`run_id`, `status` (`queued`, `running`, `stopping`, `completed`, `failed`, `cancelled`), `session_id`,
`provider`, `model`, `reasoning_effort`, `output` (the final text), `usage`
(`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`; null when unknown), `error_code`,
`error`, `created_at`, `started_at`, `finished_at`.

Stopping a queued run cancels it at once, and its `run.cancelled` event is committed with the cancellation.
Stopping a running run moves it to `stopping`; the worker ends it as `cancelled` and kills its running command.
Stopping a finished run changes nothing. Whichever terminal transition commits first wins; a later attempt returns
the stored outcome and produces no event.

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

`run.completed`, `run.failed` and `run.cancelled` are terminal. The terminal event is committed together with the
run's terminal state and is the last event the run accepts; anything a worker tries to record afterwards (a late
`tool.completed`, for example) is dropped. A run that was forcibly terminated may therefore end without a matching
`tool.completed`: reconcile its tool calls from the stored messages (`GET .../messages?after_id=`).

The events of a run finished more than `KEEL_EVENT_RETENTION_DAYS` ago are deleted, terminal event included.
Streaming such a run sends nothing and closes, as does streaming a terminal run from before this guarantee that has
no stored terminal event; read the run itself with `GET /v1/profiles/{p}/runs/{id}`. A replay is identified by its
`(run_id, seq)` pair, so a client can resume with `after` without duplicating frames.

`context.compacted` means a long session's earlier turns were summarized before a model call; the transcript itself
is unchanged. A client should ignore event types it does not know.

## Sessions and messages

A session is `id`, `title`, `title_source` (`derived` from the first message, then `llm` after the first completed
run, or `user` after a rename; `user` outranks `llm`, which outranks `derived`), `created_at`, `last_active_at`,
`message_count`. Setting the model title emits no event; a client reads the title from the session.

`GET .../messages` returns `{"data": [message, …], "pagination": {"limit", "offset", "returned"}}` with `limit`
(1-500, default 200) and `offset`. With `after_id=<message id>` it returns only the later messages, without
`pagination`; use it with `boundary` to read what a run added.

A message has `id`, `role` (`user`, `assistant`, `tool`), `content` (the text), `parts`, `tool_calls`
(`[{"id", "type": "function", "function": {"name", "arguments"}}]` or null), `tool_call_id`, `tool_name`,
`run_id`, `created_at`. Image parts appear as `{"type": "image", "media_type", "blob_id"}`; the data is not
returned. If a stored image is gone later, the model sees "[image no longer available]".

A turn cut short (cancelled, crashed or swept) can leave tool calls without results. They are answered with
"The command was cancelled." when the run ends or at the start of the next turn.

## Memory

`GET /v1/profiles/{profile_id}/memory` returns the profile's active memory entries, per target, whether or not
`memory` is enabled:

    {"memory": {"usage": "0/2200", "entries": []},
     "user": {"usage": "9/1375", "entries": [{"id": "…", "text": "likes tea", "version": 1,
       "created_at": 1730000000.0, "updated_at": 1730000000.0,
       "source": {"kind": "foreground", "run_id": "…", "session_id": "…", "message_id": 42}}]}}

`source.kind` is `foreground` (the agent during a run), `review` (a background review), `consumer` (this API) or
`legacy` (an entry stored before ids existed; its run and message are unknown). Deleting a session clears the
run, session and message of the changes it produced but keeps the facts.

`PATCH /v1/profiles/{profile_id}/memory/{entry_id}` with `{"text": "…", "expected_version": 1}` replaces the text
and returns the entry at its new version. `DELETE /v1/profiles/{profile_id}/memory/{entry_id}?expected_version=1`
removes it. Both answer 404 `memory_not_found` for an unknown entry, 409 `version_conflict` when the entry has
changed since that version, 409 `profile_deletion_busy` while the profile is being deleted, and 400 `invalid_input`
for a missing or non-integer `expected_version`. PATCH also answers 400 `invalid_input` when the text is empty,
looks like a secret or an instruction, or would exceed the target's character budget. Sessions already open keep the
memory snapshot in their stored prompt until they compact; new sessions see the change. Removing an entry stops it
being used; it does not erase the conversations it came from.

### `GET /v1/profiles/{p}/memory/reviews`

Background memory reviews, newest first. Query: `status` (`pending`, `running`, `done`, `failed`, `discarded`) and
`limit` (1-100, default 20). No conversation text is returned.

```json
{"reviews": [{"id": "3f2a…", "session_id": "s1", "status": "failed", "attempts": 3, "error_code": "deadline",
  "error": "The review did not finish in time.", "provider": "bedrock", "model": "…",
  "source": {"after_message_id": 120, "through_message_id": 131}, "cursor": {"message_id": 120, "offset": 0},
  "usage": [{"attempt": 3, "outcome": "deadline", "requests": 2,
             "usage": {"input_tokens": 5210, "output_tokens": 180,
                       "cache_read_tokens": null, "cache_write_tokens": null}}],
  "created_at": 1761800000.0, "updated_at": 1761800300.0, "finished_at": 1761800300.0}]}
```

A job reviews the session's text after `after_message_id` up to `through_message_id`; `cursor` is how far it has
got. `usage` lists the last 20 attempts; an unknown count is `null`. A `failed` job is retried automatically up to
three attempts, then waits for an operator; while it waits, that session gets no new review. `discarded` means its
profile, session or memory switch changed underneath it.

### `POST /v1/profiles/{p}/memory/reviews/{id}/retry`

Requeue a `failed` review with fresh attempts and return it. Retrying a `pending` or `running` review returns it
unchanged. `404 review_not_found`; `409 review_not_retryable` for `done` or `discarded`; `409 review_source_gone`
when its conversation was deleted or replaced.

With `memory` on, new sessions are also offered `session_search`. It searches and reads the profile's earlier
conversations: user and assistant text only, secrets redacted, never across profiles. A session keeps the tools it
started with until it compacts, so open sessions do not gain `session_search` until then. Deleting a session removes
its messages from search.

## Learned skills

A profile with `learning` on can keep methods as learned skills: text-only, private to the profile, and separate
from the deployment's read-only `skills`. A skill has a `name` (lowercase slug, at most 64 characters), a one-line
`description` (at most 200), an optional `category` (slug, at most 32) and a `body` (at most 8000 characters, no
front matter). A profile holds at most 50 active or suspended skills. A deployment skill of the same name hides a
learned one. Names, bodies and descriptions are checked like memory text: empty text, secrets and instruction-like text
are refused.

The model creates, revises and retires skills with `skill_manage` (`list`, `create`, `revise`, `retire`), only when
the user explicitly asks it to keep a method; the guidance in its prompt says so. A skill is `active`, `suspended`
(not offered, kept) or `retired`. Every change writes an immutable version (`create`, `revise`, `rollback`,
`suspend`, `resume` or `retire`) with its source (`foreground` with the run, session and message, or `consumer`),
evidence and rationale, so a skill can be inspected and reversed. A dream's changes appear in the versions with
source `dream` and the dream's job id (`job_id`); `merge` is a dream-only change kind, written on the target of a
merge after the merged skills are retired with the rationale "merged into <name>". A dream's revise or merge makes
the skill active again if it was suspended. A merge, like a revise, starts the `stats` fresh.

Active learned skills are listed under the category `learned` in `skills_list`, and `skill_view` shows the current
version, with or without a sandbox, and records the load. A suspended or retired skill answers that it is no longer
available. The system prompt's skills index is built at session start or compaction, so a change shows up in the
index from the next session; `skill_view` always reads the current version. With `learning` off, nothing is
offered or listed, and the stored skills are kept.

The routes below work whether or not `learning` is enabled, like the memory routes. Consumer changes are
authoritative and enter the history with source `consumer`.

| Route | Body and result |
|---|---|
| `GET …/skills/learned?state=` | `state` is `active`, `suspended` or `retired`; any other value is 400. Returns `{"skills": […]}` (no bodies). |
| `POST …/skills/learned` | `{"name", "description", "body", "category"?}`. 201 with the skill. |
| `GET …/skills/learned/{skill_id}` | The skill with `body` and `versions`, each with `version`, `change`, `name`, `description`, `category`, `body`, `source` (`kind`, `run_id`, `session_id`, `job_id`), `evidence` (`message_ids`, `observation_ids`), `rationale`, `created_at`. |
| `PATCH …/skills/learned/{skill_id}` | `{"expected_version": n, …}` with any of `name`, `description`, `body`, `category`. |
| `POST …/{skill_id}/rollback` | `{"expected_version": n, "version": m}`: a new version equal to version `m`. |
| `POST …/{skill_id}/suspend`, `…/resume` | `{"expected_version": n}`. |
| `DELETE …/{skill_id}?expected_version=n` | Retire the skill: `{"retired": true}`. The history is kept until the profile is deleted. |

Each skill is `{"id", "name", "description", "category", "state", "version", "origin", "created_at", "updated_at",
"consumer_changed_at", "stats": {"loads_30d", "up_30d", "down_30d", "completed_30d", "failed_30d",
"cancelled_30d"}}`; `origin` is who created it. The `stats` cover the last 30 days and add up the versions since the
current text began (a rollback to older text starts again from that text; a suspend or resume does not reset them):
`loads_30d` counts `skill_view` loads, the `up` and `down` counts are the ratings of the loading runs, and the other
three are the loading runs' terminal statuses. Every change route except DELETE returns the
skill with its `body`. A change names the version it expects: 409 `version_conflict` when the skill has moved on.
Other errors: 404
`learned_skill_not_found`, 409 `name_taken` (an active or suspended skill has the name), 409 `skill_limit` (50
reached), 409 `skill_retired` (a retired skill cannot change), and 400 `invalid_input` for a missing or non-integer
`expected_version`, an invalid field, an invalid `state`, a rollback to a version that does not exist, or a suspend
or resume from the wrong state (resume needs a suspended skill, suspend an active one). All routes answer 404
`profile_not_found` for a missing or foreign profile, and 409 `profile_deletion_busy` while the profile is being
deleted.

With `learning` on, the background memory review can also record observations: short notes of a method and how it
turned out (`corrected`, `worked`, `failed` or `requested`), each citing messages of the reviewed text, at most 5 per
review. They are stored privately, pending for 30 days, and are the input of the dream (see Dreams).

A learned skill is suspended automatically when its current version has at least 3 `down` ratings in 30 days and more
`down` than `up`, but only while `learning` is on. The change is recorded as a version with change `suspend` and
source kind `automatic`; resume it like any suspended skill.
Resuming writes a new version, and suspension counts only ratings on runs that loaded the current version, so a
resumed skill is suspended again only after 3 new `down` ratings (and more `down` than `up`) on the resumed version.
The `stats` still include the earlier ratings, because they cover the whole current text.

## Run feedback

A consumer can rate a finished run. One rating per run; a new `PUT` replaces the old one. The routes work whether or
not `learning` is enabled, like the memory routes.

| Route | Body and result |
|---|---|
| `PUT …/runs/{run_id}/feedback` | `{"rating": "up" or "down", "comment"?}`. Returns `{"run_id", "rating", "comment", "created_at", "updated_at", "suspended": […]}`; `suspended` lists the ids of learned skills this rating suspended (usually empty). |
| `GET …/runs/{run_id}/feedback` | The same object without `suspended`. |
| `DELETE …/runs/{run_id}/feedback` | `{"deleted": true}`. |

`comment` is optional text of at most 2000 characters, checked like memory text (secrets and instruction-like text
are refused); it is stored as data and never treated as instructions. Feedback is accepted once the run has finished
(any terminal status). With `learning` on, a rating also records a `feedback_up` or `feedback_down` observation for
the run's session, without a model call; replacing or deleting the rating drops its observation if still pending. A
`down` rating on a run that loaded a learned skill counts toward that skill's automatic suspension. Deleting a
session or a profile deletes its feedback.

Errors: 400 `invalid_input` (a rating other than `up` or `down`, or an invalid comment), 404 `run_not_found`, 404
`feedback_not_found` (`GET` or `DELETE` on a run with no rating), 404 `profile_not_found`, 409 `run_not_terminal`
(the run has not finished), and 409 `profile_deletion_busy` while the profile is being deleted.

## Dreams

With `learning` on, a background job called a dream consolidates a profile's pending observations into learned
skills: it creates, revises, merges and retires skills and dismisses observations that teach nothing. These routes
inspect dreams and retry a failed one; they work whether or not `learning` is enabled, like the memory routes, and
return no conversation text.

| Route | Body and result |
|---|---|
| `GET …/dreams?status=&limit=` | Dreams newest first. `status` is `pending`, `running`, `done`, `failed` or `discarded`; `limit` is 1-100 (default 20). Returns `{"dreams": […]}`. |
| `POST …/dreams/{dream_id}/retry` | Requeue a `failed` dream with fresh attempts and the profile's current model; returns it. Retrying a `pending` or `running` dream returns it unchanged. |

```json
{"dreams": [{"id": "9c1e…", "status": "done", "trigger": "observations", "attempts": 1, "error_code": null,
  "error": null, "provider": "bedrock", "model": "…",
  "usage": [{"attempt": 1, "outcome": "committed", "requests": 4,
             "usage": {"input_tokens": 21040, "output_tokens": 910,
                       "cache_read_tokens": null, "cache_write_tokens": null}}],
  "changes": {"skills": [{"action": "create", "skill_id": "7ab…", "name": "release-notes", "version": 1}],
              "consumed": ["3f0a…", "5d2c…", "b81e…"], "dismissed": ["c47d…"]},
  "created_at": 1761800000.0, "updated_at": 1761800120.0, "finished_at": 1761800120.0}]}
```

`trigger` is why the dream was queued: `observations` (enough new pending observations), `interval` (a day since the
last dream), `feedback` (a `down` rating on a run that loaded a learned skill) or `suspension` (a skill was
suspended automatically). `changes` lists the skills the dream wrote (`action` is `create`, `revise`, `merge` or
`retire`), the observations it used (`consumed`) and the ones it dismissed; it is empty until the dream is `done`.
`usage` lists the last 20 attempts; an unknown count is `null`. A failed dream does not block the next one: when new
evidence makes the profile due again, a new dream is queued beside the failed one, which stays until an operator
retries it. After a `no_route` or `model_unavailable` failure no new dream is queued until the profile completes a
run on another model or the failed one is retried. `discarded` means its profile, learning switch or memory switch changed underneath it.

Errors: 400 `invalid_input` (an unknown `status`, or a `limit` outside 1-100), 404 `dream_not_found`, 404
`profile_not_found`, 409 `dream_not_retryable` (the dream is `done` or `discarded`), 409 `dream_busy` (another dream
of this profile is queued or running), and 409 `profile_deletion_busy` while the profile is being deleted.

## Verifying a model

`POST /v1/models/verify` checks that a provider and model id work, and whether the model reads images:

```json
{"provider": "bedrock", "model": "openai.gpt-6-luna"}
```

It makes two tiny calls through the provider: a short text prompt, then the same prompt with a 1×1 PNG. A model that
answers both reads images (`{"supports_vision": true}`); one that refuses the image with a bad-request answer reads
text only (`false`). Any tenant's service key may call it: it reads no profile and writes nothing.

A refused model is 422, with code `access_denied` (the account may not call it), `not_found` (the provider does not
recognize the id) or `unsupported` (the provider rejected the test request for another reason). A provider that did
not answer (rate limit, server error, timeout) is 503 `unavailable`.

## Deleting a profile

`DELETE /v1/profiles/{profile_id}` with a JSON body `{"operation_id": "<uuid>"}` removes one profile's sessions,
messages, runs, events, blobs, memory, learned skills and model/persona configuration, and retires its sandbox by exact recorded
container identity. It is tenant-owned and resumable: a retry with the same operation id continues from the
persisted phase. A different operation id while a deletion is active is refused with 409 `profile_deletion_busy`.

Success is exactly `{"deleted": true, "blocked": true}` and means the profile's runtime data is gone and the
profile cannot be written. The fence stays in place until the consumer acknowledges that its own data is gone:

`POST /v1/profiles/{profile_id}/deletion/{operation_id}/complete` returns `{"completed": true}` and releases only
that operation's fence. It is idempotent for a retried operation and never releases a newer fence or changes a
newer profile. A minimal receipt (profile, tenant, operation, time) outlives the fence so a lost-response retry is
acknowledged safely.

A profile that was never prepared is still fenced by the operation and succeeds once the service establishes that
no runtime resources exist. A profile owned by another tenant is `404 profile_not_found`, exactly as if it did not
exist. A malformed operation id is `400 invalid_input` before anything is written.

While a deletion is active, profile updates, run/session creation and memory writes answer `409
profile_deletion_busy`. A `503 unavailable` means a cleanup dependency could not answer; the fence remains and the
same operation may be retried. Cleanup is bounded, and a timeout is never reported as success. Requests never
remove a container by name alone.

## Error codes

| HTTP | Code | When |
|---|---|---|
| 400 | `invalid_input` | A malformed body, id, policy, image or parameter. |
| 400 | `model_not_allowed` | The provider and model are not in the profile's `models`. |
| 400 | `provider_unavailable` | The provider is not configured on this service. |
| 400 | `model_not_vision_capable` | The input has images and the model cannot read them. |
| 401 | `unauthorized` | Missing or wrong key. |
| 404 | `profile_not_found`, `session_not_found`, `run_not_found` | No such object. |
| 404 | `dream_not_found` | No such dream in this profile. |
| 404 | `memory_not_found` | No such memory entry in this profile. |
| 404 | `learned_skill_not_found` | No such learned skill in this profile. |
| 404 | `feedback_not_found` | The run has no feedback. |
| 409 | `run_not_terminal` | Feedback is accepted only once the run has finished. |
| 409 | `version_conflict` | The memory entry or learned skill changed since `expected_version`. |
| 409 | `name_taken`, `skill_limit`, `skill_retired` | A learned skill's name is in use, the profile has 50 active or suspended skills, or the skill is retired. |
| 409 | `dream_not_retryable` | Only a failed dream can be retried. |
| 409 | `dream_busy` | Another dream of the profile is queued or running. |
| 409 | `session_busy` | The session already has a run in progress (also when deleting it). |
| 409 | `sandbox_busy` | `PUT /v1/profiles/{id}` would change the effective sandbox policy (including enabling or clearing it) while the profile has a queued, running or stopping run, unreleased sandbox usage, or unresolved sandbox work. Nothing in the request is applied. |
| 409 | `sandbox_requires_migration` | An existing profile's host mounts require an operator migration before its policy can be updated. |
| 409 | `title_in_use` | Another session already has that title. |
| 422 | `access_denied`, `not_found`, `unsupported` | `/v1/models/verify`: the model is unusable (no access, unknown id, or the provider refused the test request). |
| 429 | `at_capacity` | `KEEL_MAX_RUNNING` runs are in flight. Has a `Retry-After: 1` header. |
| 500 | `internal_error` | An unexpected error. |
| 503 | `unavailable` | The service is not ready, the store does not answer, or `/v1/models/verify` could not reach the provider. |

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
