import asyncio

import httpx

from keel.config import ServiceCredential, Settings
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.sandbox.records import ContainerObservation
from keel.service import create_service
from tests.sandbox_support import LOCAL_DAEMON, LocalDocker, container_id, install_helper_assets


async def test_the_service_starts_runs_and_stops(tmp_path, pg_dsn):
    # Docker mode with no reachable Docker: chat-only runs still start and complete.
    settings = Settings(credentials=(ServiceCredential(1, "k"),),
                        database_url=pg_dsn, workers=1,
                        docker=str(tmp_path / "no-docker"))
    providers = ProviderRegistry()
    providers.register(FakeProvider())
    app = create_service(settings, providers=providers)
    headers = {"Authorization": "Bearer k"}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://keel",
                                     headers=headers) as client:
            assert (await client.get("/healthz")).status_code == 200
            await client.put("/v1/profiles/p1", json={"models": [{"provider": "fake", "model": "m"}]})
            run_id = (await client.post("/v1/profiles/p1/runs", json={
                "input": "hi", "provider": "fake", "model": "m"})).json()["run_id"]
            for _ in range(250):
                status = (await client.get(f"/v1/profiles/p1/runs/{run_id}")).json()["status"]
                if status == "completed":
                    break
                await asyncio.sleep(0.02)
            assert status == "completed"


async def test_the_service_starts_without_a_sandbox_backend(tmp_path, pg_dsn):
    from keel.service import build

    settings = Settings(credentials=(ServiceCredential(1, "k"),),
                        database_url=pg_dsn, sandbox="none",
                        compression_threshold=0.5, memory_chars=100, user_chars=50, memory_review_turns=3)
    keel, pool, closables, backend = await build(settings)
    try:
        assert backend is None and keel.executor.coordinator is None and pool.coordinator is None
        assert keel.executor.background is keel.background
        assert keel.executor.compactor.threshold == 0.5
        assert (keel.executor.memory_chars, keel.executor.user_chars) == (100, 50)
        assert pool.event_retention_s == 7 * 86_400
        assert pool.reviewer is not None and pool.reviewer.settings.turns == 3
    finally:
        for item in closables:
            close = getattr(item, "aclose", None) or getattr(item, "close")
            await close()


async def test_a_failing_pool_start_still_closes_everything(tmp_path, monkeypatch, pg_dsn):
    import pytest

    from keel import service
    from keel.worker import WorkerPool

    async def boom(self):
        raise RuntimeError("no start")

    closed = []

    class Spy:
        async def aclose(self):
            closed.append(True)

    real_build = service.build

    async def build(*args, **kwargs):
        keel, pool, closables, backend = await real_build(*args, **kwargs)
        return keel, pool, [*closables, Spy()], backend

    monkeypatch.setattr(WorkerPool, "start", boom)
    monkeypatch.setattr(service, "build", build)
    settings = Settings(credentials=(ServiceCredential(1, "k"),),
                        database_url=pg_dsn, sandbox="none")
    app = service.create_service(settings)
    with pytest.raises(RuntimeError, match="no start"):
        async with app.router.lifespan_context(app):
            pass
    assert closed == [True]


async def test_openai_is_registered_only_when_a_key_is_set(tmp_path):
    from keel.service import default_providers

    base = {"credentials": (ServiceCredential(1, "k"),), "database_url": "postgresql://unused/keel"}
    without = default_providers(Settings(**base))
    with_key = default_providers(Settings(**base, openai_api_key="sk-x"))
    try:
        assert without.names() == ["bedrock"]
        assert with_key.names() == ["bedrock", "openai"]
    finally:
        await without.aclose()
        await with_key.aclose()


async def test_both_providers_use_the_configured_stall_limits(tmp_path):
    from keel.service import default_providers

    settings = Settings(credentials=(ServiceCredential(1, "k"),), database_url="postgresql://unused/keel",
                        openai_api_key="sk-x", model_first_event_seconds=7.0, model_stall_seconds=42.0,
                        model_high_effort_stall_seconds=300.0)
    registry = default_providers(settings)
    try:
        for name in ("bedrock", "openai"):
            provider = registry.get(name)
            # The byte-level read timeout is only a backstop; the stall limits are timed between events.
            assert provider._client.timeout.read == 600.0 and provider._client.timeout.connect == 10.0
            assert provider._transport.stall == settings.stall_limits
    finally:
        await registry.aclose()


