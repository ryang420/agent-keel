# Operations

Commands an operator runs against a keel database directly, beside the service. They read `KEEL_DATABASE_URL` like
the service does; in a container, run them in the keel container (`docker exec -i keel python -m keel.admin …`).

## Importing conversations

`python -m keel.admin import-sessions [--dry-run]` imports conversations copied from another system. It reads one
conversation per line of JSON on stdin and writes one result per line to stdout.

Input line:

```json
{"profile": "p1", "tenant_id": 7, "session_id": "s-0001", "title": "Row count", "title_source": "llm",
 "created_at": 1789000000.0, "last_active_at": 1789000300.0,
 "messages": [
   {"source_id": 11, "role": "user", "text": "How many rows?", "created_at": 1789000000.0},
   {"source_id": 12, "role": "assistant", "text": "", "created_at": 1789000001.0,
    "tool_calls": [{"id": "c1", "name": "terminal", "arguments": "{\"command\": \"wc -l f\"}"}]},
   {"source_id": 13, "role": "tool", "text": "42 f", "tool_call_id": "c1", "tool_name": "terminal",
    "created_at": 1789000002.0},
   {"source_id": 14, "role": "assistant", "text": "42 rows.", "created_at": 1789000003.0}
 ]}
```

- `profile` and `session_id` follow the API's id rules. `tenant_id` is the tenant that owns the profile.
- `title_source` is `derived`, `llm`, `user` or null.
- Each message's `role` is `user`, `assistant` or `tool`, and a `tool` message needs the `tool_call_id` of an earlier
  call.
- `source_id` is any value the caller uses to match messages to the ids keel assigns.
- Reasoning is never imported: an imported message has none, so no provider is replayed another system's reasoning.

Result line:

```json
{"session_id": "s-0001", "status": "imported", "profile_created": true, "title": "Row count", "reason": null,
 "message_ids": {"11": 501, "12": 502, "13": 503, "14": 504}}
```

- `imported`: the session and its messages were written in one transaction. The title, times and message order are
  kept, and a title the profile already uses becomes `Title (2)`, `Title (3)`, ….
- `exists`: a session with this id exists already, under any profile. Nothing is changed, so a re-run is safe.
- `invalid`: the line breaks one of the rules above, or the profile belongs to another tenant. `reason` says which, and
  nothing of that conversation is written.
- `valid`: the `--dry-run` result for a line that would be imported.

A missing profile is created empty for `tenant_id`: no models, no skills, an empty sandbox and soul. The next profile
update through the API (`PUT /v1/profiles/{id}`) fills it in before its first run. The command exits 1 when any line is
`invalid`.

## Upgrading to atomic run termination

This release commits a run's terminal state and its terminal event in one transaction. It needs no schema migration,
but the guarantee holds only while every process writing the database runs this version. An old writer can still
leave a terminal run without its event; an old event writer can still append after a new-style terminal event.

To cut over, stop admitting new runs, let in-flight runs drain, and stop the old instances before starting the new
version. A run left in flight when its worker stops is failed as `worker_lost` by the new version's lease sweep. Total
downtime can be as short as the drain window. Rolling back is safe — the new version writes ordinary rows — but the
atomicity guarantee ends while old writers run.
