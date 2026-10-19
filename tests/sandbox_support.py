"""Shared fixtures for the sandbox coordination store tests.

The helpers seed only what a test needs and always go through the public store surface for the behaviour under
test; direct SQL is used just to arrange states that later tasks will produce through guarded transitions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import uuid
from contextlib import asynccontextmanager

import keel.sandbox.runtime as sandbox_runtime
from keel.runner import RunExecutor
from keel.sandbox.base import ExecResult, Sandbox, parse_policy
from keel.sandbox.coordinator import SandboxCoordinator
from keel.sandbox.local import LocalSandbox
from keel.sandbox.records import CleanupReceipt, ContainerObservation, DockerOutcome, RunClaim
from keel.sandbox.runtime import HELPER_MANIFEST_NAME, HELPER_MANIFEST_VERSION, HELPER_PROTOCOL_VERSION

MODELS = [{"provider": "fake", "model": "m"}]
DEFAULT_SANDBOX = {"image": "img", "workdir": "/w"}
LOCAL_DEPLOYMENT = "dep"
LOCAL_DAEMON = "daemon-1"


def install_helper_assets(directory: Path, monkeypatch) -> dict[str, str]:
    """Write synthetic helper artifacts into `directory` and point `artifact_directory` at it.

    Tests never read the gitignored built binaries. The returned mapping holds each architecture's digest.
    """
    digests = write_helper_assets(directory)
    monkeypatch.setattr(sandbox_runtime, "artifact_directory", lambda: directory)
    return digests


def write_helper_assets(directory: Path) -> dict[str, str]:
    """Write the synthetic helper artifacts and manifest only (a child process points at them itself)."""
    directory.mkdir(parents=True, exist_ok=True)
    digests, artifacts = {}, {}
    for architecture in ("amd64", "arm64"):
        data = f"synthetic keel supervisor for {architecture}".encode()
        path = directory / f"keel-supervisor-linux-{architecture}"
        path.write_bytes(data)
        path.chmod(0o755)
        digests[architecture] = hashlib.sha256(data).hexdigest()
        artifacts[architecture] = {"file": path.name, "sha256": digests[architecture]}
    manifest = {"version": HELPER_MANIFEST_VERSION, "protocol": HELPER_PROTOCOL_VERSION, "artifacts": artifacts}
    (directory / HELPER_MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    return digests


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
        # Every external call and exact-ID inspection, in order: ("create"|"start"|"remove", id) and
        # ("inspect", id, "present"|"absent"), so tests can check that absence was verified before a replacement.
        self.journal: list[tuple[str, ...]] = []
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
        self.journal.append(("create", exact))
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
        self.journal.append(("start", attempt.container_id))
        observation = self._running_observation(attempt.container_id)
        if observation is None:
            return DockerOutcome("definite_failure", diagnostic="the container does not exist")
        return DockerOutcome("succeeded", observation=self._reported("start", observation))

    async def inspect_id(self, container_id: str) -> DockerOutcome:
        self.inspections.append(container_id)
        observation = self._running_observation(container_id)
        self.journal.append(("inspect", container_id, "absent" if observation is None else "present"))
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
            self.journal.append(("remove", attempt.container_id))
            self._created.pop(attempt.container_id, None)
            self.running_ids.discard(attempt.container_id)
        return result

    async def aclose(self) -> None:
        return None


class LocalDocker(FakeDocker):
    """A coordinated backend double whose bound executions run as host processes.

    For executor and worker tests: sandbox paths are host paths, and a new generation creates its policy's
    workdir. `exec_hook(ticket, argv)` runs before each dispatch and may block or raise; `control_hook(ticket)`
    runs before each control request. A cancel ends the exec's process group through `LocalSandbox` and returns a
    complete receipt, standing in for the supervisor (this double is test-only; production never signals a
    numeric group from outside the container).
    """

    def __init__(self, *, kill_grace_s: float = 0.5, **kwargs) -> None:
        super().__init__(**kwargs)
        self.kill_grace_s = kill_grace_s
        self.local = LocalSandbox(kill_grace_s=kill_grace_s)
        self.exec_calls: list[tuple[str, list[str]]] = []
        self.control_calls: list[str] = []
        self._exec_containers: dict[str, str] = {}
        self.exec_hook = None
        self.control_hook = None

    async def create_generation(self, attempt, policy) -> DockerOutcome:
        try:
            Path(policy.workdir).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # a workdir that cannot exist on this host simply fails the commands that need it
        return await super().create_generation(attempt, policy)

    @staticmethod
    def _handle(ticket) -> Sandbox:
        return Sandbox(ticket.use.key.profile_id, ticket.binding.container_id, ticket.binding.policy)

    async def exec_bound(self, ticket, argv, *, cwd, timeout_s, env=None, stdin=None, authorize_control=None):
        self.exec_calls.append((ticket.exec_id, list(argv)))
        self._exec_containers[ticket.exec_id] = ticket.binding.container_id
        if self.exec_hook is not None:
            await self.exec_hook(ticket, argv)
        result = await self.local.exec(self._handle(ticket), argv, cwd=cwd, timeout_s=timeout_s,
                                       exec_id=ticket.exec_id, env=env, stdin=stdin)
        return result, CleanupReceipt(ticket.exec_id, ticket.control_token, result.exit_code, True, True, None)

    async def remove_id(self, attempt) -> DockerOutcome:
        """Removing a container ends every process that ran in it, as Docker's forced removal does."""
        result = await super().remove_id(attempt)
        if result.state == "succeeded":
            for exec_id, container in list(self._exec_containers.items()):
                if container == attempt.container_id:
                    await self.local.kill_tree(None, exec_id)
        return result

    async def control_exec(self, ticket, *, cancel: bool) -> CleanupReceipt:
        self.control_calls.append(ticket.exec_id)
        if self.control_hook is not None:
            await self.control_hook(ticket)
        if cancel:
            await self.local.kill_tree(self._handle(ticket), ticket.exec_id)
        return CleanupReceipt(ticket.exec_id, ticket.control_token, 0, True, True, None)


