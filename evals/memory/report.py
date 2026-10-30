"""Writes the evaluation's results.json and a short report.md: model, protocol, per-arm group and language rates,
and the memory arm's gate (≥90% per group, zero exposure, zero unsupported preferences)."""

from __future__ import annotations

import json
from pathlib import Path


def _pct(entry: dict) -> str:
    return f"{entry['passed']}/{entry['total']} ({entry['rate']:.0%})"


def write(out: Path, summary: dict, outcomes, meta: dict) -> None:
    out = Path(out)
    (out / "results.json").write_text(json.dumps({"meta": meta, "summary": summary,
                                                  "outcomes": [o.__dict__ for o in outcomes]},
                                                 ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# Memory evaluation ({meta['protocol']})", "",
             f"- Model: `{meta['provider']}` / `{meta['model']}`", f"- keel: {meta['keel_version']}",
             *([f"- Region: {meta['region']}"] if meta.get("region") else []),
             f"- Started: {meta['started']}", f"- Scenarios: {meta['scenarios']}",
             f"- Suite sha256: `{meta.get('suite_sha256', 'unknown')}`", f"- Commit: `{meta.get('commit', 'unknown')}`", "",
             "| Arm | Preference | Recall | English | Chinese | Overall |", "|---|---|---|---|---|---|"]
    for arm, data in summary.items():
        groups, languages = data["groups"], data["languages"]
        lines.append(f"| {arm} | {_pct(groups['preference'])} | {_pct(groups['recall'])} | "
                     f"{_pct(languages['en'])} | {_pct(languages['zh'])} | {_pct(data['overall'])} |")
    lines += ["", "Recall passes by evidence (searched = the probe called session_search; memorised = the stored memory "
                  "held part of the answer):"]
    for arm, data in summary.items():
        evidence = data["recall_evidence"]
        lines.append(f"- {arm}: {evidence['passed']} recall passes, {evidence['searched']} used search, "
                     f"{evidence['memorised']} had the answer memorised")
    if "memory" in summary and meta.get("gate_applicable", True):
        gate = summary["memory"]["gate"]
        verdict = "PASSED" if gate["passed"] else "FAILED"
        lines += ["", f"Gate (memory arm): {verdict} — ≥{gate['rate']:.0%} per group, exposure {gate['exposure']}, "
                      f"unsupported preferences {gate['unsupported']}, errors {gate['errors']}."]
    elif "memory" in summary:
        lines += ["", "Gate: not applicable (subset run)"]
    errors = [o for o in outcomes if any(f.startswith("error:") for f in o.failures)]
    if errors:
        lines += ["", f"Cases that errored (counted as failures): {len(errors)}"]
        lines += [f"- {o.scenario_id} ({o.arm}): {o.failures[0]}" for o in errors]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
