"""Strict artifact outcomes; no fabricated score for a partially executed suite."""

from collections import Counter
import json

from . import PROTOCOL


def build(records: list[dict], selected: list[str], *, protocol: str = PROTOCOL) -> dict:
    by_name = {r['task']: r for r in records}
    if len(by_name) != len(records) or set(by_name) - set(selected) or len(set(selected)) != len(selected):
        raise ValueError('unknown or duplicate task records')
    rows = [by_name.get(name, {'task': name, 'status': 'planned', 'passed': False}) for name in selected]
    passed = sum(r.get('passed') is True for r in rows)
    steps = [step for r in rows for scenario in r.get('scenarios', []) for step in scenario.get('steps', [])]
    def scenario_complete(scenario):
        outcomes = scenario.get('steps', [])
        return bool(outcomes) and len(outcomes) == scenario.get('planned_steps') and all(
            step.get('execution', {}).get('status') == 'executed' and
            'evaluator_error' not in step.get('failures', []) for step in outcomes)
    complete = bool(rows) and all(r.get('status') == 'executed' and len(r.get('scenarios', [])) == 3 and
                                 all(scenario_complete(s) for s in r['scenarios']) for r in rows)
    return {'protocol': protocol, 'selected': len(selected), 'passed': passed, 'complete': complete,
            'strict_pass_rate': passed / len(selected) if selected else None,
            'states': dict(Counter(r.get('status', 'planned') for r in rows)), 'rows': rows,
            'verification_states': dict(Counter(s.get('execution', {}).get('status', 'unknown') for s in steps)),
            'notes': ['Synthetic data tasks; independent from DAB. A partial run has no full-suite score.',
                      'Fixture/checker and scripted-provider checks are not live-model quality results.',
                      'Token admission uses estimates; unknown reported usage stays null.']}


def markdown(report: dict) -> str:
    rate = report['strict_pass_rate']
    lines = ['# Data-artifact evaluation', '', f"Protocol: `{report['protocol']}`.", '',
             f"Selected: {report['selected']}; passed: {report['passed']}; complete: {report['complete']}.",
             f"Strict selected-task rate: {rate:.4f}." if rate is not None else 'No tasks selected.', '',
             '| task | status | strict pass | development | hidden | replay |', '|---|---|---|---|---|---|']
    for row in report['rows']:
        scenarios = row.get('scenarios', [])
        dev = next((s['passed'] for s in scenarios if s['name'] == 'development'), None)
        hidden = all(s['passed'] for s in scenarios[1:]) if len(scenarios) == 3 else None
        replay = hidden if row['task'] in ('incremental', 'replay') else 'n/a'
        lines.append(f"| {row['task']} | {row['status']} | {row.get('passed', False)} | {dev} | {hidden} | {replay} |")
    lines += ['', *['- ' + note for note in report['notes']], '']
    lines += ['Verification execution states: ' + str(report['verification_states']), '']
    for row in report['rows']:
        issues = row.get('source', {}).get('export_issues', [])
        if issues:
            lines += [f"Export diagnostics for {row['task']}:", '', '```json', json.dumps(issues, indent=2), '```', '']
    return '\n'.join(lines)
