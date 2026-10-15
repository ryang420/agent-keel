"""OpenAI Responses API with bearer auth (OpenAI and compatible servers)."""

from __future__ import annotations

import dataclasses
import logging
from typing import AsyncIterator

import httpx

from keel.models import ModelEvent, ModelRequest
from keel.providers.base import DEFAULT_STALL_SECONDS, Capabilities, ModelError
from keel.providers.responses import ResponsesTransport

DEFAULT_BASE_URL = "https://api.openai.com/v1"

log = logging.getLogger(__name__)

# The window is a conservative default across models (override with KEEL_OPENAI_CONTEXT_WINDOW).
DEFAULT_CONTEXT_WINDOW = 128_000
OPENAI_CAPABILITIES = Capabilities(context_window=DEFAULT_CONTEXT_WINDOW, vision=True, reasoning_replay=True,
                                   parallel_tool_calls=True, prompt_cache="key")


class OpenAIProvider:
    def __init__(self, api_key: str, *, base_url: str = DEFAULT_BASE_URL, name: str = "openai",
                 client: httpx.AsyncClient | None = None, timeout: float = DEFAULT_STALL_SECONDS, compat: bool = False,
                 context_window: int = DEFAULT_CONTEXT_WINDOW) -> None:
        self.name = name
        self._key = api_key
        self._capabilities = dataclasses.replace(OPENAI_CAPABILITIES, context_window=context_window,
                                                 prompt_cache="none" if compat else "key")
        if base_url.lower().startswith("http://"):
            log.warning("KEEL_OPENAI_BASE_URL is plain http; the API key travels in clear text.")
        self._client = client or httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(timeout, connect=10.0),
        )
        self._transport = ResponsesTransport(self.name, base_url.rstrip("/") + "/responses", self._client,
                                           compat=compat)

    def capabilities(self, model: str) -> Capabilities:
        return self._capabilities

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        try:
            async for event in self._transport.stream(request):
                yield event
        except ModelError as exc:
            # A server may echo the bearer key in an error body; run errors are stored and returned.
            if exc.code == "auth":
                message = "The provider rejected the API key."
            else:
                message = str(exc).replace(self._key, "***") if self._key else str(exc)
            raise ModelError(exc.code, message, exc.retry_after) from None

    async def aclose(self) -> None:
        await self._client.aclose()
