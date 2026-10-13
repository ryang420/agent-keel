import asyncio

import httpx

from keel.config import Settings
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.service import create_service


async def test_the_service_starts_runs_and_stops(tmp_path):
    settings = Settings(api_key="k", database_url=f"sqlite:///{tmp_path / 'keel.sqlite3'}", workers=1)
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

    settings = Settings(api_key="k", database_url=f"sqlite:///{tmp_path / 'k.db'}", sandbox="none",
                        compression_threshold=0.5)
    keel, pool, closables, backend = await build(settings)
    try:
        assert backend is None and keel.executor.sandbox is None
        assert keel.executor.compactor.threshold == 0.5
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
    settings = Settings(api_key="k", database_url=f"sqlite:///{tmp_path / 'k.db'}", sandbox="none")
    app = service.create_service(settings)
    with pytest.raises(RuntimeError, match="no start"):
        async with app.router.lifespan_context(app):
            pass
    assert closed == [True]


async def test_openai_is_registered_only_when_a_key_is_set(tmp_path):
    from keel.service import default_providers

    base = {"api_key": "k", "database_url": f"sqlite:///{tmp_path / 'k.db'}"}
    without = default_providers(Settings(**base))
    with_key = default_providers(Settings(**base, openai_api_key="sk-x"))
    try:
        assert without.names() == ["bedrock"]
        assert with_key.names() == ["bedrock", "openai"]
    finally:
        await without.aclose()
        await with_key.aclose()


async def test_build_derives_the_database_timeout_from_the_lease(tmp_path, monkeypatch):
    from keel import service

    seen = {}
    real = service.open_store

    async def spy(url, **kwargs):
        seen.update(kwargs)
        return await real(url)

    monkeypatch.setattr(service, "open_store", spy)
    settings = Settings(api_key="k", database_url=f"sqlite:///{tmp_path / 'k.db'}", sandbox="none",
                        lease_seconds=60)
    keel, pool, closables, backend = await service.build(settings)
    try:
        assert seen == {"command_timeout": 20.0}
    finally:
        for item in closables:
            close = getattr(item, "aclose", None) or getattr(item, "close")
            await close()
