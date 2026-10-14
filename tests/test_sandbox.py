import asyncio
import os
import time

import pytest

from keel.sandbox import base
from keel.sandbox.base import TIMEOUT_EXIT_CODE, Mount, PolicyError, parse_policy, private_mount_conflict
from keel.sandbox.local import LocalSandbox


def test_a_full_policy_parses():
    policy = parse_policy({
        "image": "img:1", "workdir": "/work", "runtime": "runsc", "network": "net",
        "mounts": [{"source": "/host/a", "target": "/a", "read_only": True}], "env": {"B": "2", "A": "1"},
        "user": "1000:1000", "cpus": 1.5, "memory_mb": 512, "pids_limit": 64, "skills_path": "/skills"})
    assert policy.mounts == (Mount("/host/a", "/a", True),)
    assert policy.env == (("A", "1"), ("B", "2"))
    assert (policy.cpus, policy.memory_mb, policy.pids_limit) == (1.5, 512, 64)


def test_the_digest_follows_the_policy():
    a = parse_policy({"image": "img", "workdir": "/w"})
    assert a.digest() == parse_policy({"image": "img", "workdir": "/w"}).digest()
    assert a.digest() != parse_policy({"image": "img", "workdir": "/w", "cpus": 2}).digest()


@pytest.mark.parametrize("data, message", [
    ({"workdir": "/w"}, "image"),
    ({"image": "i", "workdir": "rel"}, "workdir"),
    ({"image": "i", "workdir": "/w", "runtime": "lxc"}, "runtime"),
    ({"image": "i", "workdir": "/w", "mounts": [{"source": "/a,b", "target": "/t"}]}, "mounts"),
    ({"image": "i", "workdir": "/w", "env": {"1BAD": "x"}}, "env"),
    ({"image": "i", "workdir": "/w", "env": {"A": 1}}, "env"),
    ({"image": "i", "workdir": "/w", "user": "a b"}, "user"),
    ({"image": "i", "workdir": "/w", "cpus": 0}, "cpus"),
    ({"image": "i", "workdir": "/w", "memory_mb": True}, "memory_mb"),
])
def test_bad_policies_are_refused(data, message):
    with pytest.raises(PolicyError, match=message):
        parse_policy(data)


def test_mount_sources_can_be_confined_to_a_root():
    parse_policy({"image": "i", "workdir": "/w", "mounts": [{"source": "/srv/x", "target": "/x"}]}, "/srv")
    with pytest.raises(PolicyError, match="under /srv"):
        parse_policy({"image": "i", "workdir": "/w", "mounts": [{"source": "/etc", "target": "/x"}]}, "/srv")
    with pytest.raises(PolicyError, match="under /srv"):
        parse_policy({"image": "i", "workdir": "/w", "mounts": [{"source": "/srv/../etc", "target": "/x"}]}, "/srv")


def test_private_mounts_may_not_overlap_another_profile():
    def policy(source, read_only=False):
        return parse_policy({"image": "i", "workdir": "/work", "mounts": [
            {"source": source, "target": "/data", "read_only": read_only}]})

    assert private_mount_conflict(policy("/data/one/child"), [policy("/data/one")]) is not None
    assert private_mount_conflict(policy("/data/one", True), [policy("/data/one")]) is not None
    assert private_mount_conflict(policy("/data/one"), [policy("/data/one", True)]) is not None
    assert private_mount_conflict(policy("/data/one", True), [policy("/data/one", True)]) is not None
    assert private_mount_conflict(policy("/data/one"), [policy("/data/one-other")]) is None


