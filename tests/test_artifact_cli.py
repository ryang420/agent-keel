import json

import pytest

from evals.artifacts.cli import main
from evals.artifacts.report import build
from evals.artifacts.tasks import tasks


def test_prepare_and_validate_make_no_inference_calls(tmp_path, monkeypatch):
    def no_models(*args, **kwargs):
        raise AssertionError('inference not permitted')
    monkeypatch.setattr('evals.artifacts.cli.execute', no_models)
    assert main(['prepare', '--root', str(tmp_path)]) == 0
    assert main(['validate', '--root', str(tmp_path), '--skip-image']) == 0


def test_execute_requires_explicit_budget(tmp_path):
    with pytest.raises(SystemExit):
        main(['execute', '--root', str(tmp_path)])
    with pytest.raises(SystemExit):
        main(['execute', '--root', str(tmp_path), '--max-tokens', '0'])


def test_partial_report_preserves_selected_denominator():
    records = [{'task': 'joins', 'passed': True, 'status': 'executed'},
               {'task': 'cleaning', 'passed': False, 'status': 'budget_exhausted'}]
    report = build(records, [t.name for t in tasks()])
    assert report['selected'] == 6
    assert report['passed'] == 1
    assert report['strict_pass_rate'] == 1 / 6
    assert not report['complete']
    assert report['states']['planned'] == 4


def test_rerun_cannot_overwrite_prior_results(tmp_path, monkeypatch):
    main(['prepare', '--root', str(tmp_path)])
    (tmp_path / 'runs' / 'same').mkdir(parents=True)
    with pytest.raises(SystemExit, match='exists'):
        main(['execute', '--root', str(tmp_path), '--run-id', 'same', '--max-tokens', '10000'])


def test_report_rejects_unknown_or_duplicate_task_records():
    with pytest.raises(ValueError):
        build([{'task': 'other', 'passed': True}], ['joins'])
    with pytest.raises(ValueError):
        build([{'task': 'joins'}, {'task': 'joins'}], ['joins'])


def test_budget_uses_artifact_protocol(tmp_path):
    from evals.artifacts.budget import Ledger
    ledger = Ledger.grant(tmp_path / 'budget.json', 100)
    ledger.settle('joins', ledger.reserve('joins', 10), None)
    saved = json.loads(ledger.path.read_text())
    assert saved['protocol'] == 'artifact-correctness-v2'
    assert saved['entries'][0]['actual'] is None


async def test_provider_reserves_before_call_and_caps_output(tmp_path):
    from evals.artifacts.budget import Ledger, LimitedProvider
    from evals.dab.accounting import Charging
    from evals.dab.budget import BudgetExhausted
    from keel.models import Message, ModelRequest, TextPart
    from keel.providers.fake import FakeProvider
    inner = FakeProvider()
    request = ModelRequest('fake', 'm', 'system', (Message('user', (TextPart('x'),)),))
    small = LimitedProvider(inner, Charging(Ledger.grant(tmp_path / 'small.json', 1)))
    with pytest.raises(BudgetExhausted):
        _ = [event async for event in small.stream(request)]
    assert inner.requests == []
    admitted = LimitedProvider(inner, Charging(Ledger.grant(tmp_path / 'ok.json', 100000)))
    _ = [event async for event in admitted.stream(request)]
    assert inner.requests[0].max_output_tokens == 8192


def test_report_does_not_hide_verification_infrastructure_failures():
    record = {'task': 'joins', 'passed': False, 'status': 'executed', 'scenarios': [
        {'name': name, 'passed': False, 'steps': [
            {'passed': False, 'failures': ['setup_failed'], 'execution': {'status': 'setup_failed'}}]}
        for name in ['development', 'verification-1', 'verification-2']]}
    report = build([record], ['joins'])
    assert not report['complete']
    assert report['verification_states'] == {'setup_failed': 3}


def test_artifact_budget_can_be_reopened(tmp_path):
    from evals.artifacts.budget import Ledger
    ledger = Ledger.grant(tmp_path/'ledger.json', 100)
    ledger.settle('joins', ledger.reserve('joins', 10), 8)
    reopened = Ledger.open(ledger.path)
    assert reopened.remaining == 92 and reopened.entries == ledger.entries


def test_historical_report_retains_recorded_protocol(tmp_path, monkeypatch):
    monkeypatch.setattr('evals.artifacts.report.PROTOCOL', 'artifact-correctness-v2')
    root = tmp_path/'runs'/'historical'
    root.mkdir(parents=True)
    (root/'config.json').write_text(json.dumps({'protocol': 'artifact-correctness-v1', 'selected': ['joins']}))
    assert main(['report', '--root', str(tmp_path), '--run-id', 'historical']) == 1
    report = json.loads((root/'report.json').read_text())
    assert report['protocol'] == 'artifact-correctness-v1'
    assert '`artifact-correctness-v1`' in (root/'report.md').read_text()


def test_report_explains_rejected_submission_entries():
    from evals.artifacts.report import markdown
    report = build([{'task': 'cleaning', 'status': 'export_error', 'passed': False, 'source': {
        'export_issues': [{'code': 'nonregular_entry', 'name': '__pycache__', 'type': 'directory'}]}}], ['cleaning'])
    output = markdown(report)
    assert '__pycache__' in output and 'nonregular_entry' in output and 'directory' in output
