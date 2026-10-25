"""Model submission followed by isolated, no-model script verification."""

import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import time
import uuid

from evals.dab.accounting import Charging
from evals.dab.inputs import keel_state
from evals.dab.runner import _retire_profile, _settings, image_id, temporary_database
from keel.providers.base import ProviderRegistry
from keel.sandbox.base import profile_mount_root
from keel.service import default_providers

from . import PROTOCOL
from .checks import check_step
from .budget import LimitedProvider
from .model import json_bytes
from .runtime import Job, runtime


def _tool_errors(messages):
    errors = 0
    for item in messages:
        if item.message.role != 'tool':
            continue
        try:
            value = json.loads(item.message.text)
        except ValueError:
            continue
        if isinstance(value, dict) and (value.get('error') or value.get('exit_code') not in (None, 0)):
            errors += 1
    return errors


def policy(root: Path, profile: str, inputs: dict[str, bytes], image: str) -> tuple[dict, Path]:
    folder = Path(profile_mount_root(str(root), profile))
    folder.mkdir(parents=True, exist_ok=False)
    for name, data in inputs.items():
        path = folder / name
        if path.is_absolute() and not path.is_relative_to(folder):
            raise ValueError('invalid input path')
        if '..' in Path(name).parts:
            raise ValueError('invalid input path')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return {'image': image, 'workdir': '/work', 'network': 'none',
            'cpus': 1, 'memory_mb': 512, 'pids_limit': 128,
            'mounts': [{'source': str(folder), 'target': '/input', 'read_only': True}]}, folder


async def run_job(rt, job: Job, *, prompt: str, inputs: dict[str, bytes], root: Path, image: str,
                  model: str = 'gpt-6-luna', provider: str = 'openai', timeout: float = 900) -> dict:
    root.mkdir(parents=True, exist_ok=False)
    profile = 'artifact-' + uuid.uuid4().hex
    sandbox_policy, folder = policy(Path(rt.executor.mount_root), profile, inputs, image)
    rt.executor.jobs[profile] = job
    started = time.monotonic()
    result = {'status': 'setup_failed', 'files': {}, 'usage': None}
    try:
        response = await rt.client.put('/v1/profiles/' + profile, json={
            'models': [{'provider': provider, 'model': model}], 'sandbox': sandbox_policy,
            'soul': '', 'skills': [], 'memory': False})
        response.raise_for_status()
        response = await rt.client.post(f'/v1/profiles/{profile}/runs', json={
            'input': prompt, 'session_id': profile + '-s', 'provider': provider, 'model': model,
            'reasoning_effort': 'low'})
        response.raise_for_status()
        run_id = response.json()['run_id']
        deadline = time.monotonic() + timeout
        timed_out = False
        while not job.done.is_set():
            if time.monotonic() >= deadline:
                timed_out = True
                await rt.client.post(f'/v1/profiles/{profile}/runs/{run_id}/stop')
                break
            await asyncio.sleep(0.05)
        await asyncio.wait_for(job.done.wait(), 120)
        run_response = await rt.client.get(f'/v1/profiles/{profile}/runs/{run_id}')
        run_response.raise_for_status()
        run = run_response.json()
        # Let title calls settle before changing the accounting case for the next task.
        pending = rt.keel.background.profile_tasks(profile)
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending), 120)
        messages = await rt.keel.store.messages(run['session_id'])
        status = 'executed' if run['status'] == 'completed' else 'agent_failed'
        if timed_out or job.exit_code == 124:
            status = 'timeout'
        if job.error:
            status = 'export_error'
        if 'budget' in (run.get('error') or '').lower():
            status = 'budget_exhausted'
        result = {'status': status, 'files': job.files, 'exit_code': job.exit_code,
                  'usage': run.get('usage'), 'run': run, 'error': job.error,
                  'tool_calls': sum(len(m.message.tool_calls) for m in messages),
                  'tool_errors': _tool_errors(messages)}
        (root / 'messages.json').write_bytes(json_bytes([
            {'id': m.id, 'role': m.message.role, 'text': m.message.text,
             'tool_calls': [{'name': c.name, 'arguments': c.arguments} for c in m.message.tool_calls]}
            for m in messages]))
        (root / 'stdout.txt').write_text(job.stdout)
        (root / 'prompt.txt').write_text(prompt)
        for name, data in job.files.items():
            target = root / 'submission' / name
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(data)
    except Exception as exc:
        result['error'] = type(exc).__name__
    finally:
        try:
            cleanup = await _retire_profile(rt.client, profile)
        except Exception as exc:
            cleanup = {'status': 'failed', 'error': type(exc).__name__}
        result['cleanup'] = cleanup
        if cleanup['status'] in ('absent', 'retired'):
            shutil.rmtree(folder)
        else:
            result['status'] = 'cleanup_failed'
        rt.executor.jobs.pop(profile, None)
    result['duration_s'] = time.monotonic() - started
    result['prompt_sha256'] = hashlib.sha256(prompt.encode()).hexdigest()
    result['input_sha256'] = {name: hashlib.sha256(data).hexdigest() for name, data in inputs.items()}
    (root / 'execution.json').write_bytes(json_bytes({k: v for k, v in result.items() if k != 'files'}))
    return result