async def test_build_derives_the_database_timeout_from_the_lease(tmp_path, monkeypatch, pg_dsn):
    from keel import service

    seen = {}
    real = service.open_store

    async def spy(url, **kwargs):
        seen.update(kwargs)
        return await real(url)

    monkeypatch.setattr(service, "open_store", spy)
    settings = Settings(credentials=(ServiceCredential(1, "k"),),
                        database_url=pg_dsn, sandbox="none",
                        lease_seconds=60)
    keel, pool, closables, backend = await service.build(settings)
    try:
        assert seen == {"command_timeout": 20.0, "tenant_ids": (1,), "tenant_backfill": {}}
    finally:
        for item in closables:
            close = getattr(item, "aclose", None) or getattr(item, "close")
            await close()


def _docker_settings(tmp_path, pg_dsn, **extra) -> Settings:
    return Settings(credentials=(ServiceCredential(1, "k"),), database_url=pg_dsn,
                    workers=1, docker=str(tmp_path / "no-docker"), deployment="dep", **extra)


async def _close(closables) -> None:
    for item in closables:
        close = getattr(item, "aclose", None) or getattr(item, "close")
        await close()


async def _wait_for_status(client, profile_id: str, run_id: str, wanted: str, seconds: float = 5.0) -> str:
    status = None
    for _ in range(int(seconds / 0.02)):
        status = (await client.get(f"/v1/profiles/{profile_id}/runs/{run_id}")).json()["status"]
        if status == wanted:
            break
        await asyncio.sleep(0.02)
    return status


def _labels(profile_id: str) -> tuple[tuple[str, str], ...]:
    return (("keel.deployment", "dep"), ("keel.managed", "1"), ("keel.profile", profile_id))


class _ServiceDocker(LocalDocker):
    """The service's coordinated backend in these tests; it has only exact-ID lifecycle operations."""

    def __init__(self, **kwargs) -> None:
        super().__init__(daemon_id=LOCAL_DAEMON)
        self.kwargs = kwargs
        self.extra_inventory: list[ContainerObservation] = []
        self.inventory_calls = 0

    async def inventory(self):
        self.inventory_calls += 1
        return [*await super().inventory(), *self.extra_inventory]


async def test_build_wires_one_coordinator_into_the_executor_and_the_pool(tmp_path, pg_dsn):
    from keel.sandbox.coordinator import SandboxCoordinator
    from keel.service import build

    settings = _docker_settings(tmp_path, pg_dsn, lease_seconds=45.0, sandbox_reap_seconds=7.0, sandbox_idle_seconds=99.0)
    keel, pool, closables, coordinator = await build(settings)
    try:
        assert isinstance(coordinator, SandboxCoordinator)
        assert keel.executor.coordinator is coordinator and pool.coordinator is coordinator
        assert coordinator.bus is keel.bus and coordinator.store is keel.store
        # One lease length for the run claim, the usage and the lifecycle operations.
        assert coordinator.lease_seconds == pool.lease_seconds == 45.0
        assert coordinator.reap_seconds == coordinator.backend.reap_seconds == 7.0
        assert coordinator.idle_seconds == 99.0
        assert (coordinator.owner, coordinator.deployment) == (settings.instance_id, "dep")
    finally:
        await _close(closables)


async def test_sandboxed_runs_wait_while_chat_only_runs_proceed_when_docker_cannot_initialize(tmp_path, pg_dsn):
    settings = _docker_settings(tmp_path, pg_dsn)
    providers = ProviderRegistry()
    providers.register(FakeProvider())
    app = create_service(settings, providers=providers)
    async with app.router.lifespan_context(app):
        store = app.state.keel.store
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://keel",
                                     headers={"Authorization": "Bearer k"}) as client:
            models = [{"provider": "fake", "model": "m"}]
            await client.put("/v1/profiles/boxed", json={"models": models, "sandbox": {"image": "i", "workdir": "/w"}})
            await client.put("/v1/profiles/chat", json={"models": models})
            boxed = (await client.post("/v1/profiles/boxed/runs", json={
                "input": "hi", "session_id": "s1", "provider": "fake", "model": "m"})).json()["run_id"]
            chat = (await client.post("/v1/profiles/chat/runs", json={
                "input": "hi", "session_id": "s2", "provider": "fake", "model": "m"})).json()["run_id"]
            assert await _wait_for_status(client, "chat", chat, "completed") == "completed"
            # The sandboxed run at the head of the queue waits for sandbox admission; it never runs tool-less.
            assert (await client.get(f"/v1/profiles/boxed/runs/{boxed}")).json()["status"] == "queued"
            assert (await store.get_session("boxed", "s1")).system_prompt is None
            assert (await client.post(f"/v1/profiles/boxed/runs/{boxed}/stop")).json()["status"] == "cancelled"


