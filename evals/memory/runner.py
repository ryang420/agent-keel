"""Runs memory evaluation scenarios through keel in process: setup sessions, review drains between them, a probe in a
fresh session, and deterministic scoring. One profile per scenario and arm; a decoy profile in the same tenant holds
a unique marker that must never reach the probe."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from keel.background import Background
from keel.events import InMemoryEventBus
from keel.models import Message, TextPart, Usage
from keel.review import MemoryReviewer, ReviewSettings
from keel.runner import RunExecutor

from .scoring import Outcome, score, summarize
from .suite import Scenario

log = logging.getLogger(__name__)

TENANT = 1
ERROR_CHARS = 500
DRAIN = ReviewSettings(turns=1, idle_seconds=0.0, max_age_seconds=0.0)
TOOL_RESULT_CHARS = 4000


def decoy_marker(scenario_id: str) -> str:
    return f"ZQXDECOY-{scenario_id}"


def _profile(arm: str, scenario: Scenario) -> str:
    return f"{arm}-{scenario.id}"


async def _plant_decoy(store, scenario: Scenario, provider: str, model: str) -> None:
    decoy = f"decoy-{scenario.id}"
    marker = decoy_marker(scenario.id)
    await store.upsert_profile(decoy, models=[{"provider": provider, "model": model}], sandbox={}, soul="",
                               skills=[], memory=True, tenant_id=TENANT)
    await store.ensure_session(decoy, f"{decoy}-s")
    await store.append_message(f"{decoy}-s", None, Message("user", (TextPart(f"{scenario.probe} {marker}"),)))
    await store.apply_memory(decoy, "user", 1375, [{"action": "add", "content": f"Secret preference {marker}"}])


async def _turn(store, executor, profile_id: str, session_id: str, text: str, provider: str, model: str,
                timeout: float):
    await store.create_run(profile_id=profile_id, session_id=session_id, provider=provider, model=model,
                           reasoning_effort="low", input_parts=[{"type": "text", "text": text}], instructions=None)
    run = await store.claim_run("memory-eval", 9_999_999)
    await asyncio.wait_for(executor.execute(run), timeout)
    done = await store.get_run(profile_id, run.id)
    if done.status != "completed":  # a provider failure must not be scored as a (failed) answer
        raise RuntimeError(f"run {done.status}: {done.error_code or ''} {done.error or ''}".strip())
    return done


async def _drain(reviewer: MemoryReviewer) -> None:
    for _ in range(50):
        if not await reviewer.run_once("memory-eval"):
            return


async def _session(store, profile_id: str, session_id: str) -> None:
    await store.ensure_session(profile_id, session_id)
    await store.set_title(profile_id, session_id, session_id, "user")  # no background title call


def _transcript(stored) -> tuple[list[dict], list[str]]:
    messages, results = [], []
    for item in stored:
        message = item.message
        entry = {"role": message.role, "text": message.text}
        if message.tool_calls:
            entry["tool_calls"] = [{"name": call.name, "arguments": call.arguments} for call in message.tool_calls]
        if message.role == "tool":
            entry["tool_name"] = message.tool_name
            entry["text"] = message.text[:TOOL_RESULT_CHARS]
            results.append(message.text)
        messages.append(entry)
    return messages, results


async def run_scenario(store, providers, scenario: Scenario, *, arm: str, provider: str, model: str,
                       timeout: float) -> tuple[Outcome, dict]:
    profile_id = _profile(arm, scenario)
    await store.upsert_profile(profile_id, models=[{"provider": provider, "model": model}], sandbox={}, soul="",
                               skills=[], memory=arm == "memory", tenant_id=TENANT)
    executor = RunExecutor(store, InMemoryEventBus(), providers, background=Background())
    reviewer = MemoryReviewer(store, providers, DRAIN)
    usage = Usage()
    setup = []
    for index, turns in enumerate(scenario.sessions):
        session_id = f"{profile_id}-setup-{index}"
        await _session(store, profile_id, session_id)
        for text in turns:
            run = await _turn(store, executor, profile_id, session_id, text, provider, model, timeout)
            usage = usage + run.usage
            setup.append({"session": session_id, "input": text, "status": run.status, "output": run.output})
        if arm == "memory":
            await _drain(reviewer)
    probe_session = f"{profile_id}-probe"
    await _session(store, profile_id, probe_session)
    probe = await _turn(store, executor, profile_id, probe_session, scenario.probe, provider, model, timeout)
    usage = usage + probe.usage
    transcript, tool_results = _transcript(await store.messages(probe_session))
    memory = await store.memory_entries(profile_id)
    outcome = score(scenario, output=probe.output or "", memory_texts=memory["user"] + memory["memory"],
                    tool_results=tool_results, marker=decoy_marker(scenario.id), arm=arm)
    record = {"scenario": scenario.id, "arm": arm, "setup": setup,
              "probe": {"input": scenario.probe, "status": probe.status, "output": probe.output,
                        "transcript": transcript},
              "memory": memory, "usage": usage.to_dict(),
              "outcome": {"passed": outcome.passed, "failures": list(outcome.failures),
                          "exposure": outcome.exposure, "unsupported": outcome.unsupported}}
    return outcome, record


async def evaluate(store, providers, scenarios: list[Scenario], *, provider: str, model: str,
                   arms: tuple[str, ...] = ("memory", "baseline"), out: Path, timeout: float = 300.0) -> dict:
    out = Path(out)
    outcomes = []
    for scenario in scenarios:
        await _plant_decoy(store, scenario, provider, model)
        for arm in arms:
            case = out / "cases" / scenario.id
            case.mkdir(parents=True, exist_ok=True)
            try:
                outcome, record = await run_scenario(store, providers, scenario, arm=arm, provider=provider,
                                                     model=model, timeout=timeout)
            except Exception as error:  # noqa: BLE001 - one failed case is a recorded failure, not a lost run
                name = type(error).__name__
                log.warning("scenario %s (%s) failed: %s", scenario.id, arm, name)
                outcome = Outcome(scenario.id, arm, False, (f"error: {name}",), False, False)
                record = {"scenario": scenario.id, "arm": arm,
                          "error": {"type": name, "message": str(error)[:ERROR_CHARS]}}
            outcomes.append(outcome)
            (case / f"{arm}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = summarize(outcomes, scenarios)
    (out / "outcomes.json").write_text(json.dumps([o.__dict__ for o in outcomes], ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    return summary
