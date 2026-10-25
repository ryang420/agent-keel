# DAB scoring harness (`dab-score-v2`)

Scores Keel's own agent loop on a frozen slice of
[DataAgentBench](https://github.com/ucbepic/DataAgentBench): the 12 documented datasets and 54 queries, executed
through the normal runtime (profile, session, run, sandbox tools, terminal transition) and graded by DAB's pinned
validators. The design is `docs/design/2026-10-25-dab-scoring-baseline.md`.

This is evaluator tooling, not product code: it lives outside `src/keel`, is not packaged, and changes no runtime
behaviour. Datasets, generated runs and reports live under the gitignored `.local/evals/dab/` tree; gold answers and
validators stay outside every sandbox.

## Protocol in one paragraph

One fresh profile per query: memory off, no skills, one session, one run. Instructions are the query text, the
dataset description without hints, and boilerplate (sanctioned store paths, `ANSWER: <text>` marker). The answer is
the marker text when present, otherwise the final assistant message; the source is recorded per case. It is scored
by the query's official `validate.py`, outside the sandbox. Pass@1 is the mean over datasets of each dataset's mean
pass rate. A smoke subset has its own denominator and is never a leaderboard score.

## Setup

```bash
# one pinned evaluation image (the product sandbox image is untouched)
docker build -t keel-dab-eval:1 evals/dab

# a PostgreSQL admin URL with CREATEDB; each execution creates and drops its own database
export KEEL_DAB_ADMIN_DSN=postgresql://keel:keel_test@127.0.0.1:55437/keel_test
# model credentials come from the environment and are never written to artifacts
export KEEL_OPENAI_API_KEY=...
```

Never commit credentials, datasets, gold or reports. Everything under `.local/evals/` is ignored.

## Service-backed datasets

Seven of the twelve datasets need database servers (PostgreSQL and/or MongoDB). `execute` starts pinned
`postgres:17` and `mongo:7` containers on an internal Docker network (`keel-dab-internal`, created with
`--internal`), restores a fresh database per case from the pinned dump, and joins only that case's sandbox to the
network. The sandbox receives `PGHOST`/`PGPORT`/`PGDATABASE`/`PGUSER`/`PGPASSWORD` or `MONGO_URI`; the role is
read-only (PostgreSQL `default_transaction_read_only` plus grants, MongoDB `read` role). Databases and users are
dropped after each case, and the containers and network are removed when execution ends. Passwords are generated per
process and redacted from artifacts.

```bash
python -m evals.dab prepare --all --services postgres --services mongo
python -m evals.dab validate --all --services postgres --services mongo
```

## Commands

```bash
# fetch and verify pinned inputs; no inference
python -m evals.dab prepare                       # stage 0: stockindex + music_brainz_20k (6 queries)
python -m evals.dab prepare --datasets bookreview --services postgres

# offline readiness: inputs, prompt leakage, image; no inference
python -m evals.dab validate

# execute (serial). --max-tokens starts a fresh cumulative grant; omit it to resume an existing one
python -m evals.dab execute --max-tokens 200000
python -m evals.dab execute --datasets stockindex --max-tokens 200000 --timeout 900

# score and report
python -m evals.dab score
python -m evals.dab report
```

Outputs: `.local/evals/dab/prepared.json`, `validation.json`, `runs/dab-score-v1/executed.json`, `scores.json`,
`runs/dab-score-v1/report.json` and `report.md`, plus per-run artifacts (`prompt.txt`, `messages.json`, `run.json`,
`answer.txt`, `meta.json`).

## States

`planned`, `ready`, `blocked`, `executed`, `scored`, `budget_exhausted`, `failed`, `setup_failed`, `timeout`. A
missing `ANSWER:` line is a scored failure (`answer_missing`), never a dropped query. Setup failures and agent
failures are counted separately. A budget stop keeps the query in the report.

## What the harness guarantees

- **Frozen inputs.** `suite.json` pins the 12/54 membership and one SHA-256 per `query.json`, `validate.py`,
  `ground_truth.csv` and sanctioned data file. `prepare` fetches missing mirror files and verifies bytes and hashes;
  `validate` re-verifies offline and fails closed if anything changed.
- **No leakage.** The prompt builder accepts only the question, the non-hint description, sanctioned store paths and
  fixed boilerplate, and refuses a prompt that contains a gold value, the validator, the hint file or a hint marker.
- **Real isolation.** File stores are hard-linked into `<mount-root>/<sha256(profile_id)>` and mounted read-only at
  `/data`; a file-only query gets `network: "none"`; service datasets require their declared service and an internal
  network. The profile is retired through the deletion API after the run, so no `keel.managed=1` container is left.
- **Honest accounting.** A missing usage count is `None` (never zero). A transparent provider wrapper reserves an
  input estimate plus the output cap before every call, including titles, and charges the reported usage; a call
  that cannot reserve never reaches the provider.
- **Reproducibility.** Records carry the Keel revision and dirty state, image ID, pinned revision, prompt hashes,
  requested and returned model IDs, and per-run usage.

## Stage 0 smoke

`stockindex` and `music_brainz_20k`: six queries, about 7.6 MB, SQLite and DuckDB only, no services. This proves
prepare → validate → execute → score → report before any service-backed dataset is added. Stage 1 adds PostgreSQL
(`bookreview`, `googlelocal`) and MongoDB (`agnews`); stage 2 runs the full 12/54, five runs per query for a
leaderboard-comparable figure.

## Non-goals and caveats

- No public leaderboard submission; no DAB-tuned prompt and no hints (`db_description_withhint.txt`). Enabling either
  changes the protocol and needs a new named version.
- Keel's generic tools replace DAB's `query_db`/`list_db`/`return_answer` scaffold; per DAB's rubric the boilerplate
  that tells the agent where the sanctioned stores are is legitimate, but this is a generic-runtime baseline, not the
  DAB scaffold, and it is not directly comparable to leaderboard entries.
- Validators check answer content, not derivation. The report says so; inspect run artifacts for provenance.
- Budget enforcement is per provider call, not per run: a run fails with a budget stop if a later call cannot
  reserve, and the query stays in the report.
- The `prepare` command needs network access for the pinned checkout and mirror files; `validate` and `score` do not.
