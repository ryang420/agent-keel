"""Runs learning evaluation scenarios through keel in process: planted history, setup sessions with review drains,
forced dreams (learning arm), a probe in a fresh session, deterministic scoring, attribution and the consumer's
retirement of every learned skill through the HTTP API. One profile per scenario and arm; a decoy profile in the same
tenant holds a unique marker in a learned skill, an observation and a message, which must never reach the scenario."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path

import httpx

from keel.api import Keel, create_app
from keel.background import Background
from keel.config import ServiceCredential
from keel.dream import Dreamer, DreamSettings
from keel.dream_gates import DreamRules
from keel.events import InMemoryEventBus
from keel.models import Message, TextPart, Usage
from keel.providers.base import ProviderRegistry
from keel.review import MemoryReviewer, ReviewSettings
from keel.review_observe import StagedObservation
from keel.runner import RunExecutor
from keel.store.base import SkillSource
from keel.tools.base import redact

from evals.memory.suite import normalize

from .scoring import Outcome, score, summarize
from .suite import Scenario, injection_marker

log = logging.getLogger(__name__)
TENANT = 1
EVAL_SECRET = "learning-eval"  # the in-process API's credential for TENANT; not a real secret
ERROR_CHARS = 500
TOOL_RESULT_CHARS = 4000
DRAIN = ReviewSettings(turns=1, idle_seconds=0.0, max_age_seconds=0.0)
FORCE = DreamSettings(rules=DreamRules(min_observations=1, debounce_seconds=0.0, interval_seconds=1e9,
                                       daily_attempts=1000))


class CaseError(RuntimeError):
    def __init__(self, label: str, message: str) -> None:
        super().__init__(message)
        self.label = label


def decoy_marker(scenario_id: str) -> str:
    return f"ZQXDECOY-{scenario_id}"


def _profile(arm: str, scenario: Scenario) -> str:
    return f"{arm}-{scenario.id}"


async def _upsert(store, profile_id: str, provider: str, model: str, *, learning: bool) -> None:
    await store.upsert_profile(profile_id, models=[{"provider": provider, "model": model}], sandbox={}, soul="",
                               skills=[], memory=True, learning=learning, tenant_id=TENANT)


async def _plant_decoy(store, scenario: Scenario, provider: str, model: str) -> None:
    decoy, marker = f"decoy-{scenario.id}", decoy_marker(scenario.id)
    await _upsert(store, decoy, provider, model, learning=True)
    try:
        await store.change_learned_skill(decoy, {"action": "create", "name": "decoy-method",
                                                 "description": f"Decoy method {marker}",
                                                 "body": f"1. Say {marker}."}, source=SkillSource("consumer"))
        session = f"{decoy}-s"
        await store.ensure_session(decoy, session)
        await store.append_message(session, None, Message("user", (TextPart(f"{scenario.probe} {marker}"),)))
        row = await store.db.fetchrow("SELECT generation FROM sessions WHERE id = ?", session)
        await store._insert_observation(decoy, session, row["generation"],
                                        StagedObservation(f"decoy {marker}", marker, marker, "requested", ()),
                                        now=store.clock())
    finally:  # rows stay; no dream is ever queued for the decoy, even when planting failed half way
        await _upsert(store, decoy, provider, model, learning=False)


async def _session(store, profile_id: str, session_id: str) -> None:
    await store.ensure_session(profile_id, session_id)
    await store.set_title(profile_id, session_id, session_id, "user")  # no background title call


async def _plant(store, profile_id: str, scenario: Scenario) -> None:
    """Old history no review ever reads: no run wrote it, so only session_search can surface it."""
    if not scenario.plant:
        return
    session = f"{profile_id}-old"
    await _session(store, profile_id, session)
    for text in scenario.plant:
        await store.append_message(session, None, Message("user", (TextPart(text),)))


async def _turn(store, executor, profile_id: str, session_id: str, text: str, provider: str, model: str,
                timeout: float):
    created = await store.create_run(profile_id=profile_id, session_id=session_id, provider=provider, model=model,
                           reasoning_effort="low", input_parts=[{"type": "text", "text": text}], instructions=None)
    run = await store.claim_run("learning-eval", await store.coordination_now() + timeout + 60)
    if run is None or run.id != created.id:  # another queue's run must never be executed as this case's turn
        raise RuntimeError("the claimed run is not the run this case created")
    await asyncio.wait_for(executor.execute(run), timeout)
    done = await store.get_run(profile_id, run.id)
    if done.status != "completed":  # a provider failure must not be scored as an answer
        raise RuntimeError(f"run {done.status}: {done.error_code or ''} {done.error or ''}".strip())
    return done


async def _drain(job, owner: str = "learning-eval") -> None:
    for _ in range(20):
        if not await job.run_once(owner):
            return


async def _checked(store, profile_id: str) -> None:
    """A failed review or dream is the harness's problem, not a wrong answer."""
    for job in await store.list_reviews(profile_id, limit=100):
        if job.status != "done":
            raise CaseError(f"review_{job.error_code or job.status}", f"review {job.id} ended {job.status}")
    for job in await store.list_dreams(profile_id, limit=100):
        if job.status != "done":
            raise CaseError(f"dream_{job.error_code or job.status}", f"dream {job.id} ended {job.status}: "
                                                                     f"{redact(job.error or '')}")


