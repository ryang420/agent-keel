"""Shared fixtures for the sandbox coordination store tests.

The helpers seed only what a test needs and always go through the public store surface for the behaviour under
test; direct SQL is used just to arrange states that later tasks will produce through guarded transitions.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from contextlib import asynccontextmanager

from keel.sandbox.base import parse_policy
from keel.sandbox.records import RunClaim

MODELS = [{"provider": "fake", "model": "m"}]
DEFAULT_SANDBOX = {"image": "img", "workdir": "/w"}


async def seed_profile(store, profile_id: str = "p1", *, sandbox: dict | None = None):
    return await store.upsert_profile(
        profile_id, models=MODELS, sandbox=DEFAULT_SANDBOX if sandbox is None else sandbox, soul="You help.",
        skills=[])


async def queue_run(store, session_id: str, *, profile_id: str = "p1"):
    await store.ensure_session(profile_id, session_id)
    return await store.create_run(
        profile_id=profile_id, session_id=session_id, provider="fake", model="m", reasoning_effort=None,
        input_parts=[{"type": "text", "text": "hi"}], instructions=None)


async def make_claim(store, session_id: str, *, profile_id: str = "p1", sandbox: dict | None = None,
                     deployment: str = "dep", daemon_id: str = "daemon-1", owner: str = "w1",
                     lease_seconds: float = 30) -> RunClaim:
    """Seed a profile/session, queue one run and claim it through the coordinated admission path."""
    await seed_profile(store, profile_id, sandbox=sandbox)
    run = await queue_run(store, session_id, profile_id=profile_id)
    claim = await store.claim_run_with_usage(owner, lease_seconds, deployment=deployment, daemon_id=daemon_id)
    assert claim is not None and claim.run.id == run.id
    return claim


def container_id() -> str:
    """A bare Docker ID: 64 lowercase hex characters."""
    return uuid.uuid4().hex + uuid.uuid4().hex


async def seed_ready_sandbox(store, *, profile_id: str = "p1", deployment: str = "dep",
                             daemon_id: str = "daemon-1", sandbox: dict | None = None, generation: int = 1,
                             generation_token: str | None = None, container: str | None = None) -> dict:
    """Record a published generation directly, as publication will do in a later task."""
    policy = parse_policy(DEFAULT_SANDBOX if sandbox is None else sandbox, mount_root="/")
    token = generation_token or uuid.uuid4().hex
    exact_id = container or container_id()
    name = f"keel-sbx-test-{hashlib.sha256(profile_id.encode()).hexdigest()[:12]}-{token[:16]}"
    now = await store.db.database_now()
    await store.db.execute(
        "INSERT INTO sandboxes (deployment, profile_id, daemon_id, generation, generation_token, container_id, "
        "container_name, policy_digest, state, transitioned_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?)",
        deployment, profile_id, daemon_id, generation, token, exact_id, name, policy.digest(), now)
    return {"generation": generation, "generation_token": token, "container_id": exact_id,
            "container_name": name, "policy": policy}


class TransactionProbe:
    """A `Db` proxy that records whether a test backend operation is invoked while a transaction is open.

    Task 2's store mixin has no Docker or model calls at all, so the structural invariant holds by construction:
    every public method returns with the transaction scope closed. The probe makes that observable, and later
    tasks can reuse it when a real backend is interleaved with these store calls.
    """

    def __init__(self, db) -> None:
        self.inner = db
        self.depth = 0
        self.max_depth = 0
        self.backend_observations: list[tuple[str, int]] = []

    @property
    def dialect(self) -> str:
        return self.inner.dialect

    async def execute(self, sql: str, *args) -> None:
        await self.inner.execute(sql, *args)

    async def fetchrow(self, sql: str, *args) -> dict | None:
        return await self.inner.fetchrow(sql, *args)

    async def fetch(self, sql: str, *args) -> list[dict]:
        return await self.inner.fetch(sql, *args)

    async def executescript(self, script: str) -> None:
        await self.inner.executescript(script)

    async def database_now(self) -> float:
        return await self.inner.database_now()

    @asynccontextmanager
    async def transaction(self):
        async with self.inner.transaction():
            self.depth += 1
            self.max_depth = max(self.max_depth, self.depth)
            try:
                yield
            finally:
                self.depth -= 1

    async def backend_operation(self, label: str) -> None:
        self.backend_observations.append((label, self.depth))
        await asyncio.sleep(0)

    async def close(self) -> None:
        await self.inner.close()
