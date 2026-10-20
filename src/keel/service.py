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
from keel.sandbox.coordinator import SandboxCoordinator
from keel.sandbox.docker import DockerSandbox
from keel.store import open_store
from keel.worker import WorkerPool


log = logging.getLogger(__name__)
# Idle and recovery scans: every pass handles a bounded batch and skips profiles another instance has locked.
MAINTENANCE_SECONDS = 60.0
MAINTENANCE_BATCH = 100
# Opening sandbox admission (initialize, then classify the whole inventory) is bounded per attempt, including at
# startup, and retried at this interval while closed. Claims never wait for it: chat-only runs proceed meanwhile.
ADMISSION_TIMEOUT_SECONDS = 30.0
ADMISSION_RETRY_SECONDS = 15.0


def default_providers(settings: Settings) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(BedrockMantleProvider(settings.aws_region, stall=settings.stall_limits))
    if settings.openai_api_key:
        registry.register(OpenAIProvider(settings.openai_api_key, base_url=settings.openai_base_url,
                                        compat=settings.openai_compat,
                                        context_window=settings.openai_context_window,
                                        stall=settings.stall_limits))
    return registry


async def build(settings: Settings, *, providers: ProviderRegistry | None = None):
    store = await open_store(settings.database_url, command_timeout=settings.db_command_timeout,
                             tenant_ids=tuple(credential.tenant_id for credential in settings.credentials),
                             tenant_backfill=settings.tenant_backfill)
    bus = PostgresEventBus(settings.database_url) if settings.is_postgres else InMemoryEventBus()
    await bus.start()
    registry = providers or default_providers(settings)
    coordinator = None
    closables = [registry, bus, store]
    if settings.sandbox == "docker":
        backend = DockerSandbox(docker=settings.docker, deployment=settings.deployment,
                                mount_root=settings.sandbox_mount_root,
                                reap_seconds=settings.sandbox_reap_seconds)
        # One coordinator per instance, shared by the executor and the workers. Its leases (usage and lifecycle
        # operations) use the same KEEL_LEASE_SECONDS as the run claims; terminal transitions notify the shared bus.
        coordinator = SandboxCoordinator(
            store, backend, deployment=settings.deployment, owner=settings.instance_id,
            lease_seconds=settings.lease_seconds, idle_seconds=settings.sandbox_idle_seconds,
            reap_seconds=settings.sandbox_reap_seconds, bus=bus)
        closables.insert(0, backend)
    background = Background(limit=settings.background_limit)
    executor = RunExecutor(store, bus, registry, coordinator=coordinator, max_iterations=settings.max_iterations,
                          mount_root=settings.sandbox_mount_root, compression_threshold=settings.compression_threshold,
                          background=background, memory_chars=settings.memory_chars, user_chars=settings.user_chars,
                          memory_review_turns=settings.memory_review_turns)
    pool = WorkerPool(store, bus, executor, owner=settings.instance_id, workers=settings.workers,
                      lease_seconds=settings.lease_seconds, event_retention_s=settings.event_retention_days * 86_400.0,
                      coordinator=coordinator)
    keel = Keel(store=store, bus=bus, providers=registry, executor=executor, credentials=settings.credentials,
                max_running=settings.max_running, sandbox_mount_root=settings.sandbox_mount_root,
                background=background, coordinator=coordinator)
    return keel, pool, closables, coordinator


async def _maintenance_pass(coordinator: SandboxCoordinator, retention_seconds: float) -> None:
    """One bounded reconciliation, one bounded idle pass and one bounded evidence-pruning pass.

    Every pass handles at most `MAINTENANCE_BATCH` rows, and failures are logged, never raised. Reconciliation and
    idle reaping never delete by name, age or a profile list: every removal is an exact-ID decision taken under the
    coordination guard, and unknown or conflicting inventory only blocks the affected profile's admission. Pruning
    only deletes resolved rows older than the configured retention; the store never selects an unresolved attempt,
    an unreleased usage, an unresolved exec or a current reference.
    """
    for name, scan in (("reconciling", coordinator.reconcile_once), ("reaping idle", coordinator.reap_idle_once)):
        try:
            report = await scan(limit=MAINTENANCE_BATCH)
        except Exception:
            log.warning("%s sandboxes failed", name, exc_info=True)
            continue
        if report.blocked or report.removed or report.released or report.unresolved or report.diagnostics:
            oldest = "none" if report.oldest_age_s is None else f"{report.oldest_age_s:.1f}"
            log.info("%s sandboxes: removed=%d released=%d blocked=%d unresolved=%d oldest_age_s=%s "
                     "diagnostics=%s", name, report.removed, report.released, report.blocked, report.unresolved,
                     oldest, ",".join(sorted(set(report.diagnostics))))
    try:
        pruned = await coordinator.store.prune_sandbox_records(retention_seconds, limit=MAINTENANCE_BATCH)
    except Exception:
        log.warning("pruning sandbox records failed", exc_info=True)
    else:
        if pruned:
            log.info("pruned %d resolved sandbox records", pruned)


async def _open_admission(coordinator: SandboxCoordinator) -> bool:
    """One bounded attempt to open sandbox admission; progress made before a timeout is kept for the next one."""
    try:
        opened = await asyncio.wait_for(coordinator.open_admission(), ADMISSION_TIMEOUT_SECONDS)
    except TimeoutError:
        log.warning("opening sandbox admission timed out; sandbox runs stay queued and it is retried")
        return False
    except Exception:
        log.warning("opening sandbox admission failed; sandbox runs stay queued and it is retried", exc_info=True)
        return False
    if opened:
        log.info("sandbox admission is open")
    return opened


async def _maintain(coordinator: SandboxCoordinator, retention_seconds: float) -> None:
    while True:
        if not coordinator.admitting():
            await asyncio.sleep(ADMISSION_RETRY_SECONDS)
            await _open_admission(coordinator)
            continue
        await asyncio.sleep(MAINTENANCE_SECONDS)
        await _maintenance_pass(coordinator, retention_seconds)


def create_service(settings: Settings | None = None, *, providers: ProviderRegistry | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        keel, pool, closables, coordinator = await build(settings, providers=providers)
        maintenance = None
        app.state.keel = keel
        app.state.background = keel.background
        try:
            if coordinator is not None:
                # Before workers start, classify the whole inventory and ledger (bounded); never delete containers
                # based on a list of profiles with runs in flight. Until admission opens, sandbox runs stay queued
                # and chat-only runs proceed; the maintenance task keeps retrying.
                await _open_admission(coordinator)
            await pool.start()
            if coordinator is not None:
                maintenance = asyncio.create_task(
                    _maintain(coordinator, settings.event_retention_days * 86_400.0),
                    name="keel-sandbox-maintenance")
            yield
        finally:
            if maintenance is not None:
                maintenance.cancel()
                await asyncio.gather(maintenance, return_exceptions=True)
            await pool.close()
            await keel.background.close()
            for item in closables:
                close = getattr(item, "aclose", None) or getattr(item, "close")
                await close()

    return create_app(lifespan=lifespan)
