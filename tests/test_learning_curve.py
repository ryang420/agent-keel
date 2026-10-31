"""The DAB learning curve with a scripted model and no sandbox: the split, a learning and a baseline sequence per
dataset, feedback after learning tasks, held-out metrics and the summary."""

import json
from pathlib import Path

import pytest

from evals.dab import inputs
from evals.dab.budget import Ledger
from evals.dab.suite import Case, Dataset, Query
from evals.learning import curve
from evals.learning.curve import CurveError, _task, execute_curve, metrics, split, summarize_curve
from evals.learning.report import write_curve
from keel.models import Completed, TextDelta, Usage
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider


def toy(tmp_path: Path, numbers=("1", "2", "3"), dbms=("sqlite",)):
    repo = tmp_path / "repo"
    for number in numbers:
        query_dir = repo / "query_stockindex" / f"query{number}"
        query_dir.mkdir(parents=True)
        (query_dir / "query.json").write_text(json.dumps(f"Question {number}?"))
        (query_dir / "validate.py").write_text("def validate(answer):\n    return answer.strip() == '42', 'exact'\n")
        (query_dir / "ground_truth.csv").write_text("42\n")
    (repo / "query_stockindex" / "db_description.txt").write_text("one table with one row")
    data = repo / "query_stockindex" / "query_dataset"
    data.mkdir(parents=True)
    (data / "one.db").write_bytes(b"x")
    queries = {n: Query(n, {}) for n in numbers}
    meta = Dataset(name="stockindex", dbms=dbms,
                   db_clients={"one": {"db_type": dbms[0], "db_path": "query_dataset/one.db"}},
                   data_files={"query_dataset/one.db": {"sha256": "d" * 64, "bytes": 1, "source": "repo"}},
                   queries=queries)
    return repo, [Case(dataset=meta, query=q) for q in queries.values()]


def test_the_last_third_is_held_out_in_numeric_order(tmp_path):
    _, cases = toy(tmp_path, numbers=("10", "2", "1"))
    learn, held = split(cases)
    assert [c.query.number for c in learn] == ["1", "2"] and [c.query.number for c in held] == ["10"]
    _, two = toy(tmp_path / "b", numbers=("1", "2"))
    assert [len(part) for part in split(two)] == [1, 1]
    _, one = toy(tmp_path / "c", numbers=("1",))
    with pytest.raises(CurveError):
        split(one)


def test_metrics_count_iterations_tool_calls_and_tool_errors():
    messages = [{"role": "user", "content": "q"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
                {"role": "tool", "content": json.dumps({"error": "no such file"})},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "2"}]},
                {"role": "tool", "content": json.dumps({"success": False, "error": "x"})},
                {"role": "assistant", "content": "ANSWER: 42"}]
    assert metrics(messages) == {"iterations": 3, "tool_calls": 2, "tool_errors": 2}


async def test_a_scripted_curve_runs_both_arms_and_summarises(tmp_path, pg_dsn):
    repo, cases = toy(tmp_path)
    registry = ProviderRegistry()
    registry.register(FakeProvider(script=[[TextDelta("ANSWER: 42"), Completed(Usage(10, 2, 0, 0))]] * 200))
    paths = inputs.Paths(tmp_path / "eval").ensure()
    records = await execute_curve(cases, repo=repo, paths=paths, ledger=Ledger.grant(paths.budget, 1_000_000),
                                  mount_root=tmp_path / "mounts", admin_dsn=pg_dsn, provider="fake", model="m",
                                  repeats=1, sandbox="none", providers=registry, timeout_s=60)
    runs = [r for r in records if r.get("kind") == "task"]
    assert len(runs) == 2 * 3  # two arms, two learning tasks and one held-out task
    assert {(r["arm"], r["phase"]) for r in runs} == {("learning", "learn"), ("learning", "heldout"),
                                                       ("baseline", "learn"), ("baseline", "heldout")}
    assert all(r["passed"] for r in runs)
    sequences = [r for r in records if r.get("kind") == "sequence"]
    assert {s["arm"] for s in sequences} == {"learning", "baseline"}
    learning = next(s for s in sequences if s["arm"] == "learning")
    assert learning["feedback"] == 2 and learning["observations"] >= 2  # feedback became observations
    baseline = next(s for s in sequences if s["arm"] == "baseline")
    assert baseline["observations"] == 0
    # forced reviews ran in both arms; a dream only in the learning arm
    assert learning["reviews"] >= 1 and baseline["reviews"] >= 1
    assert learning["dreams"] >= 1 and baseline["dreams"] == 0
    assert learning["feedback_comments"] == ["Correct."] * 2 == baseline["feedback_comments"]
    assert learning["overhead_tokens"] > 0 and baseline["overhead_tokens"] > 0
    assert learning["retire"]["status"] == "retired" and baseline["retire"]["status"] == "retired"
    assert all(s["reviews_not_done"] == 0 == s["dreams_not_done"] for s in sequences)
    assert (paths.root / "curve" / "records.jsonl").read_text().count("\n") == len(records)
    summary = summarize_curve(records)
    assert summary["complete"] and summary["errors"] == 0
    assert summary["learning"]["heldout_pass"] == summary["baseline"]["heldout_pass"] == 1.0