async def test_startup_reconciles_inventory_and_unknown_inventory_blocks_only_its_profile(tmp_path, monkeypatch, pg_dsn):
    from keel import service

    install_helper_assets(tmp_path / "helper", monkeypatch)
    backends: list[_ServiceDocker] = []

    def factory(**kwargs):
        backend = _ServiceDocker(**kwargs)
        # More containers than one bounded batch, with the unknown one for p2 sorting last: startup keeps
        # classifying batches until the whole inventory is covered before any sandbox admission.
        for index in range(150):
            backend.extra_inventory.append(ContainerObservation(
                LOCAL_DAEMON, "0" * 60 + f"{index:04d}", f"keel-sbx-filler{index}", _labels(f"filler{index}")))
        backend.extra_inventory.append(ContainerObservation(
            LOCAL_DAEMON, "f" * 64, "keel-sbx-unknown", _labels("p2"), running=True))
        backends.append(backend)
        return backend

    monkeypatch.setattr(service, "DockerSandbox", factory)
    settings = _docker_settings(tmp_path, pg_dsn, sandbox_reap_seconds=3.0)
    providers = ProviderRegistry()
    providers.register(FakeProvider())
    sandbox = {"image": "img", "workdir": str(tmp_path / "work")}
    app = create_service(settings, providers=providers)
    async with app.router.lifespan_context(app):
        backend = backends[0]
        assert backend.kwargs["reap_seconds"] == 3.0
        assert backend.inventory_calls >= 2  # startup covered the whole inventory before workers started
        assert app.state.keel.executor.coordinator.admitting()
        store = app.state.keel.store
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://keel",
                                     headers={"Authorization": "Bearer k"}) as client:
            models = [{"provider": "fake", "model": "m"}]
            for profile in ("p1", "p2"):
                await client.put(f"/v1/profiles/{profile}", json={"models": models, "sandbox": sandbox})
            blocked = (await client.post("/v1/profiles/p2/runs", json={
                "input": "hi", "session_id": "s2", "provider": "fake", "model": "m"})).json()["run_id"]
            healthy = (await client.post("/v1/profiles/p1/runs", json={
                "input": "hi", "session_id": "s1", "provider": "fake", "model": "m"})).json()["run_id"]
            assert await _wait_for_status(client, "p1", healthy, "completed") == "completed"
            assert (await client.get(f"/v1/profiles/p2/runs/{blocked}")).json()["status"] == "queued"
            row = await store.db.fetchrow("SELECT state, error_code FROM sandboxes WHERE profile_id = 'p2'")
            assert row == {"state": "blocked", "error_code": "sandbox_inventory_conflict"}
            # Unknown inventory is preserved as evidence, never removed by name, age or a stale profile list.
            assert backend.removed_ids == [] and backend.calls.count("create") == 1


async def test_maintenance_runs_bounded_reconcile_and_idle_passes_until_shutdown(tmp_path, monkeypatch, pg_dsn):
    from keel import service
    from keel.sandbox.coordinator import SandboxCoordinator
    from keel.sandbox.records import RecoveryReport

    install_helper_assets(tmp_path / "helper", monkeypatch)
    monkeypatch.setattr(service, "DockerSandbox", _ServiceDocker)
    monkeypatch.setattr(service, "MAINTENANCE_SECONDS", 0.05)
    monkeypatch.setattr(service, "ADMISSION_RETRY_SECONDS", 0.05)
    calls: list[tuple[str, int]] = []

    async def reconcile_once(self, *, limit=100):
        calls.append(("reconcile", limit))
        if len(calls) == 1:
            raise RuntimeError("the first pass fails")  # a failed attempt is logged; maintenance retries
        self._inventory_daemon, self._inventory_pending = self._daemon_id, 0  # a complete inventory cycle
        return RecoveryReport()

    async def reap_idle_once(self, *, limit=100):
        calls.append(("idle", limit))
        return RecoveryReport()

    monkeypatch.setattr(SandboxCoordinator, "reconcile_once", reconcile_once)
    monkeypatch.setattr(SandboxCoordinator, "reap_idle_once", reap_idle_once)
    app = create_service(_docker_settings(tmp_path, pg_dsn))
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if calls.count(("idle", 100)) >= 2:
                break
            await asyncio.sleep(0.02)
    stopped = len(calls)
    await asyncio.sleep(0.15)
    assert calls[0] == ("reconcile", 100)
    assert calls.count(("reconcile", 100)) >= 2 and calls.count(("idle", 100)) >= 2
    assert set(calls) == {("reconcile", 100), ("idle", 100)}
    assert len(calls) == stopped  # nothing runs after shutdown


