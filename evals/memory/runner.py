"""Runs memory evaluation scenarios through keel in process: setup sessions, review drains between them, a probe in a
fresh session, and deterministic scoring. One profile per scenario and arm; a decoy profile in the same tenant holds
a unique marker that must never reach the probe."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
import logging
from pathlib import Path

from keel.background import Background
from keel.events import InMemoryEventBus
from keel.models import Message, TextPart, Usage
from keel.review import MemoryReviewer, ReviewSettings
from keel.runner import RunExecutor
from keel.tools.base import redact

from .scoring import Outcome, memorised, score, summarize
from .suite import Scenario

log = logging.getLogger(__name__)

TENANT = 1
ERROR_CHARS = 500
DRAIN = ReviewSettings(turns=1, idle_seconds=0.0, max_age_seconds=0.0)
TOOL_RESULT_CHARS = 4000


class CaseError(RuntimeError):
    """A case that cannot be judged for a reason of its own; `label` is the name shown in the outcome."""

    def __init__(self, label: str, message: str) -> None:
        super().__init__(message)
        self.label = label


def _label(error: Exception) -> str:
    return error.label if isinstance(error, CaseError) else type(error).__name__


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
    run = await store.claim_run("memory-eval", await store.coordination_now() + timeout + 60)
    await asyncio.wait_for(executor.execute(run), timeout)
    done = await store.get_run(profile_id, run.id)
    if done.status != "completed":  # a provider failure must not be scored as a (failed) answer
        raise RuntimeError(f"run {done.status}: {done.error_code or ''} {done.error or ''}".strip())
    return done


async def _drain(reviewer: MemoryReviewer) -> None:
    for _ in range(50):
        if not await reviewer.run_once("memory-eval"):
            return


async def _checked_drain(store, reviewer: MemoryReviewer, profile_id: str) -> None:
    """Drain, then insist every review job finished: a failed or unfinished review is the harness's problem, not a
    wrong answer by the model."""
    await _drain(reviewer)
    for job in await store.list_reviews(profile_id, limit=100):
        if job.status != "done" or job.error_code:
            label = f"review_{job.error_code or job.status}"
            raise CaseError(label, f"review job {job.id} ended {job.status}: {redact(job.error or '')}")


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
            if len(message.text) > TOOL_RESULT_CHARS:
                entry["truncated"] = True
                entry["full_length"] = len(message.text)
            results.append(message.text)
        messages.append(entry)
    return messages, results


def _searched(stored) -> bool:
    return any(call.name == "session_search" for item in stored for call in item.message.tool_calls or ())


async def _session_texts(store, profile_id: str, session_id: str) -> tuple[str, list[str]]:
    """(stored system prompt, tool results) of one session: everything the model was shown besides the user turns."""
    session = await store.get_session(profile_id, session_id)
    system = (session.system_prompt if session else None) or ""
    return system, _transcript(await store.messages(session_id))[1]


async def _ledger(store, profile_id: str) -> list[dict]:
    rows = await store.db.fetch(
        "SELECT action, target, old_text, new_text, source_kind, created_at FROM memory_changes "
        "WHERE profile_id = ? ORDER BY id", profile_id)
    return [dict(row) for row in rows]


async def _reviews(store, profile_id: str) -> tuple[list[dict], Usage]:
    total, jobs = Usage(), []
    for job in sorted(await store.list_reviews(profile_id, limit=100), key=lambda j: j.created_at):
        for attempt in job.usage:
            total = total + Usage.from_dict(attempt.get("usage"))
        jobs.append({"id": job.id, "status": job.status, "attempts": job.attempts, "error_code": job.error_code,
                     "error": redact(job.error) if job.error else job.error, "usage": job.usage})
    return jobs, total


async def run_scenario(store, providers, scenario: Scenario, *, arm: str, provider: str, model: str,
                       timeout: float, setup: list | None = None) -> tuple[Outcome, dict]:
    profile_id = _profile(arm, scenario)
    await store.upsert_profile(profile_id, models=[{"provider": provider, "model": model}], sandbox={}, soul="",
                               skills=[], memory=arm == "memory", tenant_id=TENANT)
    background = Background()
    try:
        executor = RunExecutor(store, InMemoryEventBus(), providers, background=background)
        reviewer = MemoryReviewer(store, providers, DRAIN)
        usage = Usage()
        setup = [] if setup is None else setup  # the caller keeps what exists if this raises
        exposure_texts: list[str] = []
        for index, turns in enumerate(scenario.sessions):
            session_id = f"{profile_id}-setup-{index}"
            await _session(store, profile_id, session_id)
            for text in turns:
                run = await _turn(store, executor, profile_id, session_id, text, provider, model, timeout)
                usage = usage + run.usage
                setup.append({"session": session_id, "input": text, "status": run.status, "output": run.output})
            setup[-1]["transcript"] = _transcript(await store.messages(session_id))[0]
            if arm == "memory" and scenario.group == "preference":  # recall facts stay "old, never memorised"
                await _checked_drain(store, reviewer, profile_id)
        probe_session = f"{profile_id}-probe"
        await _session(store, profile_id, probe_session)
        probe = await _turn(store, executor, profile_id, probe_session, scenario.probe, provider, model, timeout)
        usage = usage + probe.usage
        stored_probe = await store.messages(probe_session)
        transcript, tool_results = _transcript(stored_probe)
        for index in range(len(scenario.sessions)):
            system, results = await _session_texts(store, profile_id, f"{profile_id}-setup-{index}")
            exposure_texts += [system, *results]
        probe_system, _ = await _session_texts(store, profile_id, probe_session)
        exposure_texts.append(probe_system)
        memory = await store.memory_entries(profile_id)
        jobs, review_usage = await _reviews(store, profile_id)
        outcome = score(scenario, output=probe.output or "", memory_texts=memory["user"] + memory["memory"],
                        tool_results=tool_results + exposure_texts, marker=decoy_marker(scenario.id), arm=arm)
        outcome = replace(outcome, searched=_searched(stored_probe),
                          memorised=memorised(scenario, memory["user"] + memory["memory"]))
        record = {"scenario": scenario.id, "arm": arm, "setup": setup,
                  "probe": {"input": scenario.probe, "status": probe.status, "output": probe.output,
                            "system": probe_system, "transcript": transcript},
                  "memory": memory, "memory_changes": await _ledger(store, profile_id), "reviews": jobs,
                  "usage": usage.to_dict(), "review_usage": review_usage.to_dict(),
                  "outcome": {"passed": outcome.passed, "failures": list(outcome.failures),
                              "exposure": outcome.exposure, "unsupported": outcome.unsupported,
                              "searched": outcome.searched, "memorised": outcome.memorised}}
        return outcome, record
    finally:
        await background.close()


async def _after_error(store, scenario: Scenario, arm: str) -> tuple[bool, bool, list[str], dict]:
    """Best effort: a leak or an unsupported entry must still be flagged on a case that errored, and whatever setup
    transcript exists is kept."""
    profile_id, notes, haystack, texts, transcripts = _profile(arm, scenario), [], [], [], {}
    sessions = [f"{profile_id}-setup-{i}" for i in range(len(scenario.sessions))] + [f"{profile_id}-probe"]
    for session_id in sessions:
        try:
            system, results = await _session_texts(store, profile_id, session_id)
            haystack += [system, *results]
            stored = await store.messages(session_id)
            if stored:
                transcripts[session_id] = _transcript(stored)[0]
        except Exception as error:  # noqa: BLE001
            notes.append(f"session {session_id} unreadable: {type(error).__name__}")
    try:
        memory = await store.memory_entries(profile_id)
        texts = memory["user"] + memory["memory"]
    except Exception as error:  # noqa: BLE001
        notes.append(f"memory unreadable: {type(error).__name__}")
    found = score(scenario, output="", memory_texts=texts, tool_results=haystack, marker=decoy_marker(scenario.id),
                  arm=arm)
    return found.exposure, found.unsupported, notes, transcripts


async def _retire(store, scenario: Scenario, arm: str, provider: str, model: str) -> None:
    """Memory off once a profile is judged, so later drains never review its probe session."""
    try:
        await store.upsert_profile(_profile(arm, scenario), models=[{"provider": provider, "model": model}],
                                   sandbox={}, soul="", skills=[], memory=False, tenant_id=TENANT)
    except Exception as error:  # noqa: BLE001
        log.warning("could not retire profile %s: %s", _profile(arm, scenario), type(error).__name__)


def _error_case(scenario: Scenario, arm: str, error: Exception, *, exposure: bool = False,
                unsupported: bool = False, notes=(), setup=(), transcripts=None) -> tuple[Outcome, dict]:
    name = _label(error)
    failures = [f"error: {name}"] + (["decoy marker exposed"] if exposure else []) + (
        ["memory holds an unsupported preference"] if unsupported else [])
    outcome = Outcome(scenario.id, arm, False, tuple(failures), exposure, unsupported)
    record = {"scenario": scenario.id, "arm": arm, "setup": list(setup),
              "error": {"type": name, "message": redact(str(error))[:ERROR_CHARS], "notes": list(notes)}}
    if transcripts:
        record["setup_transcripts"] = transcripts
    return outcome, record


async def evaluate(store, providers, scenarios: list[Scenario], *, provider: str, model: str,
                   arms: tuple[str, ...] = ("memory", "baseline"), out: Path, timeout: float = 300.0) -> dict:
    out = Path(out)
    outcomes = []
    for scenario in scenarios:
        decoy_error = None
        try:
            await _plant_decoy(store, scenario, provider, model)
        except Exception as error:  # noqa: BLE001 - the scenario's arms become errors; the run continues
            log.warning("scenario %s: decoy could not be planted: %s", scenario.id, type(error).__name__)
            decoy_error = CaseError("decoy_" + type(error).__name__, str(error))
        for arm in arms:
            case = out / "cases" / scenario.id
            case.mkdir(parents=True, exist_ok=True)
            setup: list = []
            if decoy_error is not None:
                outcome, record = _error_case(scenario, arm, decoy_error)
            else:
                try:
                    outcome, record = await run_scenario(store, providers, scenario, arm=arm, provider=provider,
                                                         model=model, timeout=timeout, setup=setup)
                except Exception as error:  # noqa: BLE001 - one failed case is a recorded failure, not a lost run
                    log.warning("scenario %s (%s) failed: %s", scenario.id, arm, _label(error))
                    exposure, unsupported, notes, transcripts = await _after_error(store, scenario, arm)
                    outcome, record = _error_case(scenario, arm, error, exposure=exposure, unsupported=unsupported,
                                                  notes=notes, setup=setup, transcripts=transcripts)
            outcomes.append(outcome)
            (case / f"{arm}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            if decoy_error is None:
                await _retire(store, scenario, arm, provider, model)
    summary = summarize(outcomes, scenarios)
    (out / "outcomes.json").write_text(json.dumps([o.__dict__ for o in outcomes], ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    return summary