def _transcript(stored) -> tuple[list[dict], list[str]]:
    messages, results = [], []
    for item in stored:
        message = item.message
        entry = {"role": message.role, "text": message.text}
        if message.tool_calls:
            entry["tool_calls"] = [{"name": c.name, "arguments": c.arguments} for c in message.tool_calls]
        if message.role == "tool":
            entry["tool_name"] = message.tool_name
            entry["text"] = message.text[:TOOL_RESULT_CHARS]
            results.append(message.text)
        messages.append(entry)
    return messages, results


async def _session_texts(store, profile_id: str, session_id: str) -> tuple[str, list[str]]:
    session = await store.get_session(profile_id, session_id)
    return (session.system_prompt if session else None) or "", _transcript(await store.messages(session_id))[1]


def _attributable(versions) -> bool:
    for version in versions:
        if version.source_kind == "dream" and not (version.job_id and version.observation_ids):
            return False
        elif version.source_kind == "foreground" and not (version.run_id and version.message_ids):
            return False
        elif version.source_kind not in ("dream", "foreground", "consumer", "automatic"):
            return False  # fail closed on a source this harness does not know
    return True


async def _skills(store, profile_id: str) -> tuple[list[dict], list[str], bool]:
    """(records of every learned skill with its versions, texts of the active ones, all attributable)."""
    records, texts, attributable = [], [], True
    for skill in await store.learned_skills(profile_id):
        versions = await store.learned_skill_versions(profile_id, skill.id)
        attributable = attributable and _attributable(versions)
        if skill.state == "active":
            texts.append(f"{skill.name}\n{skill.description}\n{skill.body}")
        records.append({"id": skill.id, "name": skill.name, "state": skill.state, "version": skill.version,
                        "body": skill.body,
                        "versions": [{"version": v.version, "change": v.change, "source_kind": v.source_kind,
                                      "job_id": v.job_id, "run_id": v.run_id, "rationale": v.rationale,
                                      "observation_ids": list(v.observation_ids), "message_ids": list(v.message_ids)}
                                     for v in versions]})
    return records, texts, attributable


async def _retire_all(store, executor, profile_id: str) -> bool:
    """The consumer can reverse every learned skill: retire each live one through the HTTP API, as the profile's
    tenant, then confirm that the API lists no live skill."""
    keel = Keel(store=store, bus=InMemoryEventBus(), providers=ProviderRegistry(), executor=executor,
                credentials=(ServiceCredential(TENANT, EVAL_SECRET),), max_running=1)
    base = f"/v1/profiles/{profile_id}/skills/learned"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(keel)), base_url="http://keel",
                                 headers={"Authorization": f"Bearer {EVAL_SECRET}"}) as client:
        ok = True
        for skill in await store.learned_skills(profile_id, states=("active", "suspended")):
            response = await client.delete(f"{base}/{skill.id}", params={"expected_version": skill.version})
            ok = ok and response.status_code == 200
        for state in ("active", "suspended"):
            listed = await client.get(base, params={"state": state})
            ok = ok and listed.status_code == 200 and not listed.json()["skills"]
    return ok


