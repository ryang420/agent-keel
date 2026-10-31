"""The learning evaluation's command line: offline validation, a scripted conversations run and the combined gate."""

import argparse
import json
import os
from pathlib import Path

import pytest

from evals.learning import cli
from evals.learning.suite import load_suite
from keel.models import Completed, TextDelta, ToolCall, ToolCallReady, Usage
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider


def test_validate_accepts_the_tracked_suite(capsys):
    assert cli.main(["validate"]) == 0
    assert "32 scenarios OK (learning-eval-v1)" in capsys.readouterr().out


def test_validate_refuses_a_broken_suite(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"protocol": "nope"}')
    assert cli.main(["validate", "--suite", str(bad)]) == 1


CURVE_META = {"protocol": "learning-eval-v1", "provider": "openai", "model": "scripted"}
CONVERSATIONS_META = {**CURVE_META, "gate_applicable": True, "not_applicable": []}
ARM = {"corrections": 1, "negatives_learned": 0, "exposure": 0, "attributable": True, "reversible": True,
       "complete": True, "errors": 0}
CURVE_SUMMARY = {"learning": {"heldout_pass": 0.6, "iterations": 8.0, "learned_skills": 2.0},
                 "baseline": {"heldout_pass": 0.5, "iterations": 10.0, "learned_skills": 0.0},
                 "complete": True, "errors": 0}


def test_gate_combines_two_result_files_and_records_their_inputs(tmp_path, capsys):
    argv = _gate_files(tmp_path, {"learning": ARM, "baseline": {**ARM, "corrections": 4}})
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "PASSED" in out and "correction delta (learning - baseline): -3" in out
    gate = json.loads((tmp_path / "gate.json").read_text())
    assert gate["passed"] and gate["correction_delta"] == -3 and gate["checks"]["curve_learned"]
    assert gate["inputs"] == {"curve": str(tmp_path / "curve.json"), "conversations": str(tmp_path / "conv.json")}
    assert gate["meta"] == {"curve": CURVE_META, "conversations": CONVERSATIONS_META}


SCENARIO = {"id": "req-one", "group": "positive", "kind": "request", "language": "en",
            "sessions": [["From now on, list assumptions first."]], "probe": "Write a SQL query.",
            "checks": {"contains_all": ["Assumptions:"], "skill_contains": ["Assumptions"]}}
SKILL = {"name": "sql-assumptions", "description": "List assumptions before a SQL query",
         "body": "1. Assumptions: first.\n2. Then the query.\n3. Check: one sanity check."}


def _text(value):
    return [TextDelta(value), Completed(Usage(5, 2, 0, 0))]


@pytest.mark.integration
def test_a_scripted_run_writes_results_with_a_summary(tmp_path, monkeypatch, capsys):
    assert os.environ.get("KEEL_TEST_POSTGRES_DSN"), "KEEL_TEST_POSTGRES_DSN is required"
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({"protocol": "learning-eval-v1", "scenarios": [SCENARIO]}))
    script = [[ToolCallReady(ToolCall("c1", "skill_manage", json.dumps({"action": "create", **SKILL}))),
               Completed(Usage(5, 2, 0, 0))], _text("Saved."), _text("ok"),
              _text("Assumptions: one table.\nSELECT 1;"),  # learning arm
              _text("Noted."), _text("ok"), _text("SELECT 1;")]  # baseline arm

    class Providers(ProviderRegistry):
        async def aclose(self):
            return None

    def factory(settings):
        found = Providers()
        found.register(FakeProvider(name="openai", script=script))
        return found

    monkeypatch.setattr(cli, "default_providers", factory)
    monkeypatch.setenv("KEEL_LIVE_OPENAI_MODEL", "scripted")
    code = cli.main(["run", "--provider", "openai", "--suite", str(suite), "--root", str(tmp_path / "out"),
                     "--max-tokens", "1000000"])
    (results,) = (tmp_path / "out" / "runs").glob("*/results.json")
    data = json.loads(results.read_text())
    assert set(data["summary"]) == {"learning", "baseline"}
    assert data["summary"]["learning"]["overall"]["passed"] == 1
    assert data["meta"]["model"] == "scripted" and (results.parent / "report.md").is_file()
    meta = data["meta"]
    assert meta["full_run"] and not meta["suite_tracked"] and not meta["suite_coverage"]["complete"]
    assert not meta["gate_applicable"]  # a one-scenario suite of its own never claims the gate
    assert meta["max_tokens"] == 1_000_000 and 0 < meta["spent_tokens"] < 1_000_000
    budget = json.loads((results.parent / "budget.json").read_text())
    assert {entry["case"] for entry in budget["entries"]} == {"learning/req-one", "baseline/req-one"}
    assert code == 0, capsys.readouterr()


