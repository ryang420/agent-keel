"""Protocol codecs and Linux supervisor behavior.

The codec tests run everywhere. Every test that builds or executes the native helper is marked Linux-only with an
explicit skip reason; they are never silently treated as passing on macOS.
"""

from __future__ import annotations

import json
import os
import random
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from keel.sandbox.records import CleanupReceipt
from keel.sandbox.runtime import decode_receipt, encode_control

REPO_ROOT = Path(__file__).resolve().parents[1]
SUPERVISOR_SOURCE = REPO_ROOT / "native" / "supervisor"
LINUX_ONLY = pytest.mark.skipif(sys.platform != "linux", reason="the supervisor helper is Linux-only")


# ---------------------------------------------------------------------------
# Codec tests: available on every platform.
# ---------------------------------------------------------------------------

def _receipt_value(**overrides):
    value = {
        "version": 1,
        "exec_id": "exec-1",
        "control_token": uuid.uuid4().hex,
        "leader_status": 0,
        "control_closed": True,
        "descendants_complete": False,
        "uncertain_reason": "descendants_unverified",
    }
    value.update(overrides)
    return value


def test_control_frame_is_exactly_kel1_tokens_and_a_cancel_byte():
    control = uuid.uuid4().hex
    usage = uuid.uuid4().hex
    frame = encode_control(control, usage, cancel=True)

    assert frame == b"KEL1" + bytes.fromhex(control) + bytes.fromhex(usage) + b"\x01"
    assert len(frame) == 37
    assert encode_control(control, usage, cancel=False)[-1] == 0


@pytest.mark.parametrize("control,usage", [
    ("x" * 32, uuid.uuid4().hex),
    (uuid.uuid4().hex, "z" * 32),
    (uuid.uuid4().hex.upper(), uuid.uuid4().hex),
    (uuid.uuid4().hex[:-1], uuid.uuid4().hex),
    ("g" * 32, uuid.uuid4().hex),
])
def test_control_frame_rejects_non_lowercase_hex_tokens(control, usage):
    with pytest.raises(ValueError):
        encode_control(control, usage, cancel=False)


def test_control_frame_rejects_non_boolean_cancel():
    with pytest.raises(ValueError):
        encode_control(uuid.uuid4().hex, uuid.uuid4().hex, cancel=1)


def test_receipt_decoder_accepts_the_exact_versioned_field_set():
    control = uuid.uuid4().hex
    receipt = CleanupReceipt("exec-1", control, 7, True, False, "descendants_unverified")

    assert decode_receipt(json.dumps(_receipt_value(control_token=control, leader_status=7)).encode()) == receipt
    nullable = _receipt_value(control_token=control, leader_status=None, uncertain_reason=None)
    assert decode_receipt(json.dumps(nullable).encode()) == CleanupReceipt(
        "exec-1", control, None, True, False, None)


@pytest.mark.parametrize("value", [
    {**_receipt_value(), "extra": True},
    {key: value for key, value in _receipt_value().items() if key != "uncertain_reason"},
    {**_receipt_value(), "version": 2},
    {**_receipt_value(), "version": True},
    {**_receipt_value(), "version": 1.0},
    {**_receipt_value(), "leader_status": True},
    {**_receipt_value(), "leader_status": 256},
    {**_receipt_value(), "leader_status": -1},
    {**_receipt_value(), "control_closed": 1},
    {**_receipt_value(), "descendants_complete": "true"},
    {**_receipt_value(), "exec_id": ""},
    {**_receipt_value(), "exec_id": "x" * 129},
    {**_receipt_value(), "exec_id": "exec\u0000id"},
    {**_receipt_value(), "exec_id": "exec id"},
    {**_receipt_value(), "control_token": uuid.uuid4().hex.upper()},
    {**_receipt_value(), "control_token": "not-a-token"},
    {**_receipt_value(), "uncertain_reason": ""},
    {**_receipt_value(), "uncertain_reason": "x" * 513},
    {**_receipt_value(), "uncertain_reason": "reason with spaces"},
])
def test_receipt_decoder_rejects_malformed_or_unbounded_records(value):
    with pytest.raises(ValueError):
        decode_receipt(json.dumps(value).encode())


@pytest.mark.parametrize("data", [
    b"",
    b"x" * 5000,
    b"not json",
    b"[]",
    b"null",
    b"{" + b'"version":1}' * 400,
    b'{"version":1,"exec_id":"exec-1"}',
    b'\xff\xfe',
])
def test_receipt_decoder_rejects_oversize_empty_or_non_object_input(data):
    with pytest.raises(ValueError):
        decode_receipt(data)