async def test_maintenance_prunes_records_with_the_configured_retention_and_logs_failures(tmp_path, monkeypatch,
                                                                                          caplog, pg_dsn):
    import logging

    from keel import service
    from keel.sandbox.coordinator import SandboxCoordinator
    from keel.sandbox.records import RecoveryReport
    from keel.store.sql import SqlStore

    install_helper_assets(tmp_path / "helper", monkeypatch)
    monkeypatch.setattr(service, "DockerSandbox", _ServiceDocker)
    monkeypatch.setattr(service, "MAINTENANCE_SECONDS", 0.05)
    monkeypatch.setattr(service, "ADMISSION_RETRY_SECONDS", 0.05)

    async def reconcile_once(self, *, limit=100):
        self._inventory_daemon, self._inventory_pending = self._daemon_id, 0
        return RecoveryReport(unresolved=2, oldest_age_s=90.0)

    async def reap_idle_once(self, *, limit=100):
        return RecoveryReport()

    monkeypatch.setattr(SandboxCoordinator, "reconcile_once", reconcile_once)
    monkeypatch.setattr(SandboxCoordinator, "reap_idle_once", reap_idle_once)
    calls: list[tuple[float, int]] = []

    async def prune(self, retention_seconds, *, limit=100):
        calls.append((retention_seconds, limit))
        if len(calls) == 1:
            raise RuntimeError("the prune pass fails")
        return 0

    monkeypatch.setattr(SqlStore, "prune_sandbox_records", prune)
    caplog.set_level(logging.INFO, logger=service.log.name)
    app = create_service(_docker_settings(tmp_path, pg_dsn, event_retention_days=3))
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.02)
    assert calls[0] == (3 * 86_400.0, 100)  # the configured event retention, one bounded batch
    assert len(calls) >= 2, "a failed prune must be logged, never stop maintenance"
    assert "pruning sandbox records failed" in caplog.text
    # Unresolved tombstones alone are logged, with the age design section 14 asks to expose.
    assert "unresolved=2 oldest_age_s=90.0" in caplog.text


async def test_claims_never_wait_on_docker_and_admission_opens_after_a_full_inventory(tmp_path, monkeypatch, store):
    from keel.sandbox.base import SandboxError
    from keel.sandbox.coordinator import SandboxCoordinator

    class Flaky(LocalDocker):
        down = True
        identity_calls = 0

        async def daemon_identity(self):
            self.identity_calls += 1
            if self.down:
                raise SandboxError("the daemon is down")
            return await super().daemon_identity()

    install_helper_assets(tmp_path / "helper", monkeypatch)
    backend = Flaky(daemon_id=LOCAL_DAEMON)
    coordinator = SandboxCoordinator(
        store, backend, deployment="dep", owner="w1", lease_seconds=30, idle_seconds=60, reap_seconds=1)
    models = [{"provider": "fake", "model": "m"}]
    await store.upsert_profile("boxed", models=models, sandbox={"image": "i", "workdir": "/w"}, soul="", skills=[])
    await store.upsert_profile("chat", models=models, sandbox={}, soul="", skills=[])
    runs = {}
    for profile in ("boxed", "chat"):
        await store.ensure_session(profile, profile + "-s")
        runs[profile] = await store.create_run(
            profile_id=profile, session_id=profile + "-s", provider="fake", model="m", reasoning_effort=None,
            input_parts=[{"type": "text", "text": "hi"}], instructions=None)
    claim = await coordinator.claim()  # withheld, not failed: the chat-only run is claimed without touching Docker
    assert claim is not None and claim.run.id == runs["chat"].id and claim.use is None
    assert backend.identity_calls == 0 and await coordinator.claim() is None
    assert await coordinator.open_admission() is False and not coordinator.admitting()
    backend.down = False
    assert await coordinator.open_admission() is True and coordinator.admitting()
    claim = await coordinator.claim()
    assert claim is not None and claim.run.id == runs["boxed"].id and claim.use is not None


