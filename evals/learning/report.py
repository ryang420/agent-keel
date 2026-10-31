"""Writes the learning evaluation's results.json and report.md: the conversations (per-arm rates, corrections,
negatives learned, exposure, attribution) and the learning curve (held-out pass, iterations, tool errors, tokens)."""

from __future__ import annotations

import json
from pathlib import Path

NOTE = "Correlations on few held-out tasks; not proof that a skill caused an outcome."


def _pct(entry: dict) -> str:
    return f"{entry['passed']}/{entry['total']} ({entry['rate']:.0%})"


def _num(value, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _yes(value: bool) -> str:
    return "yes" if value else "no"


def write_conversations(out: Path, summary: dict, outcomes, meta: dict) -> None:
    out = Path(out)
    (out / "results.json").write_text(json.dumps({"meta": meta, "summary": summary,
                                                  "outcomes": [o.__dict__ for o in outcomes]},
                                                 ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# Learning evaluation: conversations ({meta['protocol']})", "",
             f"- Model: `{meta['provider']}` / `{meta['model']}`", f"- keel: {meta['keel_version']}",
             *([f"- Region: {meta['region']}"] if meta.get("region") else []),
             f"- Started: {meta['started']}", f"- Scenarios: {meta['scenarios']}",
             f"- Suite sha256: `{meta.get('suite_sha256', 'unknown')}`", f"- Commit: `{meta.get('commit', 'unknown')}`",
             "", "| Arm | Positive | Negative | English | Chinese | Overall |", "|---|---|---|---|---|---|"]
    for arm, data in summary.items():
        groups, languages = data["groups"], data["languages"]
        lines.append(f"| {arm} | {_pct(groups['positive'])} | {_pct(groups['negative'])} | "
                     f"{_pct(languages['en'])} | {_pct(languages['zh'])} | {_pct(data['overall'])} |")
    lines += ["", "| Arm | Corrections | Negatives learned | Exposure | Attributable | Reversible | Errors |",
              "|---|---|---|---|---|---|---|"]
    for arm, data in summary.items():
        lines.append(f"| {arm} | {data['corrections']} | {data['negatives_learned']} | {data['exposure']} | "
                     f"{_yes(data['attributable'])} | {_yes(data['reversible'])} | {data['errors']} |")
    if "learning" in summary and meta.get("gate_applicable", True):
        verdict = "PASSED" if summary["learning"]["gate"]["passed"] else "FAILED"
        lines += ["", f"Conversation gate (learning arm): {verdict}. The combined release gate also needs the "
                      "learning curve (`gate` command)."]
    elif "learning" in summary:
        lines += ["", "Gate: not applicable (subset run)"]
    errors = [o for o in outcomes if any(f.startswith("error:") for f in o.failures)]
    if errors:
        lines += ["", f"Cases that errored (counted as failures): {len(errors)}"]
        lines += [f"- {o.scenario_id} ({o.arm}): {o.failures[0]}" for o in errors]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_curve(out: Path, summary: dict, records: list[dict], meta: dict) -> None:
    out = Path(out)
    (out / "records.json").write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
    (out / "results.json").write_text(json.dumps({"meta": meta, "summary": summary}, indent=2), encoding="utf-8")
    lines = [f"# Learning evaluation: curve ({meta['protocol']})", "",
             f"- Model: `{meta['provider']}` / `{meta['model']}`", f"- keel: {meta['keel_version']}",
             f"- Started: {meta['started']}", f"- Datasets: {', '.join(meta['datasets'])}",
             f"- Repeats: {meta['repeats']}", f"- Commit: `{meta.get('commit', 'unknown')}`", "",
             "| Arm | Held-out pass | Held-out errors | Iterations | Tool errors | Tokens | Overhead tokens | "
             "Learned skills |", "|---|---|---|---|---|---|---|---|"]
    for arm in ("learning", "baseline"):
        data = summary[arm]
        lines.append(f"| {arm} | {data['heldout_pass']:.0%} ± {data['heldout_pass_sd']:.0%} "
                     f"({data['repeats']} repeats) | {data['heldout_errors']} | {_num(data['iterations'])} | "
                     f"{_num(data['tool_errors'])} | {_num(data['tokens'], 0)} | "
                     f"{_num(data['overhead_tokens'], 0)} | {_num(data['learned_skills'])} |")
    lines += ["", f"Complete: {_yes(summary['complete'])}; errors: {summary['errors']}.", "",
              "| Dataset | Arm | Held-out pass | Held-out runs | Iterations |", "|---|---|---|---|---|"]
    held = [r for r in records if r.get("kind") == "task" and r["phase"] == "heldout"
            and r["status"] not in ("setup_failed", "blocked", "budget_exhausted")]
    for name in sorted({r["dataset"] for r in held}):
        for arm in ("learning", "baseline"):
            mine = [r for r in held if r["dataset"] == name and r["arm"] == arm]
            if mine:
                passed = sum(r["passed"] for r in mine)
                iterations = [r["iterations"] for r in mine if r.get("iterations") is not None]
                lines.append(f"| {name} | {arm} | {passed}/{len(mine)} ({passed / len(mine):.0%}) | {len(mine)} | "
                             f"{_num(sum(iterations) / len(iterations) if iterations else None)} |")
    lines += ["", NOTE]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