# ---------------------------------------------------------------------------
# Linux harness.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def supervisor_binaries():
    if sys.platform != "linux":
        pytest.skip("the supervisor helper is Linux-only")
    if shutil.which("make") is None:
        pytest.fail("the Linux supervisor tests require make; install the build toolchain")
    compiler = shutil.which("cc") or shutil.which("gcc")
    if compiler is None:
        pytest.fail("the Linux supervisor tests require a C compiler (cc or gcc)")
    build = Path(tempfile.mkdtemp(prefix="keel-supervisor-build-", dir="/tmp"))
    result = subprocess.run(
        ["make", "-C", str(SUPERVISOR_SOURCE), f"BUILD={build}", f"CC={compiler}", "all",
         "supervisor-harness", "pid-reuse"],
        capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        pytest.fail(f"supervisor build failed: {result.stdout}\n{result.stderr}")
    yield {"production": build / "keel-supervisor", "harness": build / "supervisor-harness",
           "pid_reuse": build / "pid-reuse"}
    shutil.rmtree(build, ignore_errors=True)


@pytest.fixture(scope="session")
def exec_root():
    if sys.platform != "linux":
        pytest.skip("the supervisor helper is Linux-only")
    root = Path(tempfile.mkdtemp(prefix="keel-supervisor-", dir="/tmp"))
    yield root
    shutil.rmtree(root, ignore_errors=True)


@dataclass
class Launch:
    process: subprocess.Popen
    root: Path
    workdir: Path
    control: str
    usage: str
    exec_id: str
    trace_path: Path
    barrier_dir: Path
    stdout_buffer: bytes = b""

    @property
    def directory(self) -> Path:
        return self.root / str(os.geteuid()) / self.control

    @property
    def socket_path(self) -> Path:
        return self.directory / "control.sock"

    def events(self) -> list[dict]:
        return read_events(self.trace_path)

    def kinds(self) -> list[str]:
        return [event["kind"] for event in self.events()]

    def wait_for(self, kind: str, timeout: float = 10.0) -> dict:
        return wait_for(lambda: next((event for event in self.events() if event["kind"] == kind), None),
                        timeout, f"trace event {kind}")

    def wait_for_barrier(self, name: str, timeout: float = 10.0) -> dict:
        return wait_for(
            lambda: next((event for event in self.events()
                          if event["kind"] == "barrier" and event["detail"] == name), None),
            timeout, f"barrier {name}")

    def release(self, name: str) -> None:
        (self.barrier_dir / f"release-{name}").write_text("")

    def wait(self, timeout: float = 20.0) -> tuple[int, str, str]:
        if self.process.stdin is not None and self.process.stdin.closed:
            self.process.stdin = None
        stdout, stderr = self.process.communicate(timeout=timeout)
        return self.process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")

    def wait_line(self, timeout: float = 10.0) -> str:
        """Read one payload line without buffered-read-ahead hiding a second line from select()."""
        deadline = time.monotonic() + timeout
        while b"\n" not in self.stdout_buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pytest.fail("timed out waiting for payload readiness")
            readable, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not readable:
                pytest.fail("timed out waiting for payload readiness")
            chunk = os.read(self.process.stdout.fileno(), 4096)
            if not chunk:
                pytest.fail("payload stdout closed before readiness")
            self.stdout_buffer += chunk
        line, self.stdout_buffer = self.stdout_buffer.split(b"\n", 1)
        return line.decode(errors="replace").strip()

    def receipt_json(self) -> dict:
        return json.loads((self.directory / "receipt.json").read_bytes())

    def receipt(self) -> CleanupReceipt:
        return decode_receipt((self.directory / "receipt.json").read_bytes())


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a line still being appended
    return events


def wait_for(predicate, timeout: float, description: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    pytest.fail(f"timed out waiting for {description}")


def cancel(binary: Path, launch: Launch, *, control: str | None = None, usage: str | None = None,
           root: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(binary), "cancel", "--root", str(root or launch.root),
         "--control-token", control or launch.control, "--usage-token", usage or launch.usage],
        capture_output=True, text=True, timeout=20, cwd=launch.workdir)


def read_receipt(binary: Path, launch: Launch, *, control: str | None = None,
                 root: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(binary), "receipt", "--root", str(root or launch.root),
         "--control-token", control or launch.control],
        capture_output=True, text=True, timeout=20, cwd=launch.workdir)


