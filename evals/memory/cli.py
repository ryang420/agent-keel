"""Command line for the memory evaluation: validate the suite offline, or run it against one named model."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
import os
from pathlib import Path
import sys

import keel
from keel.config import ServiceCredential, Settings
from keel.service import default_providers
from keel.store import open_store

from . import PROTOCOL
from .report import write
from .runner import evaluate
from .scoring import Outcome
from .suite import SuiteError, load_suite

DEFAULT_ROOT = Path(".local/evals/memory")
DEFAULT_SUITE = Path("evals/memory/suite.json")
_MODEL_ENV = {"bedrock": "KEEL_LIVE_MODEL", "openai": "KEEL_LIVE_OPENAI_MODEL"}


def _admin_dsn(args) -> str:
    dsn = (args.database_admin_dsn or os.environ.get("KEEL_MEMORY_EVAL_ADMIN_DSN")
           or os.environ.get("KEEL_TEST_POSTGRES_DSN") or "").strip()
    if not dsn.startswith(("postgres://", "postgresql://")):
        raise SystemExit("set --database-admin-dsn or KEEL_MEMORY_EVAL_ADMIN_DSN to a PostgreSQL URL with CREATEDB")
    return dsn


def _model(args) -> str:
    model = (args.model or os.environ.get(_MODEL_ENV[args.provider]) or "").strip()
    if not model:
        raise SystemExit(f"pass --model or set {_MODEL_ENV[args.provider]}; no model is guessed")
    return model


async def _run(args) -> int:
    from evals.dab.runner import temporary_database

    scenarios = load_suite(args.suite)
    if args.scenarios:
        wanted = set(args.scenarios.split(","))
        scenarios = [s for s in scenarios if s.id in wanted]
    model = _model(args)
    key = (os.environ.get("KEEL_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip() or None
    started = datetime.now(UTC)
    out = Path(args.root) / "runs" / started.strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True, exist_ok=True)
    async with temporary_database(_admin_dsn(args)) as dsn:
        settings = Settings(credentials=(ServiceCredential(1, "memory-eval"),), database_url=dsn, sandbox="none",
                            openai_api_key=key)
        providers = default_providers(settings)
        store = await open_store(dsn)
        try:
            arms = ("memory", "baseline") if args.arm == "both" else (args.arm,)
            summary = await evaluate(store, providers, scenarios, provider=args.provider, model=model, arms=arms,
                                     out=out)
        finally:
            await store.close()
            await providers.aclose()
    meta = {"protocol": PROTOCOL, "provider": args.provider, "model": model, "keel_version": keel.__version__,
            "started": started.isoformat(), "scenarios": len(scenarios)}
    outcomes = [Outcome(**{**item, "failures": tuple(item["failures"])})
                for item in json.loads((out / "outcomes.json").read_text(encoding="utf-8"))]
    write(out, summary, outcomes, meta)
    print(f"report: {out / 'report.md'}")
    return 0 if args.arm == "baseline" or summary.get("memory", {}).get("gate", {}).get("passed") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.memory", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="check the scenario file offline")
    validate.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    run = commands.add_parser("run", help="run the scenarios against one named model")
    run.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    run.add_argument("--provider", choices=sorted(_MODEL_ENV), required=True)
    run.add_argument("--model")
    run.add_argument("--arm", choices=("memory", "baseline", "both"), default="both")
    run.add_argument("--scenarios", help="comma-separated scenario ids (default: all)")
    run.add_argument("--root", default=str(DEFAULT_ROOT))
    run.add_argument("--database-admin-dsn")
    args = parser.parse_args(argv)
    if args.command == "validate":
        try:
            scenarios = load_suite(args.suite)
        except SuiteError as error:
            print(f"invalid suite: {error}", file=sys.stderr)
            return 1
        print(f"{len(scenarios)} scenarios OK ({PROTOCOL})")
        return 0
    return asyncio.run(_run(args))
