import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from evals.artifacts.runtime import ArtifactExecutor, Job, runtime
from evals.artifacts.runner import verify_scenario
from evals.artifacts.tasks import tasks
from evals.artifacts.transport import ExportError
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider


async def test_export_happens_before_release_and_profile_retirement(monkeypatch):
    order = []

    async def export(*args):
        order.append('export')
        return {'solution.py': b'pass'}

    async def release(*args, **kwargs):
        order.append('release')
        return True

    monkeypatch.setattr('evals.artifacts.runtime.export_submission', export)
    executor = ArtifactExecutor(None, None, ProviderRegistry(), coordinator=SimpleNamespace(release=release))
    job = Job(('a.csv',))
    executor.jobs['p'] = job
    state = SimpleNamespace(sandbox=object(), use=object())
    await executor._release(SimpleNamespace(profile_id='p', id='r'), state)
    assert order == ['export', 'release']
    assert job.done.is_set() and job.files['solution.py'] == b'pass'


async def test_export_error_still_releases_usage(monkeypatch):
    release = AsyncMock(return_value=True)

    async def broken(*args):
        raise ExportError('broken')

    monkeypatch.setattr('evals.artifacts.runtime.export_submission', broken)
    executor = ArtifactExecutor(None, None, ProviderRegistry(), coordinator=SimpleNamespace(release=release))
    job = executor.jobs['p'] = Job(())
    await executor._release(SimpleNamespace(profile_id='p', id='r'),
                            SimpleNamespace(sandbox=object(), use=object()))
    release.assert_awaited_once()
    assert job.done.is_set() and job.error


async def test_verification_never_calls_provider():
    facade = SimpleNamespace(prepare=AsyncMock(return_value=object()), write_file=AsyncMock(),
                             exec=AsyncMock(return_value=SimpleNamespace(exit_code=0, output='done')))
    fake = FakeProvider()
    providers = ProviderRegistry()
    providers.register(fake)
    executor = ArtifactExecutor(None, None, providers, coordinator=facade)
    executor._finish = AsyncMock(return_value=True)
    executor.jobs['p'] = Job((), script=b'pass')
    state = SimpleNamespace(use=object(), sandbox=None, exec_id=None)
    await executor._loop(SimpleNamespace(profile_id='p', id='r'), state)
    assert fake.requests == []
    assert facade.exec.await_count == 1
    assert executor.jobs['p'].exit_code == 0


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get('KEEL_TEST_DOCKER') != '1', reason='requires opt-in Docker')
async def test_real_sandbox_verifies_reference_and_wrong_script(pg_dsn, tmp_path):
    from dataclasses import replace
    from evals.dab.runner import _settings

    registry = ProviderRegistry()
    fake = FakeProvider()
    registry.register(fake)
    settings = replace(_settings(pg_dsn, mount_root=tmp_path / 'mounts', docker='docker', max_iterations=60,
                                 openai_base_url=None, require_key=False),
                       deployment='artifact-test-' + tmp_path.name, lease_seconds=120)
    task = next(t for t in tasks() if t.name == 'joins')
    script = b'''import argparse,csv,json,pathlib,collections
p=argparse.ArgumentParser();p.add_argument('--input-dir');p.add_argument('--output-dir');a=p.parse_args()
i=pathlib.Path(a.input_dir);o=pathlib.Path(a.output_dir);o.mkdir(exist_ok=True,parents=True)
orders=list(csv.DictReader((i/'orders.csv').open()));tags=collections.defaultdict(set)
for r in csv.DictReader((i/'customer_tags.csv').open()):tags[r['customer_id']].add(r['tag'])
groups=collections.defaultdict(lambda:[0,0])
for r in orders:
 for tag in tags[r['customer_id']] or {'UNASSIGNED'}:
  groups[tag][0]+=1;groups[tag][1]+=int(r['net_cents'])
(o/'overall.json').write_text(json.dumps(dict(order_count=len(orders),net_cents=sum(int(r['net_cents']) for r in orders))))
with (o/'by_tag.csv').open('w') as f:
 w=csv.writer(f);w.writerow(['tag','order_count','net_cents']);w.writerows([k,*v] for k,v in groups.items())
'''
    async with runtime(settings, registry) as rt:
        result = await verify_scenario(rt, task, task.verification[0], script, {}, tmp_path / 'results',
                                       image='python:3.12-slim', provider='fake', model='m')
        assert result['passed'], result
        wrong = await verify_scenario(rt, task, task.verification[1], b'pass', {}, tmp_path / 'wrong',
                                      image='python:3.12-slim', provider='fake', model='m')
        assert not wrong['passed']
        assert fake.requests == []


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get('KEEL_TEST_DOCKER') != '1', reason='requires opt-in Docker')
async def test_scripted_model_complete_lifecycle_and_budget_stop(pg_dsn, tmp_path):
    import json
    from evals.artifacts.budget import Ledger
    from evals.artifacts.runner import execute
    from keel.models import Completed, TextDelta, ToolCall, ToolCallReady, Usage
    from tests.artifact_scripts import JOINS

    def call(name, args, identifier):
        return [ToolCallReady(ToolCall(identifier, name, json.dumps(args))), Completed(Usage(100, 10, 0, 0))]
    fake = FakeProvider(name='openai', script=[
        call('write_file', {'path': '/work/output/solution.py', 'content': JOINS.decode()}, 'code'),
        call('terminal', {'command': 'python /work/output/solution.py --input-dir /input/0 '
                                     '--output-dir /work/output'}, 'run'),
        [TextDelta('Done.'), Completed(Usage(100, 10, 0, 0))]])
    registry = ProviderRegistry()
    registry.register(fake)
    task = next(t for t in tasks() if t.name == 'joins')
    result = await execute([task], admin_dsn=pg_dsn, ledger=Ledger.grant(tmp_path/'budget.json', 100000),
                           root=tmp_path/'success', providers=registry, image='python:3.12-slim')
    assert result[0]['passed'], result
    assert sum(bool(r.tools) for r in fake.requests) == 3  # source write, execute, final
    assert len(result[0]['accounting']['calls']) == len(fake.requests)
    assert all(s['passed'] for s in result[0]['scenarios'])

    denied = FakeProvider(name='openai')
    registry = ProviderRegistry()
    registry.register(denied)
    result = await execute([task], admin_dsn=pg_dsn, ledger=Ledger.grant(tmp_path/'small.json', 1),
                           root=tmp_path/'denied', providers=registry, image='python:3.12-slim')
    assert result[0]['status'] == 'budget_exhausted', result
    assert denied.requests == []


