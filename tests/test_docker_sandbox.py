import asyncio
import os
import subprocess
from pathlib import Path
import uuid

import pytest

from keel.sandbox.base import (TIMEOUT_EXIT_CODE, SandboxError, parse_policy,
                               profile_mount_root)
from keel.sandbox.docker import DockerSandbox

IMAGE = os.environ.get("KEEL_TEST_DOCKER_IMAGE", "python:3.12-slim")
needs_docker = pytest.mark.skipif(os.environ.get("KEEL_TEST_DOCKER") != "1", reason="set KEEL_TEST_DOCKER=1")


def test_the_container_name_is_stable_per_deployment_and_profile():
    a = DockerSandbox(deployment="d1")
    assert a.container_name("p1") == a.container_name("p1") != a.container_name("p2")
    assert a.container_name("p1") != DockerSandbox(deployment="d2").container_name("p1")
    assert a.container_name("p1").startswith("keel-sbx-")


def test_run_args_carry_the_policy(tmp_path):
    root = str(tmp_path / "mounts")
    source = profile_mount_root(root, "p1") + "/h"
    policy = parse_policy({"image": "img:1", "workdir": "/w", "runtime": "runsc", "network": "net",
                           "mounts": [{"source": source, "target": "/t", "read_only": True}], "env": {"A": "1"},
                           "user": "10:10", "cpus": 1, "memory_mb": 256, "pids_limit": 32},
                          profile_mount_root(root, "p1"))
    args = DockerSandbox(deployment="d1", mount_root=root).run_args("p1", policy, "/env/file")

    def has(flag, value):
        return any(args[i] == flag and args[i + 1] == value for i in range(len(args) - 1))

    for flag, value in (("--runtime", "runsc"), ("--network", "net"), ("--user", "10:10"), ("--cpus", "1.0"),
                        ("--memory", "256m"), ("--pids-limit", "32"), ("--workdir", "/w"), ("--env-file", "/env/file"),
                        ("--cap-drop", "ALL"), ("--cap-add", "DAC_OVERRIDE"), ("--cap-add", "CHOWN"),
                        ("--cap-add", "FOWNER"), ("--security-opt", "no-new-privileges"),
                        ("--tmpfs", "/tmp:rw,nosuid,size=512m"), ("--tmpfs", "/var/tmp:rw,nosuid"),
                        ("--tmpfs", "/run:rw,nosuid"),
                        ("--mount", f"type=bind,source={source},target=/t,readonly"),
                        ("--label", f"keel.policy={policy.digest()}"), ("--label", "keel.deployment=d1")):
        assert has(flag, value), (flag, value)
    assert "A" not in args and not any(a.startswith("A=") or a == "1" for a in args)
    assert args[:4] == ["run", "-d", "--init", "--name"]
    assert args[-3:] == ["img:1", "sleep", "infinity"]


def test_recreating_a_container_rechecks_a_retarged_host_symlink(tmp_path):
    root = str(tmp_path / "mounts")
    own = Path(profile_mount_root(root, "p1"))
    other = Path(profile_mount_root(root, "p2"))
    own.mkdir(parents=True)
    other.mkdir(parents=True)
    alias = own / "alias"
    alias.symlink_to(own, target_is_directory=True)
    policy = parse_policy({"image": "i", "workdir": "/w", "mounts": [
        {"source": str(alias), "target": "/data"}]}, str(own))
    alias.unlink()
    alias.symlink_to(other, target_is_directory=True)
    with pytest.raises(SandboxError, match="mount policy"):
        DockerSandbox(mount_root=root).run_args("p1", policy)


async def test_a_missing_docker_cli_is_a_sandbox_error():
    backend = DockerSandbox(docker="keel-no-such-docker")
    with pytest.raises(SandboxError):
        await backend.ensure("p1", parse_policy({"image": "i", "workdir": "/w"}))


@pytest.fixture
async def docker(tmp_path):
    backend = DockerSandbox(deployment=f"test-{uuid.uuid4().hex[:8]}", kill_grace_s=1.0,
                            mount_root=str(tmp_path / "mounts"))
    yield backend
    remove_deployment(backend.deployment)


