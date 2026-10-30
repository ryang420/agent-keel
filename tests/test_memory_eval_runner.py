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
    for arm in ("memory", "baseline"):
        assert summary[arm]["gate"]["complete"] is True
