# Cross-session memory: user continuity and history recall

Date: 2026-10-24. Status: proposed; approach selected, design awaiting approval.

## Intent and decisions

Make a new conversation remember the user's durable preferences and find specific prior discussions within the
same profile. The selected approach is **curated profile memory plus on-demand history retrieval**. This extends
M3 rather than replacing its storage, prompt snapshots, or tenant boundaries. It is a separate feature, not the
consumer-specific M4 history import.

The user selected this problem before procedural learning. Agent-authored skills, private skill overlays,
embeddings, external memory providers, automatic retrieval on every message, cross-profile sharing, and a new
approval UI are outside this phase. No model-weight training is involved.

### Approaches

| Approach | Benefit | Cost / limitation | Decision |
|---|---|---|---|
| Improve curated memory only | Smallest change; better preference capture | Cannot recover details omitted from the small memory budget | Insufficient |
| Curated memory + on-demand history search | Covers user preferences and specific historical evidence | A new retrieval tool and reliable maintenance work | Selected by the user |
| Retrieve and inject history on every turn | More automatic recall | Adds latency, context cost, and irrelevant or stale history to ordinary turns | Deferred |

The concrete contracts and defaults below are engineering proposals, not previously approved user requirements.

## Current implementation

- `memory.py` validates and edits two bounded lists; `store/sql.py:apply_memory` serializes mutations in a
  transaction and locks the target row on Postgres. Defaults are 2,200 characters for environment facts and
  1,375 for the user profile. `Profile.memory` is opt-in.
- `runner.py` builds a stored system-prompt snapshot at session start and after compaction. Mid-session writes
  do not rewrite it. `memory` supports atomic batches but entries have no stable IDs or provenance.
- `review.py` replays assembled conversation context with a memory-only tool surface and at most four iterations.
  `_after_complete` counts successful turns; reaching the threshold resets the count before a best-effort task
  is spawned. An in-process set prevents duplicate reviews only within that process. Background capacity or a
  restart can discard that opportunity. There is no auxiliary usage record for these calls.
- Raw messages are stored, but the model has no cross-session search tool. Ordinary store reads filter
  `active = 1`; recall needs a separate path that also sees messages hidden from the live context by compaction.
- Existing skills are deployment-supplied, read through the sandbox, and selected by the profile.

## 1. User-visible contract

1. An explicit durable preference such as "from now on, give the conclusion first" should be saved by the
   foreground agent before it claims to have remembered it. A successful write is available to a new session
   whose snapshot is built after that commit. A failed write must not be described as a successful save.
2. "Make this particular report detailed" is task-local. It does not replace a general preference for concise
   answers. Persist the scope of a preference instead of generalizing it to every task.
3. When the user refers to a prior conversation, the agent searches before asking them to repeat it. It reads
   context around a match before claiming why a prior decision was made. No match means no verified recall.
4. An explicit correction replaces the applicable old fact; it must not leave contradictory active entries.
   Existing sessions retain their snapshots until compaction; the correcting session already has the user's
   correction in its live conversation. Other open sessions are not promised immediate refresh.
5. Short conversations receive an idle review even if they never reach ten completed turns. This is eventual
   persistence, not a guarantee that an immediately following new session sees the result.
6. Memory is profile-scoped, including for two profiles in one tenant. "Same user" does not authorize sharing.

Keep `memory: false` as the default. With it disabled, neither memory nor history-search tools are offered and
background learning is not scheduled. Enabling memory permits recall of existing retained conversations in that
profile, but does not automatically extract preferences from the entire old archive. Disabling it stops new
writes and reviews without deleting stored facts or history; re-enabling uses those facts again. Explicit
erasure is separate. Turning it off cannot remove facts already present in a stored conversation snapshot.

## 2. Curated memory and provenance

Replace the JSON lists as the authoritative store with individual `memory_entries`. Preserve the public targets
`user` and `memory`, the default budgets, ordering, existing validation, exact duplicate handling and atomic
batch semantics. The prompt renders only active entry text, not audit metadata.

