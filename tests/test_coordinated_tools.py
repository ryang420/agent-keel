"""Every tool and internal helper must register before dispatch; EOF is never cleanup evidence."""

import asyncio
from dataclasses import replace
import json

import pytest

from keel.sandbox.coordinator import SandboxCoordinator
from keel.sandbox.local import LocalSandbox
from keel.sandbox.records import CleanupReceipt, SandboxLeaseLost, SandboxUncertain
from keel.skills import discover_skills
from keel.tools import run_tool
from keel.tools.base import ToolContext
from tests.sandbox_support import TransactionProbe, make_claim, seed_profile, seed_ready_sandbox
from tests.test_sandbox_coordinator import helper_assets, coordination  # noqa: F401


@pytest.fixture
async def managed(coordination, tmp_path):
    coordinator, backend, store, clock = coordination
    work = tmp_path / 'work'
    work.mkdir()
    await seed_profile(store, sandbox={'image': 'img', 'workdir': str(work)})
    await seed_ready_sandbox(store, sandbox={'image': 'img', 'workdir': str(work)})
    claim = await make_claim(store, 's1', sandbox={'image': 'img', 'workdir': str(work)})
    bound = await coordinator.prepare(claim.use)
    probe = TransactionProbe(store.db)
    store.db = probe
    local = LocalSandbox(kill_grace_s=0.01)
    backend.exec_calls = []
    backend.control_calls = []
    backend.receipt_change = lambda receipt: receipt
    backend.failure = None
    backend.block = None

    async def exec_bound(ticket, argv, **kwargs):
        row = await store.db.fetchrow('SELECT * FROM sandbox_execs WHERE id = ?', ticket.exec_id)
        assert row is not None and row['state'] == 'pending'
        assert row['control_token'] == ticket.control_token
        assert row['container_id'] == bound.binding.container_id
        assert probe.depth == 0
        backend.exec_calls.append((ticket, argv, kwargs))
        kwargs.pop('authorize_control', None)
        if backend.block is not None and argv[0] == "bash":
            backend.block.set()
            await asyncio.Event().wait()
        if backend.failure is not None:
            raise backend.failure
        result = await local.exec(bound, argv, exec_id=ticket.exec_id, **kwargs)
        receipt = CleanupReceipt(ticket.exec_id, ticket.control_token, result.exit_code, True, True, None)
        return result, backend.receipt_change(receipt)

    async def control_exec(ticket, *, cancel):
        backend.control_calls.append(ticket)
        await local.kill_tree(bound, ticket.exec_id)
        return backend.receipt_change(CleanupReceipt(ticket.exec_id, ticket.control_token, 0, True, True, None))

    backend.exec_bound = exec_bound
    backend.control_exec = control_exec
    yield coordinator, backend, store, clock, bound, work
    await local.aclose()


async def invoke(managed, name, **args):
    facade, _, _, _, bound, _ = managed
    ctx = ToolContext(facade, bound, cwd=bound.policy.workdir, tool_call_id='call8')
    return await run_tool(name, json.dumps(args), ctx)


@pytest.mark.parametrize('route', ['terminal', 'execute_code', 'read_file', 'write_file', 'patch',
                                  'search_files', 'skills', 'spill'])
