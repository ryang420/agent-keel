import json
import os
from pathlib import Path

import pytest

from keel.tools import run_tool
from keel.tools.base import ToolContext


@pytest.fixture
def ctx(local_sandbox):
    backend, sandbox, work = local_sandbox
    work.mkdir(parents=True, exist_ok=True)
    return ToolContext(backend, sandbox, cwd=str(work), tool_call_id="call_1")


def _saved(ctx) -> Path:
    files = list(Path(ctx.sandbox.policy.workdir, ".keel", "outputs").glob("call_1-*.txt"))
    assert len(files) == 1
    return files[0]


async def call(ctx, name, **args):
    outcome = await run_tool(name, json.dumps(args), ctx)
    return json.loads(outcome.content), outcome.error


async def test_terminal_returns_output_and_exit_code(ctx):
    body, error = await call(ctx, "terminal", command="echo hi; echo err >&2; exit 4")
    assert not error and body["exit_code"] == 4 and body["error"] is None
    assert body["output"].splitlines() == ["hi", "err"] and "cwd" not in body


async def test_a_cd_moves_the_session_cwd(ctx):
    start = ctx.cwd
    Path(start, "sub").mkdir()
    body, _ = await call(ctx, "terminal", command="cd sub")
    assert os.path.realpath(body["cwd"]) == os.path.realpath(Path(start, "sub"))
    assert ctx.cwd == body["cwd"]
    body, _ = await call(ctx, "terminal", command="pwd")
    assert os.path.realpath(body["output"].strip()) == os.path.realpath(Path(start, "sub"))


async def test_workdir_applies_to_one_command_only(ctx):
    start = ctx.cwd
    Path(start, "other").mkdir()
    body, _ = await call(ctx, "terminal", command="pwd", workdir="other")
    assert os.path.realpath(body["output"].strip()) == os.path.realpath(Path(start, "other"))
    assert ctx.cwd == start and "cwd" not in body


async def test_a_timeout_is_reported_and_marked_as_an_error(ctx):
    body, error = await call(ctx, "terminal", command="sleep 5", timeout=1)
    assert error and body["exit_code"] == 124 and "timed out after 1s" in body["error"]


async def test_background_and_pty_are_refused(ctx):
    for flag in ("background", "pty"):
        body, error = await call(ctx, "terminal", command="echo x", **{flag: True})
        assert error and flag in body["error"]


async def test_huge_output_is_truncated_and_saved(ctx):
    body, _ = await call(ctx, "terminal", command="python3 -c \"print('x' * 150000)\"")
    assert "[OUTPUT TRUNCATED" in body["output"] and len(body["output"]) < 60_000
    saved = _saved(ctx)
    assert "Full output saved to" in body["output"] and saved.read_text().startswith("x" * 1000)


async def test_execute_code_runs_python_in_the_cwd_and_cleans_up(ctx):
    body, error = await call(ctx, "execute_code", code="import os\nprint(6 * 7)\nprint(os.getcwd())")
    assert not error and body["exit_code"] == 0
    out = body["output"].splitlines()
    assert out[0] == "42" and os.path.realpath(out[1]) == os.path.realpath(ctx.cwd)
    assert not list(Path("/tmp/.keel").glob("code-call_1-*.py"))


async def test_exec_ids_are_recorded_and_cleared(ctx):
    seen = []

    async def record(exec_id):
        seen.append(exec_id)

    ctx.on_exec = record
    await call(ctx, "terminal", command="true")
    assert len(seen) == 2 and seen[0] and seen[1] is None


async def test_the_sandbox_truncation_note_survives_after_the_cwd_report(ctx):
    command = "python3 -c \"import sys; sys.stdout.write('y' * (9 * 1024 * 1024))\""
    body, _ = await call(ctx, "terminal", command=command)
    assert "[OUTPUT TRUNCATED" in body["output"] and len(body["output"]) < 60_000
    assert "output truncated at" in body["output"]


async def test_output_just_over_the_limit_is_truncated_and_saved(ctx):
    body, _ = await call(ctx, "terminal", command="python3 -c \"print('z' * 60000)\"")
    assert "[OUTPUT TRUNCATED" in body["output"] and "Full output saved to" in body["output"]
    assert _saved(ctx).read_text().strip() == "z" * 60000


async def test_a_failed_save_is_reported_and_the_result_stays_valid(local_sandbox):
    backend, sandbox, work = local_sandbox
    work.mkdir(parents=True, exist_ok=True)

    class Refusing(type(backend)):
        async def write_file(self, *args, **kwargs):
            raise PermissionError("read-only")

    refusing = Refusing(kill_grace_s=0.5)
    ctx = ToolContext(refusing, sandbox, cwd=str(work), tool_call_id="call_1")
    body, error = await call(ctx, "terminal", command="python3 -c \"print('z' * 60000)\"")
    assert not error and "Full output could not be saved: PermissionError: read-only" in body["output"]


