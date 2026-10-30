"""The memory evaluation runner end to end against a scripted model: setup sessions, review drains, a fresh-session
probe, scoring, the decoy exposure check, and artifacts. No real model is called."""

import json

from evals.memory.runner import evaluate
from evals.memory.suite import Scenario
from keel.models import Completed, TextDelta, ToolCall, ToolCallReady, Usage
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider


class Scripted(FakeProvider):
    """Saves a stated preference with the memory tool, says nothing in reviews, and answers a probe by echoing the
    memory snapshot's sign-off when it has one."""

    async def stream(self, request):
        self.requests.append(request)
        last = request.messages[-1]
        if request.system.startswith("You maintain"):
            yield TextDelta("Nothing to save.")
        elif last.role == "user" and "From now on" in last.text:
            yield ToolCallReady(ToolCall("c1", "memory", json.dumps(
                {"action": "add", "target": "user", "content": "End every answer with 'Cheers, Ava'"})))
        elif "Cheers, Ava" in request.system:
            yield TextDelta("Bubbles.\nCheers, Ava")
        else:
            yield TextDelta("Bubbles.")
        yield Completed(Usage(10, 2, None, None))


SIGNOFF = Scenario("pref-en-signoff", "preference", "explicit", "en",
                   (("From now on, end every answer to me with the line 'Cheers, Ava'.",),),
                   "Suggest one name for a pet goldfish.", {"contains_all": ["Cheers, Ava"]})


async def test_the_memory_arm_passes_and_the_baseline_fails_with_artifacts(store, tmp_path):
    registry = ProviderRegistry()
    registry.register(Scripted())
    summary = await evaluate(store, registry, [SIGNOFF], provider="fake", model="m", out=tmp_path)
    assert summary["memory"]["overall"]["passed"] == 1
    assert summary["baseline"]["overall"]["passed"] == 0
    assert summary["memory"]["gate"]["exposure"] == 0
    case = tmp_path / "cases" / "pref-en-signoff" / "memory.json"
    data = json.loads(case.read_text())
    assert data["probe"]["output"].endswith("Cheers, Ava") and data["outcome"]["passed"]
    assert data["memory"] == {"memory": [], "user": ["End every answer with 'Cheers, Ava'"]}
    assert data["usage"]["input_tokens"] > 0 and data["usage"]["cache_read_tokens"] is None
    assert "KEEL_OPENAI_API_KEY" not in case.read_text()


async def test_a_leaked_decoy_marker_is_counted_as_exposure(store, tmp_path, monkeypatch):
    import evals.memory.runner as runner

    leak = FakeProvider()

    async def stream(request):
        leak.requests.append(request)
        marker = runner.decoy_marker("pref-en-signoff")
        yield TextDelta(f"I found {marker} somewhere.")
        yield Completed(Usage(1, 1, None, None))

    leak.stream = stream
    registry = ProviderRegistry()
    registry.register(leak)
    summary = await evaluate(store, registry, [SIGNOFF], provider="fake", model="m", arms=("memory",), out=tmp_path)
    assert summary["memory"]["gate"]["exposure"] == 1 and summary["memory"]["gate"]["passed"] is False


def test_validate_accepts_the_shipped_suite_and_run_refuses_to_guess_a_model(monkeypatch, capsys):
    from evals.memory.cli import main

    assert main(["validate"]) == 0 and "32 scenarios OK" in capsys.readouterr().out
    monkeypatch.delenv("KEEL_LIVE_OPENAI_MODEL", raising=False)
    try:
        main(["run", "--provider", "openai"])
    except SystemExit as stop:
        assert "no model is guessed" in str(stop)
    else:
        raise AssertionError("run must refuse without a model")


