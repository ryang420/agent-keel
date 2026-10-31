"""Official DAB scoring, outside every sandbox.

The pinned ``validate.py`` is imported from the evaluator tree and called with the answer string only. Gold stays
here; a sandbox never sees this module or its inputs. Pass@1 follows upstream: the mean over datasets of each
dataset's mean pass rate.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from .suite import Case


class ScoringError(RuntimeError):
    """The pinned validator could not be imported."""


def _load_validator(path: Path, repo: Path):
    """Import a pinned validator with the DAB checkout importable.

    Nine validators import helpers from ``common_scaffold``; upstream runs them with the repository root on
    ``sys.path``, so the evaluator does the same. Gold and validator code stay outside every sandbox.
    """
    root = str(repo.resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    name = f"dab_validate_{path.parent.parent.name}_{path.parent.name}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ScoringError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # a broken pinned validator is an evaluator failure, not a query failure
        raise ScoringError(f"{path}: {exc}") from exc
    return module


def score(case: Case, repo: Path, answer: str | None, *, tool_calls: int = 0, duration_s: float | None = None) -> dict:
    """Score one answer with the pinned validator.

    A missing marker or empty answer is a recorded failure. A validator that raises is also a failure, with the
    error kept distinct so the report shows an evaluator problem rather than a model problem.
    """
    record = {"case": case.case_id, "answer": answer, "official_validator_pass": False, "reason": None,
              "validator_error": None, "tool_calls": tool_calls, "duration_s": duration_s}
    if not answer or not answer.strip():
        record["reason"] = "answer_missing"
        return record
    query_dir = case.query_dir(repo)
    try:
        module = _load_validator(query_dir / "validate.py", repo)
        passed, reason = module.validate(answer)
    except ScoringError as exc:
        record["validator_error"] = str(exc)
        record["reason"] = "validator_error"
        return record
    except Exception as exc:  # noqa: BLE001 - a crashing validator is recorded, never a crash of the run
        record["validator_error"] = f"{type(exc).__name__}: {exc}"
        record["reason"] = "validator_error"
        return record
    record["official_validator_pass"] = bool(passed)
    record["reason"] = str(reason)
    return record


def aggregate(records: list[dict]) -> dict:
    """Pass@1 as the mean over datasets of each dataset's mean pass rate, plus the fixed denominator.

    Runs stopped by the budget or blocked by setup are reported in their own counts and excluded here; the
    denominator change is explicit so a partial run can never be presented as a full Pass@1.
    """
    by_dataset: dict[str, list[bool]] = {}
    for record in records:
        if record.get("status") != "executed":
            continue
        dataset = record["case"].split("/", 1)[0]
        by_dataset.setdefault(dataset, []).append(bool(record["official_validator_pass"]))
    rates = {name: sum(values) / len(values) for name, values in sorted(by_dataset.items())}
    executed = sum(len(values) for values in by_dataset.values())
    passed = sum(sum(values) for values in by_dataset.values())
    return {
        "protocol": "dataset-mean Pass@1",
        "pass_at_1": (sum(rates.values()) / len(rates)) if rates else None,
        "datasets": {name: {"pass_rate": rate, "queries": len(by_dataset[name])}
                     for name, rate in rates.items()},
        "executed_queries": executed,
        "passed_queries": passed,
        "denominator": "executed queries grouped by dataset; smoke subsets are not leaderboard scores",
    }
