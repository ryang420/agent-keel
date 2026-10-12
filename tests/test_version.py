import tomllib
from pathlib import Path

import keel


def test_version_matches_pyproject():
    data = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())
    assert keel.__version__ == data["project"]["version"]