An entry has a stable ID, profile ID/incarnation, target, text, order, version, state (`active` / `removed`),
creation and update times, and the latest source revision. A separate `memory_changes` ledger records each
committed add/replace/remove with old/new text, source kind (`foreground`, `review`, `legacy`), source run and
message IDs, and the mutation's idempotency key. References are logical links: missing source messages do not
make legacy or retained facts invalid. Do not invent provenance for migrated entries.

Maintain a revision for each profile/target. Add optional `entry_id` and `expected_version` to the memory tool;
retain `old_text` for compatibility, resolved under the transaction using its current unambiguous-match rule.
Return IDs and versions alongside the existing text-list result so the model can target subsequent corrections.
Add a read-only `list` action to obtain live entries rather than writing against a frozen snapshot. Adds remain
bounded; when full, return live entries and ask the agent to consolidate, never silently evict an unrelated fact.

All foreground writes are tied by trusted ToolContext to the executing run, its user-message boundary and
tool-call ID. The model cannot choose a different owner. Model-supplied evidence IDs, when provided, must refer
to messages actually made available to that call in the same profile. Explicit preferences require user-message
evidence; assistant assertions or retrieved tool content alone cannot establish a user preference. Validation
can check ownership and roles; whether the source really supports a fact remains a model-quality question.

The prompt and reviewer must save only stated durable preferences or well-supported environment facts. Do not
create a speculative user-profile candidate database in this phase: leave uncertain inferences in history.
Repeated guesses are not confirmations. Store applicable conditions in the entry text. User corrections take
precedence over earlier statements about the same scope.

A replayed tool call with the same trusted mutation ID returns its prior result instead of applying it again.
Per-target revisions protect background proposals from intervening foreground writes (§4); entry-level versions
support precise edits. Existing secret and injection checks apply to both foreground and background commits.

## 3. On-demand history retrieval

Add `session_search`, available without a sandbox when memory is enabled. It has three bounded modes:

| Arguments | Meaning |
|---|---|
| `query`, optional `session_id`, `before`, `after`, `limit` | Search retained text, default 5 hits, maximum 10 |
| `session_id`, `around_message_id`, optional `window` | Read context around an anchor, default 4 messages each side, maximum 10 |
| no query or anchor, optional `before`, `after`, `limit` | Browse recent sessions; if `session_id` is given, return the latest bounded page |

Dates are timezone-qualified ISO timestamps; reject ambiguous timestamps. Search defaults to other sessions;
an explicit current-session ID permits recovery of compacted context. Browse results provide IDs, titles and
timestamps. Search hits provide session ID, message ID, role, timestamp, excerpt, and a truncation indicator.
Context pages return exact stored text within limits, plus earlier/later anchors for continuation. Each message
is capped at 4,000 characters and each response at 16,000; a long message can be read in additional bounded
chunks using `message_id` and a character offset in the same tool. Offsets count Unicode code points. The
implementation must expose these continuation parameters explicitly in its schema rather than silently lose
the middle of an important message. All modes return plain data in the tool result, not system instructions.

Index user and assistant text parts only. Exclude opaque reasoning, image bytes/URLs, tool-call arguments and
tool-result bodies in this phase. Context retrieval follows the same rule and labels omitted non-text/tool
content; it never turns a tool call into a claim that an action succeeded. Include original text archived by
compaction. Attach run status to messages where available so interrupted/failed work is not presented as a
completed outcome. Imported messages without runs are marked unknown. A search failure or an empty query result
is distinguishable from an incomplete index.

### Index and matching

Use a shared deterministic lexical tokenizer and a portable relational posting index for this phase, rather
than require SQLite extensions or a Postgres search extension. Normalize a search copy with Unicode NFKC and
case folding; preserve original text for display. Tokenize Latin/digit words and adjacent Han-character bigrams;
index single Han characters for one-character queries. Punctuation is a separator. A bounded query accepts up to
256 characters and 32 distinct tokens. This is lexical retrieval, not semantic equivalence across paraphrases.