@pytest.fixture
def launch_factory(supervisor_binaries, exec_root, tmp_path):
    started: list[Launch] = []

    def start(argv, *, control=None, usage=None, exec_id=None, grace=0.2, reap=2.0, barriers="",
              stdin=subprocess.DEVNULL, extra_env=None) -> Launch:
        control = control or uuid.uuid4().hex
        usage = usage or uuid.uuid4().hex
        exec_id = exec_id or f"exec-{uuid.uuid4().hex[:16]}"
        trace_path = tmp_path / f"trace-{exec_id}.jsonl"
        barrier_dir = tmp_path / f"barrier-{exec_id}"
        barrier_dir.mkdir()
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        trace_fd = os.open(trace_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        env["KEEL_TEST_TRACE_FD"] = str(trace_fd)
        env["KEEL_TEST_BARRIER_DIR"] = str(barrier_dir)
        if barriers:
            env["KEEL_TEST_BARRIER"] = barriers
        command = [
            str(supervisor_binaries["harness"]), "run", "--root", str(exec_root), "--exec-id", exec_id,
            "--control-token", control, "--usage-token", usage, "--grace", str(grace), "--reap", str(reap),
            "--", *argv,
        ]
        try:
            process = subprocess.Popen(command, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=env, pass_fds=(trace_fd,), cwd=tmp_path)
        finally:
            os.close(trace_fd)
        launch = Launch(process, exec_root, tmp_path, control, usage, exec_id, trace_path, barrier_dir)
        started.append(launch)
        return launch

    yield start
    for launch in started:
        for name in ("handshake_before", "handshake_after", "term_before", "kill_before",
                     "after_leader_reaped"):
            (launch.barrier_dir / f"release-{name}").write_text("")
        if launch.process.poll() is None:
            launch.process.terminate()
            try:
                launch.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                launch.process.kill()
                launch.process.wait(timeout=5)


SLEEPER = [sys.executable, "-c", "import time; time.sleep(30)"]
IGNORE_TERM_SLEEPER = [
    sys.executable, "-u", "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "print('ready', flush=True); time.sleep(30)",
]


def signal_events(events: list[dict]) -> list[dict]:
    return [event for event in events if event["kind"] in ("term", "kill")]


def assert_no_signal_after_reap(events: list[dict]) -> None:
    kinds = [event["kind"] for event in events]
    assert "leader_reaped" in kinds
    sweep = kinds[kinds.index("leader_reaped"):]
    assert "term" not in sweep and "kill" not in sweep


def assert_success_receipt(receipt: CleanupReceipt, status: int) -> None:
    assert receipt.control_closed is True
    assert receipt.descendants_complete is True
    assert receipt.uncertain_reason is None
    assert receipt.leader_status == status


def assert_uncertain_receipt(receipt: CleanupReceipt, reason: str | None = None) -> None:
    assert receipt.control_closed is True
    assert receipt.descendants_complete is False
    assert receipt.uncertain_reason is not None
    if reason is not None:
        assert receipt.uncertain_reason == reason


def python_payload(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", code]


# The leader forks a descendant into its own session (so group TERM/KILL cannot reach it), prints its identity,
# closes its inherited stdout/stderr so a surviving descendant cannot hold the supervisor's pipes open, and exits.
# The escaped descendant keeps the supervisor's descendant proof open until the reap deadline.
ESCAPED_DESCENDANT = (
    "import os, signal, time\n"
    "read_fd, write_fd = os.pipe()\n"
    "child = os.fork()\n"
    "if child == 0:\n"
    "    os.close(read_fd)\n"
    "    os.setsid()\n"
    "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "    os.write(write_fd, b'x')\n"
    "    os.close(write_fd)\n"
    "    os.write(1, ('escaped %d\\n' % os.getpid()).encode())\n"
    "    os.close(1)\n"
    "    os.close(2)\n"
    "    time.sleep(10)\n"
    "    os._exit(0)\n"
    "os.close(write_fd)\n"
    "os.read(read_fd, 1)\n"
    "os.close(read_fd)\n"
    "os.write(1, ('leader %d\\n' % os.getpid()).encode())\n"
)

IGNORING_ESCAPED_PAYLOAD = (
    "import os, signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "read_fd, write_fd = os.pipe()\n"
    "child = os.fork()\n"
    "if child == 0:\n"
    "    os.close(read_fd)\n"
    "    os.setsid()\n"
    "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "    os.write(write_fd, b'x')\n"
    "    os.close(write_fd)\n"
    "    os.write(1, ('escaped %d\\n' % os.getpid()).encode())\n"
    "    os.close(1)\n"
    "    os.close(2)\n"
    "    time.sleep(30)\n"
    "    os._exit(0)\n"
    "os.close(write_fd)\n"
    "os.read(read_fd, 1)\n"
    "os.close(read_fd)\n"
    "os.write(1, b'ready\\n')\n"
    "time.sleep(30)\n"
)


@LINUX_ONLY
def test_reserves_identity_before_fork_and_refuses_a_duplicate_launch(launch_factory):
    launch = launch_factory(SLEEPER)
    launch.wait_for("fork")

    kinds = launch.kinds()
    assert kinds.index("reservation") < kinds.index("fork")
    assert (launch.directory / "reservation").exists()
    assert launch.process.poll() is None

    duplicate = _duplicate_run(launch)
    assert duplicate.returncode == 193

    events = launch.events()
    assert sum(event["kind"] == "fork" for event in events) == 1
    assert sum(event["kind"] == "payload_start" for event in events) == 1
    assert any(event["kind"] == "duplicate" for event in events)
    assert launch.process.poll() is None

    assert cancel(Path(launch.process.args[0]), launch).returncode == 0
    code, _, _ = launch.wait()
    assert code == 128 + signal.SIGTERM


def _append_fd(path: Path) -> int:
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)


def _duplicate_run(launch: Launch) -> subprocess.CompletedProcess:
    fd = _append_fd(launch.trace_path)
    try:
        return subprocess.run(
            [str(launch.process.args[0]), "run", "--root", str(launch.root), "--exec-id", launch.exec_id,
             "--control-token", launch.control, "--usage-token", launch.usage,
             "--grace", "0.2", "--reap", "1", "--", *SLEEPER],
            capture_output=True, text=True, timeout=20, cwd=launch.workdir, pass_fds=(fd,),
            env={**os.environ, "KEEL_TEST_TRACE_FD": str(fd), "KEEL_TEST_BARRIER_DIR": str(launch.barrier_dir)})
    finally:
        os.close(fd)


@LINUX_ONLY
def test_payload_runs_in_a_private_session_and_process_group(launch_factory):
    probe = [sys.executable, "-c",
             "import os; print(os.getpid(), os.getpgrp(), os.getsid(0), flush=True)"]
    launch = launch_factory(probe, grace=0.05, reap=1.0)
    fork = launch.wait_for("fork")
    code, stdout, stderr = launch.wait()
    assert code == 0, stderr

    pid, pgid, sid = (int(part) for part in stdout.split())
    assert pid == pgid == sid
    assert pid != launch.process.pid
    assert fork["value"] == pid
    assert launch.wait_for("handshake")["detail"] == "verified"
    kinds = launch.kinds()
    assert kinds.index("subreaper") < kinds.index("fork") < kinds.index("payload_start")

    receipt = launch.receipt()
    assert_success_receipt(receipt, 0)
    events = launch.events()
    assert kinds.index("leader_reaped") < kinds.index("echild")
    assert_no_signal_after_reap(events)


@LINUX_ONLY
def test_wrong_usage_or_control_bindings_are_rejected_without_signaling(launch_factory):
    launch = launch_factory(SLEEPER)
    launch.wait_for("fork")
    harness = Path(launch.process.args[0])

    wrong_usage = uuid.uuid4().hex
    assert cancel(harness, launch, usage=wrong_usage).returncode == 4
    assert cancel(harness, launch, control=uuid.uuid4().hex).returncode == 5

    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
        client.connect(str(launch.socket_path))
        client.send(encode_control(launch.control, wrong_usage, cancel=True))
        assert client.recv(1) == b"R"
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
        client.connect(str(launch.socket_path))
        client.send(encode_control(uuid.uuid4().hex, launch.usage, cancel=True))
        assert client.recv(1) == b"R"

    time.sleep(0.2)
    assert launch.process.poll() is None
    assert not signal_events(launch.events())
    assert any(event["kind"] == "control_rejected" for event in launch.events())

    assert cancel(harness, launch).returncode == 0
    code, _, _ = launch.wait()
    assert code == 128 + signal.SIGTERM


@LINUX_ONLY
def test_duplicate_cancel_joins_the_original_deadline(launch_factory):
    launch = launch_factory(IGNORE_TERM_SLEEPER, grace=0.8, reap=5.0)
    launch.wait_for("fork")
    assert launch.wait_line() == "ready"
    harness = Path(launch.process.args[0])

    assert cancel(harness, launch).returncode == 0
    term = launch.wait_for("term")
    time.sleep(0.5)
    assert cancel(harness, launch).returncode == 0

    code, _, _ = launch.wait()
    assert code == 128 + signal.SIGKILL
    events = launch.events()
    terms = [event for event in events if event["kind"] == "term"]
    kills = [event for event in events if event["kind"] == "kill"]
    assert len(terms) == 1 and len(kills) == 1
    # A duplicate cancel must not restart the grace deadline (a restart would be >= 1.2s here).
    delay = (kills[0]["t_ms"] - terms[0]["t_ms"]) / 1000.0
    assert 0.6 <= delay <= 1.15, delay
    assert all(event["leader_pinned"] for event in signal_events(events))


@LINUX_ONLY
def test_a_silent_control_peer_cannot_block_a_legitimate_cancel(launch_factory):
    launch = launch_factory(SLEEPER, grace=0.2, reap=2.0)
    launch.wait_for("handshake")
    harness = Path(launch.process.args[0])

    silent = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        silent.connect(str(launch.socket_path))
        started = time.monotonic()
        assert cancel(harness, launch).returncode == 0
        elapsed = time.monotonic() - started
    finally:
        silent.close()

    code, _, _ = launch.wait()
    assert code == 128 + signal.SIGTERM
    idle = launch.wait_for("client_idle")
    kinds = launch.kinds()
    assert idle["detail"] == "closed"
    # The silent peer held the single client slot until its idle deadline, and the queued cancel was not lost.
    assert kinds.index("client_idle") < kinds.index("control")
    assert elapsed >= 0.2, elapsed
    assert_success_receipt(launch.receipt(), code)
    assert_no_signal_after_reap(launch.events())


@LINUX_ONLY
def test_cancel_during_setup_waits_for_the_group_handshake(launch_factory):
    launch = launch_factory(SLEEPER, grace=0.3, reap=2.0, barriers="handshake_before")
    launch.wait_for_barrier("handshake_before")
    harness = Path(launch.process.args[0])

    assert cancel(harness, launch).returncode == 0
    launch.wait_for("control")
    time.sleep(0.25)
    kinds = launch.kinds()
    assert "control" in kinds
    assert "handshake" not in kinds
    assert "term" not in kinds
    assert "payload_start" not in kinds

    launch.release("handshake_before")
    code, _, _ = launch.wait()
    assert code == 128 + signal.SIGTERM
    kinds = launch.kinds()
    assert kinds.index("control") < kinds.index("handshake") < kinds.index("term")
    assert kinds.index("control_closed") < kinds.index("leader_reaped")
    assert_no_signal_after_reap(launch.events())
    assert all(event["leader_pinned"] for event in signal_events(launch.events()))


@LINUX_ONLY
def test_natural_exit_and_cancel_races_keep_one_pinned_leader(launch_factory):
    quick = [sys.executable, "-c", "import time; time.sleep(0.05)"]
    for _ in range(5):
        launch = launch_factory(quick, grace=0.05, reap=2.0)
        launch.wait_for("fork")
        time.sleep(random.uniform(0.0, 0.12))
        assert cancel(Path(launch.process.args[0]), launch).returncode == 0
        code, _, _ = launch.wait()
        assert code in (0, 128 + signal.SIGTERM)
        receipt = launch.receipt()
        assert_success_receipt(receipt, code)
        events = launch.events()
        kinds = [event["kind"] for event in events]
        assert kinds.count("leader_reaped") == 1
        assert kinds.count("control_closed") == 1
        assert kinds.index("control_closed") < kinds.index("leader_reaped")
        assert kinds.index("leader_reaped") < kinds.index("echild")
        assert all(event["leader_pinned"] for event in signal_events(events))
        assert_no_signal_after_reap(events)


@LINUX_ONLY
def test_pauses_just_before_term_and_kill_keep_the_leader_pinned(launch_factory):
    launch = launch_factory(IGNORE_TERM_SLEEPER, grace=0.4, reap=5.0,
                            barriers="term_before,kill_before")
    fork = launch.wait_for("fork")
    leader = fork["value"]
    harness = Path(launch.process.args[0])
    assert launch.wait_line() == "ready"

    # Cleanup starts on cancel; the supervisor pauses immediately before TERM.
    assert cancel(harness, launch).returncode == 0
    launch.wait_for_barrier("term_before")
    time.sleep(0.25)
    assert launch.process.poll() is None
    assert "term" not in launch.kinds()
    assert os.path.exists(f"/proc/{leader}")
    assert not (launch.directory / "receipt.json").exists()

    launch.release("term_before")
    launch.wait_for("term")
    launch.wait_for_barrier("kill_before")
    assert "kill" not in launch.kinds()
    assert "control_closed" not in launch.kinds()
    assert os.path.exists(f"/proc/{leader}")
    assert not (launch.directory / "receipt.json").exists()

    launch.release("kill_before")
    code, _, _ = launch.wait()
    assert code == 128 + signal.SIGKILL
    events = launch.events()
    kinds = [event["kind"] for event in events]
    assert kinds.index("control_closed") < kinds.index("leader_reaped")
    assert all(event["leader_pinned"] for event in signal_events(events))
    assert_no_signal_after_reap(events)


@LINUX_ONLY
def test_closing_stdin_is_not_cleanup_and_never_success(launch_factory):
    launch = launch_factory(IGNORE_TERM_SLEEPER, grace=0.3, reap=1.0, stdin=subprocess.PIPE)
    launch.wait_for("fork")
    assert launch.wait_line() == "ready"
    launch.process.stdin.close()

    time.sleep(0.5)
    assert launch.process.poll() is None
    assert "cleanup" not in launch.kinds()
    assert "control_closed" not in launch.kinds()
    assert not (launch.directory / "receipt.json").exists()

    assert cancel(Path(launch.process.args[0]), launch).returncode == 0
    code, _, _ = launch.wait()
    assert code == 128 + signal.SIGKILL
    receipt = launch.receipt()
    assert_success_receipt(receipt, 128 + signal.SIGKILL)


@LINUX_ONLY
def test_late_control_after_reap_sees_the_terminal_receipt_or_a_closed_endpoint(launch_factory):
    launch = launch_factory(["/bin/true"], grace=0.05, reap=1.0)
    launch.wait_for("fork")
    code, _, _ = launch.wait()
    assert code == 0
    before = launch.events()
    harness = Path(launch.process.args[0])

    assert not launch.socket_path.exists()
    late = cancel(harness, launch)
    assert late.returncode == 0
    assert json.loads(late.stdout) == launch.receipt_json()
    shown = read_receipt(harness, launch)
    assert shown.returncode == 0
    assert json.loads(shown.stdout) == launch.receipt_json()
    assert launch.events() == before
    assert_no_signal_after_reap(before)
    assert [event["kind"] for event in before].index("control_closed") < \
        [event["kind"] for event in before].index("leader_reaped")


@LINUX_ONLY
def test_production_binary_contains_no_test_hooks(supervisor_binaries):
    production = supervisor_binaries["production"].read_bytes()
    harness = supervisor_binaries["harness"].read_bytes()
    for marker in (b"KEEL_TEST_TRACE_FD", b"KEEL_TEST_BARRIER", b"KEEL_TEST_FAIL_SUBREAPER",
                   b"KEEL_TEST_WAIT_ERROR", b"leader_pinned", b"payload_start"):
        assert marker not in production
    assert b"KEEL_TEST_TRACE_FD" in harness


@LINUX_ONLY
def test_production_binary_completes_a_payload_and_keeps_the_receipt(supervisor_binaries, exec_root, tmp_path):
    binary = supervisor_binaries["production"]
    control = uuid.uuid4().hex
    usage = uuid.uuid4().hex
    result = subprocess.run(
        [str(binary), "run", "--root", str(exec_root), "--exec-id", "exec-production",
         "--control-token", control, "--usage-token", usage, "--grace", "0.05", "--reap", "1",
         "--", "/bin/sh", "-c", "exit 3"],
        capture_output=True, text=True, timeout=20, cwd=tmp_path)
    assert result.returncode == 3

    shown = subprocess.run(
        [str(binary), "receipt", "--root", str(exec_root), "--control-token", control],
        capture_output=True, text=True, timeout=20, cwd=tmp_path)
    assert shown.returncode == 0
    receipt = decode_receipt(shown.stdout.strip().encode())
    assert receipt.exec_id == "exec-production"
    assert_success_receipt(receipt, 3)


@LINUX_ONLY
def test_invalid_run_and_receipt_requests_exit_nonzero(supervisor_binaries, exec_root, tmp_path):
    harness = supervisor_binaries["harness"]
    usage = uuid.uuid4().hex
    invalid = subprocess.run(
        [str(harness), "run", "--root", str(exec_root), "--exec-id", "exec-1",
         "--control-token", uuid.uuid4().hex.upper(), "--usage-token", usage,
         "--grace", "0.1", "--reap", "0.1", "--", "/bin/true"],
        capture_output=True, text=True, timeout=20, cwd=tmp_path)
    assert invalid.returncode == 194

    overlong = subprocess.run(
        [str(harness), "run", "--root", str(exec_root), "--exec-id", "x" * 129,
         "--control-token", uuid.uuid4().hex, "--usage-token", usage,
         "--grace", "0.1", "--reap", "0.1", "--", "/bin/true"],
        capture_output=True, text=True, timeout=20, cwd=tmp_path)
    assert overlong.returncode == 194

    absent = subprocess.run(
        [str(harness), "receipt", "--root", str(exec_root), "--control-token", uuid.uuid4().hex],
        capture_output=True, text=True, timeout=20, cwd=tmp_path)
    assert absent.returncode == 5
    assert absent.stdout == ""

    invalid_token = subprocess.run(
        [str(harness), "receipt", "--root", str(exec_root), "--control-token", "nope"],
        capture_output=True, text=True, timeout=20, cwd=tmp_path)
    assert invalid_token.returncode == 194

    no_reservation = subprocess.run(
        [str(harness), "cancel", "--root", str(exec_root),
         "--control-token", uuid.uuid4().hex, "--usage-token", uuid.uuid4().hex],
        capture_output=True, text=True, timeout=20, cwd=tmp_path)
    assert no_reservation.returncode == 5


@LINUX_ONLY
def test_subreaper_is_enabled_and_verified_before_the_payload_fork(launch_factory):
    launch = launch_factory(["/bin/true"], grace=0.05, reap=1.0)
    launch.wait_for("fork")
    code, _, stderr = launch.wait()
    assert code == 0, stderr

    events = launch.events()
    kinds = [event["kind"] for event in events]
    assert kinds.index("subreaper") < kinds.index("fork") < kinds.index("payload_start")
    subreaper = next(event for event in events if event["kind"] == "subreaper")
    assert subreaper["detail"] == "verified"
    assert_success_receipt(launch.receipt(), 0)


@LINUX_ONLY
def test_success_follows_leader_reap_and_an_actual_echild(launch_factory):
    started = time.monotonic()
    launch = launch_factory(["/bin/sh", "-c", "exit 0"], grace=0.2, reap=1.0)
    launch.wait_for("fork")
    code, _, stderr = launch.wait()
    elapsed = time.monotonic() - started
    assert code == 0, stderr

    events = launch.events()
    kinds = [event["kind"] for event in events]
    # The handshake pipe's EOF confirms exec, but it is not the completion proof.
    assert kinds.index("handshake") < kinds.index("leader_reaped")
    assert kinds.index("control_closed") < kinds.index("leader_reaped")
    assert kinds.index("leader_reaped") < kinds.index("echild") < kinds.index("receipt_written")
    assert "deadline" not in kinds
    assert_success_receipt(launch.receipt(), 0)
    assert_no_signal_after_reap(events)
    assert all(event["leader_pinned"] for event in signal_events(events))
    assert elapsed >= 0.15, "the configured TERM grace must be part of normal cleanup latency"


@LINUX_ONLY
def test_multiple_orphaned_intermediates_are_adopted_and_reaped(launch_factory):
    payload = python_payload(
        "import os, time\n"
        "for _ in range(3):\n"
        "    if os.fork() == 0:\n"
        "        time.sleep(0.4)\n"
        "        os._exit(0)\n"
        "os._exit(0)\n")
    launch = launch_factory(payload, grace=0.1, reap=2.0)
    launch.wait_for("fork")
    code, _, stderr = launch.wait()
    assert code == 0, stderr

    events = launch.events()
    reaped = [event for event in events if event["kind"] == "descendant_reaped"]
    assert len(reaped) == 3
    kinds = [event["kind"] for event in events]
    assert kinds.index("leader_reaped") < kinds.index("echild") < kinds.index("receipt_written")
    assert_success_receipt(launch.receipt(), 0)
    assert_no_signal_after_reap(events)


@LINUX_ONLY
def test_double_fork_with_closed_stdout_is_still_proven(launch_factory):
    payload = python_payload(
        "import os, time\n"
        "read_fd, write_fd = os.pipe()\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    os.close(read_fd)\n"
        "    if os.fork() == 0:\n"
        "        os.close(1)\n"
        "        os.close(2)\n"
        "        os.write(write_fd, b'x')\n"
        "        os.close(write_fd)\n"
        "        time.sleep(0.5)\n"
        "        os._exit(0)\n"
        "    os.close(write_fd)\n"
        "    os._exit(0)\n"
        "os.close(write_fd)\n"
        "os.read(read_fd, 1)\n"
        "os.close(read_fd)\n"
        "os._exit(0)\n")
    launch = launch_factory(payload, grace=0.1, reap=2.0)
    launch.wait_for("fork")
    code, _, stderr = launch.wait()
    assert code == 0, stderr

    events = launch.events()
    assert len([event for event in events if event["kind"] == "descendant_reaped"]) >= 2
    kinds = [event["kind"] for event in events]
    assert kinds.index("leader_reaped") < kinds.index("echild") < kinds.index("receipt_written")
    assert_success_receipt(launch.receipt(), 0)
    assert_no_signal_after_reap(events)


@LINUX_ONLY
def test_live_escaped_descendant_keeps_cleanup_uncertain_until_the_deadline(launch_factory):
    grace, reap = 0.3, 0.6
    started = time.monotonic()
    launch = launch_factory(python_payload(ESCAPED_DESCENDANT), grace=grace, reap=reap)
    launch.wait_for("fork")
    lines = {launch.wait_line(), launch.wait_line()}
    assert any(line.startswith("escaped ") for line in lines)
    launch.wait_for("leader_exit")
    launch.wait_for("leader_reaped")

    # A zero return from the child wait means children remain; it must not be success.
    time.sleep(0.2)
    assert not (launch.directory / "receipt.json").exists()

    code, _, _ = launch.wait()
    elapsed = time.monotonic() - started
    assert code == 195
    receipt = launch.receipt()
    assert_uncertain_receipt(receipt, "descendants_unreaped")
    assert receipt.leader_status == 0

    events = launch.events()
    kinds = [event["kind"] for event in events]
    assert "kill" in kinds
    assert "echild" not in kinds
    assert kinds.index("control_closed") < kinds.index("leader_reaped") < kinds.index("deadline")
    assert grace + reap - 0.05 <= elapsed <= grace + reap + 1.5, elapsed
    assert_no_signal_after_reap(events)
    assert all(event["leader_pinned"] for event in signal_events(events))


@LINUX_ONLY
def test_kill_delivery_without_descendant_exit_is_not_success(launch_factory):
    grace, reap = 0.3, 0.6
    started = time.monotonic()
    launch = launch_factory(python_payload(IGNORING_ESCAPED_PAYLOAD), grace=grace, reap=reap)
    launch.wait_for("fork")
    lines = {launch.wait_line(), launch.wait_line()}
    assert "ready" in lines
    assert any(line.startswith("escaped ") for line in lines)

    assert cancel(Path(launch.process.args[0]), launch).returncode == 0
    code, _, _ = launch.wait()
    elapsed = time.monotonic() - started
    assert code == 195
    receipt = launch.receipt()
    assert_uncertain_receipt(receipt, "descendants_unreaped")
    assert receipt.leader_status == 128 + signal.SIGKILL

    events = launch.events()
    kinds = [event["kind"] for event in events]
    assert "kill" in kinds
    assert "echild" not in kinds
    assert grace + reap - 0.05 <= elapsed <= grace + reap + 1.5, elapsed
    assert_no_signal_after_reap(events)
    assert all(event["leader_pinned"] for event in signal_events(events))


@LINUX_ONLY
def test_unexpected_child_wait_errors_are_uncertain(launch_factory):
    launch = launch_factory(["/bin/true"], grace=0.05, reap=1.0,
                            extra_env={"KEEL_TEST_WAIT_ERROR": "1"})
    launch.wait_for("fork")
    code, _, _ = launch.wait()
    assert code == 195
    receipt = launch.receipt()
    assert_uncertain_receipt(receipt, "wait_failed")

    events = launch.events()
    kinds = [event["kind"] for event in events]
    assert "wait_failed" in kinds
    assert "echild" not in kinds
    assert kinds.index("leader_reaped") < kinds.index("wait_failed")


@LINUX_ONLY
def test_missing_subreaper_support_fails_closed(launch_factory):
    launch = launch_factory(["/bin/true"], grace=0.05, reap=1.0,
                            extra_env={"KEEL_TEST_FAIL_SUBREAPER": "1"})
    code, stdout, stderr = launch.wait()
    assert code == 192
    assert stdout == ""

    events = launch.events()
    kinds = [event["kind"] for event in events]
    assert "subreaper_failed" in kinds
    assert "fork" not in kinds and "payload_start" not in kinds
    receipt = launch.receipt()
    assert_uncertain_receipt(receipt, "subreaper_unavailable")
    assert receipt.leader_status is None


@LINUX_ONLY
def test_pid_reuse_is_blocked_while_pinned_and_never_signals_the_new_process(
        supervisor_binaries, exec_root, tmp_path):
    """Requires a private PID namespace with a writable ns_last_pid.

    Run the Linux suite in a privileged container (or on a host with unprivileged user namespaces and
    CAP_SYS_ADMIN in the namespace's user namespace). Without that, the helper exits 77 and this test skips
    explicitly; random PID churn is never accepted as evidence.
    """
    scratch = tmp_path / "pid-reuse"
    scratch.mkdir()
    result = subprocess.run(
        [str(supervisor_binaries["pid_reuse"]), str(supervisor_binaries["harness"]),
         str(exec_root), str(scratch)],
        capture_output=True, text=True, timeout=60)
    if result.returncode == 77:
        pytest.skip(result.stderr.strip() or "private PID namespace or ns_last_pid is unavailable")
    assert result.returncode == 0, result.stderr

    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["probe_pid"] != report["leader_pid"]
    assert report["reused_pid"] == report["leader_pid"]
    assert report["survived"] is True
    assert report["signal_after_reap"] is False
    assert report["receipt_uncertain"] is True
