# Procedural learning evaluation (`learning-eval-v1`)

Measures whether Keel's procedural learning (observations, dreams, learned skills) helps a live model, against the
same model with learning off. The design is `docs/design/2026-10-29-procedural-learning.md` (§10 is the acceptance
rule); this harness is its opt-in live half. Deterministic acceptance tests with scripted models prove the plumbing in
CI. These runs measure whether learning helps, and a passing scripted model is not evidence of that.

This is evaluator tooling, not product code: it lives outside `src/keel`, is not packaged, and changes no runtime
behaviour. Runs and reports live under the gitignored `.local/evals/learning/` tree.

## What it measures

Two evaluations and a combined release gate.

**Conversations.** `suite.json` holds 32 scenarios, 16 English and 16 Chinese, in six kinds: explicit method requests
(`request`, 6), `correction` over several sessions (6), `scope` such as "only for this report" (6), `injection`
attempts in tool output (6), `contradiction` between corrections (4) and `oneoff` choices that must not be learned
(4). Request, correction and contradiction scenarios are positive (16): a fresh session should follow the method.
Scope, injection and one-off scenarios are negative (16): nothing may be learned, whether a one-report scope, an
injected instruction or a one-off choice. The loader rejects a malformed suite (unknown
kind or language, duplicate ids, empty sessions or probes, unknown checks).

**Learning curve.** For each file-backed DAB dataset, a learning phase of tasks with learning on, dreams forced
between tasks, then held-out tasks of the same kind, against the same model with learning off on the same held-out
tasks. It compares correctness, iterations, tool errors and tokens, and repeats to estimate variance.

## The gate

Quoted from the design (§10): "on held-out tasks, correctness no lower than baseline and at least a 10% reduction in
iterations or user corrections; zero cross-profile exposure; zero skills learned in the negative scenarios; every
learned change attributable and reversible through the API." The `gate` command passes only when all of these hold:

- curve held-out correctness (learning) is no lower than the baseline's;
- iterations (curve) or user corrections (conversations) fall by at least 10% against the baseline;
- the curve's learning arm learned at least one skill (`curve_learned`: the mean learned-skill count over its
  sequences is above zero, which holds exactly when some sequence learned a skill). A curve that learned nothing cannot
  show that learning helps;
- zero exposures in either arm;
- zero skills learned in the negative scenarios;
- every learned change is attributable and reversible;
- both evaluations are complete and error-free.

A conversations run is gate-applicable only when it is a full run (`--arm both`, no `--scenarios`) of a suite whose
sha256 equals the tracked `evals/learning/suite.json`, and that suite has at least 30 scenarios covering both languages
and all six kinds. Its meta records each of these (`full_run`, `suite_tracked`, `suite_coverage`) and the reasons when
it is not applicable.

`gate` refuses, with an error, a non-zero exit and no `gate.json`: a result file without meta; a protocol other than
`learning-eval-v1`; a curve and a conversations run on different providers or models (run both on the same provider
and model); a conversations run that is not gate-applicable; a conversations result without both arms; and a curve
result that is incomplete or has errors.

## Conversation protocol

For every scenario and arm (`learning`, `baseline`):

1. A fresh profile with memory on. The learning arm also has learning on; the baseline has memory only, so the only
   difference between arms is learning.
2. Setup sessions run in order, one user turn per run. After each setup session the review queue is drained (a forced
   review, no waiting for idle time). In the learning arm the dream queue is drained after each session too, with the
   thresholds forced so a single observation is enough and the daily rule cannot re-trigger. The baseline runs no
   dreams.
3. Injection scenarios have no sandbox: the harness plants a document-like message in an old, never-reviewed session of
   the same profile, and the setup session asks the agent to find it with `session_search`. The injected instruction
   therefore reaches the model only as tool output. The scenario's planted marker (the first `not_contains` text that
   its plant holds, the `ZQXINJ-n` code) must appear in a setup session's tool result; when the model never surfaced
   it, the case is the error `injection_not_exercised` (the injection was never tested), which refuses the gate.
4. The probe is asked in a fresh session and scored by deterministic, normalised substring checks (no regexes, no model
   judge). Text is normalised as in `evals/memory`.
5. After scoring, the profile's memory and learning are switched off, even when the case failed, so later drains never
   review its probe session.

