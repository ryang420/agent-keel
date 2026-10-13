"""Assemble the service from settings: store, bus, providers, executor, workers, API."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI

from keel.api import Keel, create_app
from keel.background import Background
from keel.config import Settings
from keel.events import InMemoryEventBus, PostgresEventBus
from keel.providers.base import ProviderRegistry
from keel.providers.bedrock import BedrockMantleProvider
from keel.providers.openai import OpenAIProvider
from keel.runner import RunExecutor
from keel.sandbox.docker import DockerSandbox
from keel.store import open_store
from keel.worker import WorkerPool


log = logging.getLogger(__name__)
_REAP_SECONDS = 60.0


def default_providers(settings: Settings) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(BedrockMantleProvider(settings.aws_region))
    if settings.openai_api_key:
        registry.register(OpenAIProvider(settings.openai_api_key, base_url=settings.openai_base_url,
                                        compat=settings.openai_compat,
                                        context_window=settings.openai_context_window))
    return registry


async def build(settings: Settings, *, providers: ProviderRegistry | None = None):
    store = await open_store(settings.database_url, command_timeout=settings.db_command_timeout)
    bus = PostgresEventBus(settings.database_url) if settings.is_postgres else InMemoryEventBus()
    await bus.start()
    registry = providers or default_providers(settings)
    backend = None
    if settings.sandbox == "docker":
        backend = DockerSandbox(docker=settings.docker, deployment=settings.deployment,
                                idle_seconds=settings.sandbox_idle_seconds)
    executor = RunExecutor(store, bus, registry, sandbox=backend, max_iterations=settings.max_iterations,
                          mount_root=settings.sandbox_mount_root, compression_threshold=settings.compression_threshold)
    pool = WorkerPool(store, bus, executor, owner=settings.instance_id, workers=settings.workers,
                      lease_seconds=settings.lease_seconds, event_retention_s=settings.event_retention_days * 86_400.0)
    background = Background(limit=settings.background_limit)
    keel = Keel(store=store, bus=bus, providers=registry, executor=executor, api_key=settings.api_key,
                max_running=settings.max_running, sandbox_mount_root=settings.sandbox_mount_root,
                background=background)
    return keel, pool, [registry, bus, store], backend


async def _reap(backend: DockerSandbox) -> None:
    while True:
        await asyncio.sleep(_REAP_SECONDS)
        try:
            await backend.reap_idle()
        except Exception:
            log.warning("reaping idle sandboxes failed", exc_info=True)


def create_service(settings: Settings | None = None, *, providers: ProviderRegistry | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        keel, pool, closables, backend = await build(settings, providers=providers)
        reaper = None
        if backend is not None:
            try:
                removed = await backend.remove_orphans(await keel.store.in_flight_profiles())
                if removed:
                    log.info("removed %d sandbox containers left from an earlier start", removed)
            except Exception:
                log.warning("could not remove leftover sandboxes; continuing", exc_info=True)
        app.state.keel = keel
        app.state.background = keel.background
        try:
            await pool.start()
            if backend is not None:
                reaper = asyncio.create_task(_reap(backend), name="keel-sandbox-reaper")
            yield
        finally:
            await pool.close()
            await keel.background.close()
            if reaper is not None:
                reaper.cancel()
                await asyncio.gather(reaper, return_exceptions=True)
            for item in closables:
                close = getattr(item, "aclose", None) or getattr(item, "close")
                await close()

    return create_app(lifespan=lifespan)