async def test_docker_down_at_startup_keeps_sandbox_admission_closed_until_inventory_is_classified(
        tmp_path, monkeypatch, pg_dsn):
    from keel import service
    from keel.sandbox.base import SandboxError

    install_helper_assets(tmp_path / "helper", monkeypatch)
    monkeypatch.setattr(service, "ADMISSION_RETRY_SECONDS", 0.05)
    state = {"down": True}
    backends: list[_ServiceDocker] = []

    class Flaky(_ServiceDocker):
        async def daemon_identity(self):
            if state["down"]:
                raise SandboxError("down")
            return await super().daemon_identity()

        async def inventory(self):
            if state["down"]:
                raise SandboxError("down")
            return await super().inventory()

    def factory(**kwargs):
        backend = Flaky(**kwargs)
        backend.extra_inventory.append(ContainerObservation(
            LOCAL_DAEMON, "f" * 64, "keel-sbx-legacy", _labels("p2"), running=True))
        backends.append(backend)
        return backend

    monkeypatch.setattr(service, "DockerSandbox", factory)
    providers = ProviderRegistry()
    providers.register(FakeProvider())
    sandbox = {"image": "img", "workdir": str(tmp_path / "work")}
    app = service.create_service(_docker_settings(tmp_path, pg_dsn), providers=providers)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://keel",
                                     headers={"Authorization": "Bearer k"}) as client:
            models = [{"provider": "fake", "model": "m"}]
            for profile in ("p1", "p2"):
                await client.put(f"/v1/profiles/{profile}", json={"models": models, "sandbox": sandbox})
            legacy = (await client.post("/v1/profiles/p2/runs", json={
                "input": "hi", "session_id": "s2", "provider": "fake", "model": "m"})).json()["run_id"]
            healthy = (await client.post("/v1/profiles/p1/runs", json={
                "input": "hi", "session_id": "s1", "provider": "fake", "model": "m"})).json()["run_id"]
            await asyncio.sleep(0.2)
            assert (await client.get(f"/v1/profiles/p1/runs/{healthy}")).json()["status"] == "queued"
            state["down"] = False
            assert await _wait_for_status(client, "p1", healthy, "completed") == "completed"
            assert (await client.get(f"/v1/profiles/p2/runs/{legacy}")).json()["status"] == "queued"
            row = await app.state.keel.store.db.fetchrow("SELECT state FROM sandboxes WHERE profile_id = 'p2'")
            assert row == {"state": "blocked"} and backends[0].removed_ids == []


async def test_a_hung_daemon_neither_stalls_startup_nor_chat_only_runs(tmp_path, monkeypatch, pg_dsn):
    import time

    from keel import service

    install_helper_assets(tmp_path / "helper", monkeypatch)
    monkeypatch.setattr(service, "ADMISSION_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(service, "ADMISSION_RETRY_SECONDS", 0.05)

    class Hung(_ServiceDocker):
        async def daemon_identity(self):
            await asyncio.Event().wait()  # a wedged daemon: the request never answers

    monkeypatch.setattr(service, "DockerSandbox", Hung)
    providers = ProviderRegistry()
    providers.register(FakeProvider())
    app = service.create_service(_docker_settings(tmp_path, pg_dsn), providers=providers)
    started = time.monotonic()
    async with app.router.lifespan_context(app):
        assert time.monotonic() - started < 1.0
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://keel",
                                     headers={"Authorization": "Bearer k"}) as client:
            await client.put("/v1/profiles/chat", json={"models": [{"provider": "fake", "model": "m"}]})
            sent = time.monotonic()
            run = (await client.post("/v1/profiles/chat/runs", json={
                "input": "hi", "session_id": "s", "provider": "fake", "model": "m"})).json()["run_id"]
            assert await _wait_for_status(client, "chat", run, "completed") == "completed"
            assert time.monotonic() - sent < 1.0


async def test_a_replaced_daemon_closes_sandbox_admission_for_this_process(tmp_path, monkeypatch, store):
    from keel.sandbox.coordinator import SandboxCoordinator

    install_helper_assets(tmp_path / "helper", monkeypatch)
    backend = LocalDocker(daemon_id=LOCAL_DAEMON)
    coordinator = SandboxCoordinator(
        store, backend, deployment="dep", owner="w1", lease_seconds=30, idle_seconds=60, reap_seconds=1)
    assert await coordinator.open_admission() is True
    await store.upsert_profile("boxed", models=[{"provider": "fake", "model": "m"}],
                               sandbox={"image": "i", "workdir": "/w"}, soul="", skills=[])
    await store.ensure_session("boxed", "s1")
    await store.create_run(profile_id="boxed", session_id="s1", provider="fake", model="m", reasoning_effort=None,
                           input_parts=[{"type": "text", "text": "hi"}], instructions=None)
    backend.daemon_id = "another-daemon"
    await coordinator.reconcile_once()
    assert not coordinator.admitting() and await coordinator.claim() is None
    assert await coordinator.open_admission() is False
    backend.daemon_id = LOCAL_DAEMON  # even the restored daemon reopens admission only after a restart
    assert await coordinator.open_admission() is False and await coordinator.claim() is None
