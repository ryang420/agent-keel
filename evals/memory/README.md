# Cross-session memory evaluation (`memory-eval-v1`)

Measures whether Keel's cross-session memory (durable entries, background review, `session_search`) changes what a
live model does in a fresh session, against the same model with memory off. The design is §7 of
`docs/design/2026-10-24-cross-session-memory.md`; this harness is its opt-in live half. Deterministic acceptance
tests with scripted models cover the plumbing in CI, and a passing scripted model is not evidence of memory quality.

This is evaluator tooling, not product code: it lives outside `src/keel`, is not packaged, and changes no runtime
behaviour. Runs and reports live under the gitignored `.local/evals/memory/` tree.

## What it measures

`suite.json` holds 32 scenarios, 16 English and 16 Chinese, in two groups and six kinds:

| Group | Kind | Scenarios | What the probe checks |
|---|---|---|---|
| preference | explicit | 8 | A stated preference is followed in a fresh session |
| preference | exception | 4 | A one-off exception does not overwrite the general preference |
| preference | correction | 4 | After a correction, only the corrected fact applies |
| recall | decision | 8 | An old, never-memorised decision is found through `session_search` |
| recall | distractor | 4 | The right decision is found among other, unrelated sessions |
| recall | absence | 4 | No matching history: the answer says honestly it found nothing |

The loader rejects a malformed suite: unknown group, kind or language, a kind outside its group, duplicate ids, empty
sessions or probes, unknown checks, and needles that are empty after normalisation.

## Protocol

For every scenario and arm (`memory`, `baseline`):

1. A fresh profile, named for the scenario and arm. The memory arm has memory on; the baseline has it off.
2. The setup sessions run in order, one user turn per run. For preference scenarios in the memory arm the review
   queue is drained after each setup session, so the memory is written before the next session starts. Recall
   scenarios are not drained: their facts are old and unmemorised, so recall is measured through `session_search`,
   not through a memory snapshot. The case record and the report show, for recall passes, how many probes used
   `session_search` (`searched`) and how many had the answer in the stored memory (`memorised`).
3. The probe is asked in a fresh session. Session titles are pinned so no background title call runs.
4. The answer is scored by deterministic, case-insensitive substring checks. There are no regexes and no model judge.
5. Afterwards the profile's memory is switched off, so later drains never review its probe session.

Text is normalised on both sides before comparing: case-folded, with whitespace, quote and apostrophe characters and
markdown emphasis (`*`, `_`, `~`) removed. Spacing, quote style and bold markers never decide a check. The check
names are `contains_all`, `contains_any`, `contains_any_2` (a second any-of), `not_contains` and `memory_not_contains`
(the stored memory must not hold the needle; used for unsupported preferences in the exception scenarios).

**Exposure.** Each scenario also gets a decoy profile in the same tenant, holding a unique marker in a message and in
a memory entry. If the marker appears in the probe's answer, in a tool result of any setup or probe session, or in the
stored system prompt of any of those sessions, the case fails with an exposure. The probe's system prompt is kept in
the case file as `probe.system`.

**Errors.** A provider failure, timeout or crash is recorded as a failed case with `error:` and the exception type,
never scored as a wrong answer. So is a memory review that did not finish: after each drain every review job of the
profile must be `done` without an error code, otherwise the case is an `error: review_<status or code>`. A decoy that
cannot be planted errors that scenario's arms and the run continues. Error text is redacted before it is written. The case is still checked for exposure and unsupported entries where the data can be
read.

**Baseline.** The same scenarios with memory off, run against the same model, show what the memory adds. The baseline
has no gate.

## The gate

Quoted from the design: "at least 90% correct outcomes in each of preference continuity and history recall, zero
cross-profile exposure, and zero unsupported durable preferences in the negative cases." The memory arm passes only
when all of these hold:

- at least 90% of the scenarios in each group pass;
- zero exposures;
- zero unsupported preferences in the exception scenarios;
- the arm is complete: exactly one outcome per scenario;
- zero errored cases.

A `--scenarios` subset run reports the gate as not applicable. The command exits non-zero when the gate fails, or,
for a subset or baseline-only run, when any case errored.

## Reviewing scenarios

The owner reviews `suite.json` before a gated run, and before accepting a gated result inspects, by hand, the stored
memory and change ledger (in the case files) of the four exception cases. A passing score does not show that nothing
unsupported was written, so these are read, not trusted. A scenario is never changed after its results have been seen: a
changed scenario gets a new id and the run is repeated.

## Commands

```bash
python -m evals.memory validate                                      # offline: checks suite.json, no database
export KEEL_MEMORY_EVAL_ADMIN_DSN=postgresql://keel:keel_test@127.0.0.1:55437/keel_test   # role needs CREATEDB
python -m evals.memory run --provider openai --model "$KEEL_LIVE_OPENAI_MODEL"           # or --provider bedrock
python -m evals.memory bench                                         # no model calls
```

`run` creates and drops a private database. `--model` defaults to `KEEL_LIVE_OPENAI_MODEL` or `KEEL_LIVE_MODEL`
(per provider); no model is guessed. Other options: `--arm memory|baseline|both` (default both), `--scenarios a,b`,
`--suite`, `--root`, `--database-admin-dsn`. A Bedrock run requires `AWS_REGION` or `AWS_DEFAULT_REGION`; no region is
assumed.

## Credentials and artifacts

Credentials, endpoint and region come from the environment (`KEEL_OPENAI_API_KEY`, `KEEL_OPENAI_BASE_URL`,
`KEEL_OPENAI_COMPAT`, `KEEL_OPENAI_CONTEXT_WINDOW`, `AWS_REGION` and the usual AWS variables) and are never written to
artifacts. The report and `results.json` record the model, the provider, the keel version, the sha256 of the suite file, the git
commit (`unknown` when git is unavailable) and, for Bedrock, the region.

Everything goes under `.local/evals/memory/`, which is gitignored:

- `runs/<timestamp>/report.md` and `results.json`: per-arm group and language rates, the gate, errored cases.
- `runs/<timestamp>/outcomes.json`: one outcome per scenario and arm.
- `runs/<timestamp>/cases/<scenario>/<arm>.json`: the setup and probe transcripts, the stored memory, the change
  ledger, the review jobs, foreground usage and review usage (kept apart), and the outcome.
- `bench/<timestamp>/bench.json` and `bench.md`: the benchmark below.

## Search benchmark

`bench` makes no model calls. In a private database it bulk-inserts synthetic English and Chinese messages (100,000 in
one profile, 20,000 across two others, by default), then indexes them with the real backfill. Words and characters
are drawn with Zipf-like weights, so a few terms are very common and posting lists are realistic. It runs
`VACUUM ANALYZE` on the search tables, then times `session_search` for 500 queries by default (Latin queries of 1-5 words, Han queries of 2-4 characters) (`--messages`,
`--others`, `--queries`).

It records the search tables' bytes (text, tokens and indexes), backfill throughput, append overhead (indexed against
raw insert, alternating order, mean, median and p95), and p50, p95 and max latency overall and per query class:
`latin`, `han` and `absent` (a word that is never in the data). The target is `met` when every class's p95 is under
500 ms, latin and han queries mostly hit (hit rate above 0.5) and absent queries never do. The search runs warm-cache
(the data was just written), which the report states; the PostgreSQL version and settings and the machine are
recorded with the result. The command exits non-zero when the target is not met.
