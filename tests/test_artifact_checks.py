import json

import pytest

from evals.artifacts.checks import check_step
from evals.artifacts.model import json_bytes
from evals.artifacts.tasks import tasks


def supplied(step):
    files = dict(step.expected)
    if step.error_code and not files:
        error = {'code': step.error_code}
        if step.error_row is not None:
            error['row'] = step.error_row
        files['error.json'] = json_bytes(error)
    return files


@pytest.mark.parametrize('task', tasks(), ids=lambda t: t.name)
def test_correct_outputs_pass_all_fixture_steps(task):
    for scenario in (task.development, *task.verification):
        for step in scenario.steps:
            result = check_step(step, supplied(step), 1 if step.error_code else 0)
            assert result.passed, result.failures


def test_duplicate_output_rows_fail():
    step = tasks()[1].development.steps[0]
    files = supplied(step)
    files['by_tag.csv'] += files['by_tag.csv'].splitlines(keepends=True)[1]
    assert not check_step(step, files, 0).passed


def test_empty_and_missing_files_differ():
    step = tasks()[0].development.steps[0]
    assert 'missing:clean.csv' in check_step(step, {}, 0).failures
    assert 'invalid:clean.csv' in check_step(step, {'clean.csv': b''}, 0).failures


def test_invalid_schema_and_row_require_structured_failure():
    step = tasks()[4].development.steps[2]
    files = supplied(step)
    assert not check_step(step, files, 0).passed
    assert not check_step(step, {}, 1).passed
    files['normalized.csv'] = b'forbidden'
    assert not check_step(step, files, 1).passed


@pytest.mark.parametrize('name', ['cleaning', 'joins', 'incremental', 'replay', 'schema', 'revenue'])
def test_named_faulty_values_fail(name):
    task = next(t for t in tasks() if t.name == name)
    step = task.verification[0].steps[0]
    for filename in step.expected:
        files = supplied(step)
        if filename.endswith('.json'):
            data = json.loads(files[filename])
            if 'rows' in data:  # stale overwrite / resurrected tombstone
                data['rows'][0]['version'] = 0
            elif 'transactions' in data:  # replay duplication
                data['transactions'].append(dict(data['transactions'][0]))
            else:
                data['net_cents'] += 1
            files[filename] = json_bytes(data)
        else:
            lines = files[filename].decode().splitlines()
            files[filename] = ('\n'.join(lines[:-1]) + '\n').encode()
        assert not check_step(step, files, 0).passed, filename


def test_null_to_zero_and_hardcoded_development_answers_fail():
    task = tasks()[0]
    step = task.development.steps[0]
    files = supplied(step)
    files['clean.csv'] = files['clean.csv'].replace(b',,void', b',0,void')
    assert not check_step(step, files, 0).passed
    for task in tasks():
        assert not check_step(task.verification[0].steps[0], supplied(task.development.steps[0]), 0).passed


def test_bool_is_not_an_integer_and_json_row_order_is_irrelevant():
    step = tasks()[1].development.steps[0]
    files = supplied(step)
    files['overall.json'] = json_bytes({'order_count': True, 'net_cents': 1014})
    assert not check_step(step, files, 0).passed
    step = tasks()[2].development.steps[0]
    files = supplied(step)
    state = json.loads(files['state.json'])
    state['rows'].reverse()
    files['state.json'] = json_bytes(state)
    assert check_step(step, files, 0).passed


def test_deeply_nested_submission_fails_without_crashing_checker():
    step = tasks()[1].development.steps[0]
    files = supplied(step)
    files['overall.json'] = b'[' * 2000 + b'0' + b']' * 2000
    assert 'invalid:overall.json' in check_step(step, files, 0).failures