def test_private_mounts_detect_a_host_symlink_alias(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(private, target_is_directory=True)

    def policy(source):
        return parse_policy({"image": "i", "workdir": "/work", "mounts": [
            {"source": str(source), "target": "/data"}]})

    assert private_mount_conflict(policy(alias), [policy(private)]) is not None


def test_mount_root_checks_the_resolved_host_path(tmp_path):
    root = tmp_path / "root"
    private = tmp_path / "private"
    root.mkdir()
    private.mkdir()
    (root / "alias").symlink_to(private, target_is_directory=True)
    with pytest.raises(PolicyError, match="under"):
        parse_policy({"image": "i", "workdir": "/work", "mounts": [
            {"source": str(root / "alias"), "target": "/data"}]}, str(root))


@pytest.fixture
async def local(tmp_path):
    backend = LocalSandbox(kill_grace_s=0.5)
    sandbox = await backend.ensure("p1", parse_policy({"image": "local", "workdir": str(tmp_path / "work"),
                                                       "env": {"KEEL_TEST_VAR": "from-policy"}}))
    return backend, sandbox, tmp_path / "work"


async def test_exec_runs_in_the_cwd_with_policy_env_and_stdin(local):
    backend, sandbox, work = local
    result = await backend.exec(sandbox, ["sh", "-c", 'pwd; echo "$KEEL_TEST_VAR $EXTRA"; cat'], cwd=str(work),
                                timeout_s=10, exec_id="e1", env={"EXTRA": "x"}, stdin=b"piped")
    assert result.exit_code == 0 and not result.timed_out
    lines = result.output.splitlines()
    assert os.path.realpath(lines[0]) == os.path.realpath(work)  # realpath absorbs macOS's /private/var symlink
    assert lines[1:] == ["from-policy x", "piped"]


async def test_exit_codes_and_stderr_are_kept(local):
    backend, sandbox, work = local
    result = await backend.exec(sandbox, ["sh", "-c", "echo oops >&2; exit 3"], cwd=str(work), timeout_s=10,
                                exec_id="e2")
    assert (result.exit_code, result.output.strip()) == (3, "oops")


async def test_a_timeout_kills_the_whole_process_group(local):
    backend, sandbox, work = local
    started = time.monotonic()
    result = await backend.exec(sandbox, ["sh", "-c", "(sleep 1; touch late) & sleep 30"], cwd=str(work),
                                timeout_s=0.5, exec_id="e3")
    assert result.timed_out and result.exit_code == TIMEOUT_EXIT_CODE
    assert time.monotonic() - started < 8
    await asyncio.sleep(1.5)
    assert not (work / "late").exists()  # the background child was in the killed group


async def test_kill_tree_ends_a_running_command(local):
    backend, sandbox, work = local
    task = asyncio.create_task(backend.exec(sandbox, ["sh", "-c", "(sleep 1; touch late) & sleep 30"],
                                            cwd=str(work), timeout_s=60, exec_id="e4"))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await backend.kill_tree(sandbox, "e4")  # cancelling killed only `sh`; the group still holds the child
    await asyncio.sleep(1.5)
    assert not (work / "late").exists()
    await backend.kill_tree(sandbox, "unknown")  # a no-op


async def test_files_round_trip_and_fail_like_files(local):
    backend, sandbox, work = local
    await backend.write_file(sandbox, str(work / "a" / "b.txt"), b"hello")
    assert await backend.read_file(sandbox, str(work / "a" / "b.txt"), 3) == b"hel"
    with pytest.raises(FileNotFoundError):
        await backend.read_file(sandbox, str(work / "missing"), 10)
    with pytest.raises(IsADirectoryError):
        await backend.read_file(sandbox, str(work / "a"), 10)


async def test_a_missing_program_is_exit_127(local):
    backend, sandbox, work = local
    result = await backend.exec(sandbox, ["keel-no-such-program"], cwd=str(work), timeout_s=5, exec_id="e5")
    assert result.exit_code == 127


@pytest.mark.parametrize("data, message", [
    ({"image": "i", "workdir": "/w", "memory_mb": 0.5}, "memory_mb"),
    ({"image": "i", "workdir": "/w", "pids_limit": 0.9}, "pids_limit"),
    ({"image": "i", "workdir": "/w", "pids_limit": 1.5}, "pids_limit"),
    ({"image": "i", "workdir": "/w", "cpus": float("nan")}, "cpus"),
    ({"image": "i", "workdir": "/w", "cpus": float("inf")}, "cpus"),
    ({"image": "--privileged", "workdir": "/w"}, "image"),
    ({"image": "a b", "workdir": "/w"}, "image"),
    ({"image": "a\nb", "workdir": "/w"}, "image"),
    ({"image": "i", "workdir": "/w", "env": {"A": "x\0y"}}, "env"),
    ({"image": "i", "workdir": "/w", "user": "-u"}, "user"),
    ({"image": "i", "workdir": "/w", "bogus": 1}, "bogus"),
    ({"image": "i", "workdir": "/w", "mounts": [{"source": "/a", "target": "/t", "mode": "rw"}]}, "mode"),
])
def test_unsafe_policies_are_refused(data, message):
    with pytest.raises(PolicyError, match=message):
        parse_policy(data)


async def test_large_stdin_does_not_deadlock_a_streaming_command(local):
    backend, sandbox, work = local
    payload = b"x" * (1024 * 1024)
    started = time.monotonic()
    result = await backend.exec(sandbox, ["cat"], cwd=str(work), timeout_s=20, exec_id="e6", stdin=payload)
    assert result.exit_code == 0 and not result.timed_out
    assert len(result.output) == len(payload)
    assert time.monotonic() - started < 10


async def test_a_background_child_holding_stdout_does_not_hold_the_command(local, monkeypatch):
    backend, sandbox, work = local
    monkeypatch.setattr(base, "DRAIN_S", 0.3)
    started = time.monotonic()
    result = await backend.exec(sandbox, ["sh", "-c", "sleep 5 & echo done"], cwd=str(work), timeout_s=20,
                                exec_id="e7")
    assert (result.exit_code, result.timed_out, result.output.strip()) == (0, False, "done")
    assert time.monotonic() - started < 4


async def test_output_past_the_cap_is_marked(local, monkeypatch):
    backend, sandbox, work = local
    monkeypatch.setattr(base, "MAX_OUTPUT_BYTES", 100)
    result = await backend.exec(sandbox, ["sh", "-c", "head -c 1000 /dev/zero | tr '\\0' a"], cwd=str(work),
                                timeout_s=10, exec_id="e8")
    assert result.output.startswith("a" * 100 + "\n[output truncated at 100 bytes]")
    assert result.output.splitlines()[0] == "a" * 100


async def _spawn_sleeper():
    return await asyncio.create_subprocess_exec(
        "sleep", "30", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT)


async def test_cancelling_collect_during_on_timeout_kills_the_process_and_leaves_no_task():
    proc = await _spawn_sleeper()
    entered = asyncio.Event()

    async def slow_on_timeout():
        entered.set()
        await asyncio.sleep(60)

    before = asyncio.all_tasks()
    task = asyncio.create_task(base.collect(proc, None, 0.1, slow_on_timeout))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(proc.wait(), 5)
    assert proc.returncode is not None
    assert asyncio.all_tasks() <= before


async def test_a_failing_on_timeout_still_kills_the_process_and_reports_the_timeout():
    proc = await _spawn_sleeper()

    async def broken():
        raise RuntimeError("boom")

    output, timed_out = await base.collect(proc, None, 0.1, broken)
    assert timed_out and proc.returncode is not None
    assert proc.returncode != 0


async def test_a_cancelled_kill_tree_can_be_retried(local):
    backend, sandbox, work = local
    backend.kill_grace_s = 5
    task = asyncio.create_task(backend.exec(sandbox, ["sh", "-c", "trap '' TERM; sleep 30"], cwd=str(work),
                                            timeout_s=60, exec_id="e9"))
    await asyncio.sleep(0.3)
    pgid = backend._groups["e9"]
    killer = asyncio.create_task(backend.kill_tree(sandbox, "e9"))
    await asyncio.sleep(0.3)
    killer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await killer
    os.killpg(pgid, 0)  # still alive: TERM is ignored
    backend.kill_grace_s = 0.2
    await backend.kill_tree(sandbox, "e9")
    result = await asyncio.wait_for(task, 10)
    assert result.exit_code != 0
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


async def test_remove_orphans_spares_profiles_with_a_run_in_flight(tmp_path):
    from keel.sandbox.docker import DockerSandbox

    log = tmp_path / "log"
    fake = tmp_path / "docker"
    fake.write_text('#!/bin/sh\necho "$@" >> "' + str(log) + '"\n'
                    'case "$1" in ps) printf "c1 p1\\nc2 p2\\nc3 p3\\n";; esac\nexit 0\n')
    fake.chmod(0o755)
    assert await DockerSandbox(docker=str(fake), deployment="t").remove_orphans({"p2"}) == 2
    calls = log.read_text().splitlines()
    assert calls[0].startswith("ps -a --filter label=keel.managed=1 --filter label=keel.deployment=t --format")
    assert calls[1] == "rm -f c1 c3"


def test_reference_names_the_sandbox_ensure_would_return_without_touching_anything(tmp_path):
    from keel.sandbox.docker import DockerSandbox

    policy = parse_policy({"image": "local", "workdir": str(tmp_path / "w")})
    local = LocalSandbox().reference("p1", policy)
    assert (local.profile_id, local.name, local.policy) == ("p1", "local-p1", policy)
    assert not (tmp_path / "w").exists()
    docker = DockerSandbox(docker="keel-no-such-docker", deployment="t")
    assert docker.reference("p1", policy) == base.Sandbox("p1", docker.container_name("p1"), policy)


async def test_killing_a_lost_command_in_a_missing_container_creates_nothing(store, tmp_path):
    from keel.events import InMemoryEventBus
    from keel.providers.base import ProviderRegistry
    from keel.runner import RunExecutor
    from keel.sandbox.docker import DockerSandbox

    log = tmp_path / "log"
    fake = tmp_path / "docker"
    fake.write_text('#!/bin/sh\necho "$@" >> "' + str(log) + '"\n'
                    'case "$1" in exec) echo "Error response from daemon: No such container: c" >&2; exit 1;; esac\n'
                    'exit 0\n')
    fake.chmod(0o755)
    await store.upsert_profile("p1", models=[{"provider": "fake", "model": "m"}], soul="", skills=[],
                               sandbox={"image": "img", "workdir": "/w"})
    await store.ensure_session("p1", "s1")
    run = await store.create_run(profile_id="p1", session_id="s1", provider="fake", model="m",
                                 reasoning_effort=None, input_parts=[{"type": "text", "text": "hi"}],
                                 instructions=None)
    await store.set_current_exec(run.id, "x1")
    backend = DockerSandbox(docker=str(fake), deployment="t", kill_grace_s=0.5)
    executor = RunExecutor(store, InMemoryEventBus(), ProviderRegistry(), sandbox=backend)
    await executor.kill_orphaned_command(await store.get_run_by_id(run.id))
    calls = log.read_text().splitlines()
    assert calls and all(call.startswith(f"exec {backend.container_name('p1')} sh -c ") for call in calls), calls
    assert (await store.get_run_by_id(run.id)).current_exec_id is None
