# Data-artifact correctness evaluation

Date: 2026-10-25. Status: approved by the owner on 2026-10-25; implementation planning in progress.

## Intent and scope

The owner authorized the next two steps after the DAB pilot: review the existing failed traces, and add six
small executable data tasks. The goal is to measure whether Keel delivers correct, reusable data-processing
artifacts, complementing the answer-string checks in `dab-score-v2`. Keep memory evaluation and the other public
benchmark integrations paused. Do not change the runtime, model prompt, tools or DAB scoring to improve results.

The existing full DAB pilot has 54 scored cases, 23 passes and dataset-mean Pass@1 0.3917. An offline trace review
classifies the 31 failures provisionally into answer-contract loss (4), joins/entity resolution (6), business
rules/cohorts (9), text parsing (5) and unresolved task/metric semantics (7). These are observations from one
execution per task, not proven causal shares. Full final messages pass the same validators for four cases where
the extracted `ANSWER:` marker lost names/list contents. Preserve the original score; an alternative extraction
is diagnostic only. The full annotations and source hashes remain in ignored evaluation artifacts.

## Approaches

| Approach | Tradeoff | Decision |
|---|---|---|
| Run more independent public tasks | Better general coverage; still limited artifact checks and higher setup cost | Defer |
| Ask a model to judge generated code | Flexible but expensive and not sufficient to establish numerical correctness | Defer |
| Six small data tasks with hidden executable checks | Small reproducible inputs, precise errors, reusable artifacts tested on new data | Selected direction |

Use Python standard-library-compatible CSV/JSON data and SQLite for this first suite. Pandas or DuckDB are
allowed if installed in the pinned evaluation image, but no task requires a new dependency or cloud database.
Keel's own service, provider, Docker sandbox and tools perform the tasks; a separate evaluator verifies outputs.

## Execution contract

Protocol name: `artifact-correctness-v1`. Place the harness under `evals/artifacts/`, outside the packaged runtime,
and reuse the existing evaluator's provider accounting, budget primitives and supported lifecycle where practical.
Avoid a new generic evaluation framework. Helpers may be shared without changing DAB behavior or its recorded
protocol. Add unit contracts for shared behavior if code is extracted.

Each task requires a script named `solution.py` with the public interface:

```text
python solution.py --input-dir /input --output-dir /output
```

Paths are arguments; absolute task-specific paths must not be embedded in the solution. The task prompt supplies
the schemas, exact business rules, output filenames and interface. It does not prescribe a particular algorithm.
The agent must run the script against a visible development batch, leaving machine-readable outputs and the
script. The final prose answer is not the submission and is not scored for keywords.

One fresh profile per task and repetition, memory off, no skills, one session, one run, serial execution. The
proposed model remains official OpenAI `gpt-6-luna`, reasoning `low`, matching the current DAB baseline. Record
requested/returned model IDs, Keel SHA and dirty state, input/prompt hashes, image digest and all tool traces.
No API credential enters the sandbox. Use `network: none` and read-only input mounts; outputs and script live in
evaluation-owned writable space. No production profiles or data are accessed.

Before deleting the source profile/sandbox, export the script and declared artifacts through the supported
sandbox executor/coordinator facade. Do not rely on reading a Docker host path or bypass lifecycle admission.
The exporter requires a script that is a bounded regular file inside the declared output tree; refuse symlink
escapes, unexpected artifact names and oversized exports. Proposed cap: 1 MiB per file, 10 MiB per submission.
Preserve empty files as submitted artifacts; distinguish them from missing artifacts.

The evaluator then runs the submitted script in fresh, disposable verification sandboxes with the same pinned
image, no model credentials, no network and only each verification batch mounted read-only. Trusted checkers and
gold outputs run outside these sandboxes. Treat generated scripts as untrusted; do not import/execute them in
the evaluator process. Output directories are cleared or reset according to each task's state contract below.
Use the same process supervision and bounded command execution surface as other sandbox commands; verification
does not start extra model runs. Release all acquired uses and destroy only evaluation-owned resources.

## Data and hidden verification