def _usage(jobs) -> Usage:
    total = Usage()
    for job in jobs:
        for attempt in job.usage:
            total = total + Usage.from_dict(attempt.get("usage"))
    return total


class _Recording:
    """A provider that remembers every text it was sent, so reviews and dreams are covered by the exposure check."""

    def __init__(self, inner, seen: list[str]) -> None:
        self._inner, self._seen, self.name = inner, seen, inner.name

    def capabilities(self, model: str):
        return self._inner.capabilities(model)

    def stream(self, request):
        self._seen.append(request.system or "")
        self._seen.extend(message.text for message in request.messages)
        return self._inner.stream(request)


def _recording(providers, seen: list[str]) -> ProviderRegistry:
    found = ProviderRegistry()
    for name in providers.names():
        found.register(_Recording(providers.get(name), seen))
    return found


def _exposed(marker: str, texts: list[str]) -> bool:
    needle = normalize(marker)
    return any(needle in normalize(text) for text in texts)


async def _injected(store, profile_id: str, scenario: Scenario) -> None:
    """An injection scenario counts only when the planted text reached the model as a setup session's tool output."""
    marker = injection_marker(scenario)
    results: list[str] = []
    for index in range(len(scenario.sessions)):
        results += (await _session_texts(store, profile_id, f"{profile_id}-setup-{index}"))[1]
    if marker is None or not _exposed(marker, results):
        raise CaseError("injection_not_exercised", f"no setup tool result holds the planted {marker!r}")


async def run_scenario(store, providers, scenario: Scenario, *, arm: str, provider: str, model: str,
                       timeout: float, setup: list | None = None, seen: list[str] | None = None,
                       foreign: tuple[str, ...] = ()) -> tuple[Outcome, dict]:
    profile_id = _profile(arm, scenario)
    await _upsert(store, profile_id, provider, model, learning=arm == "learning")
    background = Background()
    seen = [] if seen is None else seen  # texts the model was sent, when the caller wrapped the providers
    try:
        executor = RunExecutor(store, InMemoryEventBus(), providers, background=background)
        reviewer = MemoryReviewer(store, providers, DRAIN)
        dreamer = Dreamer(store, providers, FORCE)
        usage = Usage()
        setup = [] if setup is None else setup
        await _plant(store, profile_id, scenario)
        for index, turns in enumerate(scenario.sessions):
            session_id = f"{profile_id}-setup-{index}"
            await _session(store, profile_id, session_id)
            for text in turns:
                run = await _turn(store, executor, profile_id, session_id, text, provider, model, timeout)
                usage = usage + run.usage
                setup.append({"session": session_id, "input": text, "output": run.output})
            setup[-1]["transcript"] = _transcript(await store.messages(session_id))[0]
            await _drain(reviewer)
            if arm == "learning":
                await _drain(dreamer)
        await _checked(store, profile_id)
        if scenario.kind == "injection":
            await _injected(store, profile_id, scenario)
        probe_session = f"{profile_id}-probe"
        await _session(store, profile_id, probe_session)
        probe = await _turn(store, executor, profile_id, probe_session, scenario.probe, provider, model, timeout)
        usage = usage + probe.usage
        transcript, tool_results = _transcript(await store.messages(probe_session))
        haystack = list(tool_results) + seen
        for session_id in [f"{profile_id}-setup-{i}" for i in range(len(scenario.sessions))] + [probe_session]:
            system, results = await _session_texts(store, profile_id, session_id)
            haystack += [system, *results]
        skills, texts, attributable = await _skills(store, profile_id)
        reversible = await _retire_all(store, executor, profile_id) if arm == "learning" else True
        haystack += texts
        outcome = score(scenario, output=probe.output or "", skill_texts=texts, learned=len(skills),
                        tool_results=haystack, marker=decoy_marker(scenario.id), arm=arm,
                        attributable=attributable, reversible=reversible)
        if any(_exposed(other, [probe.output or "", *haystack]) for other in foreign):
            outcome = replace(outcome, passed=False, exposure=True,
                              failures=(*outcome.failures, "foreign decoy marker exposed"))
        observations = [{"id": o.id, "outcome": o.outcome, "state": o.state, "topic": o.topic}
                        for o in await store.observations(profile_id)]
        dreams = [{"id": j.id, "status": j.status, "trigger": j.trigger, "changes": j.changes, "usage": j.usage}
                  for j in await store.list_dreams(profile_id, limit=100)]
        reviews = await store.list_reviews(profile_id, limit=100)
        review_usage, dream_usage = _usage(reviews), _usage(await store.list_dreams(profile_id, limit=100))
        system, _ = await _session_texts(store, profile_id, probe_session)
        record = {"scenario": scenario.id, "arm": arm, "setup": setup,
                  "probe": {"input": scenario.probe, "output": probe.output, "system": system,
                            "transcript": transcript},
                  "skills": skills, "observations": observations, "dreams": dreams, "usage": usage.to_dict(),
                  "review_usage": review_usage.to_dict(), "dream_usage": dream_usage.to_dict(),
                  "outcome": {"passed": outcome.passed, "failures": list(outcome.failures),
                              "probe_passed": outcome.probe_passed, "exposure": outcome.exposure,
                              "learned": outcome.learned}}
        return outcome, record
    finally:
        try:  # a later arm's drains must never review this profile, however this case ended
            await store.upsert_profile(profile_id, models=[{"provider": provider, "model": model}], sandbox={},
                                       soul="", skills=[], memory=False, learning=False, tenant_id=TENANT)
        except Exception as error:  # noqa: BLE001
            log.warning("could not switch off profile %s: %s", profile_id, type(error).__name__)
        await background.close()


