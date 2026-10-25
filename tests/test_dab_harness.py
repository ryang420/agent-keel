"""Unit checks for the DAB scoring harness (protocol dab-score-v1).

No inference, no Docker and no PostgreSQL: these cover the frozen membership, prompt leakage, answer extraction,
budget enforcement, isolation staging, scoring and report states. The real execution is exercised separately.
"""

import hashlib
import json
from pathlib import Path

import pytest

from evals.dab import inputs, prompt, report, runner, scoring
from evals.dab.budget import BudgetExhausted, BudgetError, Ledger
from evals.dab.policy import IsolationError, isolation, profile_id_for
from evals.dab.suite import EXPECTED_QUERIES, SMOKE_DATASETS, Dataset, Query, Suite, Case

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "evals/dab/suite.json"
PINNED_REPO = ROOT / ".local/evals/dab/repo"


@pytest.fixture(scope="module")
def suite() -> Suite:
    return Suite.load(SUITE)


def case(dataset: str = "stockindex", number: str = "1") -> Case:
    query = Query(number=number, files={"query.json": "a" * 64, "validate.py": "b" * 64,
                                        "ground_truth.csv": "c" * 64})
    meta = Dataset(name=dataset, dbms=("duckdb", "sqlite"),
                   db_clients={"one": {"db_type": "sqlite", "db_path": "query_dataset/one.db"}},
                   data_files={"query_dataset/one.db": {"sha256": "d" * 64, "bytes": 1, "source": "repo"}},
                   queries={number: query})
    return Case(dataset=meta, query=query)


def test_the_manifest_is_the_frozen_membership(suite: Suite):
    assert set(suite.datasets) == set(EXPECTED_QUERIES)
    assert sum(len(dataset.queries) for dataset in suite.datasets.values()) == 54
    assert {name: len(dataset.queries) for name, dataset in suite.datasets.items()} == EXPECTED_QUERIES
    for dataset in suite.datasets.values():
        for query in dataset.queries.values():
            assert set(query.files) == {"query.json", "validate.py", "ground_truth.csv"}
            assert all(len(digest) == 64 for digest in query.files.values())


def test_the_smoke_is_two_file_backed_datasets(suite: Suite):
    smoke = suite.smoke()
    assert {c.dataset.name for c in smoke} == set(SMOKE_DATASETS)
    assert len(smoke) == 6
    assert all(not c.dataset.service_dbms for c in smoke)
    assert [c.case_id for c in smoke] == ["stockindex/query1", "stockindex/query2", "stockindex/query3",
                                          "music_brainz_20k/query1", "music_brainz_20k/query2",
                                          "music_brainz_20k/query3"]


def test_a_wrong_protocol_or_membership_is_rejected(suite: Suite):
    data = json.loads(SUITE.read_text())
    data["protocol"] = "other"
    with pytest.raises(ValueError):
        Suite(data)
    data = json.loads(SUITE.read_text())
    data["datasets"]["imdb"] = data["datasets"]["stockindex"]
    with pytest.raises(ValueError):
        Suite(data)


def test_the_tracked_manifest_regenerates_from_the_pinned_checkout(suite: Suite):
    if not PINNED_REPO.is_dir():
        pytest.skip("pinned DAB checkout is not present")
    from evals.dab.suitegen import REVISION, build_suite

    regenerated = build_suite(PINNED_REPO)
    assert regenerated["revision"] == REVISION
    assert json.dumps(regenerated, sort_keys=True) == json.dumps(suite.data, sort_keys=True)


def test_the_prompt_carries_the_question_and_no_gold(suite: Suite):
    if not PINNED_REPO.is_dir():
        pytest.skip("pinned DAB checkout is not present")
    selected = suite.smoke()[0]
    staged = prompt.build(selected, PINNED_REPO, [("one", "SQLite database file at /data/query_dataset/one.db")])
    assert staged.text.count(prompt.ANSWER_MARKER) == 1
    assert prompt.ANSWER_MARKER in staged.text
    assert staged.clean(), staged.leaks
    assert "ground_truth" not in staged.text
    assert "399001.SZ" not in staged.text  # this query's gold answer must never reach the prompt
    assert len(staged.sha256) == 64


def test_the_prompt_inspector_catches_evaluator_material():
    if not PINNED_REPO.is_dir():
        pytest.skip("pinned DAB checkout is not present")
    selected = Suite.load(SUITE).smoke()[0]
    leaked = prompt.inspect("answer is 399001.SZ", selected, PINNED_REPO)
    assert any("ground_truth value" in item for item in leaked)
    assert prompt.inspect("see validate.py", selected, PINNED_REPO)