def test_curve_refuses_a_missing_budget_before_anything_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("KEEL_LIVE_OPENAI_MODEL", "scripted")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["curve", "--root", str(tmp_path)])
    assert exit_info.value.code == 2
    assert not list(tmp_path.iterdir())


ADMIN = "postgresql://keel:" + "unused@127.0.0.1:1/keel"  # never connected to


def test_curve_refuses_a_service_dataset_and_a_missing_model(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KEEL_LIVE_OPENAI_MODEL", "scripted")
    monkeypatch.setenv("KEEL_LEARNING_EVAL_ADMIN_DSN", ADMIN)  # the DSN check runs first; CI sets no DSN
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["curve", "--datasets", "crmarenapro", "--max-tokens", "1000", "--root", str(tmp_path),
                  "--dab-root", str(tmp_path / "none")])
    assert "file-backed" in str(exit_info.value)
    assert not list(tmp_path.iterdir())
    monkeypatch.delenv("KEEL_LIVE_OPENAI_MODEL")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["curve", "--max-tokens", "1000", "--root", str(tmp_path)])
    assert "no model is guessed" in str(exit_info.value)


def test_curve_refuses_an_unprepared_checkout(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KEEL_LIVE_OPENAI_MODEL", "scripted")
    monkeypatch.setenv("KEEL_LEARNING_EVAL_ADMIN_DSN", ADMIN)  # the DSN check runs first; CI sets no DSN
    code = cli.main(["curve", "--max-tokens", "1000", "--root", str(tmp_path / "out"),
                     "--dab-root", str(tmp_path / "none")])
    assert code == 1 and "prepare" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


MISSING = object()


def _gate_files(tmp_path, conversations, meta=CONVERSATIONS_META, curve=CURVE_SUMMARY, curve_meta=CURVE_META):
    curve_file, conv_file = tmp_path / "curve.json", tmp_path / "conv.json"
    curve_file.write_text(json.dumps({"summary": curve, **({} if curve_meta is MISSING else {"meta": curve_meta})}))
    conv_file.write_text(json.dumps({"summary": conversations, **({} if meta is MISSING else {"meta": meta})}))
    return ["gate", "--curve", str(curve_file), "--conversations", str(conv_file)]


@pytest.mark.parametrize(("change", "message"), [
    ({"meta": MISSING}, "conversations results have no meta"),
    ({"curve_meta": MISSING}, "curve results have no meta"),
    ({"curve_meta": {**CURVE_META, "protocol": "learning-eval-v0"}}, "protocol 'learning-eval-v0'"),
    ({"meta": {**CONVERSATIONS_META, "protocol": "other"}}, "protocol 'other'"),
    ({"meta": {**CONVERSATIONS_META, "provider": "bedrock"}}, "different providers"),
    ({"meta": {**CONVERSATIONS_META, "model": "another"}}, "different models"),
    ({"meta": {**CONVERSATIONS_META, "gate_applicable": False, "not_applicable": ["not the tracked suite"]}},
     "not gate-applicable: not the tracked suite"),
    ({"meta": {k: v for k, v in CONVERSATIONS_META.items() if k != "gate_applicable"}}, "not gate-applicable"),
])
def test_gate_refuses_results_it_cannot_vouch_for(tmp_path, capsys, change, message):
    assert cli.main(_gate_files(tmp_path, {"learning": ARM, "baseline": ARM}, **change)) == 1
    assert message in capsys.readouterr().err
    assert not (tmp_path / "gate.json").exists()


def test_gate_refuses_a_subset_a_single_arm_and_an_incomplete_curve(tmp_path, capsys):
    arm = ARM
    both = {"learning": arm, "baseline": arm}
    subset = {**CONVERSATIONS_META, "gate_applicable": False, "not_applicable": ["subset run"]}
    assert cli.main(_gate_files(tmp_path, both, meta=subset)) == 1
    assert "subset" in capsys.readouterr().err
    assert cli.main(_gate_files(tmp_path, {"learning": arm})) == 1
    assert "both" in capsys.readouterr().err
    assert cli.main(_gate_files(tmp_path, both, curve={"complete": False, "errors": 0})) == 1
    assert "not a complete" in capsys.readouterr().err
    assert not (tmp_path / "gate.json").exists()


def test_unknown_scenario_ids_are_refused_before_anything_is_written(tmp_path, monkeypatch):
    monkeypatch.setenv("KEEL_LIVE_OPENAI_MODEL", "scripted")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["run", "--provider", "openai", "--scenarios", "nope-1", "--root", str(tmp_path / "out"),
                  "--max-tokens", "1000"])
    assert "nope-1" in str(exit_info.value)
    monkeypatch.delenv("KEEL_TEST_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("KEEL_LEARNING_EVAL_ADMIN_DSN", raising=False)
    with pytest.raises(SystemExit):
        cli.main(["run", "--provider", "openai", "--root", str(tmp_path / "out"), "--max-tokens", "1000"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["run", "--provider", "openai", "--root", str(tmp_path / "out"), "--max-tokens", "0"])
    assert "positive" in str(exit_info.value)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["run", "--provider", "openai", "--root", str(tmp_path / "out")])
    assert exit_info.value.code == 2  # --max-tokens is required
    assert not (tmp_path / "out").exists()


@pytest.mark.integration
def test_a_subset_run_exits_zero_without_claiming_the_gate(tmp_path, monkeypatch):
    assert os.environ.get("KEEL_TEST_POSTGRES_DSN"), "KEEL_TEST_POSTGRES_DSN is required"
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({"protocol": "learning-eval-v1", "scenarios": [SCENARIO]}))

    class Providers(ProviderRegistry):
        async def aclose(self):
            return None

    def factory(settings):
        found = Providers()
        found.register(FakeProvider(name="openai", script=[_text("Noted."), _text("ok"), _text("SELECT 1;")]))
        return found

    monkeypatch.setattr(cli, "default_providers", factory)
    monkeypatch.setenv("KEEL_LIVE_OPENAI_MODEL", "scripted")
    code = cli.main(["run", "--provider", "openai", "--suite", str(suite), "--arm", "baseline",
                     "--scenarios", "req-one", "--root", str(tmp_path / "out"), "--max-tokens", "1000000"])
    (results,) = (tmp_path / "out" / "runs").glob("*/results.json")
    assert code == 0
    assert json.loads(results.read_text())["meta"]["gate_applicable"] is False
    assert "not applicable" in (results.parent / "report.md").read_text()


