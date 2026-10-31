"""Every keel module imports on its own in a fresh interpreter: a server, worker or tool process can start from any of
them, so an import cycle that only an unlucky import order exposes still breaks a process."""

import os
from pathlib import Path
import pkgutil
import subprocess
import sys

import pytest

import keel

SOURCE = Path(keel.__file__).resolve().parents[1]  # the keel this test imported, so the child checks the same code
MODULES = sorted(info.name for info in pkgutil.walk_packages(keel.__path__, "keel.") if not info.name.endswith(
    "__main__"))


@pytest.mark.parametrize("module", MODULES)
def test_a_module_imports_in_a_fresh_interpreter(module, tmp_path):
    result = subprocess.run([sys.executable, "-c", f"import {module}"], capture_output=True, text=True, timeout=60,
                            cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(SOURCE)})
    assert result.returncode == 0, result.stderr[-2000:]