@pytest.mark.parametrize("text,expected,source", [
    ("work\nANSWER: 42", "42", "marker"),
    ("ANSWER:  three  ", "three", "marker"),
    ("no marker here", "no marker here", "final_message"),
    ("ANSWER:", None, "marker"),
    ("", None, None),
])
def test_answer_extraction(text, expected, source):
    extracted = runner.extract_answer([{"role": "assistant", "content": text}])
    assert (extracted.text, extracted.source) == (expected, source)


def test_only_the_last_assistant_message_is_eligible():
    messages = [
        {"role": "assistant", "content": "ANSWER: early", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": "result", "tool_call_id": "1"},
        {"role": "assistant", "content": "reasoning only"},
    ]
    extracted = runner.extract_answer(messages)
    assert (extracted.text, extracted.source) == ("reasoning only", "final_message")


def test_tool_calls_are_counted_from_the_transcript():
    messages = [{"role": "assistant", "tool_calls": [{"id": "1"}, {"id": "2"}]}, {"role": "assistant"}]
    assert runner.tool_calls(messages) == 2


def _validator_repo(root: Path, body: str, gold: str = "42") -> Path:
    query_dir = root / "query_stockindex" / "query1"
    query_dir.mkdir(parents=True)
    (query_dir / "validate.py").write_text(body)
    (query_dir / "ground_truth.csv").write_text(gold + "\n")
    return root


def test_scoring_uses_the_pinned_validator(tmp_path):
    repo = _validator_repo(tmp_path, "def validate(answer):\n    return answer.strip() == '42', 'exact'\n")
    passed = scoring.score(case(), repo, "42")
    assert passed["official_validator_pass"] and passed["reason"] == "exact"
    failed = scoring.score(case(), repo, "41")
    assert not failed["official_validator_pass"]


def test_a_missing_answer_is_a_failure_not_a_skip(tmp_path):
    repo = _validator_repo(tmp_path, "def validate(answer):\n    return True, 'ok'\n")
    record = scoring.score(case(), repo, None)
    assert record["official_validator_pass"] is False and record["reason"] == "answer_missing"
    assert scoring.score(case(), repo, "")["reason"] == "answer_missing"


def test_a_crashing_validator_is_recorded_separately(tmp_path):
    repo = _validator_repo(tmp_path, "def validate(answer):\n    raise RuntimeError('boom')\n")
    record = scoring.score(case(), repo, "42")
    assert record["official_validator_pass"] is False
    assert record["validator_error"] and record["reason"] == "validator_error"


def test_pass_at_1_is_a_dataset_mean(tmp_path):
    rows = [
        {"case": "a/query1", "status": "executed", "official_validator_pass": True},
        {"case": "a/query2", "status": "executed", "official_validator_pass": False},
        {"case": "b/query1", "status": "executed", "official_validator_pass": True},
        {"case": "b/query2", "status": "executed", "official_validator_pass": True},
        {"case": "b/query3", "status": "blocked"},
    ]
    summary = scoring.aggregate(rows)
    assert summary["datasets"]["a"]["pass_rate"] == 0.5
    assert summary["datasets"]["b"]["pass_rate"] == 1.0
    assert summary["pass_at_1"] == 0.75
    assert summary["executed_queries"] == 4


def test_the_budget_refuses_before_overspending(tmp_path):
    path = tmp_path / "budget.json"
    with pytest.raises(BudgetError):
        Ledger.grant(path, 0)
    ledger = Ledger.grant(path, 10_000)
    reserved = ledger.reserve("a/query1", 500)
    assert reserved == 500
    ledger.settle("a/query1", reserved, actual=250)
    assert ledger.spent_tokens == 250
    with pytest.raises(BudgetExhausted):
        ledger.reserve("a/query2", 100_000)
    assert Ledger.open(path).spent_tokens == 250


def test_unknown_usage_charges_the_reservation_and_stays_none(tmp_path):
    ledger = Ledger.grant(tmp_path / "budget.json", 20_000)
    reserved = ledger.reserve("a/query1", 700)
    ledger.settle("a/query1", reserved, actual=None)
    saved = Ledger.open(tmp_path / "budget.json")
    assert saved.spent_tokens == reserved
    assert saved.entries[-1]["actual"] is None


def test_isolation_stages_a_read_only_mount_under_the_profile_tree(tmp_path):
    repo = tmp_path / "repo"
    (repo / "query_stockindex" / "query_dataset").mkdir(parents=True)
    source = repo / "query_stockindex" / "query_dataset" / "one.db"
    source.write_bytes(b"data")
    staged = isolation(case(), repo, tmp_path / "mounts", "p1")
    try:
        assert staged.policy["network"] == "none"
        mount = staged.policy["mounts"][0]
        assert mount["read_only"] is True and mount["target"] == "/data"
        assert Path(mount["source"]).is_relative_to(tmp_path / "mounts" / hashlib.sha256(b"p1").hexdigest())
        assert (Path(mount["source"]) / "query_dataset" / "one.db").stat().st_ino == source.stat().st_ino
        assert staged.stores == [("one", "SQLite database file at /data/query_dataset/one.db (read-only)")]
    finally:
        staged.cleanup()
    assert not Path(mount["source"]).exists()


def test_a_file_backed_case_refuses_a_network(tmp_path):
    repo = tmp_path / "repo"
    (repo / "query_stockindex" / "query_dataset").mkdir(parents=True)
    (repo / "query_stockindex" / "query_dataset" / "one.db").write_bytes(b"data")
    with pytest.raises(IsolationError):
        isolation(case(), repo, tmp_path / "mounts", "p1", service_network="bench")


def test_a_service_case_requires_its_service():
    service = Case(dataset=Dataset(
        name="bookreview", dbms=("postgres", "sqlite"),
        db_clients={"books": {"db_type": "postgres", "db_name": "b", "sql_file": "query_dataset/b.sql"}},
        data_files={"query_dataset/b.sql": {"sha256": "a" * 64, "bytes": 1, "source": "mirror"}},
        queries={"1": Query("1", {})}), query=Query("1", {}))
    with pytest.raises(IsolationError):
        isolation(service, Path("/nonexistent"), Path("/tmp/m"), "p1")


def test_profile_ids_are_deterministic():
    assert profile_id_for(case(), 0) == profile_id_for(case(), 0)
    assert profile_id_for(case(), 0) != profile_id_for(case(), 1)


def test_the_report_keeps_blocked_and_budget_states_distinct(suite: Suite):
    cases = suite.smoke()
    prepared = {"cases": {cases[0].case_id: {"status": "ready"},
                          cases[1].case_id: {"status": "blocked", "reason": "missing"}}}
    records = [{"case": cases[0].case_id, "status": "executed", "answer": "x", "usage": None, "tool_calls": 3,
                "duration_s": 1.5, "error": None, "prompt_sha256": "a" * 64, "artifacts": "/tmp/x", "model": "m"}]
    scores = [{"case": cases[0].case_id, "official_validator_pass": True, "reason": "ok", "validator_error": None}]
    payload = report.build(suite=suite, cases=cases, prepared=prepared, records=records, scores=scores,
                           keel={"revision": "abc", "dirty": False}, image={"id": "sha256:x"},
                           config={"model": "gpt-6-luna", "reasoning_effort": "low"})
    assert payload["counts"]["scored"] == 1 and payload["counts"]["blocked"] == 1
    assert payload["counts"]["budget_exhausted"] == 0
    assert payload["smoke"] is True
    assert payload["score"]["pass_at_1"] == 1.0
    text = report.markdown(payload)
    assert "not a leaderboard score" in text


def _toy_repo(tmp_path: Path) -> tuple[Path, Case]:
    """A tiny pinned-shaped case: one SQLite store, one validator, no DAB data needed."""
    repo = tmp_path / "repo"
    query_dir = repo / "query_stockindex" / "query1"
    query_dir.mkdir(parents=True)
    (query_dir / "query.json").write_text(json.dumps("What is the answer?"))
    (query_dir / "validate.py").write_text("def validate(answer):\n    return answer.strip() == '42', 'exact'\n")
    (query_dir / "ground_truth.csv").write_text("42\n")
    (repo / "query_stockindex" / "db_description.txt").write_text("one table with one row")
    data = repo / "query_stockindex" / "query_dataset"
    data.mkdir(parents=True)
    (data / "one.db").write_bytes(b"x")
    meta = Dataset(name="stockindex", dbms=("sqlite",),
                   db_clients={"one": {"db_type": "sqlite", "db_path": "query_dataset/one.db"}},
                   data_files={"query_dataset/one.db": {"sha256": "d" * 64, "bytes": 1, "source": "repo"}},
                   queries={"1": Query("1", {})})
    return repo, Case(dataset=meta, query=Query("1", {}))


async def test_a_fake_run_writes_artifacts_and_keeps_usage(tmp_path, pg_dsn):
    """The driver's lifecycle, answer extraction, artifacts and accounting, without a model or a sandbox."""
    from keel.models import Completed, TextDelta, Usage
    from keel.providers.base import ProviderRegistry
    from keel.providers.fake import FakeProvider

    repo, toy = _toy_repo(tmp_path)
    providers = ProviderRegistry()
    providers.register(FakeProvider(script=[[TextDelta("thinking\nANSWER: 42"), Completed(Usage(10, 2, 0, 0))]]))
    paths = inputs.Paths(tmp_path / "eval").ensure()
    ledger = Ledger.grant(paths.budget, 100_000)
    records = await runner.execute([toy], paths=paths, repo=repo, ledger=ledger,
                                   mount_root=tmp_path / "mounts", admin_dsn=pg_dsn, providers=providers,
                                   provider="fake", model="m", sandbox="none", timeout_s=60)
    assert records[0]["status"] == "executed"
    assert records[0]["answer"] == "42"
    assert records[0]["answer_source"] == "marker"
    assert records[0]["usage"]["input_tokens"] == 10
    artifacts = Path(records[0]["artifacts"])
    assert (artifacts / "answer.txt").read_text() == "42"
    assert (artifacts / "meta.json").is_file()
    assert scoring.score(toy, repo, records[0]["answer"])["official_validator_pass"]


async def test_a_real_sandbox_run_leaves_no_container_behind(tmp_path, pg_dsn):
    """Opt-in Docker preflight: the sandbox is created for a profile policy and reaped after the run."""
    import os
    import subprocess

    from keel.models import Completed, TextDelta, Usage
    from keel.providers.base import ProviderRegistry
    from keel.providers.fake import FakeProvider

    if os.environ.get("KEEL_TEST_DOCKER") != "1":
        pytest.skip("set KEEL_TEST_DOCKER=1 to exercise the Docker sandbox")
    image = "keel-dab-eval:1"
    if subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode != 0:
        pytest.skip(f"{image} is not built")
    repo, toy = _toy_repo(tmp_path)
    providers = ProviderRegistry()
    providers.register(FakeProvider(script=[[TextDelta("ANSWER: 42"), Completed(Usage(10, 2, 0, 0))]]))
    paths = inputs.Paths(tmp_path / "eval").ensure()
    ledger = Ledger.grant(paths.budget, 100_000)
    records = await runner.execute([toy], paths=paths, repo=repo, ledger=ledger, image=image,
                                   mount_root=tmp_path / "mounts", admin_dsn=pg_dsn, providers=providers,
                                   provider="fake", model="m", sandbox="docker", timeout_s=120)
    assert records[0]["status"] == "executed"
    assert not runner.managed_containers()


def _request():
    from keel.models import ModelRequest

    return ModelRequest(provider="fake", model="m", system="system", messages=())


async def test_each_provider_call_is_charged_and_forwarded_unchanged(tmp_path):
    from keel.models import Completed, TextDelta, Usage
    from keel.providers.fake import FakeProvider

    from evals.dab.accounting import BudgetProvider, Charging

    ledger = Ledger.grant(tmp_path / "budget.json", 10_000)
    charging = Charging(ledger)
    charging.switch("a/query1")
    inner = FakeProvider(script=[[TextDelta("hi"), Completed(Usage(100, 20, 0, 0))]])
    provider = BudgetProvider(inner, charging, max_output=1_000)
    events = [event async for event in provider.stream(_request())]
    assert isinstance(events[0], TextDelta) and isinstance(events[-1], Completed)
    assert ledger.spent_tokens == 120
    assert ledger.entries[-1]["actual"] == 120


async def test_an_unaffordable_call_never_reaches_the_provider(tmp_path):
    from keel.providers.fake import FakeProvider

    from evals.dab.accounting import BudgetProvider, Charging

    ledger = Ledger.grant(tmp_path / "budget.json", 10)
    charging = Charging(ledger)
    charging.switch("a/query1")
    inner = FakeProvider()
    provider = BudgetProvider(inner, charging, max_output=1_000)
    with pytest.raises(BudgetExhausted):
        [event async for event in provider.stream(_request())]
    assert inner.requests == []


async def test_an_unknown_usage_charges_the_reservation(tmp_path):
    from keel.models import Completed, TextDelta, Usage
    from keel.providers.fake import FakeProvider

    from evals.dab.accounting import BudgetProvider, Charging, request_tokens

    ledger = Ledger.grant(tmp_path / "budget.json", 10_000)
    charging = Charging(ledger)
    charging.switch("a/query1")
    inner = FakeProvider(script=[[TextDelta("hi"), Completed(Usage(None, None, 0, 0))]])
    provider = BudgetProvider(inner, charging, max_output=1_000)
    [event async for event in provider.stream(_request())]
    assert ledger.spent_tokens == request_tokens(_request()) + 1_000
    assert ledger.entries[-1]["actual"] is None