def test_only_a_full_run_of_the_tracked_complete_suite_is_gate_applicable(tmp_path):
    def applicability(suite, **kw):
        args = argparse.Namespace(suite=suite, scenarios=kw.get("scenarios"), arm=kw.get("arm", "both"))
        return cli._applicability(args, load_suite(suite))

    tracked = Path("evals/learning/suite.json")
    found = applicability(tracked)
    assert found["gate_applicable"] and found["suite_tracked"] and found["suite_coverage"]["complete"]
    copy = tmp_path / "suite.json"
    copy.write_bytes(tracked.read_bytes())
    assert applicability(copy)["gate_applicable"]  # the same bytes are the same suite
    assert applicability(tracked, arm="learning")["not_applicable"] == ["subset run"]
    assert applicability(tracked, scenarios="req-en-sql")["not_applicable"] == ["subset run"]
    copy.write_bytes(tracked.read_bytes() + b"\n")
    assert applicability(copy)["not_applicable"] == ["not the tracked suite"]


@pytest.mark.integration
def test_a_budget_stop_makes_every_case_an_error(tmp_path, monkeypatch):
    assert os.environ.get("KEEL_TEST_POSTGRES_DSN"), "KEEL_TEST_POSTGRES_DSN is required"
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({"protocol": "learning-eval-v1", "scenarios": [SCENARIO]}))

    class Providers(ProviderRegistry):
        async def aclose(self):
            return None

    model = FakeProvider(name="openai", script=[_text("never sent")])

    def factory(settings):
        found = Providers()
        found.register(model)
        return found

    monkeypatch.setattr(cli, "default_providers", factory)
    monkeypatch.setenv("KEEL_LIVE_OPENAI_MODEL", "scripted")
    code = cli.main(["run", "--provider", "openai", "--suite", str(suite), "--root", str(tmp_path / "out"),
                     "--max-tokens", "100"])
    (results,) = (tmp_path / "out" / "runs").glob("*/results.json")
    data = json.loads(results.read_text())
    assert code == 1 and model.requests == [] and data["meta"]["spent_tokens"] == 0
    assert {tuple(o["failures"]) for o in data["outcomes"]} == {("error: budget_exhausted",)}


def test_curve_refuses_a_missing_admin_dsn_before_the_run_directory_exists(tmp_path, monkeypatch):
    from evals.dab import inputs

    monkeypatch.setenv("KEEL_LIVE_OPENAI_MODEL", "scripted")
    monkeypatch.delenv("KEEL_TEST_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("KEEL_LEARNING_EVAL_ADMIN_DSN", raising=False)
    monkeypatch.setattr(inputs, "readiness", lambda *args: None)
    monkeypatch.setattr(inputs, "require_ready", lambda *args: None)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["curve", "--max-tokens", "1000", "--root", str(tmp_path / "out"), "--dab-root", str(tmp_path)])
    assert "CREATEDB" in str(exit_info.value)
    assert not (tmp_path / "out").exists()


def test_a_bedrock_curve_refuses_a_missing_region_before_anything_is_written(tmp_path, monkeypatch):
    monkeypatch.setenv("KEEL_LIVE_MODEL", "scripted")
    for name in ("AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["curve", "--provider", "bedrock", "--max-tokens", "1000", "--root", str(tmp_path)])
    assert "no region is assumed" in str(exit_info.value)
    assert not list(tmp_path.iterdir())
