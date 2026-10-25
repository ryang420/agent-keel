"""Drive one fresh profile, session and run per DAB query through the current Keel runtime.

This is the normal lifecycle: a profile with a sandbox policy, one session, one user turn, real model responses,
tool calls, a terminal transition and the stored messages. It is a serial evaluator, not a benchmark of concurrency.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import httpx

from keel.config import ServiceCredential, Settings
from keel.service import create_service, default_providers

from . import PROTOCOL
from .accounting import BudgetProvider, Charging
from .budget import Ledger
from .inputs import Paths, keel_state
from .policy import DEFAULT_IMAGE, Isolation, isolation, profile_id_for
from .prompt import ANSWER_MARKER, Prompt, build
from .suite import Case

TERMINAL = frozenset({"completed", "failed", "stopped"})
MIN_RUN_FLOOR = 5_000  # do not start a run with less than the first request could need
MANAGED_LABEL = "keel.managed=1"


class RunnerError(RuntimeError):
    """The evaluation runtime could not be prepared."""


@dataclass(frozen=True)
class Execution:
    """One executed case's observable outcome."""

    record: dict
    artifacts: Path


@dataclass(frozen=True)
class Extracted:
    """The answer text and where it came from: the marker, or the final message itself."""

    text: str | None
    source: str | None  # "marker", "final_message" or None


def extract_answer(messages: list[dict]) -> Extracted:
    """Extract the declared answer from the last assistant message without tool calls.

    The ``ANSWER:`` marker is preferred. A final message without it is still the model's answer under
    ``dab-score-v2`` and is used as-is, with the source recorded so marker compliance stays visible. Earlier
    assistant text belongs to a tool-calling turn and is never eligible.
    """
    for message in reversed(messages):
        if message.get("role") != "assistant" or message.get("tool_calls"):
            continue
        content = message.get("content") or ""
        for line in reversed(content.splitlines()):
            stripped = line.strip()
            if stripped.startswith(ANSWER_MARKER):
                return Extracted(stripped[len(ANSWER_MARKER):].strip() or None, "marker")
        final = content.strip()
        return Extracted(final or None, "final_message" if final else None)
    return Extracted(None, None)


def tool_calls(messages: list[dict]) -> int:
    return sum(len(message.get("tool_calls") or ()) for message in messages)


def image_id(image: str) -> str:
    """The local image ID, recorded so a score is tied to exact bytes. Missing images fail before any run."""
    try:
        result = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", image],
                                capture_output=True, text=True, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RunnerError(f"cannot inspect the evaluation image {image!r}: {exc}") from exc
    return result.stdout.strip()


