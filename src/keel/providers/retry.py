"""Retry policy shared by every provider. Backoff numbers follow Hermes's agent/retry_utils.py."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import random
from typing import AsyncIterator, Awaitable, Callable

from keel.models import ModelEvent, ModelRequest
from keel.providers.base import ModelError, ModelProvider

RETRYABLE = {"rate_limited", "server_error", "timeout"}
_BASE_SECONDS, _MAX_SECONDS, _MAX_RETRY_AFTER = 5.0, 120.0, 600.0


def backoff_seconds(attempt: int, retry_after: float | None, jitter: float) -> float:
    if retry_after:
        return max(0.0, min(retry_after, _MAX_RETRY_AFTER))
    base = min(_BASE_SECONDS * 2 ** (attempt - 1), _MAX_SECONDS)
    return base + base * 0.5 * jitter


def strip_reasoning(request: ModelRequest) -> ModelRequest:
    return replace(request, messages=tuple(replace(m, reasoning=()) for m in request.messages))


async def stream_with_retries(
    provider: ModelProvider,
    request: ModelRequest,
    *,
    max_attempts: int = 3,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: Callable[[], float] = random.random,
) -> AsyncIterator[ModelEvent]:
    """Retry a call only while it has produced nothing: output already forwarded cannot be taken back."""
    stripped = False
    attempt = 0
    while True:
        attempt += 1
        emitted = False
        try:
            async for event in provider.stream(request):
                emitted = True
                yield event
            return
        except ModelError as error:
            if emitted:
                raise
            if error.code == "invalid_reasoning" and not stripped:
                request, stripped = strip_reasoning(request), True
                continue
            if error.code not in RETRYABLE or attempt >= max_attempts:
                raise
            await sleep(backoff_seconds(attempt, error.retry_after, rng()))
