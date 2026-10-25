# Data-artifact correctness evaluation

Protocol `artifact-correctness-v2` checks six small synthetic data tasks through Keel's own runtime:
cleaning, joins, incremental upsert, replay idempotence, schema evolution and reusable revenue reporting.
It is separate from DAB; it changes no `src/keel` runtime code and does not measure cross-session memory.

## Prepare and inspect (no inference)

From the repository root:

```bash
.venv/bin/python -m evals.artifacts prepare
.venv/bin/python -m evals.artifacts validate --skip-image
docker build -t keel-artifact-eval:1 evals/artifacts
.venv/bin/python -m evals.artifacts validate
```

The default root is ignored `.local/evals/artifacts-v2/`. `inputs/<task>/development/` contains visible step inputs;
the public task instructions are under `public/`. Verification inputs and `expected/` remain evaluator-only.
`manifest.json` fixes the bytes before inference. Validation fails on changed inputs or unexpected files in
these trees. Changing fixture code requires a fresh root and a new documented protocol revision for comparisons.

There are two independently seeded hidden scenarios per task. Stateful scenarios contain every step, not just
two individual data batches. The revenue scenario also varies input row counts. These are intentionally small
engineering contracts, not a representative sample of a consumer's workload.

## Offline and Docker acceptance

```bash
.venv/bin/python -m pytest -q -m 'not integration' tests/test_artifact_tasks.py \
  tests/test_artifact_checks.py tests/test_artifact_transport.py tests/test_artifact_runner.py tests/test_artifact_cli.py

# Use the dedicated test PostgreSQL compose service, never a production database.
docker compose -f docker-compose.test.yml up -d --wait
KEEL_TEST_POSTGRES_DSN=postgresql://keel:keel_test@127.0.0.1:55437/keel_test \
KEEL_TEST_DOCKER=1 .venv/bin/python -m pytest -q tests/test_artifact_runner.py
```

The Docker tests execute known scripts and a scripted provider. They verify output export, unseen-input replay,
budget rejection, and lifecycle cleanup; they make no paid model calls. A passing test run is not a model score.
The coordinator needs a built supervisor for the Docker daemon's architecture; follow the project setup guide
if `src/keel/sandbox/bin/manifest.json` is absent.

## Later live pilot (explicit spending grant)

Configure `KEEL_OPENAI_API_KEY` (or `OPENAI_API_KEY`) in the invoking process. The harness uses the official
OpenAI endpoint; secrets are never copied into the sandbox or result artifacts. Set `KEEL_ARTIFACT_ADMIN_DSN`
to a scratch PostgreSQL server role with CREATEDB, or reuse `KEEL_TEST_POSTGRES_DSN`. Each execution creates and
drops a private runtime metadata database; it does not change the supplied base database.

```bash
.venv/bin/python -m evals.artifacts execute --run-id pilot-1 --tasks joins \
  --model gpt-6-luna --max-tokens 200000
.venv/bin/python -m evals.artifacts report --run-id pilot-1
```

Omit `--tasks` to select all six. Execution is serial, one repetition per invocation; use a new run ID for every
repetition. Existing run directories cannot be overwritten. Both titles and source model calls count against the
cumulative grant. Admission reserves an input estimate plus an 8,192-token output cap before each provider call;
the reported usage settles the reservation. Unknown usage remains null and charges the reserved estimate.
This is estimate-based admission, not a guarantee that reported input tokens cannot exceed an estimate. Dollar
cost is not inferred. Budget stops remain visible in reports and do not trigger a full DAB rerun.

## Submission and verification contract

The agent must deliver `/work/output/solution.py`, run all public development steps, and leave the declared final
development artifacts in that directory. The script interface is:

```text
python solution.py --input-dir /input --output-dir /output
```

Final results must be regular files directly in `/work/output`, alongside `solution.py`. Each prompt lists both
allowed filenames and the required final files. Subdirectories such as `results/`, `stateful/` or `independent/`
are not submissions. Use `/work/tmp` for scratch work; remove all caches, temporary files and extra directories
before finishing. `python -m py_compile` creates `__pycache__`, which must be removed or redirected outside the
submission directory. A final rejection step must leave the prescribed error or unchanged state, not earlier
success outputs. Links and special files remain forbidden.

