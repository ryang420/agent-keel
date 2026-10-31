"""Command line for the learning evaluation: validate the suite offline, run the conversations or the DAB learning
curve against one named model, and combine both into the release gate."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import keel
from keel.config import ServiceCredential, Settings
from keel.service import default_providers
from keel.store import open_store

from evals.dab.policy import DEFAULT_IMAGE

from . import PROTOCOL
from .gate import combine
from .report import write_conversations, write_curve
from .runner import evaluate
from .scoring import Outcome
from .suite import SuiteError, coverage, load_suite

DEFAULT_ROOT = Path(".local/evals/learning")
DEFAULT_SUITE = Path("evals/learning/suite.json")
TRACKED_SUITE = Path(__file__).resolve().parent / "suite.json"  # the reviewed suite a gated run must use
MAX_OUTPUT = 8192  # the output cap each model call reserves against the budget
DEFAULT_DAB_ROOT = Path(".local/evals/dab")
DEFAULT_DATASETS = "stockindex,music_brainz_20k,GITHUB_REPOS,stockmarket"  # file-backed, two or more learning tasks
_MODEL_ENV = {"bedrock": "KEEL_LIVE_MODEL", "openai": "KEEL_LIVE_OPENAI_MODEL"}


def _admin_dsn(args) -> str:
    dsn = (args.database_admin_dsn or os.environ.get("KEEL_LEARNING_EVAL_ADMIN_DSN")
           or os.environ.get("KEEL_TEST_POSTGRES_DSN") or "").strip()
    if not dsn.startswith(("postgres://", "postgresql://")):
        raise SystemExit("set --database-admin-dsn or KEEL_LEARNING_EVAL_ADMIN_DSN to a PostgreSQL URL with CREATEDB")
    return dsn


def _model(args, provider: str) -> str:
    model = (args.model or os.environ.get(_MODEL_ENV[provider]) or "").strip()
    if not model:
        raise SystemExit(f"pass --model or set {_MODEL_ENV[provider]}; no model is guessed")
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
        credentials=(ServiceCredential(1, "learning-eval"),), database_url=dsn, sandbox="none",
        aws_region=_region() or "us-east-1",  # only reached for bedrock when a region is set
        openai_api_key=(env.get("KEEL_OPENAI_API_KEY") or env.get("OPENAI_API_KEY") or "").strip() or None,
        openai_base_url=base_url, openai_compat=(env.get("KEEL_OPENAI_COMPAT") or "").strip().lower()
        in ("1", "true", "yes"), openai_context_window=int(window))


def _stamp(started: datetime) -> str:
    return started.strftime("%Y%m%dT%H%M%SZ")


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _applicability(args, scenarios) -> dict:
    """A conversations run claims the gate only when it is a full run of the tracked, reviewed suite and that suite is
    large and broad enough (design §10)."""
    full = not args.scenarios and args.arm == "both"
    sha = _sha256(args.suite)
    tracked = sha == _sha256(TRACKED_SUITE)
    covered = coverage(scenarios)
    reasons = [reason for reason, ok in (("subset run", full), ("not the tracked suite", tracked),
                                         ("the suite lacks 30 scenarios, both languages or all six kinds",
                                          covered["complete"])) if not ok]
    return {"full_run": full, "suite_sha256": sha, "suite_tracked": tracked, "suite_coverage": covered,
            "gate_applicable": not reasons, "not_applicable": reasons}


async def _run(args) -> int:
    from evals.dab.accounting import Charging
    from evals.dab.budget import Ledger
    from evals.dab.runner import _budgeted, temporary_database

    if args.max_tokens <= 0:
        raise SystemExit("--max-tokens must be a positive token budget")
    scenarios = load_suite(args.suite)
    applicability = _applicability(args, scenarios)
    if args.scenarios:
        wanted = {item for item in args.scenarios.split(",") if item}
        unknown = sorted(wanted - {s.id for s in scenarios})
        if unknown or not wanted:
            raise SystemExit(f"unknown scenario ids: {', '.join(unknown) or '(none given)'}")
        scenarios = [s for s in scenarios if s.id in wanted]
    model = _model(args, args.provider)
    if args.provider == "bedrock" and not _region():
        raise SystemExit("set AWS_REGION or AWS_DEFAULT_REGION for --provider bedrock; no region is assumed")
    admin = _admin_dsn(args)  # every refusal happens before anything is written
    started = datetime.now(UTC)
    out = Path(args.root) / "runs" / _stamp(started)
    out.mkdir(parents=True, exist_ok=True)
    ledger = Ledger.grant(out / "budget.json", args.max_tokens)
    charging = Charging(ledger)
    async with temporary_database(admin) as dsn:
        settings = _settings(dsn)
        providers = default_providers(settings)
        store = await open_store(dsn)
        try:
            arms = ("learning", "baseline") if args.arm == "both" else (args.arm,)
            summary = await evaluate(store, _budgeted(providers, charging, MAX_OUTPUT), scenarios,
                                     provider=args.provider, model=model, arms=arms, out=out, charging=charging)
        finally:
            await store.close()
            await providers.aclose()
    meta = {"protocol": PROTOCOL, "provider": args.provider, "model": model, "keel_version": keel.__version__,
            "started": started.isoformat(), "scenarios": len(scenarios), **applicability,
            "max_tokens": ledger.max_tokens, "spent_tokens": ledger.spent_tokens, "commit": _commit()}
    if args.provider == "bedrock":
        meta["region"] = settings.aws_region
    outcomes = [Outcome(**{**item, "failures": tuple(item["failures"])})
                for item in json.loads((out / "outcomes.json").read_text(encoding="utf-8"))]
    write_conversations(out, summary, outcomes, meta)
    print(f"report: {out / 'report.md'}")
    print(f"spent {ledger.spent_tokens}/{ledger.max_tokens} tokens")
    errors = sum(data["errors"] for data in summary.values())
    if not meta["gate_applicable"]:  # a subset or an unreviewed suite never claims the gate
        return 0 if not errors else 1
    return 0 if summary["learning"]["gate"]["passed"] else 1


async def _curve(args) -> int:
    from evals.dab import inputs
    from evals.dab.budget import Ledger
    from evals.dab.inputs import Paths
    from evals.dab.suite import Suite

    from .curve import execute_curve, summarize_curve

    model = _model(args, args.provider)
    region = _region() if args.provider == "bedrock" else None
    if args.provider == "bedrock" and not region:
        raise SystemExit("set AWS_REGION or AWS_DEFAULT_REGION for --provider bedrock; no region is assumed")
    admin = _admin_dsn(args)  # every refusal happens before the run directory and the budget ledger exist
    suite = Suite.load(Path("evals/dab/suite.json"))
    names = tuple(name for name in args.datasets.split(",") if name)
    cases = suite.cases(names)
    served = sorted({case.dataset.name for case in cases if case.dataset.service_dbms})
    if served:
        raise SystemExit(f"{', '.join(served)} need database services; the learning curve takes file-backed "
                         "datasets (SQLite/DuckDB) only")
    dab = Paths(Path(args.dab_root))
    inputs.require_ready(inputs.readiness(dab, suite, cases), cases)
    started = datetime.now(UTC)
    out = Path(args.root) / "curve" / _stamp(started)
    out.mkdir(parents=True, exist_ok=True)
    ledger = Ledger.grant(out / "budget.json", args.max_tokens)
    records = await execute_curve(cases, repo=dab.repo, paths=Paths(out), ledger=ledger, mount_root=out / "mounts",
                                  admin_dsn=admin, provider=args.provider, model=model, repeats=args.repeats,
                                  image=args.image, sandbox=args.sandbox, aws_region=region)
    summary = summarize_curve(records)
    meta = {"protocol": PROTOCOL, "provider": args.provider, "model": model, "keel_version": keel.__version__,
            "started": started.isoformat(), "datasets": list(names), "repeats": args.repeats,
            "max_tokens": args.max_tokens, "spent_tokens": ledger.spent_tokens, "commit": _commit()}
    write_curve(out, summary, records, meta)
    print(f"report: {out / 'report.md'}")
    print(f"spent {ledger.spent_tokens}/{ledger.max_tokens} tokens")
    return 0 if summary["complete"] and not summary["errors"] else 1


def _meta_problems(curve_meta, conversation_meta) -> list[str]:
    """The gate combines only results it can vouch for: both metas present, this protocol, the same model, and a
    conversations run that was full and used the tracked suite."""
    problems = [f"the {name} results have no meta" for name, meta in (("curve", curve_meta),
                                                                       ("conversations", conversation_meta))
                if not isinstance(meta, dict) or not meta]
    if problems:
        return problems
    for name, meta in (("curve", curve_meta), ("conversations", conversation_meta)):
        if meta.get("protocol") != PROTOCOL:
            problems.append(f"the {name} results are protocol {meta.get('protocol')!r}, not {PROTOCOL}")
    for field in ("provider", "model"):
        if curve_meta.get(field) != conversation_meta.get(field):
            problems.append(f"the curve and the conversations ran different {field}s "
                            f"({curve_meta.get(field)!r} and {conversation_meta.get(field)!r})")
    if conversation_meta.get("gate_applicable") is not True:
        reasons = conversation_meta.get("not_applicable") or ["it does not claim the gate"]
        problems.append(f"the conversations run is not gate-applicable: {', '.join(reasons)}")
    return problems


def _gate(args) -> int:
    try:
        curve_data = json.loads(Path(args.curve).read_text(encoding="utf-8"))
        conversation_data = json.loads(Path(args.conversations).read_text(encoding="utf-8"))
        curve, conversations = curve_data["summary"], conversation_data["summary"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"error: cannot read the result files: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    curve_meta, conversation_meta = curve_data.get("meta"), conversation_data.get("meta")
    problems = _meta_problems(curve_meta, conversation_meta)
    if not isinstance(conversations, dict) or not {"learning", "baseline"} <= set(conversations):
        problems.append("the conversations results need both the learning and the baseline arm")
    if not isinstance(curve, dict) or not curve.get("complete") or curve.get("errors"):
        problems.append("the curve results are not a complete, error-free run")
    if problems:
        print("error: the gate is refused: " + "; ".join(problems), file=sys.stderr)
        return 1
    verdict = combine(curve, conversations)
    record = {**verdict, "inputs": {"curve": str(args.curve), "conversations": str(args.conversations)},
              "meta": {"curve": curve_meta, "conversations": conversation_meta}}
    (Path(args.conversations).parent / "gate.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"gate {'PASSED' if verdict['passed'] else 'FAILED'}")
    print(f"  correction delta (learning - baseline): {verdict['correction_delta']:+d}")
    for name, ok in verdict["checks"].items():
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    return 0 if verdict["passed"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.learning", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="check the scenario file offline")
    validate.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    run = commands.add_parser("run", help="run the conversation scenarios against one named model")
    run.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    run.add_argument("--provider", choices=sorted(_MODEL_ENV), required=True)
    run.add_argument("--model")
    run.add_argument("--arm", choices=("learning", "baseline", "both"), default="both")
    run.add_argument("--max-tokens", type=int, required=True, help="cumulative token budget for this run")
    run.add_argument("--scenarios", help="comma-separated scenario ids (default: all)")
    run.add_argument("--root", default=str(DEFAULT_ROOT))
    run.add_argument("--database-admin-dsn")
    curve = commands.add_parser("curve", help="the DAB learning curve (openai only, needs a prepared DAB checkout)")
    curve.add_argument("--datasets", default=DEFAULT_DATASETS, help="comma-separated file-backed DAB datasets")
    curve.add_argument("--provider", choices=sorted(_MODEL_ENV), default="openai")
    curve.add_argument("--repeats", type=int, default=5)
    curve.add_argument("--max-tokens", type=int, required=True, help="cumulative token budget for this run")
    curve.add_argument("--model")
    curve.add_argument("--root", default=str(DEFAULT_ROOT))
    curve.add_argument("--database-admin-dsn")
    curve.add_argument("--dab-root", default=str(DEFAULT_DAB_ROOT))
    curve.add_argument("--sandbox", choices=("docker", "none"), default="docker")
    curve.add_argument("--image", default=DEFAULT_IMAGE)
    gate = commands.add_parser("gate", help="combine a curve and a conversations result into the release gate")
    gate.add_argument("--curve", type=Path, required=True)
    gate.add_argument("--conversations", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "validate":
        try:
            scenarios = load_suite(args.suite)
        except (SuiteError, ValueError) as error:
            print(f"invalid suite: {error}", file=sys.stderr)
            return 1
        print(f"{len(scenarios)} scenarios OK ({PROTOCOL})")
        return 0
    if args.command == "gate":
        return _gate(args)
    if args.command == "curve":
        from evals.dab import inputs
        from evals.dab.budget import BudgetError
        from evals.dab.suite import SuiteError as DabSuiteError

        from .curve import CurveError
        try:
            return asyncio.run(_curve(args))
        except (inputs.InputError, DabSuiteError, BudgetError, CurveError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
    return asyncio.run(_run(args))