async def local_coordinator(store, directory: Path, monkeypatch, *, owner: str = "w1",
                            lease_seconds: float = 30.0, idle_seconds: float = 1800.0, bus=None,
                            backend: LocalDocker | None = None) -> tuple[SandboxCoordinator, LocalDocker]:
    """An initialized coordinator over a `LocalDocker`, with synthetic helper artifacts under `directory`."""
    install_helper_assets(directory, monkeypatch)
    backend = backend or LocalDocker(daemon_id=LOCAL_DAEMON)
    coordinator = SandboxCoordinator(
        store, backend, deployment=LOCAL_DEPLOYMENT, owner=owner, lease_seconds=lease_seconds,
        idle_seconds=idle_seconds, reap_seconds=1.0, bus=bus)
    assert await coordinator.open_admission()  # initialized, with its (empty) inventory classified
    return coordinator, backend


class ClaimedExecutor(RunExecutor):
    """Test-only executor that remembers the usage each coordinated claim reserved, as the worker passes it.

    `claim_next` claims through the coordinator when there is one (the legacy store claim otherwise); `execute`
    without an explicit usage uses the remembered one.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.claims: dict = {}

    async def claim_next(self, owner: str = "w1"):
        if self.coordinator is None:
            return await self.store.claim_run(owner, await self.store.coordination_now() + 3600)
        claim = await self.coordinator.claim()
        assert claim is not None, "the coordinator claimed nothing"
        self.claims[claim.run.id] = claim.use
        return claim.run

    async def execute(self, run, *, usage=None) -> None:
        await super().execute(run, usage=usage if usage is not None else self.claims.pop(run.id, None))


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


# Every keel table, dependents first, so a disposable PostgreSQL database starts each test from a clean schema.
KEEL_TABLES = ("sandbox_execs, sandbox_operations, sandbox_uses, sandboxes, schema_version, memories, run_events, "
               "runs, messages, sessions, profiles, tenants, blobs, profile_deletions, "
               "profile_deletion_receipts")


async def reset_postgres_schema(dsn: str) -> None:
    """Drop keel's tables in the disposable test database named by `dsn` (never a real deployment's)."""
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(f"DROP TABLE IF EXISTS {KEEL_TABLES}")
    finally:
        await conn.close()


async def wait_until(probe, task: asyncio.Task | None = None, *, seconds: float = 30.0,
                     what: str = "the payload to become ready") -> None:
    """Poll the async `probe` until it is true, failing at the deadline or as soon as `task` ends first.

    This waits for readiness only (a file a payload wrote, a process that exists); it is never the ordering oracle.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not await probe():
        if task is not None and task.done():
            await task  # surfaces the payload's own failure
            raise AssertionError(f"the task finished before {what}")
        if loop.time() >= deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


def absence_verified_before_create(journal: list[tuple[str, ...]], old_id: str, new_id: str) -> bool:
    """True when an exact-ID inspection reported `old_id` absent after its removal and before `new_id` was created."""
    try:
        removed = journal.index(("remove", old_id))
        created = journal.index(("create", new_id))
    except ValueError:
        return False
    return any(entry == ("inspect", old_id, "absent") for entry in journal[removed:created])


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