def remove_deployment(deployment: str) -> None:
    """Test cleanup by exact ID: remove the containers labelled with this test's own deployment."""
    listed = subprocess.run(["docker", "ps", "-a", "-q", "--no-trunc", "--filter", "label=keel.managed=1",
                             "--filter", f"label=keel.deployment={deployment}"],
                            capture_output=True, text=True, timeout=60)
    if listed.returncode == 0 and listed.stdout.split():
        subprocess.run(["docker", "rm", "-f", *listed.stdout.split()], capture_output=True, timeout=120)


async def test_bound_sandboxes_never_reach_the_name_based_exec_retry_or_cleanup():
    # The coordinated path replaced transparent mid-run recreation and PID-marker cleanup: a bound handle given
    # to the legacy name-based methods is refused before any docker call (see test_sandbox_lifecycle_docker.py).
    from keel.sandbox.records import BoundSandbox, SandboxBinding, SandboxKey, UseHandle

    backend = DockerSandbox(docker="keel-no-such-docker", deployment="unit")
    sandbox_policy = parse_policy({"image": "i", "workdir": "/w"})
    binding = SandboxBinding(1, "t" * 32, "c" * 64, "keel-sbx-unit", sandbox_policy, "h" * 64)
    use = UseHandle(SandboxKey("unit", "p1", "daemon"), "use-1", "u" * 32, "w1")
    bound = BoundSandbox(profile_id="p1", name="c" * 64, policy=sandbox_policy, use=use, binding=binding)
    with pytest.raises(SandboxError, match="coordinator facade"):
        await backend.exec(bound, ["true"], cwd="/", timeout_s=1, exec_id="e")
    with pytest.raises(SandboxError, match="coordinator facade"):
        await backend.kill_tree(bound, "e")


def policy(**extra):
    return parse_policy({"image": IMAGE, "workdir": "/work", **extra}, "/")


@needs_docker
async def test_exec_keeps_cwd_env_and_exit_code(docker):
    sandbox = await docker.ensure("p1", policy(env={"KEEL_V": "x"}))
    result = await docker.exec(sandbox, ["sh", "-c", 'mkdir -p sub && cd sub && pwd && echo "$KEEL_V $E" && exit 3'],
                               cwd="/work", timeout_s=30, exec_id="e1", env={"E": "y"})
    assert result.exit_code == 3 and result.output.splitlines() == ["/work/sub", "x y"]


@needs_docker
async def test_files_round_trip(docker):
    sandbox = await docker.ensure("p1", policy())
    await docker.write_file(sandbox, "/work/a/b.bin", b"\x00\x01hello")
    assert await docker.read_file(sandbox, "/work/a/b.bin", 100) == b"\x00\x01hello"
    with pytest.raises(FileNotFoundError):
        await docker.read_file(sandbox, "/work/missing", 10)
    with pytest.raises(IsADirectoryError):
        await docker.read_file(sandbox, "/work/a", 10)


@needs_docker
async def test_two_profiles_keep_private_files_and_env_separate(docker, tmp_path):
    left = Path(profile_mount_root(str(tmp_path / "mounts"), "p1"))
    right = Path(profile_mount_root(str(tmp_path / "mounts"), "p2"))
    left.mkdir(parents=True)
    right.mkdir(parents=True)
    (left / "marker").write_text("from-left")
    (right / "marker").write_text("from-right")
    a = await docker.ensure("p1", parse_policy({"image": IMAGE, "workdir": "/work", "env": {"KEEL_TOKEN": "token-left"},
                                                "mounts": [{"source": str(left), "target": "/private"}]}, str(left)))
    b = await docker.ensure("p2", parse_policy({"image": IMAGE, "workdir": "/work", "env": {"KEEL_TOKEN": "token-right"},
                                                "mounts": [{"source": str(right), "target": "/private"}]}, str(right)))
    await docker.write_file(a, "/work/only-a", b"marker-a")
    result = await docker.exec(b, ["sh", "-c", 'echo "$KEEL_TOKEN"; ls /work; cat /private/marker'],
                               cwd="/work", timeout_s=30, exec_id="isolation")
    assert result.exit_code == 0 and a.name != b.name
    assert "token-right" in result.output and "token-left" not in result.output
    assert "from-right" in result.output and "from-left" not in result.output
    assert "only-a" not in result.output and "marker-a" not in result.output


