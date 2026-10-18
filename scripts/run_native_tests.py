#!/usr/bin/env python3
"""Run the Linux supervisor tests in a throwaway Linux container for one architecture.

The repository is mounted read-only and copied inside the container (without the host virtualenv or git data), the
build toolchain is installed there, and pytest runs the native helper tests against a fresh build. The container's
architecture, kernel and Python version are printed first, so every run records where it executed.

    scripts/run_native_tests.py --arch arm64
    scripts/run_native_tests.py --arch amd64                      # emulated on an arm64 host (needs binfmt)
    scripts/run_native_tests.py --arch amd64 --privileged         # the PID-reuse proof in a private PID namespace

`--privileged` is a test-only environment for the PID-namespace harness (it needs a writable ns_last_pid); it
selects only that test and sets KEEL_TEST_REQUIRE_PID_NAMESPACE=1, so an unavailable namespace fails instead of
skipping. The sandbox image itself never runs privileged.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("KEEL_NATIVE_TEST_IMAGE", "python:3.12-slim")
DEFAULT_TESTS = ("tests/test_sandbox_supervisor.py", "tests/test_sandbox_runtime.py", "tests/test_build_supervisor.py")
_SCRIPT = (
    "set -eu; "
    "apt-get update -qq >/dev/null; "
    "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends gcc make libc6-dev >/dev/null; "
    "mkdir /work; "
    "tar -C /src --exclude=./.venv --exclude=./.git --exclude=./.worktrees --exclude=./.claude "
    "--exclude=./src/keel/sandbox/bin -cf - . | tar -C /work -xf -; "
    "cd /work; "
    "pip install -q --root-user-action=ignore -e '.[dev]'; "
    'echo "native-run: arch=$(uname -m) kernel=$(uname -r) $(python -VV | head -n 1)"; '
    'exec python -m pytest -p no:cacheprovider -q -rs "$@"'
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arch", choices=("amd64", "arm64"), required=True)
    parser.add_argument("--privileged", action="store_true",
                        help="run only the PID-reuse proof, in a privileged test-only container")
    parser.add_argument("pytest_args", nargs="*", help="pytest arguments (default: the native helper tests)")
    arguments = parser.parse_args(argv)
    command = ["docker", "run", "--rm", "--platform", f"linux/{arguments.arch}", "-v", f"{REPO_ROOT}:/src:ro"]
    tests = list(arguments.pytest_args or DEFAULT_TESTS)
    if arguments.privileged:
        command += ["--privileged", "-e", "KEEL_TEST_REQUIRE_PID_NAMESPACE=1"]
        if not arguments.pytest_args:
            tests = ["tests/test_sandbox_supervisor.py", "-k", "pid_reuse"]
    command += [IMAGE, "sh", "-c", _SCRIPT, "native-tests", *tests]
    return subprocess.run(command).returncode


if __name__ == "__main__":
    sys.exit(main())