async def test_all_tool_helpers_register_once_before_dispatch(managed, route):
    facade, backend, store, _, bound, work = managed
    (work / 'a.txt').write_text('hello\n')
    if route == 'terminal':
        outcome = await invoke(managed, route, command='echo hello; exit 7')
        assert json.loads(outcome.content)['exit_code'] == 7
    elif route == 'execute_code':
        outcome = await invoke(managed, route, code='print(42)')
        assert json.loads(outcome.content)['output'] == '42'
        assert len(backend.exec_calls) == 3  # registered write, run, removal
    elif route == 'read_file':
        outcome = await invoke(managed, route, path='a.txt')
        assert 'hello' in json.loads(outcome.content)['content']
    elif route == 'write_file':
        outcome = await invoke(managed, route, path='b.txt', content='written')
        assert (work / 'b.txt').read_text() == 'written'
    elif route == 'patch':
        outcome = await invoke(managed, route, path='a.txt', old_string='hello', new_string='world')
        assert (work / 'a.txt').read_text() == 'world\n'
        assert len(backend.exec_calls) == 2
    elif route == 'search_files':
        outcome = await invoke(managed, route, pattern='hello', path='.')
        assert 'hello' in outcome.content
    elif route == 'skills':
        skill = work / 'skills' / 'example'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('---\nname: example\ndescription: Example\n---\nbody')
        (skill / 'ref.txt').write_text('reference')
        skills = await discover_skills(facade, bound, str(work / 'skills'), ['example'])
        ctx = ToolContext(facade, bound, cwd=str(work), skills=tuple(skills))
        outcome = await run_tool('skill_view', '{"name": "example"}', ctx)
        assert 'body' in outcome.content and 'ref.txt' in outcome.content
        assert len(backend.exec_calls) == 3
    else:
        outcome = await invoke(managed, 'terminal', command="python3 -c 'print(\"z\" * 60000)'")
        assert 'Full output saved to' in outcome.content
        assert len(backend.exec_calls) == 2
    assert not outcome.error, outcome.content
    rows = await store.db.fetch('SELECT * FROM sandbox_execs WHERE usage_id = ?', bound.use.usage_id)
    assert len(rows) == len(backend.exec_calls) > 0
    assert all(row['state'] == 'finished' for row in rows)
    assert len({row['control_token'] for row in rows}) == len(rows)
    assert await facade.release(bound.use) is True


@pytest.mark.parametrize('change', [lambda r: None, lambda r: replace(r, control_closed=False),
                                   lambda r: replace(r, descendants_complete=False),
                                   lambda r: replace(r, control_token='foreign'),
                                   lambda r: replace(r, exec_id='other'),
                                   lambda r: replace(r, uncertain_reason='escaped')])
async def test_eof_or_unverified_receipt_never_finishes_or_releases(managed, change):
    facade, backend, store, _, bound, _ = managed
    backend.receipt_change = change
    with pytest.raises(SandboxUncertain):
        await facade.exec(bound, ['true'], cwd='/', timeout_s=1, exec_id='original')
    row = await store.db.fetchrow('SELECT * FROM sandbox_execs WHERE id = ?', 'original')
    assert row['state'] == 'uncertain'
    sandbox = await store.db.fetchrow('SELECT state FROM sandboxes WHERE profile_id = ?', 'p1')
    assert sandbox['state'] == 'draining'
    assert await facade.release(bound.use) is False


async def test_dispatch_exception_is_uncertain_without_replay(managed):
    facade, backend, store, _, bound, _ = managed
    backend.failure = RuntimeError('lost transport')
    with pytest.raises(SandboxUncertain):
        await facade.exec(bound, ['true'], cwd='/', timeout_s=1, exec_id='original')
    assert len(backend.exec_calls) == 1
    assert await facade.release(bound.use) is False