Store redacted searchable text per message and unique postings `(profile, token, message_id)`. Apply ownership,
session and time filters before selecting bounded candidates. Rank by query-token coverage, exact normalized
phrase match, then recency and message ID; keep diversity across sessions (maximum two hits per session unless
the query names a session). Return at most 200 candidates to application ranking. Select candidates by token
coverage then recency in SQL, so a newest-only cutoff does not discard all older high-coverage matches. Native
database-specific indexing can replace this later behind the same retrieval contract if measurement warrants it.

New message text and postings commit with message insertion. Existing history is indexed through a resumable
maintenance backfill with a per-profile high-water mark and bounded batches; make insertion idempotent. Return
`index_complete: false` until backfill finishes, and guide the model not to claim an exhaustive negative result.
Deletion and insertion share ownership checks and lock ordering, so backfill cannot resurrect deleted sources.
The candidate budget is not a bound on database index work; benchmark both latency and index size (§7).

### Ownership and model behavior

Every store operation takes trusted profile/incarnation context and verifies every supplied session/message
against it. No `profile_id` or `tenant_id` tool argument exists. A foreign ID gets the same result as a missing
one. The HTTP caller's authorization and the consumer's account-to-profile authorization remain unchanged.
Redact recognized secrets before indexing and before returning historical text, including older unredacted rows.

Prompt guidance: search when earlier conversations are referenced or needed to answer accurately; use concrete
keywords, retry a focused alternative if useful, inspect neighboring text, and distinguish quotation from
inference. An old assistant answer is evidence of what was said, not proof that it was correct. Cite the date
and session title in natural language; consumers can resolve returned IDs to their own transcript UI. No extra
LLM call summarizes the retrieval result. Do not search on greetings or unrelated standalone questions.

## 4. Durable incremental review

Replace automatic memory-review `Background.spawn` with a small database-backed review queue and worker;
best-effort title generation can keep its existing lifecycle. This is a memory-specific facility, not a general
job framework. No review events are appended after a run's terminal event.

The winning completed-run transition atomically marks the session's review state dirty, records the latest
message boundary, and advances its unreviewed completed-turn count. An incomplete iteration-cap completion is
also reviewable for user statements, never treated as proof of task success. Failed/cancelled runs do not count
as automatic review triggers; a foreground memory commit before cancellation remains committed.

State is scoped by profile/incarnation and a session generation token introduced for this purpose, so deleting
and recreating the same session ID cannot revive old work. Record `reviewed_through`, `dirty_through`, pending
turn count, last completion time, model/provider route and first-dirty time. Claim an immutable source range
and boundary in a job. New completions extend pending state, not the claimed job. A successful foreground memory
write no longer clears the review cursor or suppresses review of other unsaved facts.

Default eligibility is ten completed turns OR 60 seconds since the last completed turn with unswept content.
For a continuously active session, first-dirty age of ten minutes also makes it eligible. Thresholds are settings;
60 seconds is an eligibility delay, not a completion SLA. At most one leased review per profile runs across all
instances; queue other sessions fairly by oldest eligible time. Use a dedicated worker with one concurrent review
per instance by default, independent of foreground slots. A pending job is never dropped at capacity.

Lease claims, renewal and expiry use database time and fencing tokens. Every commit checks the lease token,
profile incarnation, memory-enabled state, session generation, source existence and deletion admission. Expired
workers lose authority to write. Shutdown releases or lets leases expire; a different worker can retry. Store
job status, attempts, error code, route, source bounds and per-attempt usage. Retry transient failures with bounded
backoff for up to three job attempts; a final failure is inspectable and requires explicit operator retry.
Do not move `reviewed_through` past a failed range. One failed session does not block another session's reviews.

### Input, mutations, and budget

Use the originating completed run's provider/model (revalidate against current allowed models) and a compact
review-specific prompt. Read raw user/assistant text in the claimed range plus at most two preceding completed
turns for context and the live memory entries/revisions. No image replay, opaque reasoning or full-session replay.
Process large backlogs in ascending, bounded chunks and checkpoint only processed boundaries. A very long
message is chunked with a stored character cursor; no unexamined text is marked reviewed. Input text carries
message IDs and whether it is overlap-only; overlap supports interpretation but does not create duplicate work.

