# DAB scoring baseline for data work

Date: 2026-10-25. Status: proposed; DAB-only scoring selected, stage 0 smoke executed under `dab-score-v2`;
design awaiting review and approval.

## Scope decision

The owner narrowed this work to one question: how well does Keel's agent loop complete real data-analysis tasks?
DataAgentBench (DAB) is now the only scoring suite. The earlier draft of this document combined LongMemEval-S,
Spider2-DBT and six custom cross-session scenarios to measure memory; that plan is withdrawn. DAB is single-session
and independent per query, so it cannot support a memory claim: no memory arm, review threshold, import protocol or
custom scenario remains in this protocol. Cross-session memory stays with
`2026-10-24-cross-session-memory.md` and is not measured here.

The harness lives outside `src/keel` and changes no product behaviour. No memory algorithm, review threshold, prompt
text, capacity limit or product schema is modified to improve the score.

## Goal and protocol

Produce one reproducible score, protocol `dab-score-v2`: upstream Pass@1 over a frozen slice of DAB, with Keel's own
agent loop executing each query through its sandbox tools.

- One fresh profile per query: memory off, no skills, one session, one run. Nothing carries between queries and no
  previous task's files, results or scripts are visible.
- Instructions contain the query text, the dataset's description without hints, and harness boilerplate only: where
  the sanctioned stores are mounted and the required final answer line. Gold answers, `validate.py`, other runs and
  DAB's own agent scaffold never enter a sandbox or a prompt.
- Model: the OpenAI provider with `gpt-6-luna` and reasoning effort `low`. The returned model identifier is recorded
  next to the requested one; credentials stay deployment inputs and are never fixtures or artifacts.
- Final answer: the assistant's last message. An `ANSWER: <text>` line is preferred; when it is absent the final
  message itself is the answer under `dab-score-v2`, and the source is recorded per case so marker compliance stays
  visible. The harness passes that text to the query's official `validate.py`. An empty final message is a recorded
  failure, not a dropped query.
- One run per query in the pilot. Five independent runs per query are required for a leaderboard-comparable number;
  that is a separate, explicitly budgeted execution.

## Pinned inputs

DAB code, tasks, validators and gold: revision `61fb21e0766d775acae443f6074f5d498e95f938` from
<https://github.com/ucbepic/DataAgentBench>. Data files are mirrored at
<https://huggingface.co/datasets/ruiyingm/DataAgentBench-data> and pinned per file by SHA-256 and byte size in DAB's
`dataset_manifest.tsv`; the harness verifies both before a case is ready.

Frozen membership is the documented **12 datasets / 54 queries**, identified by dataset and query id and pinned by
the SHA-256 of each `query.json` in the tracked suite file. Five further query directories in the repository
(`civic_unstructured`, `cve`, `imdb`, `krama`, `usaspending`; 50 queries, 5.0 GB) are not in the documented
leaderboard denominator and are excluded.

| Dataset | Queries | DBMSes | Data (approx.) |
|---|---|---|---|
| `agnews` | 4 | MongoDB, SQLite | 4.0 MB |
| `bookreview` | 3 | PostgreSQL, SQLite | 1.7 MB |
| `crmarenapro` | 13 | DuckDB, PostgreSQL, SQLite | 32.8 MB |
| `DEPS_DEV_V1` | 2 | DuckDB, SQLite | 549.9 MB |
| `GITHUB_REPOS` | 4 | DuckDB, SQLite | 928.5 MB |
| `googlelocal` | 4 | PostgreSQL, SQLite | 0.6 MB |
| `music_brainz_20k` | 3 | DuckDB, SQLite | 3.1 MB |
| `pancancer_atlas` | 3 | DuckDB, PostgreSQL | 301.4 MB |
| `patents` | 3 | PostgreSQL, SQLite | 5,556.3 MB |
| `stockindex` | 3 | DuckDB, SQLite | 4.5 MB |
| `stockmarket` | 5 | DuckDB, SQLite | 965.7 MB |
| `yelp` | 7 | DuckDB, MongoDB | 3.4 MB |

All 104 validators in the pinned revision are answer-string checks; none imports a database driver. The evaluator
therefore needs no DB client to score, and scoring runs outside every sandbox with gold and validators evaluator-only.

## Sandbox and services