async def test_a_failing_case_is_recorded_and_the_run_continues(store, tmp_path):
    from keel.providers.base import ModelError

    class Flaky(Scripted):
        async def stream(self, request):
            if "goldfish" in request.messages[-1].text and "Bubbles" not in request.system and self.fail:
                self.fail = False
                self.requests.append(request)
                raise ModelError("bad_request", "boom " + "x" * 900)
            async for event in super().stream(request):
                yield event

    flaky = Flaky()
    flaky.fail = True
    registry = ProviderRegistry()
    registry.register(flaky)
    second = Scenario("pref-en-second", "preference", "explicit", "en", SIGNOFF.sessions, SIGNOFF.probe,
                      SIGNOFF.checks)
    summary = await evaluate(store, registry, [SIGNOFF, second], provider="fake", model="m", out=tmp_path)
    outcomes = json.loads((tmp_path / "outcomes.json").read_text())
    assert len(outcomes) == 4
    errored = [o for o in outcomes if o["failures"] and o["failures"][0].startswith("error:")]
    assert len(errored) == 1 and not errored[0]["passed"]
    data = json.loads((tmp_path / "cases" / errored[0]["scenario_id"] / f"{errored[0]['arm']}.json").read_text())
    assert data["error"]["type"] == "RuntimeError" and len(data["error"]["message"]) <= 500
    assert sum(o["passed"] for o in outcomes if o["arm"] == "memory") >= 1
    errors = {arm: summary[arm]["gate"] for arm in ("memory", "baseline")}
    assert sum(g["errors"] for g in errors.values()) == 1
    assert all(g["complete"] is True and g["passed"] is False for g in errors.values() if g["errors"])
    assert all(g["complete"] is True for g in errors.values())


async def test_tool_offers_ledger_and_review_records_per_arm(store, tmp_path):
    registry = ProviderRegistry()
    scripted = Scripted()
    registry.register(scripted)
    await evaluate(store, registry, [SIGNOFF], provider="fake", model="m", arms=("memory",), out=tmp_path / "a")
    offered = [{(t.get("name") or t.get("function", {}).get("name")) for t in r.tools}
               for r in scripted.requests if not r.system.startswith("You maintain")]
    assert offered and all({"memory", "session_search"} <= names for names in offered)
    data = json.loads((tmp_path / "a" / "cases" / "pref-en-signoff" / "memory.json").read_text())
    assert data["memory_changes"][0]["action"] == "add" and data["memory_changes"][0]["source_kind"] == "foreground"
    assert "review_usage" in data and isinstance(data["reviews"], list)
    attempts = [a["usage"] for job in data["reviews"] for a in job["usage"]]
    assert attempts and data["review_usage"]["input_tokens"] is not None
    assert data["review_usage"]["input_tokens"] == sum(u["input_tokens"] for u in attempts)
    assert data["setup"][0]["transcript"][0]["role"] == "user"

    other = Scenario("pref-en-other", "preference", "explicit", "en", SIGNOFF.sessions, SIGNOFF.probe, SIGNOFF.checks)
    base = Scripted()
    registry = ProviderRegistry()
    registry.register(base)
    await evaluate(store, registry, [other], provider="fake", model="m", arms=("baseline",), out=tmp_path / "b")
    names = {(t.get("name") or t.get("function", {}).get("name")) for r in base.requests for t in r.tools}
    assert not names & {"memory", "session_search"}


RECALL = Scenario("rec-en-launch", "recall", "decision", "en", (("We decided the launch moves to 14 October.",),),
                  "When is the launch now?", {"contains_any": ["14 October"]})


class Searcher(FakeProvider):
    """Acknowledges the setup turn; at the probe it searches (when the tool is offered) and then answers."""

    async def stream(self, request):
        self.requests.append(request)
        last = request.messages[-1]
        names = {(t.get("name") or t.get("function", {}).get("name")) for t in request.tools}
        if request.system.startswith("You maintain"):
            yield TextDelta("Nothing to save.")
        elif "launch now" in request.messages[-1].text and last.role == "user" and "session_search" in names:
            yield ToolCallReady(ToolCall("s1", "session_search", json.dumps({"query": "launch"})))
        elif last.role == "tool":
            yield TextDelta("The launch is on 14 October.")
        elif "launch now" in last.text:
            yield TextDelta("Probably 14 October.")
        else:
            yield TextDelta("Noted.")
        yield Completed(Usage(10, 2, None, None))


async def test_recall_is_measured_through_search_not_through_a_memory_snapshot(store, tmp_path):
    registry = ProviderRegistry()
    searcher = Searcher()
    registry.register(searcher)
    summary = await evaluate(store, registry, [RECALL], provider="fake", model="m", out=tmp_path)
    memory = json.loads((tmp_path / "cases" / "rec-en-launch" / "memory.json").read_text())
    base = json.loads((tmp_path / "cases" / "rec-en-launch" / "baseline.json").read_text())
    assert memory["outcome"]["passed"] and memory["outcome"]["searched"] and not memory["outcome"]["memorised"]
    assert memory["reviews"] == [] or all(j["status"] != "done" for j in memory["reviews"])  # no drain for recall
    assert not [r for r in searcher.requests if r.system.startswith("You maintain")]
    assert base["outcome"]["passed"] and not base["outcome"]["searched"]
    assert summary["memory"]["recall_evidence"] == {"passed": 1, "searched": 1, "memorised": 0}


