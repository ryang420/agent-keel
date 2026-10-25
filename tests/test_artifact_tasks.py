import json

import pytest

from evals.artifacts.fixtures import prepare, validate
from evals.artifacts.tasks import tasks


def test_six_tasks_have_two_held_out_scenarios():
    suite = tasks()
    assert len(suite) == 6
    assert len({task.name for task in suite}) == 6
    for task in suite:
        assert len(task.verification) == 2
        assert all(scenario.steps for scenario in (task.development, *task.verification))
        assert 'solution.py' not in task.outputs
        assert task.instructions and '--input-dir' in task.prompt
    for task in suite:
        if task.name in ('incremental', 'replay'):
            assert all(len(s.steps) >= 3 for s in task.verification)


def test_fixture_manifests_are_deterministic(tmp_path):
    first = prepare(tmp_path / 'one')
    assert first == prepare(tmp_path / 'two')
    assert validate(tmp_path / 'one')['ready']
    data = tmp_path / 'one' / 'inputs' / 'cleaning' / 'development' / '0' / 'events.csv'
    data.write_bytes(data.read_bytes() + b'changed')
    assert not validate(tmp_path / 'one')['ready']
    with pytest.raises(ValueError, match='changed'):
        prepare(tmp_path / 'one')


def test_public_projection_excludes_hidden_inputs_and_expected_outputs(tmp_path):
    prepare(tmp_path)
    public = tmp_path / 'inputs' / 'joins' / 'development'
    assert not list(public.rglob('*expected*'))
    assert not list(public.rglob('*verification*'))
    manifest = json.loads((tmp_path / 'manifest.json').read_text())
    assert manifest['protocol'] == 'artifact-correctness-v1'
    for task in tasks():
        dev = task.development.steps[0].inputs
        assert all(dev != s.steps[0].inputs for s in task.verification)


def test_task_contracts_cover_boundary_rules():
    by_name = {t.name: t for t in tasks()}
    assert 'UNASSIGNED' in by_name['joins'].instructions
    assert 'tombstone' in by_name['incremental'].instructions
    assert "error.json" in by_name['schema'].outputs
    assert 'Asia/Shanghai' in by_name['revenue'].instructions