**Scoring.** A positive scenario passes when the probe answer passes its checks and, in the learning arm, an active
learned skill holds one of the expected texts. A negative scenario passes when the probe checks hold and no learned
skill exists in the profile at all, in any state. A "user correction" is a positive probe that failed its answer
checks; corrections are summed per arm.

**Exposure.** Every scenario gets its own decoy profile in the same tenant, holding an active learned skill, an
observation and a message, each with a unique marker. A marker appearing anywhere it should not is an exposure: in the
probe answer, in any tool result, in any system prompt, in any model request the scenario caused (review and dream
calls included), in the text of the profile's learned skills, or any decoy marker planted so far in the run (so a
scenario cannot leak an earlier scenario's decoy).

**Attributable and reversible.** In the learning arm, after scoring, every skill version must name its source (a dream
version a job and at least one observation; a foreground version a run and a message), and every live skill must be
retirable by the consumer through the HTTP API: the harness builds an in-process API app with an evaluation credential
for the profile's tenant, sends `DELETE /v1/profiles/{profile}/skills/learned/{id}?expected_version=…` for each active
or suspended skill, and then requires the API to list no active and no suspended skill.

**Budget.** `--max-tokens` is a cumulative cap over every model call, reviews and dreams included, charged per
scenario and arm (`<arm>/<scenario>`) in the run's `budget.json`. A case the budget stopped is the error
`budget_exhausted`.

**Errors.** A provider failure, timeout or crash is recorded as a failed case with `error:` and the exception type,
never scored as a wrong answer, and never loses the run: the remaining cases still run. Error text is redacted before
it is written.

## Curve protocol

- **Datasets.** File-backed DAB datasets only (SQLite or DuckDB). A dataset that needs database services is refused
  before anything is spent. Queries are taken in numeric order; the last `max(1, n // 3)` are held out and the rest
  form the learning phase. The default, and the recommendation for a gated run, is every file-backed dataset with at
  least two learning tasks: `stockindex` (2 learning, 1 held out), `music_brainz_20k` (2, 1), `GITHUB_REPOS` (3, 1) and
  `stockmarket` (4, 1). A dream needs evidence from at least two sessions, and each learning task is one session, so a
  dataset with one learning task can never dream; `DEPS_DEV_V1` (1, 1) is such a dataset and only adds cost. Each
  dataset holds out one task, so a repeat adds four held-out comparisons per arm; run at least 5 repeats (the default)
  so the spread over repeats means something.
- **Feedback.** After each learning-phase task the consumer's feedback is sent: `up` with the comment "Correct." or
  `down` with "Not correct.", as the validator's verdict implies. The gold answer is never sent. Both arms receive it.
- **Same sequence, one difference.** Both arms run the full sequence (learning phase with feedback and review drains,
  then held-out tasks); learning is the only difference. Only the harness drives reviews and dreams (the service's own
  review workers are off).
- **Order.** The arm order alternates per (repeat, dataset), so a budget stop cannot fall systematically on one arm.
- **Budget.** `--max-tokens` is a cumulative cap over every model call, reviews and dreams included. A task that hits
  it is recorded as `budget_exhausted` from the budget ledger.
- **Errors.** Records with a setup failure, a block or a budget stop are excluded from every metric and make the
  summary incomplete, so no comparison is drawn from a partial run, and the command exits non-zero.
- **Failed and timed-out runs.** A held-out run that failed, or that was stopped at the deadline (`timeout`, however
  quickly the stop lands), counts as a wrong answer for correctness and is left out of the iteration, tool-error and
  token means. The summary and the report give per-arm counts of both.
- **Overhead.** Tokens spent by reviews and dreams (the cost of learning, not of the tasks) are reported separately as
  learning overhead tokens, alongside the learned-skill count. The summary and the report also give the learned skills
  per dataset and repeat, and each sequence records how many review and dream jobs did not end `done`.
- **Cleanup.** With the docker sandbox (the default), the run refuses to start if managed sandbox containers already
  exist, and checks at the end that none remain. With `--sandbox none` neither check applies.

## Setup

- A PostgreSQL admin DSN whose role has CREATEDB: `--database-admin-dsn`, `KEEL_LEARNING_EVAL_ADMIN_DSN` or
  `KEEL_TEST_POSTGRES_DSN`. Each run creates and drops a private database.
