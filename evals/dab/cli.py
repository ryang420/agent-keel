"""Command line for the DAB scoring harness: prepare, validate, execute, score and report.

Preparation and validation make no inference calls. Execution needs an explicit cumulative token budget and a
PostgreSQL admin DSN with ``CREATEDB``; it creates and drops a private database per execution.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from . import PROTOCOL
from . import inputs, report, runner, scoring
from .budget import BudgetError, Ledger
from .inputs import Paths
from .policy import DEFAULT_IMAGE
from .prompt import build as build_prompt
from .suite import Case, Suite

DEFAULT_ROOT = Path(".local/evals/dab")
DEFAULT_SUITE = Path("evals/dab/suite.json")


def _cases(suite: Suite, args: argparse.Namespace) -> list[Case]:
    if getattr(args, "all", False):
        return suite.cases()
    datasets = tuple(getattr(args, "datasets", ()) or ())
    return suite.cases(datasets) if datasets else suite.smoke()


def _paths(args: argparse.Namespace) -> Paths:
    return Paths(Path(args.root)).ensure()


def _admin_dsn(args: argparse.Namespace) -> str:
    dsn = (getattr(args, "database_admin_dsn", None) or os.environ.get("KEEL_DAB_ADMIN_DSN")
           or os.environ.get("KEEL_TEST_POSTGRES_DSN") or "").strip()
    if not dsn.startswith(("postgresql://", "postgres://")):
        raise SystemExit("set --database-admin-dsn, KEEL_DAB_ADMIN_DSN or KEEL_TEST_POSTGRES_DSN "
                         "to a PostgreSQL admin URL with CREATEDB")
    return dsn


def _summary(result: dict[str, dict]) -> str:
    counts: dict[str, int] = {}
    for row in result.values():
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return ", ".join(f"{name} {count}" for name, count in sorted(counts.items()))


def cmd_prepare(args: argparse.Namespace) -> int:
    suite = Suite.load(Path(args.suite))
    paths = _paths(args)
    cases = _cases(suite, args)
    prepared = inputs.prepare(paths, suite, cases, services=tuple(args.services))
    print(f"prepared {len(cases)} cases ({_summary(prepared['cases'])}) at {paths.prepared}")
    blocked = [f"{name}: {row.get('reason')}" for name, row in prepared["cases"].items()
               if row["status"] != inputs.READY]
    for line in blocked:
        print(f"  blocked {line}")
    return 0 if not blocked else 1


def cmd_validate(args: argparse.Namespace) -> int:
    suite = Suite.load(Path(args.suite))
    paths = _paths(args)
    cases = _cases(suite, args)
    result = inputs.readiness(paths, suite, cases, services=tuple(args.services))
    image_id = None
    if not args.skip_image:
        image_id = runner.image_id(args.image)
    prompt_hashes: dict[str, str] = {}
    problems: list[str] = []
    for case in cases:
        row = result[case.case_id]
        if row["status"] != inputs.READY:
            continue
        staged = None
        try:
            from .policy import isolation

            staged = isolation(case, paths.repo, Path(args.mount_root), f"validate-{abs(hash(case.case_id))}",
                               image=args.image)
            prompt = build_prompt(case, paths.repo, staged.stores)
        except Exception as exc:  # noqa: BLE001 - validation reports a blocked case instead of crashing
            result[case.case_id] = {"status": inputs.BLOCKED, "reason": str(exc)}
            problems.append(f"{case.case_id}: {exc}")
            continue
        finally:
            if staged is not None:
                staged.cleanup()
        if not prompt.clean():
            result[case.case_id] = {"status": inputs.BLOCKED, "reason": f"prompt leaks: {list(prompt.leaks)}"}
            problems.append(f"{case.case_id}: prompt leaks {list(prompt.leaks)}")
            continue
        prompt_hashes[case.case_id] = prompt.sha256
    budget = paths.budget.is_file()
    payload = {"protocol": PROTOCOL, "cases": result, "prompt_sha256": prompt_hashes,
               "image": args.image, "image_id": image_id, "budget_granted": budget, "problems": problems}
    (paths.root / "validation.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                                                encoding="utf-8")
    print(f"validated {len(cases)} cases ({_summary(result)}), image {image_id or 'skipped'}, "
          f"budget {'granted' if budget else 'not granted'}")
    for line in problems:
        print(f"  problem {line}")
    blocked = [case.case_id for case in cases if result[case.case_id]["status"] != inputs.READY]
    if blocked:
        print(f"  not ready: {', '.join(blocked)}")
        return 1
    if not budget:
        print("  note: execution will refuse until --max-tokens grants a budget")
    return 0


def cmd_execute(args: argparse.Namespace) -> int:
    suite = Suite.load(Path(args.suite))
    paths = _paths(args)
    cases = _cases(suite, args)
    result = inputs.readiness(paths, suite, cases, services=tuple(args.services))
    inputs.require_ready(result, cases)
    ledger = (Ledger.grant(paths.budget, args.max_tokens) if args.max_tokens
              else Ledger.open(paths.budget))
    admin = _admin_dsn(args)
    records = asyncio.run(runner.execute(
        cases, paths=paths, repo=paths.repo, ledger=ledger, image=args.image,
        mount_root=Path(args.mount_root), admin_dsn=admin, provider=args.provider, model=args.model,
        reasoning_effort=args.reasoning_effort, repetition=args.repetition, max_output=args.max_output,
        timeout_s=args.timeout, docker=args.docker, openai_base_url=args.openai_base_url,
        max_iterations=args.max_iterations))
    output = paths.runs() / "executed.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for record in records:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    leftovers = runner.managed_containers()
    print(f"executed {len(records)} cases ({', '.join(f'{k} {v}' for k, v in sorted(counts.items()))})")
    print(f"records: {output}")
    print(f"spent {ledger.spent_tokens}/{ledger.max_tokens} tokens")
    if leftovers:
        print(f"  warning: {len(leftovers)} managed container(s) still present")
        return 1
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    suite = Suite.load(Path(args.suite))
    paths = _paths(args)
    cases = {case.case_id: case for case in _cases(suite, args)}
    executed = paths.runs() / "executed.json"
    if not executed.is_file():
        raise SystemExit(f"{executed} is missing; run execute first")
    records = json.loads(executed.read_text(encoding="utf-8"))
    scores = []
    for record in records:
        case = cases.get(record["case"])
        if case is None or record["status"] not in ("executed", "failed", "timeout"):
            continue
        scored = _score_record(case, paths.repo, record)
        record["answer"], record["answer_source"] = scored["answer"], scored["answer_source"]
        scores.append(scored)
    executed.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output = paths.runs() / "scores.json"
    output.write_text(json.dumps(scores, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = scoring.aggregate([{**score, "status": "executed"} for score in scores])
    print(f"scored {len(scores)} cases, pass@1 {summary['pass_at_1']}")
    print(f"scores: {output}")
    return 0


def _score_record(case, repo: Path, record: dict) -> dict:
    """Re-extract from the artifacts when available, so a scoring-rule change reuses the same transcripts."""
    text, source = record.get("answer"), record.get("answer_source")
    artifacts = record.get("artifacts")
    if artifacts:
        messages = Path(artifacts) / "messages.json"
        if messages.is_file():
            extracted = runner.extract_answer(json.loads(messages.read_text(encoding="utf-8")))
            text, source = extracted.text, extracted.source
    scored = scoring.score(case, repo, text, tool_calls=record.get("tool_calls", 0),
                           duration_s=record.get("duration_s"))
    scored["answer_source"] = source
    return scored


def cmd_report(args: argparse.Namespace) -> int:
    suite = Suite.load(Path(args.suite))
    paths = _paths(args)
    cases = _cases(suite, args)
    prepared = inputs.load_prepared(paths) if paths.prepared.is_file() else None
    records = json.loads((paths.runs() / "executed.json").read_text(encoding="utf-8")) \
        if (paths.runs() / "executed.json").is_file() else []
    scores = json.loads((paths.runs() / "scores.json").read_text(encoding="utf-8")) \
        if (paths.runs() / "scores.json").is_file() else []
    image = {"name": args.image, "id": runner.image_id(args.image) if not args.skip_image else None}
    config = {"provider": args.provider, "model": args.model, "reasoning_effort": args.reasoning_effort,
              "provider_returned_model": sorted({record.get("model") for record in records if record.get("model")})}
    payload = report.build(suite=suite, cases=cases, prepared=prepared, records=records, scores=scores,
                           keel=inputs.keel_state(Path.cwd()), image=image, config=config)
    path = report.write(paths.runs(), payload)
    print(f"report: {path}")
    print(report.markdown(payload))
    return 0


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--suite", default=str(DEFAULT_SUITE), help="tracked suite manifest")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="ignored evaluator tree")
    parser.add_argument("--datasets", action="append", default=[], help="dataset id (repeatable); default smoke")
    parser.add_argument("--all", action="store_true", help="all 12 datasets / 54 queries")
    parser.add_argument("--services", action="append", default=[], choices=["postgres", "mongo"],
                        help="declare a database service available")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help="pinned evaluation image")
    parser.add_argument("--mount-root", default=str(DEFAULT_ROOT / "mounts"), help="KEEL_SANDBOX_MOUNT_ROOT")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.dab", description="DAB scoring harness for agent-keel")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="fetch and verify pinned inputs (no inference)")
    _common(prepare)
    prepare.set_defaults(func=cmd_prepare)

    validate = sub.add_parser("validate", help="offline readiness check (no inference)")
    _common(validate)
    validate.add_argument("--skip-image", action="store_true", help="do not inspect the Docker image")
    validate.set_defaults(func=cmd_validate)

    execute = sub.add_parser("execute", help="run the selected cases through Keel")
    _common(execute)
    execute.add_argument("--max-tokens", type=int, default=None, help="grant a fresh cumulative token budget")
    execute.add_argument("--database-admin-dsn", default=None, help="PostgreSQL admin URL with CREATEDB")
    execute.add_argument("--provider", default="openai")
    execute.add_argument("--model", default="gpt-6-luna")
    execute.add_argument("--reasoning-effort", default="low")
    execute.add_argument("--max-output", type=int, default=8192)
    execute.add_argument("--max-iterations", type=int, default=60)
    execute.add_argument("--timeout", type=float, default=1800.0, help="per-run wall clock limit")
    execute.add_argument("--repetition", type=int, default=0)
    execute.add_argument("--docker", default="docker")
    execute.add_argument("--openai-base-url", default=None)
    execute.set_defaults(func=cmd_execute)

    score = sub.add_parser("score", help="score executed answers with the pinned validators")
    _common(score)
    score.set_defaults(func=cmd_score)

    report_cmd = sub.add_parser("report", help="write report.json and report.md")
    _common(report_cmd)
    report_cmd.add_argument("--provider", default="openai")
    report_cmd.add_argument("--model", default="gpt-6-luna")
    report_cmd.add_argument("--reasoning-effort", default="low")
    report_cmd.add_argument("--skip-image", action="store_true")
    report_cmd.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (inputs.InputError, BudgetError, scoring.ScoringError, runner.RunnerError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
