# Session kinds and session list paging

Status: proposed (2026-11-03).

## Goal

A consumer runs two kinds of work through one profile: conversations a person reads, and background jobs that start
a session per job. `GET …/sessions` returns the newest sessions of every kind, capped at 500, with no way to page.
Once background sessions outnumber the page, conversations fall off the list, and a consumer that filters them out
afterwards shows a short list and cannot reach older conversations at all.

A session gets an optional `kind`, a label the consumer chooses. The list filters by it and pages with a cursor, so
each page costs the same however many sessions of other kinds the profile has.

## API

`kind` is a string matching `^[a-z][a-z0-9_-]{0,31}$`, or absent. keel stores it and filters on it; it never
interprets it.

- `POST /v1/profiles/{p}/runs` takes an optional `session_kind` (model and command runs). The run that creates the
  session records it. A run on an existing session without a kind records it there; on a session that has a
  different kind the run is refused with 409 `session_kind_conflict`. An invalid value is 400 `invalid_input`.
- `PATCH /v1/profiles/{p}/sessions/{s}` takes `title`, `kind`, or both; at least one. Setting `kind` follows the same
  rule: it may be set once and repeated unchanged, a different kind is 409 `session_kind_conflict`. This is how a
  consumer labels sessions created before kinds existed.
- `GET /v1/profiles/{p}/sessions` takes `kind` (only sessions of that kind) and `cursor`, and returns
  `{"data": [...], "next_cursor": "…" | null}`. Order is unchanged: `last_active_at` descending, then `id`.
  `next_cursor` is set when more sessions follow; passing it back returns the page after the last row. A cursor is
  opaque; a malformed one is 400 `invalid_input`. Without `cursor` the endpoint behaves as before, with the added
  `next_cursor` field.
- Every session object gains `kind` (`null` when unset).

### Paging semantics

The cursor is the `(last_active_at, id)` of the last row returned, and the next page is the rows strictly after it in
list order (keyset paging). A session that becomes active while a consumer pages moves to the front: it is not
returned twice by later pages, and the consumer sees it when it reloads the first page. A deleted session simply
stops appearing. Offsets would instead repeat or skip a row whenever the list changes between pages.

## Schema

Migration 15 adds `sessions.kind TEXT` (nullable) and the index
`sessions_profile_kind_active (profile_id, kind, last_active_at DESC, id)`, so a filtered page is one index range
scan. Existing sessions keep `kind` null; keel cannot know what they are, so labelling them is the consumer's job
(through `PATCH` or their next run). The unfiltered list keeps using `sessions_profile_active`.

## Not in scope

- Listing several kinds at once, or sessions with no kind. A consumer that needs either labels its sessions.
- Retention by kind. A consumer can list and delete a kind's old sessions with the existing `DELETE`.
- History import (`keel-admin import`) does not take a kind; imported sessions are labelled afterwards.
