"""Evaluation-only executor seams. Product runtime code is unchanged."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import uuid

import httpx

from keel.api import create_app
from keel.models import Usage
from keel.runner import RunExecutor
from keel.service import build
from keel.tools.base import redact

from .transport import ExportError, export_submission


@dataclass
class Job:
    allowed: tuple[str, ...]
    script: bytes | None = None
    previous: dict[str, bytes] = field(default_factory=dict)
    files: dict[str, bytes] = field(default_factory=dict)
    exit_code: int | None = None
    error: str | None = None
    export_issues: list[dict] = field(default_factory=list)
    stdout: str = ''
    done: asyncio.Event = field(default_factory=asyncio.Event)


class ArtifactExecutor(RunExecutor):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.jobs: dict[str, Job] = {}

    async def _loop(self, run, state):
        job = self.jobs.get(run.profile_id)
        if job is None or job.script is None:
            return await super()._loop(run, state)
        state.sandbox = await self.coordinator.prepare(state.use)
        for name, data in {'solution.py': job.script, **job.previous}.items():
            if name not in {'solution.py', *job.allowed}:
                raise ValueError('unexpected carried state')
            await self.coordinator.write_file(state.sandbox, '/work/output/' + name, data)
        state.exec_id = 'verify-' + uuid.uuid4().hex
        result = await self.coordinator.exec(
            state.sandbox, ['python3', '/work/output/solution.py', '--input-dir', '/input',
                            '--output-dir', '/work/output'], cwd='/work', timeout_s=60, exec_id=state.exec_id)
        state.exec_id = None
        job.exit_code = result.exit_code
        job.stdout = redact(result.output)[:8000]
        await self._finish(run, 'completed', output='', usage=Usage(0, 0, 0, 0))

    async def _release(self, run, state):
        job = self.jobs.get(run.profile_id)
        try:
            if job is not None:
                if state.sandbox is None:
                    job.error = 'sandbox_not_prepared'
                else:
                    try:
                        job.files = await export_submission(self.coordinator, state.sandbox, job.allowed)
                    except ExportError as exc:
                        job.error = 'export_error:ExportError'
                        job.export_issues = exc.issues
                    except Exception as exc:
                        job.error = f'export_error:{type(exc).__name__}'
                        job.export_issues = [{'code': 'exporter_exception', 'type': type(exc).__name__}]
        finally:
            try:
                await super()._release(run, state)
            finally:
                if job is not None:
                    job.done.set()


@dataclass
class Runtime:
    client: httpx.AsyncClient
    executor: ArtifactExecutor
    keel: object


@asynccontextmanager
async def runtime(settings, providers):
    keel, pool, closables, coordinator = await build(settings, providers=providers)
    executor = ArtifactExecutor(
        keel.store, keel.bus, keel.providers, coordinator=coordinator, background=keel.background,
        max_iterations=settings.max_iterations, mount_root=settings.sandbox_mount_root,
        compression_threshold=settings.compression_threshold)
    keel.executor = pool.executor = executor
    started = False
    try:
        if coordinator is None or not await coordinator.open_admission():
            raise RuntimeError('evaluation sandbox admission failed')
        await pool.start()
        started = True
        app = create_app(keel)
        credential = settings.credentials[0]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://keel',
                                    headers={'Authorization': 'Bearer ' + credential.secret}) as client:
            yield Runtime(client, executor, keel)
    finally:
        if started:
            await pool.close()
        await keel.background.close()
        for item in closables:
            close = getattr(item, 'aclose', None) or getattr(item, 'close')
            await close()