async def verify_scenario(rt, task, scenario, script: bytes, previous: dict, root: Path, *, image: str,
                          model='gpt-6-luna', provider='openai') -> dict:
    outcomes = []
    carried = dict(previous)
    for index, step in enumerate(scenario.steps):
        if not step.preserve_output:
            carried = {}
        job = Job(task.outputs, script=script, previous=carried)
        outcome = await run_job(rt, job, prompt='Evaluator verification: no model call.', inputs=step.inputs,
                                root=root / str(index), image=image, model=model, provider=provider, timeout=180)
        files = {k: v for k, v in outcome['files'].items() if k != 'solution.py'}
        failures = [outcome['status']] if outcome['status'] != 'executed' else []
        if not failures:
            try:
                failures = list(check_step(step, files, outcome['exit_code']).failures)
            except ValueError:
                failures = ['evaluator_error']
        outcomes.append({'passed': not failures, 'failures': failures, 'execution': {
            k: v for k, v in outcome.items() if k != 'files'}})
        carried = files
        if outcome['status'] != 'executed':
            break
    return {'name': scenario.name, 'planned_steps': len(scenario.steps),
            'passed': len(outcomes) == len(scenario.steps) and
            all(o['passed'] for o in outcomes), 'steps': outcomes}


async def execute(tasks, *, admin_dsn: str, ledger, root: Path, image='keel-artifact-eval:1', providers=None,
                  model='gpt-6-luna', provider='openai') -> list[dict]:
    from dataclasses import replace

    root.mkdir(parents=True, exist_ok=False)
    image_digest = image_id(image)
    image = image_digest  # All source/replay containers use the exact recorded image bytes.
    frozen = {task.name: {'prompt': hashlib.sha256(task.prompt.encode()).hexdigest(), 'scenarios': {
        scenario.name: [{'inputs': {name: hashlib.sha256(data).hexdigest() for name, data in step.inputs.items()},
                         'expected': {name: hashlib.sha256(data).hexdigest() for name, data in step.expected.items()},
                         'error_code': step.error_code, 'error_row': step.error_row,
                         'preserve_output': step.preserve_output} for step in scenario.steps]
        for scenario in (task.development, *task.verification)}} for task in tasks}
    metadata = {'protocol': PROTOCOL, 'keel': keel_state(Path.cwd()), 'image': image_digest,
                'provider': provider, 'model': model, 'returned_model': None, 'reasoning_effort': 'low',
                'selected': [t.name for t in tasks], 'fixtures': frozen,
                'limits': {'cpus': 1, 'memory_mb': 512, 'pids_limit': 128, 'verification_timeout_s': 60,
                           'task_timeout_s': 900, 'iterations': 60}}
    (root / 'config.json').write_bytes(json_bytes(metadata))
    records = []
    async with temporary_database(admin_dsn) as dsn:
        settings = replace(_settings(dsn, mount_root=(root / 'mounts').resolve(), docker='docker',
                                     max_iterations=60, openai_base_url=None, require_key=providers is None),
                           deployment='artifacts-' + uuid.uuid4().hex[:12], lease_seconds=120)
        charging = Charging(ledger)
        inner = providers or default_providers(settings)
        registry = ProviderRegistry()
        for name in inner.names():
            registry.register(LimitedProvider(inner.get(name), charging, max_output=8192))
        async with runtime(settings, registry) as rt:
            for task in tasks:
                charging.switch(task.name)
                first_entry = len(ledger.entries)
                first_spent = ledger.spent_tokens
                inputs = {f'{i}/{name}': data for i, step in enumerate(task.development.steps)
                          for name, data in step.inputs.items()}
                job = Job(task.outputs)
                source = await run_job(rt, job, prompt=task.prompt, inputs=inputs, root=root / task.name / 'source',
                                       image=image, model=model, provider=provider)
                record = {'task': task.name, 'status': source['status'], 'passed': False,
                          'source': {k: v for k, v in source.items() if k != 'files'}, 'scenarios': []}
                if any(e.get('outcome') == 'budget_exhausted' for e in ledger.entries[first_entry:]):
                    record['status'] = 'budget_exhausted'
                script = source['files'].get('solution.py')
                if record['status'] == 'executed' and script:
                    source_outputs = {k: v for k, v in source['files'].items() if k != 'solution.py'}
                    final_step = task.development.steps[-1]
                    record['source_final_pass'] = check_step(
                        final_step, source_outputs, 1 if final_step.error_code else 0).passed
                    for scenario in (task.development, *task.verification):
                        result = await verify_scenario(rt, task, scenario, script, {},
                                                       root / task.name / scenario.name, image=image, model=model,
                                                       provider=provider)
                        record['scenarios'].append(result)
                    record['passed'] = record['source_final_pass'] and all(s['passed'] for s in record['scenarios'])
                    record['artifact_sha256'] = {k: hashlib.sha256(v).hexdigest() for k, v in source['files'].items()}
                elif record['status'] == 'executed':
                    record['status'] = 'artifact_missing'
                record['accounting'] = {'charged_tokens': ledger.spent_tokens - first_spent,
                                        'cumulative_charged_tokens': ledger.spent_tokens,
                                        'calls': ledger.entries[first_entry:]}
                records.append(record)
                (root / 'records.json').write_bytes(json_bytes(records))
    return records
