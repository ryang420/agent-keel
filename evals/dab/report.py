"""Assemble the DAB execution report from prepared states, run records and scores.

The report keeps blocked cases, budget stops and agent failures distinct, reports Pass@1 with the dataset-mean
aggregation and never presents a smoke subset as a leaderboard score.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import PROTOCOL
from .scoring import aggregate
from .suite import Case, Suite

EXECUTED, SCORED = "executed", "scored"


def build(*, suite: Suite, cases: list[Case], prepared: dict | None, records: list[dict], scores: list[dict],
          keel: dict, image: dict, config: dict) -> dict:
    """Merge every source into one report.

    A case is ``blocked`` when preparation failed, ``budget_exhausted`` when it never started, and otherwise
    ``executed``; a score exists only for an executed case and always counts, including a missing answer.
    """
    by_case = {record["case"]: record for record in records}
    by_score = {score["case"]: score for score in scores}
    rows: list[dict] = []
    for case in cases:
        row = {"case": case.case_id, "state": "planned"}
        prior = (prepared or {}).get("cases", {}).get(case.case_id)
        if prior is not None and prior.get("status") != "ready":
            row.update(state="blocked", reason=prior.get("reason"))
        elif prior is not None:
            row["state"] = "ready"
        record = by_case.get(case.case_id)
        if record is not None:
            row["state"] = record["status"]
            row["record"] = record
            if record["status"] == EXECUTED:
                score = by_score.get(case.case_id)
                if score is not None:
                    row["state"] = SCORED
                    row["score"] = score
                else:
                    row["score"] = None
        rows.append(row)
    executed = [row for row in rows if row["state"] == SCORED]
    summary = aggregate([{"case": row["case"], "status": EXECUTED,
                          "official_validator_pass": bool(row["score"]["official_validator_pass"])}
                         for row in executed])
    counts = {
        "selected": len(cases),
        "scored": len(executed),
        "passed": sum(1 for row in executed if row["score"]["official_validator_pass"]),
        "failed": sum(1 for row in executed if not row["score"]["official_validator_pass"]),
        "answer_missing": sum(1 for row in executed if row["score"].get("reason") == "answer_missing"),
        "answer_fallback": sum(1 for row in executed if row["score"].get("answer_source") == "final_message"),
        "validator_errors": sum(1 for row in executed if row["score"].get("validator_error")),
        "blocked": sum(1 for row in rows if row["state"] == "blocked"),
        "budget_exhausted": sum(1 for row in rows if row["state"] == "budget_exhausted"),
        "setup_failed": sum(1 for row in rows if row["state"] == "setup_failed"),
        "agent_failed": sum(1 for row in rows if row["state"] == "failed"),
        "timeout": sum(1 for row in rows if row["state"] == "timeout"),
    }
    durations = [row["record"]["duration_s"] for row in executed if row["record"].get("duration_s") is not None]
    report = {
        "protocol": PROTOCOL,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "revision": suite.data["revision"],
        "smoke": sorted({case.dataset.name for case in cases}) != sorted(suite.datasets),
        "keel": keel,
        "image": image,
        "config": config,
        "counts": counts,
        "score": summary,
        "latency_seconds": {"mean": (sum(durations) / len(durations)) if durations else None,
                            "max": max(durations) if durations else None},
        "rows": [{key: value for key, value in row.items() if key != "record"} | {"record": _trim(row.get("record"))}
                 for row in rows],
        "notes": [
            "Pass@1 is the mean over datasets of each dataset's mean pass rate, over scored cases only.",
            "dab-score-v2 extraction: marker text when present, otherwise the final assistant message; the source is "
            "recorded per case.",
            "A smoke subset has its own denominator and is not a leaderboard score.",
            "Every provider call, including model titles, is charged per request by the budget wrapper.",
            "Official validator pass is not proof of derivation; inspect the run artifacts for provenance.",
        ],
    }
    return report


def _trim(record: dict | None) -> dict | None:
    if record is None:
        return None
    return {key: record.get(key) for key in ("status", "usage", "tool_calls", "duration_s", "error", "model",
                                             "prompt_sha256", "artifacts", "answer_source")}


def markdown(report: dict) -> str:
    """A compact human-readable rendering, with the denominator stated next to every score."""
    counts, score = report["counts"], report["score"]
    lines = [f"# DAB {PROTOCOL} report", "",
             f"Revision `{report['revision']}`, Keel `{report['keel'].get('revision')}`"
             f" (dirty: {report['keel'].get('dirty')}), image `{report['image'].get('id')}`.", "",
             f"Model `{report['config'].get('model')}` ({report['config'].get('reasoning_effort')}).", "",
             "## Result", "", "| metric | value |", "|---|---|"]
    lines += [f"| {key} | {value} |" for key, value in counts.items()]
    lines += [f"| pass@1 (dataset mean) | {_fmt(score['pass_at_1'])} |",
              f"| executed queries | {score['executed_queries']} |", ""]
    if report["smoke"]:
        lines += ["> Smoke subset: this aggregate is not a leaderboard score.", ""]
    lines += ["## Datasets", "", "| dataset | queries | pass rate |", "|---|---|---|"]
    for name, row in score["datasets"].items():
        lines.append(f"| {name} | {row['queries']} | {_fmt(row['pass_rate'])} |")
    lines += ["", "## Cases", "", "| case | state | pass | reason |", "|---|---|---|---|"]
    for row in report["rows"]:
        score_row = row.get("score") or {}
        lines.append(f"| {row['case']} | {row['state']} | {score_row.get('official_validator_pass', '')} | "
                     f"{score_row.get('reason') or row.get('reason') or ''} |")
    lines += ["", "## Notes", ""] + [f"- {note}" for note in report["notes"]] + [""]
    return "\n".join(lines)


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.4f}"


def write(root: Path, report: dict) -> Path:
    """Write ``report.json`` and ``report.md`` beside the run artifacts."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path = root / "report.md"
    path.write_text(markdown(report), encoding="utf-8")
    return path
