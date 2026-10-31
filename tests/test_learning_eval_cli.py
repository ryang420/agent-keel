"""The learning evaluation's command line: offline validation, a scripted conversations run and the combined gate."""

import json
import os

import pytest

from evals.learning import cli
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


def test_gate_combines_two_result_files(tmp_path, capsys):
    curve = tmp_path / "curve.json"
    curve.write_text(json.dumps({"summary": {"learning": {"heldout_pass": 0.6, "iterations": 8.0},
                                             "baseline": {"heldout_pass": 0.5, "iterations": 10.0},
                                             "complete": True, "errors": 0}}))
    conversations = tmp_path / "conv.json"
    learning = {"corrections": 1, "negatives_learned": 0, "exposure": 0, "attributable": True, "reversible": True,
                "complete": True, "errors": 0}
    conversations.write_text(json.dumps({"summary": {"learning": learning,
                                                     "baseline": {**learning, "corrections": 4}}}))
    assert cli.main(["gate", "--curve", str(curve), "--conversations", str(conversations)]) == 0
    assert "PASSED" in capsys.readouterr().out
    assert json.loads((tmp_path / "gate.json").read_text())["passed"]


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
    code = cli.main(["run", "--provider", "openai", "--suite", str(suite), "--root", str(tmp_path / "out")])
    (results,) = (tmp_path / "out" / "runs").glob("*/results.json")
    data = json.loads(results.read_text())
    assert set(data["summary"]) == {"learning", "baseline"}
    assert data["summary"]["learning"]["overall"]["passed"] == 1
    assert data["meta"]["model"] == "scripted" and (results.parent / "report.md").is_file()
    assert code == 0, capsys.readouterr()


def test_curve_refuses_a_missing_budget_before_anything_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("KEEL_LIVE_OPENAI_MODEL", "scripted")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["curve", "--root", str(tmp_path)])
    assert exit_info.value.code == 2
    assert not list(tmp_path.iterdir())


def test_curve_refuses_a_service_dataset_and_a_missing_model(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KEEL_LIVE_OPENAI_MODEL", "scripted")
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
    code = cli.main(["curve", "--max-tokens", "1000", "--root", str(tmp_path / "out"),
                     "--dab-root", str(tmp_path / "none")])
    assert code == 1 and "prepare" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def _gate_files(tmp_path, conversations, meta=None, curve=None):
    curve_file, conv_file = tmp_path / "curve.json", tmp_path / "conv.json"
    curve_file.write_text(json.dumps({"summary": curve or {"complete": True, "errors": 0}}))
    conv_file.write_text(json.dumps({"meta": meta or {}, "summary": conversations}))
    return ["gate", "--curve", str(curve_file), "--conversations", str(conv_file)]


def test_gate_refuses_a_subset_a_single_arm_and_an_incomplete_curve(tmp_path, capsys):
    arm = {"corrections": 1, "negatives_learned": 0, "exposure": 0, "attributable": True, "reversible": True,
           "complete": True, "errors": 0}
    both = {"learning": arm, "baseline": arm}
    assert cli.main(_gate_files(tmp_path, both, meta={"gate_applicable": False})) == 1
    assert "subset" in capsys.readouterr().err
    assert cli.main(_gate_files(tmp_path, {"learning": arm})) == 1
    assert "both" in capsys.readouterr().err
    assert cli.main(_gate_files(tmp_path, both, curve={"complete": False, "errors": 0})) == 1
    assert "not a complete" in capsys.readouterr().err
    assert not (tmp_path / "gate.json").exists()


def test_unknown_scenario_ids_are_refused_before_anything_is_written(tmp_path, monkeypatch):
    monkeypatch.setenv("KEEL_LIVE_OPENAI_MODEL", "scripted")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["run", "--provider", "openai", "--scenarios", "nope-1", "--root", str(tmp_path / "out")])
    assert "nope-1" in str(exit_info.value)
    monkeypatch.delenv("KEEL_TEST_POSTGRES_DSN", raising=False)
    monkeypatch.delenv("KEEL_LEARNING_EVAL_ADMIN_DSN", raising=False)
    with pytest.raises(SystemExit):
        cli.main(["run", "--provider", "openai", "--root", str(tmp_path / "out")])
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
                     "--scenarios", "req-one", "--root", str(tmp_path / "out")])
    (results,) = (tmp_path / "out" / "runs").glob("*/results.json")
    assert code == 0
    assert json.loads(results.read_text())["meta"]["gate_applicable"] is False
    assert "not applicable" in (results.parent / "report.md").read_text()