Publish development inputs and business rules. Keep two frozen verification scenarios per task evaluator-only
until the corresponding script execution. For stateless tasks each scenario is one independent input batch;
for tasks 3 and 4 each scenario contains the entire multi-batch sequence defined below, with independent IDs
and values. Both complete scenarios are required for a strict pass. Only raw verification inputs are mounted, never seeds, generators,
expected outputs or checkers. Record SHA-256 for all batches before the first model run. Keep verification
results held out while improving the harness/prompt. A later tuning round needs new held-out batches/protocol.

Use deterministic synthetic data with independent reference calculations. Generate small tables (tens to a few
hundred rows), include at least two observations per boundary condition, and make important errors affect the
expected result. Vary IDs, ordering, values and edge-case combinations across development/verification batches.
These are synthetic engineering checks; do not claim representativeness of a consumer's business distribution.

Use integer cents for monetary input/output, explicit UTF-8 and ISO timestamps where applicable. Compare parsed
values with exact row multiplicities and keys; a set comparison must not hide duplicates. Sort order is checked
only where required. Empty string means missing in declared nullable CSV fields; zero is a real value. No fuzzy
matching or LLM judge. Every checker must be demonstrated to accept a trusted reference and reject named faulty
outputs, not just mirror the submitted implementation.

## Six tasks

### 1. Cleaning, duplicates and missing values

Input `events.csv`: `event_id`, `version` (integer), `updated_at` (UTC ISO timestamp), `amount_cents` (nullable),
and `status`. Require one row per event: highest version, then latest timestamp; exact duplicates collapse.
An equal-key/version/timestamp conflict with different payloads is rejected rather than resolved by input order.
Missing keys, non-integer versions, malformed timestamps, invalid money strings and status outside the explicitly
allowed set are invalid. Output `clean.csv` and `rejects.csv` with source row number and declared reason code.
All rows participating in a conflicting maximum version/timestamp group are rejected; lower valid versions are
then eligible under the same rule. Task text specifies this unusual policy to avoid evaluator-only semantics.

Check exact winners/rejections and source-row accounting; preserve null vs zero and negative amounts. Hidden
batches shuffle input and add conflicts so first-row wins, missing-to-zero and silent dropping fail.

### 2. Join cardinality and aggregate conservation

Inputs: `orders.csv` (unique order ID, customer ID, net cents) and `customer_tags.csv` (customer ID, tag), with
duplicate tag records and customers belonging to multiple tags. Output `overall.json` and `by_tag.csv`.
Overall revenue includes each order once. A customer's order contributes once to each distinct tag; an untagged
customer contributes to the reserved `UNASSIGNED` tag. A literal input tag `UNASSIGNED` is forbidden by schema.
Do not assert that tag totals sum to overall when tags overlap. Check exact per-tag counts/sums, overall counts/
sums, duplicate elimination and inclusion of unmatched customers. Hidden data includes a many-to-many trap.

### 3. Incremental upsert with late and out-of-order arrivals

Input directory contains an immutable initial target and one change batch, keyed by `(account_id, event_id)`.
Each row carries a monotonically comparable source version, update timestamp, amount and operation (`upsert` /
`delete`). Resolve duplicates by highest version then timestamp, independent of arrival order. Identical ties
collapse; conflicting ties cause a declared nonzero exit and leave existing output state unchanged. Preserve
tombstones (key/version/timestamp) in `state.json` so stale arrivals cannot revive deleted rows. Output
`current.csv` contains live rows only. Higher versions may have older business event dates; event date is not
the update ordering key.

On first execution initialize from the provided initial target; later executions read output state. Verify a
three-batch sequence, including late updates, deletions and stale reinsertions. Check state and live rows after
each step. This task measures update semantics; task 4 separately stresses replay safety at aggregate level.

### 4. Idempotent daily aggregation under batch replay

Input `transactions.csv` contains transaction ID, event timestamp, region and cents, plus a batch ID declared in
`batch.json`. Produce persisted `state.json` and `daily.csv` (UTC date/region sum and distinct transaction count).
Transaction ID is globally unique; repeated identical transactions across batches do not accumulate again.
Conflicting payload for an already accepted transaction is an explicit failure with unchanged prior output.

