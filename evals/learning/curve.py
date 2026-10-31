"""The DAB learning curve (design §10): for each file-backed dataset, a learning phase of tasks on one profile with
learning on, the consumer's feedback after each task, and reviews and dreams forced in between; then held-out tasks of
the same dataset. The baseline runs the same sequence with learning off (memory on), so the only difference between
arms is learning. Repeats estimate variance. Scoring uses DAB's pinned validators; no gold reaches the agent."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import statistics
import time
from pathlib import Path

import httpx

from keel.dream import Dreamer, DreamSettings
from keel.dream_gates import DreamRules
from keel.review import MemoryReviewer, ReviewSettings
from keel.service import create_service, default_providers

from evals.dab import scoring
from evals.dab.accounting import Charging
from evals.dab.budget import Ledger
from evals.dab.inputs import Paths
from evals.dab.policy import DEFAULT_IMAGE, isolation
from evals.dab.prompt import build
from evals.dab.runner import (TERMINAL, _budgeted, _retire_profile, _settings, extract_answer, managed_containers,
                              temporary_database)
from evals.dab.suite import Case

ARMS = ("learning", "baseline")
DRAIN = ReviewSettings(turns=1, idle_seconds=0.0, max_age_seconds=0.0)
FORCE = DreamSettings(rules=DreamRules(min_observations=1, debounce_seconds=0.0, interval_seconds=1e9,
                                       daily_attempts=1000))
FEEDBACK = {True: ("up", "Correct."), False: ("down", "Not correct.")}
ERROR_STATUSES = frozenset({"setup_failed", "blocked", "budget_exhausted"})


class CurveError(RuntimeError):
    """The curve cannot run as asked."""


def split(cases: list[Case]) -> tuple[list[Case], list[Case]]:
    """Queries in numeric order; the last max(1, n // 3) are held out, the rest learned."""
    ordered = sorted(cases, key=lambda case: int(case.query.number))
    if len(ordered) < 2:
        raise CurveError(f"{ordered[0].dataset.name if ordered else 'a dataset'} needs at least two queries")
    held = max(1, len(ordered) // 3)
    return ordered[:-held], ordered[-held:]


def _tool_error(content) -> bool:
    try:
        value = json.loads(content or "")
    except (TypeError, ValueError):
        return False
    return isinstance(value, dict) and ("error" in value or value.get("success") is False)


def metrics(messages: list[dict]) -> dict:
    return {"iterations": sum(m.get("role") == "assistant" for m in messages),
            "tool_calls": sum(len(m.get("tool_calls") or []) for m in messages if m.get("role") == "assistant"),
            "tool_errors": sum(m.get("role") == "tool" and _tool_error(m.get("content")) for m in messages)}


def _tokens(usage: dict | None) -> int | None:
    if not usage or usage.get("input_tokens") is None or usage.get("output_tokens") is None:
        return None
    return usage["input_tokens"] + usage["output_tokens"]


async def _task(client, profile_id: str, session_id: str, prompt: str, *, provider: str, model: str,
                reasoning_effort: str, timeout_s: float) -> dict:
    """One task in its own session on a profile that outlives it (unlike evals.dab's one-run profiles)."""
    response = await client.post(f"/v1/profiles/{profile_id}/runs",
                                 json={"input": prompt, "session_id": session_id, "provider": provider,
                                       "model": model, "reasoning_effort": reasoning_effort})
    if response.status_code != 202:
        return {"status": "setup_failed", "error": f"run: {response.status_code} {response.text[:300]}"}
    run_id, started = response.json()["run_id"], time.monotonic()
    while True:
        run = (await client.get(f"/v1/profiles/{profile_id}/runs/{run_id}")).json()
        if run["status"] in TERMINAL:
            break
        if time.monotonic() - started > timeout_s:
            await client.post(f"/v1/profiles/{profile_id}/runs/{run_id}/stop")
            for _ in range(100):
                run = (await client.get(f"/v1/profiles/{profile_id}/runs/{run_id}")).json()
                if run["status"] in TERMINAL:
                    break
                await asyncio.sleep(0.2)
            break
        await asyncio.sleep(0.2)
    messages = (await client.get(f"/v1/profiles/{profile_id}/sessions/{session_id}/messages",
                                 params={"limit": 500})).json()["data"]
    status = "executed" if run["status"] == "completed" else ("timeout" if run["status"] not in TERMINAL
                                                              else "failed")
    return {"status": status, "run": run, "messages": messages, "duration_s": time.monotonic() - started}


def _artifact(paths: Paths, arm: str, dataset: str, repeat: int, case: Case, record: dict, messages: list) -> None:
    """The task record and its stored messages under the evaluator's tree (never a sandbox mount)."""
    directory = paths.root / "curve" / arm / dataset / f"rep-{repeat}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{case.query.slug}.json").write_text(json.dumps({"record": record, "messages": messages}, indent=2),
                                                      encoding="utf-8")


async def _drain(job) -> None:
    for _ in range(20):
        if not await job.run_once("learning-curve"):
            return


async def _sequence(client, keel, charging: Charging, cases: list[Case], *, paths: Paths, arm: str, repeat: int,
                    repo: Path, mount_root: Path, image: str, provider: str, model: str, reasoning_effort: str,
                    timeout_s: float, ledger: Ledger) -> list[dict]:
    dataset = cases[0].dataset.name
    profile_id = f"curve-{arm}-{dataset.lower()}-{repeat}"
    learn, held = split(cases)
    staged = isolation(cases[0], repo, mount_root, profile_id, image=image)
    records, feedback = [], 0
    try:
        response = await client.put(f"/v1/profiles/{profile_id}", json={
            "models": [{"provider": provider, "model": model}], "sandbox": staged.policy, "soul": "", "skills": [],
            "memory": True, "learning": arm == "learning"})
        if response.status_code != 200:
            return [{"kind": "sequence", "arm": arm, "dataset": dataset, "repeat": repeat, "expected": len(cases),
                     "ran": 0, "learned_skills": 0, "observations": 0, "feedback": 0,
                     "error": f"profile: {response.status_code}"}]
        reviewer = MemoryReviewer(keel.store, keel.providers, DRAIN)
        dreamer = Dreamer(keel.store, keel.providers, FORCE)
        for phase, chosen in (("learn", learn), ("heldout", held)):
            for case in chosen:
                charging.switch(f"{arm}/{case.case_id}#{repeat}")
                prompt = build(case, repo, staged.stores)
                if not prompt.clean():
                    records.append({"kind": "task", "arm": arm, "dataset": dataset, "repeat": repeat,
                                    "phase": phase, "case": case.case_id, "status": "blocked", "passed": False})
                    continue
                if ledger.remaining < 5_000:
                    records.append({"kind": "task", "arm": arm, "dataset": dataset, "repeat": repeat,
                                    "phase": phase, "case": case.case_id, "status": "budget_exhausted",
                                    "passed": False})
                    continue
                outcome = await _task(client, profile_id, f"{profile_id}-{case.query.slug}", prompt.text,
                                      provider=provider, model=model, reasoning_effort=reasoning_effort,
                                      timeout_s=timeout_s)
                messages = outcome.get("messages") or []
                answer = extract_answer(messages).text
                passed = bool(scoring.score(case, repo, answer)["official_validator_pass"])
                run = outcome.get("run") or {}
                records.append({"kind": "task", "arm": arm, "dataset": dataset, "repeat": repeat, "phase": phase,
                                "case": case.case_id, "status": outcome["status"], "passed": passed,
                                "tokens": _tokens(run.get("usage")), "duration_s": outcome.get("duration_s"),
                                **metrics(messages)})
                _artifact(paths, arm, dataset, repeat, case, records[-1], messages)
                if phase == "learn" and run.get("run_id"):
                    rating, comment = FEEDBACK[passed]
                    sent = await client.put(f"/v1/profiles/{profile_id}/runs/{run['run_id']}/feedback",
                                            json={"rating": rating, "comment": comment})
                    feedback += sent.status_code == 200
                    charging.switch(f"{arm}/{dataset}#{repeat}/review")
                    await _drain(reviewer)
                    if arm == "learning":
                        charging.switch(f"{arm}/{dataset}#{repeat}/dream")
                        await _drain(dreamer)
        learned = await keel.store.learned_skills(profile_id)
        observations = await keel.store.observations(profile_id, limit=1000)
        records.append({"kind": "sequence", "arm": arm, "dataset": dataset, "repeat": repeat,
                        "expected": len(cases), "ran": sum(r.get("kind") == "task" for r in records),
                        "learned_skills": len(learned), "observations": len(observations), "feedback": feedback,
                        "skills": [{"name": s.name, "state": s.state, "version": s.version} for s in learned]})
        return records
    finally:
        await _retire_profile(client, profile_id)
        staged.cleanup()


async def execute_curve(cases: list[Case], *, repo: Path, paths: Paths, ledger: Ledger, mount_root: Path,
                        admin_dsn: str, provider: str, model: str, repeats: int = 3, image: str = DEFAULT_IMAGE,
                        sandbox: str = "docker", providers=None, reasoning_effort: str = "low",
                        timeout_s: float = 1800.0, max_output: int = 8192, max_iterations: int = 60) -> list[dict]:
    datasets: dict[str, list[Case]] = {}
    for case in cases:
        if case.dataset.service_dbms:
            raise CurveError(f"{case.dataset.name} needs database services; the learning curve takes file-backed "
                             "datasets only")
        datasets.setdefault(case.dataset.name, []).append(case)
    for chosen in datasets.values():
        split(chosen)  # refuse a dataset that cannot be split before spending anything
    if sandbox != "none" and managed_containers():
        raise CurveError("managed sandbox containers already exist; remove them before running the curve")
    records: list[dict] = []
    async with temporary_database(admin_dsn) as database_url:
        settings = replace(_settings(database_url, mount_root=mount_root, docker="docker",
                                     max_iterations=max_iterations, openai_base_url=None, sandbox=sandbox,
                                     require_key=providers is None),
                           memory_review_workers=0)  # only the curve drives reviews and dreams
        charging = Charging(ledger)
        registry = _budgeted(providers if providers is not None else default_providers(settings), charging,
                             max_output)
        app = create_service(settings, providers=registry)
        async with app.router.lifespan_context(app):
            keel = app.state.keel
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://keel",
                                         headers={"Authorization": "Bearer keel-dab-eval"}) as client:
                for repeat in range(repeats):
                    for name in sorted(datasets):
                        for arm in ARMS:
                            records += await _sequence(client, keel, charging, datasets[name], arm=arm,
                                                       repeat=repeat, paths=paths, repo=repo, mount_root=mount_root,
                                                       image=image, provider=provider, model=model,
                                                       reasoning_effort=reasoning_effort, timeout_s=timeout_s,
                                                       ledger=ledger)
    return records