@needs_docker
async def test_a_timeout_kills_the_process_tree(docker):
    sandbox = await docker.ensure("p1", policy())
    result = await docker.exec(sandbox, ["sh", "-c", "(sleep 2; touch /work/late) & sleep 30"], cwd="/work",
                               timeout_s=1, exec_id="e2")
    assert result.timed_out and result.exit_code == TIMEOUT_EXIT_CODE
    check = await docker.exec(sandbox, ["sh", "-c", "sleep 3; ls /work"], cwd="/work", timeout_s=30, exec_id="e3")
    assert "late" not in check.output


@needs_docker
async def test_kill_tree_after_a_cancelled_exec(docker):
    sandbox = await docker.ensure("p1", policy())
    task = asyncio.create_task(docker.exec(sandbox, ["sh", "-c", "sleep 3; touch /work/late"], cwd="/work",
                                           timeout_s=60, exec_id="e4"))
    await asyncio.sleep(1.5)
    task.cancel()  # kills only the docker client; the command keeps running in the container
    with pytest.raises(asyncio.CancelledError):
        await task
    await docker.kill_tree(sandbox, "e4")
    check = await docker.exec(sandbox, ["sh", "-c", "sleep 3; ls /work"], cwd="/work", timeout_s=30, exec_id="e5")
    assert "late" not in check.output


@needs_docker
async def test_an_unreadable_file_is_a_permission_error(docker):
    sandbox = await docker.ensure("p1", policy(user="1000:1000"))
    result = await docker.exec(sandbox, ["sh", "-c", "echo x > /tmp/s && chmod 000 /tmp/s"], cwd="/", timeout_s=30,
                               exec_id="e7")
    assert result.exit_code == 0, result.output
    with pytest.raises(PermissionError):
        await docker.read_file(sandbox, "/tmp/s", 10)


@needs_docker
async def test_a_large_read_is_clamped_to_what_fits(docker):
    from keel.sandbox.docker import _MAX_READ
    sandbox = await docker.ensure("p1", policy())
    data = bytes(range(256)) * (7_000_000 // 256 + 1)
    data = data[:7_000_000]
    await docker.write_file(sandbox, "/work/big", data)
    got = await docker.read_file(sandbox, "/work/big", 10 * 1024 * 1024)
    assert len(got) == _MAX_READ and got == data[:_MAX_READ]


# --- final fix wave ---

FAKE_DOCKER = """#!/bin/sh
echo "$@" >> "$KEEL_FAKE_LOG"
echo "client PATH=$PATH HOME=$HOME" >> "$KEEL_FAKE_LOG"
while [ $# -gt 0 ]; do
  if [ "$1" = "--env-file" ]; then
    cp "$2" "$KEEL_FAKE_ENVCOPY"
    ls -ld "$(dirname "$2")" "$2" >> "$KEEL_FAKE_LOG"
    echo "$2" > "$KEEL_FAKE_ENVPATH"
  fi
  shift
done
case "$1" in inspect) exit 1;; esac
exit 0
"""


async def test_policy_env_goes_through_an_env_file_and_leaves_the_client_alone(tmp_path, monkeypatch):
    fake = tmp_path / "fake-docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(0o755)
    for name, value in (("KEEL_FAKE_LOG", tmp_path / "log"), ("KEEL_FAKE_ENVCOPY", tmp_path / "envcopy"),
                        ("KEEL_FAKE_ENVPATH", tmp_path / "envpath")):
        monkeypatch.setenv(name, str(value))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/client-home")
    backend = DockerSandbox(docker=str(fake), deployment="unit")
    secret = "s3cr3t" + " value=with $pecial chars"
    sandbox = await backend.ensure("p1", parse_policy({"image": "i", "workdir": "/w",
                                                       "env": {"PATH": "/policy/bin", "HOME": "/policy-home",
                                                               "TOKEN": secret}}))
    log = (tmp_path / "log").read_text()
    assert secret not in log and "/policy/bin" not in log and "/policy-home" not in log  # not on argv
    assert "client PATH=/usr/bin:/bin HOME=/client-home" in log  # the client's own environment is untouched
    assert "--env-file" in log and "drwx------" in log and "-rw-------" in log
    assert (tmp_path / "envcopy").read_text().splitlines() == ["HOME=/policy-home", "PATH=/policy/bin", f"TOKEN={secret}"]
    assert not Path((tmp_path / "envpath").read_text().strip()).exists()  # removed once docker is done
    (tmp_path / "log").write_text("")
    await backend.exec(sandbox, ["true"], cwd="/", timeout_s=5, exec_id="e1", env={"X": "exec-secret"})
    assert "exec-secret" not in (tmp_path / "log").read_text()
    assert (tmp_path / "envcopy").read_text() == "X=exec-secret\n"


@needs_docker
async def test_a_policy_with_path_and_home_still_creates_the_container(docker):
    sandbox = await docker.ensure("p1", policy(env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/work",
                                                    "KEEL_V": "a b=c $x"}))
    result = await docker.exec(sandbox, ["sh", "-c", 'echo "$HOME|$KEEL_V|$PATH"'], cwd="/work", timeout_s=30,
                               exec_id="e8", env={"E": "y z"})
    assert result.output.strip() == "/work|a b=c $x|/usr/local/bin:/usr/bin:/bin"
    result = await docker.exec(sandbox, ["sh", "-c", 'echo "$E"'], cwd="/work", timeout_s=30, exec_id="e9",
                               env={"E": "y z"})
    assert result.output.strip() == "y z"


