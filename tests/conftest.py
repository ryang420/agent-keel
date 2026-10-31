import os
from urllib.parse import urlsplit, urlunsplit
import uuid

import asyncpg

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


def pytest_collection_modifyitems(items):
    for item in items:
        if "pg_dsn" in item.fixturenames:
            item.add_marker(pytest.mark.integration)


@pytest.fixture
async def pg_dsn():
    """An empty, private database per test; never reset the supplied server database.

    Connections and subprocesses in one test share the returned DSN and commit normally.
    Generated database names also isolate concurrent pytest processes/workers.
    """
    server_dsn = os.environ.get("KEEL_TEST_POSTGRES_DSN")
    if not server_dsn:
        pytest.fail("PostgreSQL integration tests require KEEL_TEST_POSTGRES_DSN. "
                    "Start docker compose -f docker-compose.test.yml up -d --wait, "
                    "or run pytest -m 'not integration' for unit tests.", pytrace=False)
    parts = urlsplit(server_dsn)
    if parts.scheme not in ("postgres", "postgresql"):
        pytest.fail("KEEL_TEST_POSTGRES_DSN must be a PostgreSQL URL.", pytrace=False)
    name = "keel_test_" + uuid.uuid4().hex
    # Name is generated internally, never derived from caller input.
    admin = await asyncpg.connect(server_dsn, timeout=10)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    dsn = urlunsplit(parts._replace(path="/" + name))
    try:
        yield dsn
    finally:
        admin = await asyncpg.connect(server_dsn, timeout=10)
        try:
            await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        finally:
            await admin.close()


@pytest.fixture
async def store(pg_dsn, clock):
    opened = await open_store(pg_dsn, clock=clock)
    try:
        yield opened
    finally:
        await opened.close()


@pytest.fixture
async def local_sandbox(tmp_path):
    """A LocalSandbox whose workdir is tmp_path/work; sandbox paths are host paths."""
    work = tmp_path / "work"
    backend = LocalSandbox(kill_grace_s=0.5)
    sandbox = await backend.ensure("p1", parse_policy({"image": "local", "workdir": str(work)}))
    return backend, sandbox, work