- Pinned evaluation image from `evals/dab/Dockerfile`: Python 3.12 with DuckDB, `psycopg[binary]`, `pymongo` and
  pandas. Its digest and exact installed versions are recorded per execution; the user's ordinary sandbox image is
  untouched.
- File-backed stores (SQLite, DuckDB) are bind-mounted read-only under
  `KEEL_SANDBOX_MOUNT_ROOT/<sha256(profile_id)>`, as the sandbox policy requires. Sources are created per profile
  from hard links or filesystem clones, never copies of multi-gigabyte files, and removed after the run. A query
  whose stores are all files gets `network: "none"`.
- PostgreSQL and MongoDB datasets use dedicated servers on an internal Docker network. The evaluator restores a fresh
  database per run and joins only that sandbox to the network, with a read-only benchmark identity. The Keel
  metadata database identity is never used and the sandbox never receives internet access.
- No selected dataset requires cloud credentials. The sandbox never mounts the DAB checkout, the evaluator directory
  or another profile's tree.

## Scoring and reporting

- `official_validator_pass` is the result of the query's `validate.py`, with `ground_truth.csv` kept evaluator-only.
- Aggregation follows upstream: Pass@1 is the mean over datasets of each dataset's mean pass rate, over the frozen
  membership. A smoke subset reports its own denominator and is never presented as a leaderboard score.
- Answer match alone is not proof of computation, so the report also records executable provenance separately: which
  sanctioned store and query produced the answer, from the tool trace. Provenance is reported, never folded into the
  official pass.
- States are `planned`, `ready`, `executed`, `scored`, `blocked` and `budget_exhausted`. Missing output is failure;
  setup failures stay distinct from agent failures and both appear in the report. Empty or unknown usage is `None`,
  never zero.

## Budgets, isolation and reproducibility

- Preparation and validation make no inference calls. Execution requires an explicit cumulative token budget and has
  no default full-dataset run. Before every run the harness reserves estimated input plus maximum output; on
  exhaustion it stops with `budget_exhausted` and keeps the query in the report.
- Record Keel SHA and dirty state, input hashes, image digest, prompt hash, requested and returned model identifiers,
  reasoning effort, per-run token usage, tool traces, durations and output artifact hashes.
- Generated runs, reports, gold and datasets live under ignored `.local/evals/`. Evaluator material stays outside
  every sandbox and outside directories mounted into one.

## Staging

- **Stage 0 — smoke, file-backed only.** `stockindex` and `music_brainz_20k`, six queries, about 7.6 MB, DuckDB and
  SQLite, no services. Proves prepare → validate → execute → score → report end to end.
- **Stage 1 — service-backed stores.** PostgreSQL (`bookreview`, `googlelocal`) then MongoDB (`agnews`), with per-run
  database restore on the internal network.
- **Stage 2 — full membership.** All 12 datasets / 54 queries, one run for the pilot, five runs per query for a
  leaderboard-comparable number.

## Pilot result (stage 0)

The stage-0 smoke ran on 2026-10-25 with `openai`/`gpt-6-luna`, reasoning effort `low`, Keel `0eedd98` (dirty tree)
and image `sha256:9ff7b37f…`. All six queries executed, with no setup failures, timeouts or budget stops; 117,655
tokens were charged across 26 provider calls and the 2,000,000-token grant was not exhausted. Pass@1 was 0.8333
(`stockindex` 3/3, `music_brainz_20k` 2/3). This is a smoke subset, not a leaderboard score.

The run exposed one protocol defect. Three of six final messages carried no `ANSWER:` marker, although two of them
already stated the correct answer; strict marker-only extraction scored them as failures and measured formatting
rather than analysis. `dab-score-v2` therefore extracts the marker when present and otherwise the final message,
recording the source, and the same six transcripts were re-scored without further inference. The failing query is a
genuine one: its answer names a `(None, None)` title/artist group as the revenue leader, so the validator's title
match fails. No prompt, model, membership or hint was changed to produce the higher score, and the report keeps the
fallback count beside the result.

## Gate and non-goals

Before execution of the full membership: review this document for material planning gaps and approve it, then write
the implementation plan under ignored `.superpowers/`. No public leaderboard submission is authorized. No DAB-tuned
prompt and no hints (`db_description_withhint.txt`); enabling either, changing the extraction rule, or changing the
frozen membership creates a new named protocol version.