async def test_a_service_dataset_is_refused(tmp_path, pg_dsn):
    repo, cases = toy(tmp_path, dbms=("postgres",))
    paths = inputs.Paths(tmp_path / "eval").ensure()
    with pytest.raises(CurveError):
        await execute_curve(cases, repo=repo, paths=paths, ledger=Ledger.grant(paths.budget, 1000),
                            mount_root=tmp_path / "m", admin_dsn=pg_dsn, provider="fake", model="m", repeats=1,
                            sandbox="none", providers=ProviderRegistry())


def test_the_summary_is_a_mean_over_repeats_of_dataset_means():
    def run(arm, dataset, repeat, passed, iterations=4, tokens=100):
        return {"kind": "task", "arm": arm, "dataset": dataset, "repeat": repeat, "phase": "heldout",
                "case": f"{dataset}/q", "status": "executed", "passed": passed, "iterations": iterations,
                "tool_errors": 0, "tokens": tokens}

    def seq(arm, dataset, repeat, learned=0):
        return {"kind": "sequence", "arm": arm, "dataset": dataset, "repeat": repeat, "learned_skills": learned,
                "expected": 1, "ran": 1}

    records = [run("learning", "a", 0, True), run("learning", "b", 0, False), run("learning", "a", 1, True),
               run("learning", "b", 1, True, iterations=2), run("baseline", "a", 0, False),
               run("baseline", "b", 0, False), run("baseline", "a", 1, True), run("baseline", "b", 1, False)]
    records += [seq(arm, d, r) for arm in ("learning", "baseline") for d in "ab" for r in (0, 1)]
    summary = summarize_curve(records)
    assert summary["learning"]["heldout_pass"] == pytest.approx(0.75)  # (0.5 + 1.0) / 2
    assert summary["baseline"]["heldout_pass"] == pytest.approx(0.25)  # (0.0 + 0.5) / 2
    assert summary["learning"]["iterations"] == pytest.approx(3.5)
    assert summary["complete"]


async def _run(tmp_path, pg_dsn, cases, repo, repeats=1, tokens=1_000_000):
    registry = ProviderRegistry()
    registry.register(FakeProvider(script=[[TextDelta("ANSWER: 42"), Completed(Usage(10, 2, 0, 0))]] * 400))
    paths = inputs.Paths(tmp_path / "eval").ensure()
    return await execute_curve(cases, repo=repo, paths=paths, ledger=Ledger.grant(paths.budget, tokens),
                               mount_root=tmp_path / "mounts", admin_dsn=pg_dsn, provider="fake", model="m",
                               repeats=repeats, sandbox="none", providers=registry, timeout_s=60)


async def test_one_failing_task_keeps_the_rest_and_the_profile_is_retired(tmp_path, pg_dsn, monkeypatch):
    repo, cases = toy(tmp_path)
    real = curve.scoring.score

    def flaky(case, repo, answer, **kw):
        if case.query.number == "2":
            raise RuntimeError("validator exploded")
        return real(case, repo, answer, **kw)

    monkeypatch.setattr(curve.scoring, "score", flaky)
    records = await _run(tmp_path, pg_dsn, cases, repo)
    failed = [r for r in records if r.get("status") == "setup_failed"]
    assert len(failed) == 2 and all("validator exploded" in r["error"] for r in failed)
    assert len([r for r in records if r.get("kind") == "task" and r["status"] == "executed"]) == 4
    sequences = [r for r in records if r.get("kind") == "sequence"]
    assert len(sequences) == 2 and all(s["retire"]["status"] == "retired" for s in sequences)
    assert summarize_curve(records)["errors"] == 2


async def test_an_isolation_failure_blocks_every_case(tmp_path, pg_dsn, monkeypatch):
    repo, cases = toy(tmp_path)

    def broken(*args, **kwargs):
        raise RuntimeError("no mounts")

    monkeypatch.setattr(curve, "isolation", broken)
    records = await _run(tmp_path, pg_dsn, cases, repo)
    tasks = [r for r in records if r.get("kind") == "task"]
    assert len(tasks) == 6 and {r["status"] for r in tasks} == {"blocked"}
    assert len([r for r in records if r.get("kind") == "sequence"]) == 2