Start with four model iterations, 16,000 estimated input tokens cumulatively across requests per job attempt,
2,000 output tokens per request and a 120-second wall-clock deadline. Reduce input to fit smaller model windows
with output headroom. Reserve budget before every request; actual usage is recorded when supplied by the
provider, unknown usage remains unknown. These are proposed defaults to validate in live evals, not cost claims.

The reviewer uses memory-shaped tools against a staged in-memory proposal, not the shared live store. After a
complete valid proposal or an explicit no-op, apply the batch, audit records, job completion and source cursor
atomically. A proposal cut off by a deadline/iteration limit does not commit partial edits or advance the cursor.
Capacity/validation refusals can be corrected within the attempt. No-op review still advances its processed
boundary; no-op foreground tools do not suppress later reviews.

Under the profile lock, compare touched target revisions with those read for review. On conflict, discard the
proposal and re-read current facts; never replay a stale replacement blindly. Count this against the job retry
limit. Background adds cannot reintroduce facts whose newer explicit correction/removal is evidenced after the
job's source boundary: use the mutation ledger to reject stale-source changes for the affected target and
reschedule from current evidence. Avoid claiming semantic deduplication is mechanically perfect; evaluate it.

Immediate foreground saves have no dependency on the queue. The queue never delays the terminal response to
wait for an LLM; only the small dirty-state write joins the existing completion transaction. Do not claim reuse
of the foreground prompt cache for the compact review route.

## 5. Inspection, correction and deletion

Add tenant-authorized, profile-scoped service routes (no new end-user login system):

- `GET /v1/profiles/{p}/memory`: current entries, versions, usage and source metadata.
- `PATCH /v1/profiles/{p}/memory/{id}`: replace text with an expected version, using the same validation/budget.
- `DELETE /v1/profiles/{p}/memory/{id}`: remove with an expected version. A version conflict is 409.
- `GET /v1/profiles/{p}/memory/reviews`: bounded job status/usage/error listing; no full transcript in logs.
- `POST /v1/profiles/{p}/memory/reviews/{id}/retry`: idempotently requeue a failed job if its source and owner
  still exist. It creates no duplicate active attempt.

Consumer-originated edits/removals are explicit authoritative corrections and enter the same mutation ledger.
Removed entry text is not injected; old sources cannot recreate it through a delayed review. Relearning from
newer user statements is permitted. This is fact management, not erasure of chat history or audit revisions.

Session deletion removes its raw messages, search projections/postings and review work, and detaches provenance
links from retained memory. It preserves previously saved profile facts and their audit text, matching current
memory independence. Deleting a session does not promise to forget every fact learned there. Delete a fact
separately to stop using it. Full profile deletion removes entries, audit, indexes, queue state and usage rows,
and fences in-flight work. No background callback can recreate them under a new incarnation. Do not infer
broader privacy guarantees from fact removal; old session snapshots can contain historical memory text.

## 6. Integration and migration

Extend `ToolContext` with trusted source/mutation identity and a tool allowlist. Replace the special-case
"everything except memory needs a sandbox" dispatch rule with explicit tool requirements. Enforce the allowlist
at dispatch as well as in the offered schema; a review cannot invoke a sandbox tool by inventing its name.

Keep the session prompt frozen between compactions. Newly enabled tools/guidance take effect at a new session
or compaction boundary; persist the memory-capability version for that boundary. A profile-level disable is
checked at execution and commit even if an old session advertised a tool. Existing session snapshots are not
silently rewritten on deployment or profile updates. Additions to schemas on upgrade are activated at the same
boundary rather than claiming old and new tool bytes are identical.

