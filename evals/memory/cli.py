"""Command line for the memory evaluation: validate the suite offline, or run it against one named model."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
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


def _region() -> str | None:
    env = os.environ
    return (env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "").strip() or None


def _commit() -> str:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10,
                                cwd=Path(__file__).resolve().parents[2], check=True)
        return result.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 - best effort audit field
        return "unknown"


def _settings(dsn: str) -> Settings:
    """Mirrors Settings.from_env for the model-facing fields; the rest is fixed for an in-process evaluation."""
    env = os.environ
    base_url = (env.get("KEEL_OPENAI_BASE_URL") or "").strip() or "https://api.openai.com/v1"
    if not base_url.lower().startswith(("http://", "https://")):
        raise SystemExit("KEEL_OPENAI_BASE_URL must start with http:// or https://")
    window = (env.get("KEEL_OPENAI_CONTEXT_WINDOW") or "").strip() or "128000"
    if not window.isdigit() or int(window) < 1:
        raise SystemExit("KEEL_OPENAI_CONTEXT_WINDOW must be a positive integer")
    return Settings(
        credentials=(ServiceCredential(1, "memory-eval"),), database_url=dsn, sandbox="none",
        aws_region=_region() or "us-east-1",  # only reached for bedrock when a region is set
        openai_api_key=(env.get("KEEL_OPENAI_API_KEY") or env.get("OPENAI_API_KEY") or "").strip() or None,
        openai_base_url=base_url, openai_compat=(env.get("KEEL_OPENAI_COMPAT") or "").strip().lower()
        in ("1", "true", "yes"), openai_context_window=int(window))


async def _run(args) -> int:
    from evals.dab.runner import temporary_database

    scenarios = load_suite(args.suite)
    subset = bool(args.scenarios)
    if args.scenarios:
        wanted = set(args.scenarios.split(","))
        scenarios = [s for s in scenarios if s.id in wanted]
    model = _model(args)
    if args.provider == "bedrock" and not _region():
        raise SystemExit("set AWS_REGION or AWS_DEFAULT_REGION for --provider bedrock; no region is assumed")
    started = datetime.now(UTC)
    out = Path(args.root) / "runs" / started.strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True, exist_ok=True)
    async with temporary_database(_admin_dsn(args)) as dsn:
        settings = _settings(dsn)
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
            "started": started.isoformat(), "scenarios": len(scenarios), "gate_applicable": not subset,
            "suite_sha256": hashlib.sha256(Path(args.suite).read_bytes()).hexdigest(), "commit": _commit()}
    if args.provider == "bedrock":
        meta["region"] = settings.aws_region
    outcomes = [Outcome(**{**item, "failures": tuple(item["failures"])})
                for item in json.loads((out / "outcomes.json").read_text(encoding="utf-8"))]
    write(out, summary, outcomes, meta)
    print(f"report: {out / 'report.md'}")
    errors = sum(data["gate"]["errors"] for data in summary.values())
    if subset or args.arm == "baseline":  # a subset never claims the gate
        return 0 if not errors else 1
    return 0 if summary["memory"]["gate"]["passed"] else 1


async def _bench(args) -> int:
    from evals.dab.runner import temporary_database

    from .bench import bench

    started = datetime.now(UTC)
    out = Path(args.root) / "bench" / started.strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True, exist_ok=True)
    async with temporary_database(_admin_dsn(args)) as dsn:
        store = await open_store(dsn)
        try:
            result = await bench(store, messages=args.messages, others=args.others, queries=args.queries)
        finally:
            await store.close()
    result["started"] = started.isoformat()
    (out / "bench.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    search, append = result["search_ms"], result["append_ms"]
    classes = "\n".join(
        f"  - {name}: {c['count']} queries, hit rate {c['hit_rate']:.2f}, p50 {c['p50']:.1f} ms, "
        f"p95 {c['p95']:.1f} ms, max {c['max']:.1f} ms" for name, c in result["search_by_class"].items())
    (out / "bench.md").write_text(
        f"# Search benchmark\n\n- {result['messages']} messages in one profile, {result['others']} in others\n"
        f"- Search tables total (text + tokens + indexes): {result['index_bytes'] / 2**20:.1f} MiB; backfill "
        f"{result['backfill_messages_per_s']:.0f} msg/s (batch {result['backfill_batch']})\n"
        f"- Append (indexed and raw alternate per iteration; the indexed path includes its transaction and the "
        f"session counter update): indexed mean {result['append_stats']['indexed']['mean']:.2f} / median "
        f"{result['append_stats']['indexed']['median']:.2f} / p95 {result['append_stats']['indexed']['p95']:.2f} ms; "
        f"raw mean "
        f"{result['append_stats']['raw']['mean']:.2f} / median {result['append_stats']['raw']['median']:.2f} / p95 "
        f"{result['append_stats']['raw']['p95']:.2f} ms; overhead (means) +{append['overhead']:.2f} ms\n"
        f"- session_search (warm cache: data just written, after VACUUM ANALYZE of the search tables): overall p50 "
        f"{search['p50']:.1f} ms, p95 {search['p95']:.1f} ms, max {search['max']:.1f} ms over {search['count']} "
        f"queries (query terms: latin 1-5 words, han 2-4 characters); target: every class p95 < "
        f"{result['target_p95_ms']} ms, latin and han hit rate > 0.5, absent "
        f"hit rate 0: {'met' if result['met'] else 'NOT met'}\n{classes}\n- Machine: {result['machine']}\n",
        encoding="utf-8")
    print(f"report: {out / 'bench.md'}")
    return 0 if result["met"] else 1


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
    bench_cmd = commands.add_parser("bench", help="lexical search benchmark on a private database")
    bench_cmd.add_argument("--messages", type=int, default=100_000)
    bench_cmd.add_argument("--others", type=int, default=20_000)
    bench_cmd.add_argument("--queries", type=int, default=500)
    bench_cmd.add_argument("--root", default=str(DEFAULT_ROOT))
    bench_cmd.add_argument("--database-admin-dsn")
    args = parser.parse_args(argv)
    if args.command == "validate":
        try:
            scenarios = load_suite(args.suite)
        except SuiteError as error:
            print(f"invalid suite: {error}", file=sys.stderr)
            return 1
        print(f"{len(scenarios)} scenarios OK ({PROTOCOL})")
        return 0
    if args.command == "bench":
        return asyncio.run(_bench(args))
    return asyncio.run(_run(args))
