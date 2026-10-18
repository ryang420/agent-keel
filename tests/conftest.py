import os

import pytest

from keel.sandbox.base import parse_policy
from keel.sandbox.local import LocalSandbox
from keel.store import open_store


class Clock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


STORE_BACKENDS = ["sqlite"] + (["postgres"] if os.environ.get("KEEL_TEST_POSTGRES_DSN") else [])


@pytest.fixture(params=STORE_BACKENDS)
async def store(request, tmp_path, clock):
    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path / 'keel.sqlite3'}"
    else:
        url = os.environ["KEEL_TEST_POSTGRES_DSN"]
        from tests.sandbox_support import reset_postgres_schema

        await reset_postgres_schema(url)
    opened = await open_store(url, clock=clock)
    yield opened
    await opened.close()


@pytest.fixture
async def local_sandbox(tmp_path):
    """A LocalSandbox whose workdir is tmp_path/work; sandbox paths are host paths."""
    work = tmp_path / "work"
    backend = LocalSandbox(kill_grace_s=0.5)
    sandbox = await backend.ensure("p1", parse_policy({"image": "local", "workdir": str(work)}))
    return backend, sandbox, work