def _mean(values) -> float | None:
    found = [value for value in values if value is not None]
    return statistics.fmean(found) if found else None


def summarize_curve(records: list[dict]) -> dict:
    tasks = [r for r in records if r.get("kind") == "task"]
    sequences = [r for r in records if r.get("kind") == "sequence"]
    summary: dict = {"errors": sum(r["status"] in ERROR_STATUSES for r in tasks) + sum("error" in s
                                                                                         for s in sequences),
                     "complete": bool(sequences) and all(s.get("ran") == s.get("expected") for s in sequences)}
    for arm in ARMS:
        held = [r for r in tasks if r["arm"] == arm and r["phase"] == "heldout"]
        per_repeat = []
        for repeat in sorted({r["repeat"] for r in held}):
            mine = [r for r in held if r["repeat"] == repeat]
            per_dataset = [statistics.fmean(r["passed"] for r in mine if r["dataset"] == name)
                           for name in sorted({r["dataset"] for r in mine})]
            per_repeat.append(statistics.fmean(per_dataset))
        ran = [r for r in held if r["status"] not in ERROR_STATUSES]
        summary[arm] = {"heldout_pass": _mean(per_repeat) or 0.0,
                        "heldout_pass_sd": statistics.stdev(per_repeat) if len(per_repeat) > 1 else 0.0,
                        "repeats": len(per_repeat), "heldout_runs": len(held),
                        "iterations": _mean(r.get("iterations") for r in ran) or 0.0,
                        "tool_errors": _mean(r.get("tool_errors") for r in ran) or 0.0,
                        "tokens": _mean(r.get("tokens") for r in ran),
                        "learned_skills": _mean(s.get("learned_skills") for s in sequences if s["arm"] == arm)}
    return summary