Use new numbered migrations after the latest released version (currently 7). Migrate each existing JSON entry
to a stable ordered row with legacy provenance, preserving text, duplicates, budgets, timestamps where known,
and the rendered snapshot. Stop old writers for cutover; no dual-write period. Existing `memories` data may be
retained as a migration backup until the migration transaction succeeds, but only the new table is authoritative
afterward. Retire old turn counters from behavior; historical completed sessions are not auto-reviewed on
upgrade. New completed turns create dirty ranges from the enable/cutover boundary. Search backfill covers all
retained eligible history independently of review eligibility.

New schema and index/query behavior must work on SQLite and Postgres. The entry migration is transactional;
search backfill is resumable outside the migration and reports readiness. Rollback requires the existing
backup/restore procedure; old binaries must refuse newer schemas. Update the API/config/operations guides and
changelog during implementation, including the eventual-consistency and deletion distinctions.

## 7. Acceptance and rollout

Deterministic tests use scripted model output for plumbing; separate opt-in live evals test whether the actual
model chooses the correct tools and facts. A passing scripted model is not evidence of improved memory quality.

| Scenario | Required result |
|---|---|
| Explicit preference in A; fresh B starts after successful save | B's snapshot contains it and the live model follows it |
| One-off exception in A | B retains the general preference; no global overwrite |
| Explicit correction in B; fresh C | C has only the applicable corrected active fact |
| A short session ends before ten turns | Idle job becomes eligible, persists once, and survives restart |
| Old unmemorized decision, English and Chinese fixtures | Search returns the original relevant messages; surrounding text supports the answer |
| No matching history / incomplete backfill | Honest absence / explicit incomplete result, no invented recall |
| Old messages were compacted | They remain searchable without modifying live context boundaries |
| Long hit | Bounded output, working continuation, exact remaining text recoverable |
| Two tenants; two profiles within one tenant | No cross-owner result via query, anchor, offset, listing, mutation or retry |
| Worker dies after generation or after commit | Retry is possible; at most one committed batch/cursor advancement |
| Two instances; foreground correction races an old review | Lease/version/source checks prevent stale commits and duplicate jobs |
| Disable, session deletion, profile deletion/recreation | No unauthorized reads/writes or resurrection from stale jobs |
| Ordinary two-turn conversation | Stable prompt/tool bytes between boundaries; no automatic history LLM call |
| Migration and backfill interruption | Existing facts/history survive; resumed backfill has no duplicate or missing postings |

Use at least 30 reviewed live scenarios covering explicit preferences, scope exceptions, corrections, historical
decisions and negative recall, with both Chinese and English examples. Proposed release gate: at least 90%
correct outcomes in each of preference continuity and history recall, zero cross-profile exposure, and zero
unsupported durable preferences in the negative cases. Record model/version, inputs, outputs and judgments;
compare against baseline Keel on the same fixtures. Consumers still own their domain-quality acceptance.

Benchmark lexical search on a documented fixture of 100,000 messages in one profile plus other profiles. Record
index bytes, append overhead, backfill throughput and p50/p95 retrieval latency on both databases. Proposed
target: p95 retrieval below 500 ms on the documented local test machine/database, excluding model calls. If the
portable index misses this target, revise indexing before rollout instead of silently relaxing recall scope.
Measure foreground completion overhead and review usage separately; no promise of cache savings from review.

Roll out to an explicitly enabled test profile, then a small opt-in cohort after isolation and migration tests.
Use the profile memory switch to stop learning/recall if needed; it is not an erasure switch. Observe unsupported
fact rate, correction success, retrieval success, queue age/failures, and auxiliary tokens. More saved entries
is not the success metric.

## Builder discretion and remaining approval

The builder may choose module boundaries, internal SQL names, serialization helpers, cursor encoding, detailed
retry jitter and HTTP response envelopes consistent with existing conventions. They may tune the proposed
budgets using measurements, documenting any material change. The selected product scope, ownership boundary,
snapshot behavior, raw-evidence retrieval, stale-write prevention and deletion semantics are not discretionary.

This is a multi-step feature with schema, runtime and API changes. Approve this design before an implementation
plan or product-code changes. Keep the implementation plan in `.superpowers/` per repository policy.