Schemas, rejection codes, state format, step reset/preservation rules and required filenames are included in
each task prompt. Verification varies paths, IDs, values and boundary cases. Monetary values are integer cents;
null is distinct from zero. CSV header names/order are explicit, row order is ignored and row multiplicities
are checked. JSON numbers preserve type, so booleans and floats do not pass as integers.

An evaluator-only executor exports allowed regular files before releasing the source sandbox's active usage.
Each export helper gets a coordinator execution ticket. Symlinks, special files, unexpected output names,
files over 1 MiB or total submissions over 10 MiB are refused. Missing and empty artifacts differ.
Rejections include bounded `export_issues` in `execution.json`, task records and reports: `nonregular_entry`
identifies a directory/link/special file, `unexpected_entry` identifies an extra file, and `file_too_large`
identifies its filename and size. Inventory is capped at 128 entries; helper failures never expose raw tracebacks.

The script is then run against development and both hidden scenarios using fresh verification profiles and
registered, supervised execution; no model is called during verification. For stateful steps only declared
output bytes are carried forward into the next fresh profile. This preserves logical file state without
exposing future input batches, changing mounts, or relying on an old process. Network is disabled; only raw
inputs enter each sandbox. Gold and checkers execute outside the sandbox. No submitted script executes on the
host. Default limits are 1 CPU, 512 MiB, 128 PIDs, 60 seconds per verification invocation and 900 seconds per
source model task, with 60 agent iterations. The actual image ID is recorded.

## Results and limits

Each run directory contains frozen configuration/hashes, source prompt/transcript/submission, per-step execution
records, `records.json`, `report.json` and `report.md`. The token ledger is under `budgets/<run-id>.json`.
Requested model IDs are known; returned model ID is null because Keel's current provider-neutral event surface
does not expose it. No value is invented to fill that field.

A strict task pass requires the source's final declared artifacts plus every development/hidden verification
step. The report distinguishes development, unseen inputs and replay; setup failures, export errors, missing
scripts, timeouts, evaluator errors and budget stops remain visible. The selected-task denominator never shrinks
to only successful tasks. No headline partial credit combines checks or combines this score with DAB.
`complete` requires every selected task's verification sequence to execute fully; a checker mismatch still counts
as an executed attempt, while an infrastructure or evaluator failure leaves the evaluation incomplete. Per-step
execution states and named check failures are retained in the JSON report.

The original DAB scores stay unchanged. The previous trace analysis motivates these tasks but does not establish
that a given defect has been fixed. Code improvements need paired reruns under the same protocol and model.

## Protocol history and diagnostics

v2 clarifies top-level placement, required final filenames and cache cleanup; task inputs and numerical checkers
are unchanged. Its default root is `.local/evals/artifacts-v2`; existing v1 roots and scores must stay untouched.
Reports read their protocol from the saved run configuration, so an explicit historical `report --root
.local/evals/artifacts --run-id ...` retains the v1 label.

The default live image is built from this directory's Dockerfile and includes ripgrep and timezone data. Missing
ripgrep in the stock Python image can trigger uncertain execution before the tool fallback completes; it is not
a model-quality failure. Every run records the actual image digest. No-model tests may still use the base image.

Recovered scripts are diagnostic candidates derived from successful transcript writes/patches, not verified
copies of the retired sandbox filesystem. Audit terminal commands for additional edits. Run them unchanged only
inside coordinated sandboxes, with a provider that refuses model calls. Check input/gold hashes against the old
run config and retain transcript/script hashes. These results bypass source submission layout and never replace
or inflate the original strict delivery score. No live model rerun is part of this follow-up.

`evals.artifacts.recovery.recover_script(messages)` returns candidate bytes, applied/skipped operation hashes and
terminal commands requiring audit. It refuses missing/unpaired tool results, unverifiable write lengths and
patches inconsistent with the reconstructed source. It relies on preserved transcript ordering because historical
logs omit tool-call IDs. Recovery itself performs no execution; only the existing coordinated verifier runs the
candidate. Keep diagnostic results in a separate directory and never replace old submissions or reports.