Verifier applies A, A again, a B batch that overlaps A, then B again, preserving the output directory. Outputs
must match the independent aggregate after every step, with no duplicated keys. Also run A+B from a fresh
directory and compare logical state. Compare parsed content, not file bytes or irrelevant metadata timestamps.
Partial-write/crash recovery is outside this suite; task-level explicit rejection must still leave outputs intact.

### 5. Schema evolution and rejection behavior

Development input is schema v1 with `amount_cents`; v2 replaces it with `amount_decimal` and adds optional
`channel`. A separate `schema.json` declares the version. Both map to a fixed output schema: ID, date, cents,
channel (default `unknown`). Unknown extra columns are ignored. Strict decimal input permits at most two
fractional digits; reject invalid values, conflicting amount fields and unknown schema versions. Any such
error aborts the entire invocation with nonzero exit, structured `error.json` containing a declared error code
and offending row number when applicable, and no data output. No implicit float rounding or guessing units.

Verify both versions on new rows, reordered columns, optional-field absence and explicit extra columns. For
invalid-schema or invalid-row fixtures, require nonzero exit, structured `error.json` and no new data output; verify with a
fresh output directory so pre-existing artifacts cannot confuse rejection checks.

### 6. Reusable revenue report on unseen inputs

Inputs: orders and partial refunds. Orders carry paid timestamp, cents, currency and test-account flag. The
task explicitly requests paid, non-test USD orders, grouped by `Asia/Shanghai` payment date. Refunds reference
an order and reduce that order's payment-day revenue regardless of refund arrival date. Exclude refunds for
ineligible orders; reject orphan refunds and over-refunds with explicit structured errors. Each refund ID is
unique, and duplicate identical records collapse. Use integer cents; no exchange-rate lookup.

Output `revenue_daily.csv` and `summary.json` with eligible-order/refund counts and gross/refund/net cents.
Verify boundary timestamps, several partial refunds, net-zero days, changed IDs and unseen row counts. Retain a
day with eligible orders even when its net total is zero; days without eligible orders are absent.
Run two independent verification batches from fresh output directories. A script that prints fixed development
answers or reads only development filenames must fail. The task measures reuse of a delivered script, not memory.

## Metrics and status

Primary metric is strict per-task pass: the script exists, exits as specified and passes development plus both
verification scenarios (every step of both stateful sequences where applicable). Report six binary task outcomes and their mean;
report failed checks separately for diagnosis, not as an inflated partial-credit headline. Report development
pass, unseen-input pass and replay pass distinctly. A missing script is `artifact_missing`; checker execution
failure is `evaluator_error`, never an agent failure. Setup failures stay visible in the selected-task denominator
as unexecuted, and no partial report claims to be the full six-task score.

Capture total and per-phase wall time, foreground plus auxiliary provider usage, tool errors/recovery counts,
verification duration and output hashes. Unknown usage remains unknown. Reference/checker tests need no model;
the pilot uses one repetition per task. Require an explicit cumulative token grant before model execution;
reuse budget reservation per provider call and include retries/titles. Proposed execution bounds are 60 agent
iterations, 15 minutes per task and 60 seconds per verification invocation, with fixed memory/CPU/PID limits
recorded in the run manifest. Budget exhaustion is retained in reporting. There is no automatic full DAB rerun.

## Acceptance and implementation boundaries

Before model execution, verify golden and intentionally faulty artifacts: many-to-many duplication,
missing-to-zero, stale overwrite, tombstone resurrection, replay double-count, float-unit conversion, schema
guessing and hardcoded answers. Verify file-export bounds, sandbox isolation, accounting and cleanup with
fake providers/real sandbox integration where appropriate. Reuse the existing CI PostgreSQL and Docker pattern.

Document the suite and commands under `evals/artifacts/README.md`; keep generated inputs, traces and results in
ignored `.local/evals/artifacts/`. Keep this suite's score separate from DAB and use the recorded baseline model
configuration for later paired comparisons. No runtime fixes or benchmark-specific product instructions belong
in this change. Before publishing a success claim, review traces for whether outputs were actually generated.

The implementation may choose JSON serialization, module layout, exact task seeds and command naming, provided
the public task contracts and immutable verification manifests remain explicit. A reviewer checks this design
for important planning gaps, then the owner approves it before implementation; the implementation plan goes in
ignored `.superpowers/` per repository policy.