async def test_the_arm_order_alternates_per_repeat(tmp_path, pg_dsn):
    repo, cases = toy(tmp_path)
    records = await _run(tmp_path, pg_dsn, cases, repo, repeats=2)
    order = [(s["repeat"], s["arm"]) for s in records if s.get("kind") == "sequence"]
    assert order == [(0, "learning"), (0, "baseline"), (1, "baseline"), (1, "learning")]


def test_error_records_are_excluded_and_make_the_summary_incomplete():
    def run(arm, passed, status="executed"):
        return {"kind": "task", "arm": arm, "dataset": "a", "repeat": 0, "phase": "heldout", "case": "a/q",
                "status": status, "passed": passed, "iterations": 4, "tool_errors": 0, "tokens": 10}

    seqs = [{"kind": "sequence", "arm": arm, "dataset": "a", "repeat": 0, "expected": 1, "ran": 1}
            for arm in ("learning", "baseline")]
    summary = summarize_curve([run("learning", True), run("baseline", False, "budget_exhausted"), *seqs])
    assert not summary["complete"] and summary["baseline"]["heldout_errors"] == 1
    assert summary["baseline"]["iterations"] is None and summary["learning"]["heldout_pass"] == 1.0


async def test_a_budget_stop_is_recorded_as_budget_exhausted(tmp_path, pg_dsn):
    repo, cases = toy(tmp_path)
    records = await _run(tmp_path, pg_dsn, cases, repo, tokens=9_000)
    statuses = {r["status"] for r in records if r.get("kind") == "task"}
    assert "budget_exhausted" in statuses and "failed" not in statuses
    assert len([r for r in records if r.get("kind") == "sequence"]) == 2


class Stalled:
    """An API client whose run never finishes on its own and stops at once when asked."""

    def __init__(self):
        self.stopped = False

    async def post(self, url, json=None):
        if url.endswith("/stop"):
            self.stopped = True
        return Reply(202, {"run_id": "r1"})

    async def get(self, url, params=None):
        if url.endswith("/messages"):
            return Reply(200, {"data": [{"role": "assistant", "content": "partial"}]})
        return Reply(200, {"run_id": "r1", "status": "stopped" if self.stopped else "running"})


class Reply:
    def __init__(self, status_code, body):
        self.status_code, self._body = status_code, body

    def json(self):
        return self._body


async def test_a_run_stopped_at_the_deadline_is_a_timeout_even_when_the_stop_is_quick():
    client = Stalled()
    outcome = await _task(client, "p", "s", "q", provider="fake", model="m", reasoning_effort="low", timeout_s=0.0)
    assert client.stopped and outcome["status"] == "timeout"


def _held(arm, status, passed, iterations, repeat=0):
    return {"kind": "task", "arm": arm, "dataset": "a", "repeat": repeat, "phase": "heldout", "case": "a/q",
            "status": status, "passed": passed, "iterations": iterations, "tool_errors": iterations,
            "tokens": iterations * 10}


def test_failed_and_timed_out_runs_are_wrong_answers_left_out_of_the_effort_means(tmp_path):
    seqs = [{"kind": "sequence", "arm": arm, "dataset": "a", "repeat": 0, "expected": 3, "ran": 3,
             "learned_skills": learned, "reviews_not_done": 1, "dreams_not_done": 2 if arm == "learning" else 0}
            for arm, learned in (("learning", 2), ("baseline", 0))]
    records = [_held("learning", "executed", True, 4), _held("learning", "failed", True, 60),
               _held("learning", "timeout", False, 90), _held("baseline", "executed", True, 6), *seqs]
    summary = summarize_curve(records)
    learning = summary["learning"]
    assert (learning["heldout_failed"], learning["heldout_timeout"], learning["heldout_errors"]) == (1, 1, 0)
    assert learning["heldout_pass"] == pytest.approx(1 / 3)  # the failed run's passing text is still wrong
    assert (learning["iterations"], learning["tool_errors"], learning["tokens"]) == (4, 4, 40)
    assert learning["learned_by_sequence"] == [{"dataset": "a", "repeat": 0, "learned_skills": 2}]
    assert (learning["reviews_not_done"], learning["dreams_not_done"]) == (1, 2)
    assert summary["baseline"]["heldout_failed"] == summary["baseline"]["heldout_timeout"] == 0
    write_curve(tmp_path, summary, records, {"protocol": "learning-eval-v1", "provider": "openai", "model": "m",
                                             "keel_version": "x", "started": "now", "datasets": ["a"], "repeats": 1})
    report = (tmp_path / "report.md").read_text()
    assert "| Failed | Timeout |" in report and "| learning | 33% ± 0% (1 repeats) | 0 | 1 | 1 | 4.0 |" in report
    assert "| a | 0 | learning | 2 | 1 | 2 |" in report
