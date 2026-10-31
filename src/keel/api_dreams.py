"""Dream inspection (procedural learning design §7): a bounded listing of the profile's dream jobs and an idempotent
retry of a failed one, as for memory reviews. Never transcript text."""

from __future__ import annotations

from fastapi import FastAPI, Request

from keel.api import ApiError
from keel.store.base import DreamBusy, DreamNotRetryable
from keel.store.dreams import DREAM_STATUSES


def dream_json(job) -> dict:
    return {"id": job.id, "status": job.status, "trigger": job.trigger, "attempts": job.attempts,
            "error_code": job.error_code, "error": job.error, "provider": job.provider, "model": job.model,
            "usage": job.usage, "changes": job.changes, "created_at": job.created_at, "updated_at": job.updated_at,
            "finished_at": job.finished_at}


def add_dream_routes(app: FastAPI, k, profile_or_404) -> None:
    """Mount the dream routes. `k` returns the Keel of a request; `profile_or_404` loads the caller's profile."""

    @app.get("/v1/profiles/{profile_id}/dreams")
    async def list_dreams(request: Request, profile_id: str, status: str | None = None, limit: int = 20):
        """List the profile's dreams, newest first: trigger, route, status, errors, usage and changes made."""
        await profile_or_404(request, profile_id)
        if status is not None and status not in DREAM_STATUSES:
            raise ApiError(400, "invalid_input", f"status must be one of {', '.join(DREAM_STATUSES)}.")
        if not 1 <= limit <= 100:
            raise ApiError(400, "invalid_input", "limit must be between 1 and 100.")
        jobs = await k(request).store.list_dreams(profile_id, status=status, limit=limit)
        return {"dreams": [dream_json(job) for job in jobs]}

    @app.post("/v1/profiles/{profile_id}/dreams/{dream_id}/retry")
    async def retry_dream(request: Request, profile_id: str, dream_id: str):
        """Requeue a failed dream with the profile's current route. Repeating it, or retrying a pending or running
        one, starts nothing new."""
        await profile_or_404(request, profile_id)
        try:
            job = await k(request).store.retry_dream(profile_id, dream_id)
        except DreamNotRetryable:
            raise ApiError(409, "dream_not_retryable", "Only a failed dream can be retried.") from None
        except DreamBusy:
            raise ApiError(409, "dream_busy", "Another dream of this profile is queued or running.") from None
        if job is None:
            raise ApiError(404, "dream_not_found", "No such dream.")
        return dream_json(job)