class FileDocker:
    """A coordinated backend double whose containers live in files, so they outlive a killed keel process.

    Like a real daemon, the state directory keeps every created container (name, labels, running state) across
    process restarts, and `journal.jsonl` records each external call and exact-ID inspection in order. With
    `hold` set to a phase, the call for that phase writes `entered-<phase>` and then never returns, so the test can
    kill the process at that exact point: `create` before the container exists, `created` after Docker created it,
    `install`, `start` after it is running, and `exec` once a payload was dispatched. Executions themselves are not
    run; they succeed with a complete receipt.
    """

    def __init__(self, state: Path, *, hold: str | None = None, daemon_id: str = "daemon-file") -> None:
        self.state = state
        self.hold = hold
        self.daemon_id = daemon_id
        self.containers = state / "containers"
        self.containers.mkdir(parents=True, exist_ok=True)

    def _log(self, *entry: str) -> None:
        with (self.state / "journal.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(list(entry)) + "\n")

    def journal(self) -> list[tuple[str, ...]]:
        path = self.state / "journal.jsonl"
        if not path.exists():
            return []
        return [tuple(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line]

    async def _phase(self, phase: str) -> None:
        if self.hold == phase:
            (self.state / f"entered-{phase}").write_text("", encoding="utf-8")
            await asyncio.get_running_loop().create_future()  # held until the process is killed

    def _read(self, exact: str) -> dict | None:
        path = self.containers / f"{exact}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def _write(self, exact: str, value: dict) -> None:
        temporary = self.containers / f".{exact}.tmp"
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.replace(self.containers / f"{exact}.json")

    def _observation(self, exact: str, value: dict) -> ContainerObservation:
        labels = tuple((name, label) for name, label in value["labels"])
        return ContainerObservation(self.daemon_id, exact, value["name"], labels, value["running"],
                                    dict(labels).get("keel.policy"))

    def running(self) -> list[str]:
        return sorted(path.stem for path in self.containers.glob("*.json") if json.loads(path.read_text())["running"])

    def existing(self) -> list[str]:
        return sorted(path.stem for path in self.containers.glob("*.json"))

    async def daemon_identity(self) -> tuple[str, str]:
        return self.daemon_id, "amd64"

    async def inspect_id(self, container_id: str) -> DockerOutcome:
        value = self._read(container_id)
        self._log("inspect", container_id, "absent" if value is None else "present")
        if value is None:
            return DockerOutcome("definite_failure", diagnostic="the container does not exist")
        return DockerOutcome("succeeded", observation=self._observation(container_id, value))

    async def inventory(self) -> list[ContainerObservation]:
        return [self._observation(exact, self._read(exact)) for exact in self.existing()]

    async def create_generation(self, attempt, policy) -> DockerOutcome:
        await self._phase("create")
        exact = container_id()
        labels = [["keel.create-operation", attempt.id], ["keel.deployment", attempt.key.deployment],
                  ["keel.generation", str(attempt.generation)], ["keel.generation-token", attempt.generation_token],
                  ["keel.managed", "1"], ["keel.policy", policy.digest()], ["keel.profile", attempt.key.profile_id]]
        value = {"name": attempt.intended_name, "labels": labels, "running": False}
        self._write(exact, value)
        self._log("create", exact)
        await self._phase("created")
        return DockerOutcome("succeeded", observation=self._observation(exact, value))

    async def install_helper(self, attempt, artifact) -> DockerOutcome:
        await self._phase("install")
        return DockerOutcome("succeeded")

    async def start_id(self, attempt) -> DockerOutcome:
        value = self._read(attempt.container_id)
        if value is None:
            return DockerOutcome("definite_failure", diagnostic="the container does not exist")
        value["running"] = True
        self._write(attempt.container_id, value)
        self._log("start", attempt.container_id)
        await self._phase("start")
        return DockerOutcome("succeeded", observation=self._observation(attempt.container_id, value))

    async def remove_id(self, attempt) -> DockerOutcome:
        (self.containers / f"{attempt.container_id}.json").unlink(missing_ok=True)
        self._log("remove", attempt.container_id)
        return DockerOutcome("succeeded")

    async def exec_bound(self, ticket, argv, *, cwd, timeout_s, env=None, stdin=None, authorize_control=None):
        if self._read(ticket.binding.container_id) is None:
            raise ConnectionError("the container does not exist")
        self._log("exec", ticket.exec_id)
        await self._phase("exec")
        receipt = CleanupReceipt(ticket.exec_id, ticket.control_token, 0, True, True, None)
        return ExecResult(0, "ok\n", False, 0.0), receipt

    async def control_exec(self, ticket, *, cancel: bool) -> CleanupReceipt:
        return CleanupReceipt(ticket.exec_id, ticket.control_token, 0, True, True, None)

    async def aclose(self) -> None:
        return None