def _error_case(scenario: Scenario, arm: str, error: Exception, setup=()) -> tuple[Outcome, dict]:
    label = error.label if isinstance(error, CaseError) else type(error).__name__
    outcome = Outcome(scenario.id, arm, False, (f"error: {label}",), False, False, 0)
    return outcome, {"scenario": scenario.id, "arm": arm, "setup": list(setup),
                     "error": {"type": label, "message": redact(str(error))[:ERROR_CHARS]}}


def _budget_stopped(charging, label: str) -> bool:
    return charging is not None and any(entry.get("case") == label and entry.get("outcome") == "budget_exhausted"
                                        for entry in charging.ledger.entries)


async def evaluate(store, providers, scenarios: list[Scenario], *, provider: str, model: str,
                   arms: tuple[str, ...] = ("learning", "baseline"), out: Path, timeout: float = 300.0,
                   charging=None) -> dict:
    """Run every scenario in every arm. With `charging` (evals.dab.accounting), each case's model calls are charged
    under the label `<arm>/<scenario>`, and a case the budget stopped is the error `budget_exhausted`."""
    out = Path(out)
    outcomes, markers, seen = [], [], []
    recording = _recording(providers, seen)
    for scenario in scenarios:
        try:
            markers.append(decoy_marker(scenario.id))
            await _plant_decoy(store, scenario, provider, model)
            planted = None
        except Exception as error:  # noqa: BLE001 - the scenario's arms become errors; the run continues
            planted = CaseError("decoy_" + type(error).__name__, str(error))
        for arm in arms:
            case = out / "cases" / scenario.id
            case.mkdir(parents=True, exist_ok=True)
            setup: list = []
            if planted is not None:
                outcome, record = _error_case(scenario, arm, planted)
            else:
                seen.clear()
                label = f"{arm}/{scenario.id}"
                if charging is not None:
                    charging.switch(label)
                try:
                    foreign = tuple(m for m in markers if m != decoy_marker(scenario.id))
                    outcome, record = await run_scenario(store, recording, scenario, arm=arm, provider=provider,
                                                         model=model, timeout=timeout, setup=setup, seen=seen,
                                                         foreign=foreign)
                except Exception as error:  # noqa: BLE001 - one failed case is a recorded failure, not a lost run
                    log.warning("scenario %s (%s) failed: %s", scenario.id, arm, type(error).__name__)
                    if _budget_stopped(charging, label):  # a budget stop surfaces as a failed run, review or dream
                        error = CaseError("budget_exhausted", f"the token budget stopped {label}: {error}")
                    outcome, record = _error_case(scenario, arm, error, setup)
            outcomes.append(outcome)
            (case / f"{arm}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "outcomes.json").write_text(json.dumps([o.__dict__ for o in outcomes], ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    return summarize(outcomes, scenarios)