def managed_containers() -> list[str]:
    try:
        result = subprocess.run(["docker", "ps", "-aq", "--filter", f"label={MANAGED_LABEL}"],
                                capture_output=True, text=True, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


@contextlib.asynccontextmanager
async def temporary_database(admin_dsn: str):
    """A private PostgreSQL database per execution, dropped afterwards; the base database is untouched."""
    name = "keel_dab_" + uuid.uuid4().hex
    admin = await asyncpg.connect(admin_dsn, timeout=10)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    parts = urlsplit(admin_dsn)
    dsn = urlunsplit(parts._replace(path="/" + name))
    try:
        yield dsn
    finally:
        admin = await asyncpg.connect(admin_dsn, timeout=10)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


def _settings(database_url: str, *, mount_root: Path, docker: str, max_iterations: int,
              openai_base_url: str | None, sandbox: str = "docker", require_key: bool = True) -> Settings:
    key = (os.environ.get("KEEL_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if require_key and not key:
        raise RunnerError("set KEEL_OPENAI_API_KEY (or OPENAI_API_KEY) for execution")
    return Settings(
        credentials=(ServiceCredential(1, "keel-dab-eval"),), database_url=database_url, sandbox=sandbox,
        docker=docker, sandbox_mount_root=str(mount_root), workers=1, max_running=1,
        max_iterations=max_iterations, openai_api_key=key or None,
        openai_base_url=openai_base_url or "https://api.openai.com/v1",
        sandbox_idle_seconds=1.0, sandbox_reap_seconds=1.0,
    )


async def _run_one(client: httpx.AsyncClient, case: Case, prompt: Prompt, *, profile_id: str, provider: str,
                   model: str, reasoning_effort: str, policy: dict, timeout_s: float) -> dict:
    """Create the profile, start the run, wait for a terminal state and read the transcript."""
    response = await client.put(f"/v1/profiles/{profile_id}",
                                json={"models": [{"provider": provider, "model": model}], "sandbox": policy,
                                      "soul": "", "skills": [], "memory": False})
    if response.status_code != 200:
        return {"status": "setup_failed", "error": f"profile: {response.status_code} {response.text[:500]}"}
    session_id = f"{profile_id}-s"
    response = await client.post(f"/v1/profiles/{profile_id}/runs",
                                 json={"input": prompt.text, "session_id": session_id, "provider": provider,
                                       "model": model, "reasoning_effort": reasoning_effort})
    if response.status_code != 202:
        return {"status": "setup_failed", "error": f"run: {response.status_code} {response.text[:500]}"}
    run_id = response.json()["run_id"]
    started = time.monotonic()
    deadline = started + timeout_s
    run: dict = {}
    while True:
        run = (await client.get(f"/v1/profiles/{profile_id}/runs/{run_id}")).json()
        if run["status"] in TERMINAL:
            break
        if time.monotonic() > deadline:
            await client.post(f"/v1/profiles/{profile_id}/runs/{run_id}/stop")
            for _ in range(100):
                run = (await client.get(f"/v1/profiles/{profile_id}/runs/{run_id}")).json()
                if run["status"] in TERMINAL:
                    break
                await asyncio.sleep(0.2)
            duration = time.monotonic() - started
            messages = (await client.get(f"/v1/profiles/{profile_id}/sessions/{session_id}/messages",
                                         params={"limit": 500})).json()["data"]
            cleanup = await _retire_profile(client, profile_id)
            return {"status": "timeout", "run": run, "messages": messages, "duration_s": duration,
                    "error": f"run exceeded {timeout_s}s", "cleanup": cleanup}
        await asyncio.sleep(0.5)
    messages = (await client.get(f"/v1/profiles/{profile_id}/sessions/{session_id}/messages",
                                 params={"limit": 500})).json()["data"]
    cleanup = await _retire_profile(client, profile_id)
    return {"status": "executed" if run["status"] == "completed" else "failed", "run": run, "messages": messages,
            "duration_s": time.monotonic() - started, "error": run.get("error"), "cleanup": cleanup}


async def _retire_profile(client: httpx.AsyncClient, profile_id: str) -> dict:
    """Fence, drain, retire and purge the profile so its sandbox is removed now, not by a later idle pass."""
    operation_id = str(uuid.uuid4())
    response = await client.request("DELETE", f"/v1/profiles/{profile_id}",
                                    json={"operation_id": operation_id})
    if response.status_code == 404:
        return {"status": "absent"}
    if response.status_code not in (200, 202):
        return {"status": "failed", "code": response.status_code, "detail": response.text[:300]}
    completed = await client.post(f"/v1/profiles/{profile_id}/deletion/{operation_id}/complete")
    return {"status": "retired" if completed.status_code == 200 else "fenced",
            "complete": completed.status_code}


def _artifact_dir(paths: Paths, case: Case, repetition: int) -> Path:
    dataset, query = case.case_id.split("/", 1)
    return paths.runs() / dataset / query / f"rep-{repetition}"


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _meta(case: Case, repetition: int, prompt: Prompt, *, image: str, image_digest: str, policy: dict,
          provider: str, model: str, reasoning_effort: str, keel: dict) -> dict:
    return {"protocol": PROTOCOL, "case": case.case_id, "repetition": repetition,
            "provider": provider, "model": model, "reasoning_effort": reasoning_effort,
            "prompt_sha256": prompt.sha256, "prompt_leaks": list(prompt.leaks),
            "image": image, "image_id": image_digest, "sandbox": policy, "keel": keel}


async def execute(cases: list[Case], *, paths: Paths, repo: Path, ledger: Ledger, image: str = DEFAULT_IMAGE,
                  mount_root: Path, admin_dsn: str, provider: str = "openai", model: str = "gpt-6-luna",
                  reasoning_effort: str = "low", repetition: int = 0, max_output: int = 8192,
                  timeout_s: float = 1800.0, docker: str = "docker",
                  openai_base_url: str | None = None, max_iterations: int = 60,
                  sandbox: str = "docker", providers=None) -> list[dict]:
    """Execute the selected cases serially and return one record per case.

    Records are written as artifacts before being returned, so a crash later still leaves the evidence. A case that
    cannot be isolated or would exceed the budget is recorded as ``blocked`` or ``budget_exhausted`` rather than
    dropped.
    """
    leftover = managed_containers()
    if leftover:
        raise RunnerError(f"{len(leftover)} managed sandbox container(s) already exist ({', '.join(leftover)}); "
                          "remove them before executing so admission is not blocked and no run is measured "
                          "against unknown inventory")
    digest = image_id(image) if sandbox != "none" else None
    keel = keel_state(Path.cwd())
    records: list[dict] = []
    async with temporary_database(admin_dsn) as database_url:
        settings = _settings(database_url, mount_root=mount_root, docker=docker, max_iterations=max_iterations,
                             openai_base_url=openai_base_url, sandbox=sandbox, require_key=providers is None)
        charging = Charging(ledger)
        inner = providers if providers is not None else default_providers(settings)
        registry = _budgeted(inner, charging, max_output)
        app = create_service(settings, providers=registry)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://keel",
                                         headers={"Authorization": "Bearer keel-dab-eval"}) as client:
                for case in cases:
                    charging.switch(case.case_id)
                    records.append(await _case(client, case, paths=paths, repo=repo, ledger=ledger, image=image,
                                               image_digest=digest, mount_root=mount_root,
                                               provider=provider, model=model,
                                               reasoning_effort=reasoning_effort, repetition=repetition,
                                               timeout_s=timeout_s, keel=keel))
    await _wait_for_cleanup()
    return records


async def _case(client: httpx.AsyncClient, case: Case, *, paths: Paths, repo: Path, ledger: Ledger, image: str,
                image_digest: str, mount_root: Path, provider: str, model: str, reasoning_effort: str,
                repetition: int, timeout_s: float, keel: dict) -> dict:
    profile_id = profile_id_for(case, repetition)
    try:
        staged: Isolation = isolation(case, repo, mount_root, profile_id, image=image)
    except Exception as exc:  # noqa: BLE001 - isolation failures are blocked cases, not crashed suites
        return {"case": case.case_id, "status": "blocked", "reason": str(exc)}
    try:
        prompt = build(case, repo, staged.stores)
        if not prompt.clean():
            return {"case": case.case_id, "status": "blocked", "reason": f"prompt leaks: {list(prompt.leaks)}"}
        if ledger.remaining < MIN_RUN_FLOOR:
            return {"case": case.case_id, "status": "budget_exhausted",
                    "reason": f"{ledger.remaining} tokens remain, less than the run floor"}
        outcome = await _run_one(client, case, prompt, profile_id=profile_id, provider=provider, model=model,
                                 reasoning_effort=reasoning_effort, policy=staged.policy, timeout_s=timeout_s)
        usage = (outcome.get("run") or {}).get("usage") or {}
        status = outcome["status"]
        if status == "failed" and "budget" in (outcome.get("error") or "").lower():
            status = "budget_exhausted"
        artifacts = _artifact_dir(paths, case, repetition)
        artifacts.mkdir(parents=True, exist_ok=True)
        _write(artifacts / "prompt.txt", prompt.text)
        messages = outcome.get("messages") or []
        _write(artifacts / "messages.json", json.dumps(messages, indent=2))
        _write(artifacts / "run.json", json.dumps(outcome.get("run") or {}, indent=2))
        extracted = extract_answer(messages)
        _write(artifacts / "answer.txt", extracted.text or "")
        _write(artifacts / "meta.json", json.dumps(_meta(
            case, repetition, prompt, image=image, image_digest=image_digest, policy=staged.policy,
            provider=provider, model=model, reasoning_effort=reasoning_effort, keel=keel), indent=2))
        return {"case": case.case_id, "status": status, "answer": extracted.text,
                "answer_source": extracted.source, "usage": usage or None,
                "tool_calls": tool_calls(messages), "duration_s": outcome.get("duration_s"),
                "error": outcome.get("error"), "prompt_sha256": prompt.sha256, "artifacts": str(artifacts),
                "model": (outcome.get("run") or {}).get("model")}
    finally:
        staged.cleanup()


def _budgeted(inner, charging: Charging, max_output: int):
    """Wrap every provider in the registry so each call is charged against the ledger."""
    from keel.providers.base import ProviderRegistry

    registry = ProviderRegistry()
    for name in inner.names():
        registry.register(BudgetProvider(inner.get(name), charging, max_output=max_output))
    return registry


async def _wait_for_cleanup(timeout_s: float = 60.0) -> None:
    """Wait for sandbox reaping, then leave no container of this deployment behind."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not managed_containers():
            return
        await asyncio.sleep(1.0)


