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
from keel.sandbox.records import ContainerObservation, DockerOutcome, RunClaim

MODELS = [{"provider": "fake", "model": "m"}]
DEFAULT_SANDBOX = {"image": "img", "workdir": "/w"}


class Barrier:
    """Hold one backend call once it has recorded its dispatch, until the test releases it."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def wait(self) -> None:
        self.entered.set()
        await self.release.wait()


class FakeDocker:
    """A coordinated backend double for the preparation tests.

    `calls` records the external lifecycle dispatches in order. `hold_next` arms a one-shot barrier consumed by
    the next call of that kind, so a concurrent preparation can proceed while one call is still in flight.
    `set_result` forces an explicit definite or uncertain outcome for every later call of that kind. `observe`
    rewrites the observation reported for a create or start, so identity verification can be exercised. When
    `depth` is supplied (for example from a `TransactionProbe`), every dispatch records the transaction depth at
    entry, making it observable that no external call starts while a database transaction is open.
    """

    def __init__(self, *, daemon_id: str = "daemon-1", architecture: str = "amd64",
                 containers: list[str] | None = None, depth=None) -> None:
        self.daemon_id = daemon_id
        self.architecture = architecture
        self.calls: list[str] = []
        self.observations: list[tuple[str, int]] = []
        self.attempts: dict[str, list] = {}
        self.policies: list = []
        self.installations: list = []
        self.created_ids: list[str] = []
        self.inspections: list[str] = []
        self.removed_ids: list[str] = []
        self.running_ids: set[str] = set()
        self.observe = None
        self._containers = list(containers or [])
        self._created: dict[str, tuple[str, tuple[tuple[str, str], ...], str]] = {}
        self._barriers: dict[str, list[Barrier]] = {}
        self._results: dict[str, DockerOutcome] = {}
        self._depth = depth or (lambda: 0)

    @property
    def created_id(self) -> str | None:
        """The most recently created container ID, for single-generation assertions."""
        return self.created_ids[-1] if self.created_ids else None

    def hold_next(self, kind: str) -> Barrier:
        barrier = Barrier()
        self._barriers.setdefault(kind, []).append(barrier)
        return barrier

    def set_result(self, kind: str, outcome: DockerOutcome) -> None:
        self._results[kind] = outcome

    def _enter(self, kind: str) -> None:
        self.calls.append(kind)
        self.observations.append((kind, self._depth()))
        self.attempts.setdefault(kind, [])

    async def _wait(self, kind: str) -> None:
        barriers = self._barriers.get(kind)
        if barriers:
            await barriers.pop(0).wait()

    def _next_container(self) -> str:
        return self._containers.pop(0) if self._containers else container_id()

    def _running_observation(self, container_id: str) -> ContainerObservation | None:
        created = self._created.get(container_id)
        if created is None:
            return None
        name, labels, digest = created
        return ContainerObservation(self.daemon_id, container_id, name, labels,
                                    container_id in self.running_ids, digest)

    def _reported(self, kind: str, observation: ContainerObservation) -> ContainerObservation:
        return self.observe(kind, observation) if self.observe is not None else observation

    async def daemon_identity(self) -> tuple[str, str]:
        return self.daemon_id, self.architecture

    async def create_generation(self, attempt, policy) -> DockerOutcome:
        self._enter("create")
        exact = self._next_container()
        self.created_ids.append(exact)
        self.attempts["create"].append(attempt)
        self.policies.append(policy)
        labels = (
            ("keel.create-operation", attempt.id),
            ("keel.deployment", attempt.key.deployment),
            ("keel.generation", str(attempt.generation)),
            ("keel.generation-token", attempt.generation_token),
            ("keel.managed", "1"),
            ("keel.policy", policy.digest()),
            ("keel.profile", attempt.key.profile_id),
        )
        self._created[exact] = (attempt.intended_name, labels, policy.digest())
        await self._wait("create")
        forced = self._results.get("create")
        if forced is not None:
            return forced
        observation = ContainerObservation(
            self.daemon_id, exact, attempt.intended_name, labels, running=False, policy_digest=policy.digest())
        return DockerOutcome("succeeded", observation=self._reported("create", observation))

    async def install_helper(self, attempt, artifact) -> DockerOutcome:
        self._enter("install_helper")
        self.attempts["install_helper"].append(attempt)
        self.installations.append(artifact)
        await self._wait("install_helper")
        return self._results.get("install_helper", DockerOutcome("succeeded"))

    async def start_id(self, attempt) -> DockerOutcome:
        self._enter("start")
        self.attempts["start"].append(attempt)
        await self._wait("start")
        forced = self._results.get("start")
        if forced is not None:
            return forced
        self.running_ids.add(attempt.container_id)
        observation = self._running_observation(attempt.container_id)
        if observation is None:
            return DockerOutcome("definite_failure", diagnostic="the container does not exist")
        return DockerOutcome("succeeded", observation=self._reported("start", observation))

    async def inspect_id(self, container_id: str) -> DockerOutcome:
        self.inspections.append(container_id)
        observation = self._running_observation(container_id)
        if observation is None:
            return DockerOutcome("definite_failure", diagnostic="the container does not exist")
        return DockerOutcome("succeeded", observation=observation)

    async def inventory(self) -> list[ContainerObservation]:
        return [self._running_observation(exact) for exact in self._created]

    async def remove_id(self, attempt) -> DockerOutcome:
        self._enter("remove")
        self.attempts.setdefault("remove", []).append(attempt)
        await self._wait("remove")
        result = self._results.get("remove", DockerOutcome("succeeded"))
        if result.state == "succeeded":
            self.removed_ids.append(attempt.container_id)
            self._created.pop(attempt.container_id, None)
            self.running_ids.discard(attempt.container_id)
        return result

    async def aclose(self) -> None:
        return None


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
                             generation_token: str | None = None, container: str | None = None,
                             helper_digest: str | None = None,
                             helper_protocol: int | None = None) -> dict:
    """Record a published generation directly, as publication will do in a later task."""
    policy = parse_policy(DEFAULT_SANDBOX if sandbox is None else sandbox, mount_root="/")
    token = generation_token or uuid.uuid4().hex
    exact_id = container or container_id()
    name = f"keel-sbx-test-{hashlib.sha256(profile_id.encode()).hexdigest()[:12]}-{token[:16]}"
    now = await store.db.database_now()
    await store.db.execute(
        "INSERT INTO sandboxes (deployment, profile_id, daemon_id, generation, generation_token, container_id, "
        "container_name, policy_digest, helper_protocol, helper_digest, state, transitioned_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?)",
        deployment, profile_id, daemon_id, generation, token, exact_id, name, policy.digest(),
        helper_protocol if helper_protocol is not None else (1 if helper_digest is not None else None),
        helper_digest, now)
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


async def current_container_id(store, key) -> str | None:
    row = await store.db.fetchrow(
        "SELECT container_id FROM sandboxes WHERE deployment = ? AND profile_id = ?",
        key.deployment, key.profile_id)
    return row["container_id"] if row else None