@needs_docker
async def test_the_container_is_hardened_and_scratch_space_works(docker):
    sandbox = await docker.ensure("p1", policy())
    script = ('grep CapEff /proc/self/status; grep NoNewPrivs /proc/self/status; '
              'for d in /tmp /var/tmp /run; do touch $d/x && echo "$d ok"; done; '
              'printf "echo from-tmp\\n" > /tmp/s.sh && sh /tmp/s.sh; mount | grep -c "tmpfs on /tmp .*nosuid"')
    result = await docker.exec(sandbox, ["sh", "-c", script], cwd="/work", timeout_s=30, exec_id="e10")
    out = result.output
    assert "NoNewPrivs:\t1" in out and "/tmp ok" in out and "/var/tmp ok" in out and "/run ok" in out
    assert "from-tmp" in out and out.strip().endswith("1")
    cap = next(line for line in out.splitlines() if line.startswith("CapEff")).split()[1]
    assert int(cap, 16) == (1 << 1) | (1 << 0) | (1 << 3)  # DAC_OVERRIDE, CHOWN, FOWNER


@needs_docker
async def test_shell_tools_recover_from_a_vanished_cwd_in_docker(docker):
    import json
    from keel.tools import run_tool
    from keel.tools.base import ToolContext
    sandbox = await docker.ensure("p1", policy())
    ctx = ToolContext(docker, sandbox, cwd="/work/gone", tool_call_id="c1")
    out = json.loads((await run_tool("terminal", json.dumps({"command": "pwd"}), ctx)).content)
    assert out["exit_code"] == 0 and out["cwd"] == "/work" == ctx.cwd and "no longer exists" in out["output"]
    ctx.cwd = "/work/gone2"
    out = json.loads((await run_tool("execute_code", json.dumps({"code": "print(7)"}), ctx)).content)
    assert out["exit_code"] == 0 and "7" in out["output"] and ctx.cwd == "/work"
    outcome = await run_tool("terminal", json.dumps({"command": "pwd", "workdir": "/nope"}), ctx)
    assert outcome.error and "does not exist" in json.loads(outcome.content)["error"]


@needs_docker
async def test_skills_are_discovered_with_one_exec_in_docker(docker):
    from keel.skills import discover_skills
    sandbox = await docker.ensure("p1", policy())
    for name, category in (("alpha", "cat"), ("beta", None)):
        directory = f"/work/skills/{category + '/' if category else ''}{name}"
        await docker.write_file(sandbox, f"{directory}/SKILL.md",
                                f"---\r\nname: {name}\r\ndescription: about {name}\r\n---\r\nBody\r\n".encode())
    skills = await discover_skills(docker, sandbox, "/work/skills", ["alpha", "beta"])
    assert [(s.name, s.category, s.description) for s in skills] == [
        ("beta", None, "about beta"), ("alpha", "cat", "about alpha")]