async def test_cancelled_exec_controls_original_ticket_and_finishes_only_with_receipt(managed):
    facade, backend, store, _, bound, _ = managed
    backend.block = asyncio.Event()
    task = asyncio.create_task(facade.exec(bound, ['bash', '-c', 'true'], cwd='/', timeout_s=60, exec_id='original'))
    await asyncio.wait_for(backend.block.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.control_calls == [backend.exec_calls[0][0]]
    assert (await store.db.fetchrow('SELECT state FROM sandbox_execs WHERE id = ?', 'original'))['state'] == 'finished'
    assert await facade.release(bound.use) is True


async def test_kill_resolves_durable_ticket_after_coordinator_restart(managed):
    facade, backend, store, _, bound, _ = managed
    ticket = await store.begin_exec(bound, 'original')
    restarted = SandboxCoordinator(store, backend, deployment='dep', owner='w1', lease_seconds=30,
                                   idle_seconds=1800, reap_seconds=5)
    await restarted.kill_tree(bound, 'original')
    assert backend.control_calls == [ticket]
    assert await restarted.release(bound.use) is True


async def test_signal_without_receipt_marks_uncertain(managed):
    facade, backend, store, _, bound, _ = managed
    await store.begin_exec(bound, 'original')
    backend.receipt_change = lambda r: None
    with pytest.raises(SandboxUncertain):
        await facade.kill_tree(bound, 'original')
    assert (await store.db.fetchrow('SELECT state FROM sandbox_execs WHERE id = ?', 'original'))['state'] == 'uncertain'
    assert await facade.release(bound.use) is False


@pytest.mark.parametrize('fault', ['expired', 'generation', 'usage', 'control'])
async def test_control_rejects_stale_or_mismatched_authority(managed, fault):
    facade, backend, store, clock, bound, _ = managed
    ticket = await store.begin_exec(bound, 'original')
    if fault == 'expired':
        clock.now += 61
    elif fault == 'generation':
        bound = replace(bound, binding=replace(bound.binding, generation_token='wrong'))
    elif fault == 'usage':
        bound = replace(bound, use=replace(bound.use, token='wrong'))
    else:
        await store.db.execute('UPDATE sandbox_execs SET usage_token = ? WHERE id = ?', 'foreign', ticket.exec_id)
    with pytest.raises(SandboxLeaseLost):
        await facade.kill_tree(bound, ticket.exec_id)
    assert backend.control_calls == []


async def test_release_unbound_preparation_requires_explicit_abandonment(coordination):
    facade, _, store, _ = coordination
    claim = await make_claim(store, 's1')
    assert await facade.release(claim.use) is False
    assert await facade.release(claim.use, abandon_preparation=True) is True


async def test_execute_code_error_still_registers_removal(managed):
    facade, backend, store, _, bound, _ = managed
    original = backend.exec_bound

    async def fail_run(ticket, argv, **kwargs):
        if argv[0] == 'bash':
            backend.failure = RuntimeError('transport failed')
        try:
            return await original(ticket, argv, **kwargs)
        finally:
            backend.failure = None

    backend.exec_bound = fail_run
    outcome = await invoke(managed, 'execute_code', code='print(42)')
    assert outcome.error
    assert [argv[0] for _, argv, _ in backend.exec_calls] == ['sh', 'bash', 'rm']
    states = await store.db.fetch('SELECT state FROM sandbox_execs WHERE usage_id = ?', bound.use.usage_id)
    assert sorted(row['state'] for row in states) == ['finished', 'finished', 'uncertain']
    assert await facade.release(bound.use) is False


async def test_execute_code_cancel_still_registers_removal(managed):
    facade, backend, store, _, bound, _ = managed
    backend.block = asyncio.Event()
    task = asyncio.create_task(invoke(managed, 'execute_code', code='print(42)'))
    await asyncio.wait_for(backend.block.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [argv[0] for _, argv, _ in backend.exec_calls] == ['sh', 'bash', 'rm']
    assert await facade.release(bound.use) is True


async def test_expired_usage_cannot_dispatch_or_fallback(managed):
    facade, backend, store, clock, bound, _ = managed
    clock.now += 61
    with pytest.raises(SandboxLeaseLost):
        await facade.exec(bound, ['true'], cwd='/', timeout_s=1, exec_id='late')
    assert backend.exec_calls == []


async def test_timeout_without_cleanup_receipt_never_claims_killed(managed):
    _, backend, _, _, _, _ = managed
    backend.receipt_change = lambda r: None
    outcome = await invoke(managed, 'terminal', command='true', timeout=1)
    assert outcome.error and 'killed' not in outcome.content.lower()


async def test_control_accepts_valid_cleaning_usage(managed):
    facade, backend, store, _, bound, _ = managed
    ticket = await store.begin_exec(bound, 'original')
    assert await store.begin_usage_cleanup(bound.use) is True
    await facade.kill_tree(bound, ticket.exec_id)
    assert backend.control_calls == [ticket]
    assert await facade.release(bound.use) is True


@pytest.mark.parametrize('operation', ['exec', 'read_file', 'write_file', 'kill_tree'])
async def test_bound_handles_cannot_bypass_facade_through_legacy_docker(managed, operation):
    from keel.sandbox.base import SandboxError
    from keel.sandbox.docker import DockerSandbox

    _, _, _, _, bound, _ = managed
    backend = DockerSandbox(deployment='dep')
    calls = []

    async def legacy(*args, **kwargs):
        calls.append(args)
        raise AssertionError('legacy dispatch must not run')

    backend._exec_once = legacy
    backend._docker = legacy
    with pytest.raises(SandboxError):
        if operation == 'exec':
            await backend.exec(bound, ['true'], cwd='/', timeout_s=1, exec_id='bypass')
        elif operation == 'read_file':
            await backend.read_file(bound, '/a', 10)
        elif operation == 'write_file':
            await backend.write_file(bound, '/a', b'body')
        else:
            await backend.kill_tree(bound, 'bypass')
    assert calls == []


async def test_facade_rejects_an_unbound_local_handle(managed):
    from keel.sandbox.base import Sandbox

    facade, backend, _, _, bound, _ = managed
    local = Sandbox(bound.profile_id, 'local', bound.policy)
    with pytest.raises(SandboxLeaseLost):
        await facade.exec(local, ['true'], cwd='/', timeout_s=1, exec_id='unbound')
    assert backend.exec_calls == []


async def test_facade_control_authorizer_rejects_changed_control_token(managed):
    facade, backend, store, _, bound, _ = managed
    original = backend.exec_bound

    async def change_identity(ticket, argv, **kwargs):
        await store.db.execute('UPDATE sandbox_execs SET control_token = ? WHERE id = ?', 'new', ticket.exec_id)
        await kwargs['authorize_control']()
        return await original(ticket, argv, **kwargs)

    backend.exec_bound = change_identity
    with pytest.raises(SandboxUncertain):
        await facade.exec(bound, ['true'], cwd='/', timeout_s=1, exec_id='original')
    assert backend.exec_calls == [] and backend.control_calls == []
    assert await facade.release(bound.use) is False


async def test_cancellation_after_lease_expiry_preserves_uncertainty_without_signalling(managed):
    facade, backend, store, clock, bound, _ = managed
    backend.block = asyncio.Event()
    task = asyncio.create_task(facade.exec(bound, ['bash', '-c', 'true'], cwd='/', timeout_s=60, exec_id='late'))
    await asyncio.wait_for(backend.block.wait(), 1)
    clock.now += 61
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.control_calls == []
    assert (await store.db.fetchrow('SELECT state FROM sandbox_execs WHERE id = ?', 'late'))['state'] == 'uncertain'
    assert (await store.db.fetchrow('SELECT state FROM sandboxes WHERE profile_id = ?', 'p1'))['state'] == 'draining'
    assert await facade.release(bound.use) is False


async def test_cancel_cleanup_without_receipt_keeps_registry_uncertain(managed):
    facade, backend, store, _, bound, _ = managed
    backend.block = asyncio.Event()
    backend.receipt_change = lambda r: None
    task = asyncio.create_task(facade.exec(bound, ['bash', '-c', 'true'], cwd='/', timeout_s=60, exec_id='cancel'))
    await asyncio.wait_for(backend.block.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await store.db.fetchrow('SELECT state FROM sandbox_execs WHERE id = ?', 'cancel'))['state'] == 'uncertain'
    assert await facade.release(bound.use) is False


@pytest.mark.parametrize('valid', [True, False])
async def test_facade_gates_docker_timeout_control_with_current_store_authority(managed, monkeypatch, valid):
    from keel.sandbox.docker import DockerSandbox
    from tests.test_docker_operations import FakeProcess, attach, attach_spawn, receipt_json

    facade, _, store, clock, bound, _ = managed
    docker = DockerSandbox(deployment='dep', receipt_timeout_s=0)
    docker._identity = ('daemon-1', 'amd64')
    facade.backend = docker
    process = FakeProcess([], None)
    spawned = attach_spawn(docker, process)
    ticket = None
    begin = store.begin_exec

    async def capture(*args):
        nonlocal ticket
        ticket = await begin(*args)
        return ticket

    monkeypatch.setattr(store, 'begin_exec', capture)
    resolve = store.resolve_exec

    async def check(sandbox, exec_id):
        if not valid:
            clock.now += 61
        return await resolve(sandbox, exec_id)

    monkeypatch.setattr(store, 'resolve_exec', check)

    def handler(args, stdin):
        if 'cancel' in args:
            process.returncode = -15
            return 0, ''
        return 0, receipt_json(ticket)

    fake = attach(docker, handler)
    if valid:
        result = await facade.exec(bound, ['sleep', '30'], cwd='/', timeout_s=0.01, exec_id='timeout')
        assert result.timed_out and result.exit_code == 124
    else:
        with pytest.raises(SandboxUncertain):
            await facade.exec(bound, ['sleep', '30'], cwd='/', timeout_s=0.01, exec_id='timeout')
        assert await facade.release(bound.use) is False
    assert len(spawned) == 1
    assert any('cancel' in args for args in fake.commands()) == valid
    row = await store.db.fetchrow('SELECT state FROM sandbox_execs WHERE id = ?', 'timeout')
    assert row['state'] == ('finished' if valid else 'uncertain')


async def test_missing_recorded_container_never_recreates_or_replays(managed):
    from keel.sandbox.docker import DockerSandbox
    from tests.test_docker_operations import FakeProcess, attach, attach_spawn

    facade, _, store, _, bound, _ = managed
    docker = DockerSandbox(deployment='dep', receipt_timeout_s=0)
    docker._identity = ('daemon-1', 'amd64')
    facade.backend = docker
    process = FakeProcess([b'Error response from daemon: No such container'], 125)
    spawned = attach_spawn(docker, process)
    fake = attach(docker, lambda args, stdin: (1, 'Error: No such container'))
    with pytest.raises(SandboxUncertain):
        await facade.exec(bound, ['echo', 'effect'], cwd='/', timeout_s=1, exec_id='missing')
    assert len(spawned) == 1 and bound.binding.container_id in spawned[0]
    assert all(args[0] == 'exec' and 'receipt' in args for args in fake.commands())
    assert await facade.release(bound.use) is False


@pytest.mark.parametrize('helper', ['spill', 'remove'])
async def test_uncertain_internal_helper_fails_use_without_fallback(managed, helper):
    facade, backend, _, _, bound, _ = managed
    original = backend.exec_bound

    async def fail_helper(ticket, argv, **kwargs):
        targeted = (helper == 'remove' and argv[0] == 'rm'
                    or helper == 'spill' and argv[0] == 'sh')
        if targeted:
            backend.failure = SandboxUncertain('helper transport lost')
        try:
            return await original(ticket, argv, **kwargs)
        finally:
            backend.failure = None

    backend.exec_bound = fail_helper
    if helper == 'spill':
        outcome = await invoke(managed, 'terminal', command="python3 -c 'print(\"z\" * 60000)'")
        assert len(backend.exec_calls) == 2  # command and one spill, no ambiguous fallback write
    else:
        outcome = await invoke(managed, 'execute_code', code='print(42)')
        assert len(backend.exec_calls) == 3
    assert outcome.error and 'unavailable' in outcome.content
    assert await facade.release(bound.use) is False


@pytest.mark.parametrize('length', [12, 150_000])
async def test_dispatcher_preserves_sandbox_failure_marker_through_output_cap(managed, monkeypatch, length):
    from keel.tools.base import HANDLERS, MAX_TOOL_RESULT_CHARS

    facade, _, _, _, bound, _ = managed
    failure = SandboxUncertain('x' * length)

    async def unavailable(args, ctx):
        raise failure

    monkeypatch.setitem(HANDLERS, 'authority_test', unavailable)
    ctx = ToolContext(facade, bound)
    outcome = await run_tool('authority_test', '{}', ctx)
    assert outcome.error and outcome.sandbox_error is failure
    assert len(outcome.content) <= MAX_TOOL_RESULT_CHARS
    assert set(json.loads(outcome.content)) == {'error'}
    ordinary = await run_tool('terminal', '{}', ctx)
    assert ordinary.error and ordinary.sandbox_error is None


async def test_script_cleanup_uncertainty_does_not_replace_cancellation(managed):
    facade, backend, _, _, bound, _ = managed
    backend.block = asyncio.Event()
    original = backend.exec_bound

    async def unavailable_cleanup(ticket, argv, **kwargs):
        if argv[0] == 'rm':
            backend.failure = SandboxUncertain('cleanup transport lost')
        return await original(ticket, argv, **kwargs)

    backend.exec_bound = unavailable_cleanup
    task = asyncio.create_task(invoke(managed, 'execute_code', code='print(42)'))
    await asyncio.wait_for(backend.block.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await facade.release(bound.use) is False


@pytest.mark.parametrize('entrypoint', ['facade', 'tool'])
async def test_dispatch_ack_loss_with_store_down_preserves_sandbox_failure(managed, monkeypatch, entrypoint):
    facade, backend, store, _, bound, _ = managed
    backend.failure = RuntimeError('dispatch acknowledgement lost')
    attempts = []

    async def unavailable(*args):
        attempts.append(args)
        raise ConnectionError('coordination store unavailable')

    with monkeypatch.context() as fault:
        fault.setattr(store, 'mark_exec_uncertain', unavailable)
        fault.setattr(store, 'resolve_exec', unavailable)
        if entrypoint == 'facade':
            with pytest.raises(SandboxUncertain) as failure:
                await facade.exec(bound, ['true'], cwd='/', timeout_s=1, exec_id='lost-ack')
            assert isinstance(failure.value.__cause__, RuntimeError)
        else:
            outcome = await invoke(managed, 'terminal', command='true')
            assert outcome.error and isinstance(outcome.sandbox_error, SandboxUncertain)
    assert len(backend.exec_calls) == 1 and backend.control_calls == []
    assert len(attempts) == 1  # one best-effort mark; never replay dispatch or persistence
    ticket = backend.exec_calls[0][0]
    row = await store.db.fetchrow('SELECT state FROM sandbox_execs WHERE id = ?', ticket.exec_id)
    assert row['state'] == 'pending'
    assert await facade.release(bound.use) is False


@pytest.mark.parametrize('entrypoint', ['facade', 'tool'])
async def test_cancel_with_store_down_preserves_cancellation_and_pending_evidence(managed, monkeypatch, entrypoint):
    facade, backend, store, _, bound, _ = managed
    backend.block = asyncio.Event()
    attempts = []

    async def unavailable(*args):
        attempts.append(args)
        raise ConnectionError('coordination store unavailable')

    with monkeypatch.context() as fault:
        fault.setattr(store, 'resolve_exec', unavailable)
        fault.setattr(store, 'mark_exec_uncertain', unavailable)
        if entrypoint == 'facade':
            call = facade.exec(bound, ['bash', '-c', 'true'], cwd='/', timeout_s=60, exec_id='cancel-db-down')
        else:
            call = invoke(managed, 'terminal', command='true')
        task = asyncio.create_task(call)
        await asyncio.wait_for(backend.block.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert backend.control_calls == [] and len(backend.exec_calls) == 1
    assert len(attempts) == 2  # failed authority resolution and one failed uncertainty record
    ticket = backend.exec_calls[0][0]
    row = await store.db.fetchrow('SELECT state FROM sandbox_execs WHERE id = ?', ticket.exec_id)
    assert row['state'] == 'pending'
    assert await facade.release(bound.use) is False


@pytest.mark.parametrize('cancelled', [False, True])
async def test_control_with_failed_uncertainty_record_preserves_primary_failure(managed, monkeypatch, cancelled):
    facade, backend, store, _, bound, _ = managed
    ticket = await store.begin_exec(bound, 'control-db-down')

    async def unavailable(*args):
        raise ConnectionError('coordination store unavailable')

    async def lost_control(*args, **kwargs):
        if cancelled:
            raise asyncio.CancelledError
        raise RuntimeError('control acknowledgement lost')

    backend.control_exec = lost_control
    with monkeypatch.context() as fault:
        fault.setattr(store, 'mark_exec_uncertain', unavailable)
        with pytest.raises(asyncio.CancelledError if cancelled else SandboxUncertain):
            await facade.kill_tree(bound, ticket.exec_id)
    row = await store.db.fetchrow('SELECT state FROM sandbox_execs WHERE id = ?', ticket.exec_id)
    assert row['state'] == 'pending'
    assert await facade.release(bound.use) is False


async def test_control_store_outage_is_typed_without_signalling(managed, monkeypatch):
    facade, backend, store, _, bound, _ = managed
    ticket = await store.begin_exec(bound, 'unverified-authority')

    async def unavailable(*args):
        raise ConnectionError('coordination store unavailable')

    with monkeypatch.context() as fault:
        fault.setattr(store, 'resolve_exec', unavailable)
        with pytest.raises(SandboxUncertain) as failure:
            await facade.kill_tree(bound, ticket.exec_id)
        assert isinstance(failure.value.__cause__, ConnectionError)
    assert backend.control_calls == []
    assert await facade.release(bound.use) is False


async def test_pre_admission_store_error_is_not_suppressed(managed, monkeypatch):
    facade, backend, store, _, bound, _ = managed

    async def rejected(*args):
        raise RuntimeError('unrelated admission failure')

    monkeypatch.setattr(store, 'begin_exec', rejected)
    with pytest.raises(RuntimeError, match='unrelated admission failure'):
        await facade.exec(bound, ['true'], cwd='/', timeout_s=1, exec_id='not-admitted')
    assert backend.exec_calls == [] and backend.control_calls == []