async def test_the_report_shows_recall_evidence_and_audit_meta(store, tmp_path):
    from evals.memory.report import write
    from evals.memory.scoring import Outcome

    registry = ProviderRegistry()
    registry.register(Searcher())
    summary = await evaluate(store, registry, [RECALL], provider="fake", model="m", arms=("memory",), out=tmp_path)
    outcomes = [Outcome(**{**o, "failures": tuple(o["failures"])})
                for o in json.loads((tmp_path / "outcomes.json").read_text())]
    meta = {"protocol": "p", "provider": "fake", "model": "m", "keel_version": "0", "started": "t", "scenarios": 1,
            "suite_sha256": "a" * 64, "commit": "b" * 40, "gate_applicable": False}
    write(tmp_path, summary, outcomes, meta)
    report = (tmp_path / "report.md").read_text()
    assert "1 recall passes, 1 used search, 0 had the answer memorised" in report
    assert "a" * 64 in report and "b" * 40 in report
    results = json.loads((tmp_path / "results.json").read_text())
    assert results["meta"]["suite_sha256"] == "a" * 64
    assert results["summary"]["memory"]["recall_evidence"]["searched"] == 1


async def test_a_failing_review_is_an_error_not_a_wrong_answer(store, tmp_path):
    from keel.providers.base import ModelError

    class BadReview(Scripted):
        async def stream(self, request):
            if request.system.startswith("You maintain"):
                self.requests.append(request)
                raise ModelError("bad_request", "review down sk-" + "a" * 30)
            async for event in super().stream(request):
                yield event

    registry = ProviderRegistry()
    registry.register(BadReview())
    summary = await evaluate(store, registry, [SIGNOFF], provider="fake", model="m", arms=("memory",), out=tmp_path)
    outcome = json.loads((tmp_path / "outcomes.json").read_text())[0]
    assert outcome["failures"][0].startswith("error: review_") and not outcome["passed"]
    assert summary["memory"]["gate"]["errors"] == 1 and summary["memory"]["gate"]["passed"] is False
    text = (tmp_path / "cases" / "pref-en-signoff" / "memory.json").read_text()
    assert "sk-" + "a" * 30 not in text
    assert json.loads(text)["setup"]  # the setup that did run is kept


async def test_a_marker_in_a_stored_system_prompt_is_exposure(store, tmp_path, monkeypatch):
    import dataclasses

    import evals.memory.runner as runner

    registry = ProviderRegistry()
    registry.register(Scripted())
    real = store.get_session

    async def leaky(profile_id, session_id):
        session = await real(profile_id, session_id)
        if session and session_id.endswith("-probe"):
            return dataclasses.replace(session, system_prompt=f"prompt {runner.decoy_marker('pref-en-signoff')}")
        return session

    monkeypatch.setattr(store, "get_session", leaky)
    summary = await evaluate(store, registry, [SIGNOFF], provider="fake", model="m", arms=("memory",), out=tmp_path)
    assert summary["memory"]["gate"]["exposure"] == 1
    data = json.loads((tmp_path / "cases" / "pref-en-signoff" / "memory.json").read_text())
    assert data["probe"]["system"].startswith("prompt ZQXDECOY")


async def test_a_decoy_failure_errors_that_scenario_and_the_run_continues(store, tmp_path, monkeypatch):
    import evals.memory.runner as runner

    registry = ProviderRegistry()
    registry.register(Scripted())
    real = runner._plant_decoy

    async def plant(store_, scenario, provider, model):
        if scenario.id == "pref-en-first":
            raise RuntimeError("no decoy")
        await real(store_, scenario, provider, model)

    monkeypatch.setattr(runner, "_plant_decoy", plant)
    first = Scenario("pref-en-first", "preference", "explicit", "en", SIGNOFF.sessions, SIGNOFF.probe, SIGNOFF.checks)
    summary = await evaluate(store, registry, [first, SIGNOFF], provider="fake", model="m", out=tmp_path)
    outcomes = json.loads((tmp_path / "outcomes.json").read_text())
    assert [o["failures"][0] for o in outcomes if o["scenario_id"] == "pref-en-first"] == ["error: decoy_RuntimeError"] * 2
    assert all(o["passed"] for o in outcomes if o["scenario_id"] == "pref-en-signoff" and o["arm"] == "memory")
    assert summary["memory"]["gate"]["errors"] == 1