@pytest.mark.parametrize('status', ['timeout', 'cleanup_failed', 'agent_failed', 'export_error'])
async def test_verifier_runtime_failures_stop_sequence(monkeypatch, tmp_path, status):
    run = AsyncMock(return_value={'status': status, 'files': {}, 'exit_code': None})
    monkeypatch.setattr('evals.artifacts.runner.run_job', run)
    task = next(t for t in tasks() if t.name == 'replay')
    result = await verify_scenario(None, task, task.verification[0], b'pass', {}, tmp_path, image='unused')
    assert not result['passed']
    assert result['steps'][0]['failures'] == [status]
    assert run.await_count == 1


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get('KEEL_TEST_DOCKER') != '1', reason='requires opt-in Docker')
async def test_real_state_carry_rejection_and_reset(pg_dsn, tmp_path):
    from dataclasses import replace
    from evals.dab.runner import _settings
    from tests.artifact_scripts import REPLAY
    registry = ProviderRegistry()
    fake = FakeProvider()
    registry.register(fake)
    settings = replace(_settings(pg_dsn, mount_root=tmp_path/'mounts', docker='docker', max_iterations=60,
                                 openai_base_url=None, require_key=False),
                       deployment='artifact-state-' + tmp_path.name, lease_seconds=120)
    task = next(t for t in tasks() if t.name == 'replay')
    async with runtime(settings, registry) as rt:
        result = await verify_scenario(rt, task, task.verification[0], REPLAY, {}, tmp_path/'replay',
                                       image='python:3.12-slim', provider='fake', model='m')
    assert result['passed'], result
    assert len(result['steps']) == 6
    assert result['steps'][4]['execution']['exit_code'] == 2
    assert fake.requests == []
