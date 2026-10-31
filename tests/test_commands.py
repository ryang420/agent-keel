import subprocess
from pathlib import Path

import pytest

from keel.commands import (
    MAX_ARGV_BYTES, MAX_ARGV_ITEMS, MISSING_CWD_EXIT_CODE, OUTPUT_TAIL_CHARS, CommandError, output_tail,
    parse_command, wrapped_argv,
)

GOOD = {"argv": ["python3", "job.py"], "cwd": "/work/jobs/j1", "timeout_s": 1800}


def parse(raw, *, workdir="/work", max_timeout_s=3600):
    return parse_command(raw, workdir=workdir, max_timeout_s=max_timeout_s)


def test_a_good_command_is_normalised():
    assert parse(GOOD) == {"argv": ["python3", "job.py"], "cwd": "/work/jobs/j1", "timeout_s": 1800, "env": {}}
    assert parse({**GOOD, "cwd": "/work/jobs/./j1/", "timeout_s": 2.5, "env": {"KEY": "value"}}) == {
        "argv": ["python3", "job.py"], "cwd": "/work/jobs/j1", "timeout_s": 2.5, "env": {"KEY": "value"}}
    assert parse({**GOOD, "cwd": "/work"})["cwd"] == "/work"
    assert parse({**GOOD, "cwd": "/anywhere"}, workdir="/")["cwd"] == "/anywhere"
    assert parse({**GOOD, "env": None})["env"] == {}
    parsed = parse(GOOD)
    assert parse(parsed) == parsed  # validating a stored command again changes nothing


def test_a_profile_without_a_sandbox_cannot_run_commands():
    with pytest.raises(CommandError, match="needs a profile with a sandbox"):
        parse(GOOD, workdir=None)


def test_the_timeout_cap_is_the_callers():
    assert parse({**GOOD, "timeout_s": 600}, max_timeout_s=600)["timeout_s"] == 600
    with pytest.raises(CommandError, match="from 1 to 600"):
        parse({**GOOD, "timeout_s": 601}, max_timeout_s=600)


@pytest.mark.parametrize(("raw", "message"), [
    (None, "command must be an object"),
    (["true"], "command must be an object"),
    ({**GOOD, "shell": True}, "command.shell is not a known setting"),
    ({**GOOD, "argv": []}, "command.argv"),
    ({**GOOD, "argv": "python3 job.py"}, "command.argv"),
    ({**GOOD, "argv": ["python3", 3]}, "command.argv"),
    ({**GOOD, "argv": ["", "x"]}, "command.argv"),
    ({**GOOD, "argv": ["a\0b"]}, "command.argv"),
    ({**GOOD, "argv": ["\ud800"]}, "command.argv"),
    ({**GOOD, "argv": ["x"] * (MAX_ARGV_ITEMS + 1)}, "command.argv"),
    ({**GOOD, "argv": ["x" * (MAX_ARGV_BYTES + 1)]}, "16384 bytes"),
    ({**GOOD, "argv": ["x" * 8192, "é" * 4097]}, "16384 bytes"),  # counted in UTF-8 bytes, not characters
    ({**GOOD, "cwd": "work"}, "command.cwd must be an absolute path"),
    ({**GOOD, "cwd": None}, "command.cwd must be an absolute path"),
    ({**GOOD, "cwd": "/work/\ud800"}, "command.cwd must be an absolute path"),
    ({**GOOD, "cwd": "/etc"}, "under the sandbox workdir /work"),
    ({**GOOD, "cwd": "/workshop"}, "under the sandbox workdir /work"),
    ({**GOOD, "cwd": "/work/../etc"}, "under the sandbox workdir /work"),
    ({**GOOD, "timeout_s": 0}, "from 1 to 3600"),
    ({**GOOD, "timeout_s": 3601}, "from 1 to 3600"),
    ({**GOOD, "timeout_s": True}, "from 1 to 3600"),
    ({**GOOD, "timeout_s": "60"}, "from 1 to 3600"),
    ({**GOOD, "timeout_s": float("nan")}, "from 1 to 3600"),
    ({"argv": ["true"], "cwd": "/work"}, "from 1 to 3600"),
    ({**GOOD, "env": {"1BAD": "x"}}, "command.env"),
    ({**GOOD, "env": {"KEY": "a\nb"}}, "command.env"),
    ({**GOOD, "env": {"KEY": 1}}, "command.env"),
    ({**GOOD, "env": ["KEY=x"]}, "command.env"),
    ({**GOOD, "env": {"KEY": "\ud800"}}, "command.env"),
])
def test_bad_commands_are_refused_with_a_message_for_the_caller(raw, message):
    with pytest.raises(CommandError, match=message):
        parse(raw)


def run_wrapped(argv, cwd):
    command = {"argv": argv, "cwd": str(cwd), "timeout_s": 5, "env": {}}
    return subprocess.run(wrapped_argv(command), capture_output=True, text=True, timeout=10)


def test_the_command_starts_in_its_directory_with_its_arguments_untouched(tmp_path):
    done = run_wrapped(["sh", "-c", 'pwd; printf "%s|" "$@"', "zero", "a b", "$HOME", "'q'", "*"], tmp_path)
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert Path(lines[0]).resolve() == tmp_path.resolve()
    assert lines[1] == "a b|$HOME|'q'|*|"


def test_a_missing_directory_is_an_ordinary_exit_that_says_so(tmp_path):
    done = run_wrapped(["true"], tmp_path / "missing")
    assert done.returncode == MISSING_CWD_EXIT_CODE == 125
    assert "keel: cannot enter" in done.stderr


def test_the_exit_code_is_the_commands_own(tmp_path):
    assert run_wrapped(["sh", "-c", "exit 7"], tmp_path).returncode == 7


def test_the_output_tail_is_the_last_fifty_thousand_characters():
    assert output_tail("short") == "short"
    assert output_tail("a" * 10 + "b" * OUTPUT_TAIL_CHARS) == "b" * OUTPUT_TAIL_CHARS
