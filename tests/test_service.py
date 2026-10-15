import asyncio

import httpx

from keel.config import ServiceCredential, Settings
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.service import create_service


async def test_the_service_starts_runs_and_stops(tmp_path):
    settings = Settings(credentials=(ServiceCredential(1, "k"),),
                        database_url=f"sqlite:///{tmp_path / 'keel.sqlite3'}", workers=1)
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


async def test_the_service_starts_without_a_sandbox_backend(tmp_path):
    from keel.service import build

    settings = Settings(credentials=(ServiceCredential(1, "k"),),
                        database_url=f"sqlite:///{tmp_path / 'k.db'}", sandbox="none",
                        compression_threshold=0.5, memory_chars=100, user_chars=50, memory_review_turns=3)
    keel, pool, closables, backend = await build(settings)
    try:
        assert backend is None and keel.executor.sandbox is None
        assert keel.executor.background is keel.background
        assert keel.executor.compactor.threshold == 0.5
        assert (keel.executor.memory_chars, keel.executor.user_chars, keel.executor.memory_review_turns) == (
            100, 50, 3)
        assert pool.event_retention_s == 7 * 86_400
    finally:
        for item in closables:
            close = getattr(item, "aclose", None) or getattr(item, "close")
            await close()


async def test_a_failing_pool_start_still_closes_everything(tmp_path, monkeypatch):
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
                        database_url=f"sqlite:///{tmp_path / 'k.db'}", sandbox="none")
    app = service.create_service(settings)
    with pytest.raises(RuntimeError, match="no start"):
        async with app.router.lifespan_context(app):
            pass
    assert closed == [True]


async def test_openai_is_registered_only_when_a_key_is_set(tmp_path):
    from keel.service import default_providers

    base = {"credentials": (ServiceCredential(1, "k"),), "database_url": f"sqlite:///{tmp_path / 'k.db'}"}
    without = default_providers(Settings(**base))
    with_key = default_providers(Settings(**base, openai_api_key="sk-x"))
    try:
        assert without.names() == ["bedrock"]
        assert with_key.names() == ["bedrock", "openai"]
    finally:
        await without.aclose()
        await with_key.aclose()


async def test_both_providers_give_up_on_a_silent_model_stream_after_the_stall_timeout(tmp_path):
    from keel.service import default_providers

    settings = Settings(credentials=(ServiceCredential(1, "k"),), database_url=f"sqlite:///{tmp_path / 'k.db'}",
                        openai_api_key="sk-x", model_stall_seconds=42.0)
    registry = default_providers(settings)
    try:
        for name in ("bedrock", "openai"):
            provider = registry.get(name)
            assert provider._client.timeout.read == 42.0 and provider._client.timeout.connect == 10.0
            assert provider._transport.stall_seconds == 42.0
    finally:
        await registry.aclose()


async def test_build_derives_the_database_timeout_from_the_lease(tmp_path, monkeypatch):
    from keel import service

    seen = {}
    real = service.open_store

    async def spy(url, **kwargs):
        seen.update(kwargs)
        return await real(url)

    monkeypatch.setattr(service, "open_store", spy)
    settings = Settings(credentials=(ServiceCredential(1, "k"),),
                        database_url=f"sqlite:///{tmp_path / 'k.db'}", sandbox="none",
                        lease_seconds=60)
    keel, pool, closables, backend = await service.build(settings)
    try:
        assert seen == {"command_timeout": 20.0, "tenant_ids": (1,), "tenant_backfill": {}}
    finally:
        for item in closables:
            close = getattr(item, "aclose", None) or getattr(item, "close")
            await close()


async def test_start_spares_the_sandboxes_of_profiles_with_a_run_in_flight(tmp_path):
    from keel.store import open_store

    db = tmp_path / "keel.sqlite3"
    store = await open_store(f"sqlite:///{db}")
    await store.upsert_profile("p2", models=[{"provider": "fake", "model": "m"}], sandbox={}, soul="", skills=[])
    await store.db.execute("UPDATE profiles SET tenant_id = ? WHERE id = ?", 1, "p2")
    await store.ensure_session("p2", "s1")
    await store.create_run(profile_id="p2", session_id="s1", provider="fake", model="m", reasoning_effort=None,
                           input_parts=[{"type": "text", "text": "hi"}], instructions=None)
    await store.close()
    log = tmp_path / "docker.log"
    fake = tmp_path / "docker"
    fake.write_text('#!/bin/sh\necho "$@" >> "' + str(log) + '"\n'
                    'case "$1" in ps) printf "c1 p1\\nc2 p2\\n";; esac\nexit 0\n')
    fake.chmod(0o755)
    settings = Settings(credentials=(ServiceCredential(1, "k"),),
                        database_url=f"sqlite:///{db}", workers=1, docker=str(fake))
    providers = ProviderRegistry()
    providers.register(FakeProvider())
    app = create_service(settings, providers=providers)
    async with app.router.lifespan_context(app):
        pass
    assert "rm -f c1" in log.read_text().splitlines()


async def test_the_lifespan_closes_the_background_after_the_pool_runs(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from keel.background import Background

    settings = Settings(credentials=(ServiceCredential(1, "k"),),
                        database_url=f"sqlite:///{tmp_path / 'k.db'}", sandbox="none")
    app = create_service(settings)
    close = AsyncMock()
    monkeypatch.setattr(Background, "close", close)
    async with app.router.lifespan_context(app):
        assert isinstance(app.state.background, Background)
    close.assert_awaited_once()