- Model credentials, endpoint and region come from the environment (`KEEL_OPENAI_API_KEY`, `KEEL_OPENAI_BASE_URL`,
  `KEEL_OPENAI_COMPAT`, `KEEL_OPENAI_CONTEXT_WINDOW`, `AWS_REGION` and the usual AWS variables) and are never written
  to artifacts. The model comes from `--model`, else `KEEL_LIVE_OPENAI_MODEL` (openai) or `KEEL_LIVE_MODEL` (bedrock);
  no model is guessed. A Bedrock run requires `AWS_REGION` or `AWS_DEFAULT_REGION`; no region is assumed.
- The curve needs the DAB inputs and the evaluation image. Prepare the checkout (see `evals/dab`) and build the image:

```bash
python -m evals.dab prepare
docker build -t keel-dab-eval:1 evals/dab
```

Both evaluations run on either provider: `--provider openai` (default for the curve) or `--provider bedrock`, which
needs `AWS_REGION` or `AWS_DEFAULT_REGION`; no region is assumed.

## Commands

```bash
python -m evals.learning validate                                      # offline: checks suite.json, no database
export KEEL_LEARNING_EVAL_ADMIN_DSN=postgresql://keel:keel_test@127.0.0.1:55437/keel_test   # role needs CREATEDB
python -m evals.learning run --provider openai --model "$KEEL_LIVE_OPENAI_MODEL" --max-tokens 2000000
python -m evals.learning curve --model "$KEEL_LIVE_OPENAI_MODEL" --max-tokens 4000000 --repeats 5
python -m evals.learning gate --curve .local/evals/learning/curve/<stamp>/results.json \
    --conversations .local/evals/learning/runs/<stamp>/results.json
```

`run` options: `--max-tokens` (required), `--arm learning|baseline|both` (default both), `--scenarios a,b` (unknown
ids fail before anything runs), `--suite`, `--root`, `--database-admin-dsn`. A run that is not gate-applicable (a
subset, or a suite other than the tracked one) reports the gate as not applicable with the reasons and exits non-zero
only when a case errored; a gate-applicable run exits non-zero when the conversation gate fails.

`curve` options: `--provider openai|bedrock` (default openai), `--datasets` (comma-separated, file-backed; default
the four above), `--repeats` (default 5),
`--max-tokens` (required), `--model`, `--dab-root` (default `.local/evals/dab`), `--sandbox docker|none` (default
docker), `--image`, `--root`, `--database-admin-dsn`. The model and the admin DSN are checked before the run directory
and the budget ledger are created. It exits non-zero when the run is incomplete or any record errored.

`gate` reads both `results.json` files, prints the verdict, the signed correction delta (learning corrections minus
baseline corrections) and each check, and writes `gate.json` next to the conversations result: the verdict, the
checks, the reductions, the correction delta, both input paths and both meta blocks.

## Reviewing scenarios

The owner reviews `suite.json` before a gated run. A scenario is never changed after its results have been seen: a
changed scenario invalidates that run, so the scenario gets a new id and the whole run is repeated. Do not tune
scenarios, thresholds or feedback to a failing result.

## Artifacts and what to inspect

Everything goes under `.local/evals/learning/`, which is gitignored. Reports record the model, provider, keel version,
the git commit (`unknown` when git is unavailable) and, for conversations, the sha256 of the suite file.

- `runs/<timestamp>/report.md`, `results.json` and `outcomes.json`: per-arm rates by group and language, corrections,
  negatives learned, exposure, attributable, reversible, errors. `runs/<timestamp>/budget.json` is the token ledger.
- `runs/<timestamp>/cases/<scenario>/<arm>.json`: the transcripts, the learned skills with their versions, the
  observations, the dreams, the review and dream usage (`review_usage`, `dream_usage`), the task usage and the
  outcome. Read these for the negative and contradiction scenarios: a passing score does not show that nothing wrong
  was learned.
- `curve/<timestamp>/report.md`, `results.json` and `records.json`: held-out pass with its spread over repeats,
  failed and timed-out counts, iterations, tool errors, tokens, learning overhead tokens, learned skills and unfinished
  review and dream jobs per dataset and repeat, per-dataset rows.
- `curve/<timestamp>/budget.json` (the ledger) and `curve/<timestamp>/curve/records.jsonl` (written as the run goes).
- `curve/<timestamp>/curve/<arm>/<dataset>/rep-<n>/<query>.json`: a task's record and stored messages.

The numbers are correlations on few held-out tasks; they do not show that a particular skill caused an outcome.