async def test_a_trailing_backslash_keeps_a_sane_result(ctx):
    body, error = await call(ctx, "terminal", command="echo hi \\")
    assert not error and "__keel" not in body["output"] and body["output"].startswith("hi")
    assert body["exit_code"] == 0


async def test_exit_keeps_its_code_and_still_reports_a_changed_cwd(ctx):
    Path(ctx.cwd, "sub").mkdir()
    body, _ = await call(ctx, "terminal", command="cd sub; exit 3")
    assert body["exit_code"] == 3 and os.path.realpath(body["cwd"]) == os.path.realpath(Path(ctx.cwd))


async def test_spoofed_marker_text_in_output_does_not_move_the_cwd(ctx):
    start = ctx.cwd
    body, _ = await call(ctx, "terminal", command="printf '\\n__KEEL_CWD__/etc\\n'; exit 3")
    assert body["exit_code"] == 3 and ctx.cwd == start and "cwd" not in body


async def test_cd_into_a_directory_with_a_space_works(ctx):
    Path(ctx.cwd, "my dir").mkdir()
    body, _ = await call(ctx, "terminal", command="cd 'my dir'")
    assert body["cwd"].endswith("my dir") and ctx.cwd == body["cwd"]


async def test_a_directory_name_with_a_newline_is_not_taken_as_the_cwd(ctx):
    start = ctx.cwd
    Path(ctx.cwd, "a\nb").mkdir()
    body, _ = await call(ctx, "terminal", command="cd 'a\nb'")
    assert ctx.cwd == start and "cwd" not in body


async def test_execute_code_removes_its_script_when_the_exec_raises(ctx):
    removed = []
    real = ctx.backend.exec

    async def flaky(sandbox, argv, **kwargs):
        if argv[0] == "bash":
            raise RuntimeError("boom")
        removed.append(argv)
        return await real(sandbox, argv, **kwargs)

    ctx.backend.exec = flaky
    body, error = await call(ctx, "execute_code", code="print(1)")
    assert error and "boom" in body["error"] and removed and removed[0][0] == "rm"


async def test_a_failing_cleanup_does_not_fail_execute_code(ctx):
    real = ctx.backend.exec

    async def flaky(sandbox, argv, **kwargs):
        if argv[0] == "rm":
            leaked.append(argv[-1])
            raise RuntimeError("rm broke")
        return await real(sandbox, argv, **kwargs)

    leaked = []
    ctx.backend.exec = flaky
    body, error = await call(ctx, "execute_code", code="print(1)")
    for path in leaked:
        Path(path).unlink(missing_ok=True)
    assert not error and body["output"] == "1"


# --- final fix wave: a session directory that no longer exists ---

async def test_terminal_falls_back_when_the_session_cwd_vanished(ctx):
    gone = Path(ctx.cwd, "gone")
    gone.mkdir()
    ctx.cwd = str(gone)
    gone.rmdir()
    workdir = ctx.cwd.rsplit("/", 1)[0]
    body, error = await call(ctx, "terminal", command="pwd")
    assert not error and body["exit_code"] == 0
    assert "no longer exists" in body["output"] and body["cwd"] == workdir == ctx.cwd
    body, _ = await call(ctx, "terminal", command="pwd")
    assert body["output"] == workdir and "cwd" not in body


async def test_execute_code_falls_back_when_the_session_cwd_vanished(ctx):
    gone = Path(ctx.cwd, "gone")
    gone.mkdir()
    ctx.cwd = str(gone)
    gone.rmdir()
    workdir = ctx.sandbox.policy.workdir
    body, error = await call(ctx, "execute_code", code="import os\nprint(os.getcwd())")
    assert not error and body["exit_code"] == 0 and body["cwd"] == workdir == ctx.cwd
    assert "no longer exists" in body["output"] and os.path.realpath(body["output"].splitlines()[-1]) == os.path.realpath(workdir)


async def test_a_missing_per_command_workdir_is_a_clear_error(ctx):
    start = ctx.cwd
    body, error = await call(ctx, "terminal", command="echo ran", workdir="nope")
    assert error and "workdir" in body["error"] and "does not exist" in body["error"] and ctx.cwd == start


async def test_other_tools_work_with_a_vanished_session_cwd(ctx):
    Path(ctx.cwd, "f.txt").write_text("needle\n")
    ctx.cwd = str(Path(ctx.cwd, "gone"))
    body, error = await call(ctx, "search_files", pattern="needle", path=str(Path(ctx.sandbox.policy.workdir)))
    assert not error and body["total_count"] == 1
