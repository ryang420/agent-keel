"""Bedrock Mantle: OpenAI-family models on Bedrock, served through the OpenAI Responses API."""

from __future__ import annotations

from typing import AsyncIterator

import httpx

from keel.models import ModelEvent, ModelRequest
from keel.providers.base import DEFAULT_STALL_SECONDS, Capabilities
from keel.providers.responses import ResponsesTransport
from keel.providers.sigv4 import SigV4Auth

# OpenAI-family models on Mantle have a 272k window, takes images, needs its encrypted
# reasoning replayed, and caches by prompt_cache_key.
MANTLE_CAPABILITIES = Capabilities(context_window=272_000, vision=True, reasoning_replay=True,
                                   parallel_tool_calls=True, prompt_cache="key")


def mantle_url(region: str) -> str:
    return f"https://bedrock-mantle.{region}.api.aws/openai/v1/responses"


class BedrockMantleProvider:
    name = "bedrock"

    def __init__(self, region: str, *, client: httpx.AsyncClient | None = None, credentials=None,
                 timeout: float = DEFAULT_STALL_SECONDS) -> None:
        self.region = region
        self._client = client or httpx.AsyncClient(
            auth=SigV4Auth(region, credentials=credentials),
            timeout=httpx.Timeout(timeout, connect=10.0),
        )
        self._transport = ResponsesTransport(self.name, mantle_url(region), self._client)

    def capabilities(self, model: str) -> Capabilities:
        return MANTLE_CAPABILITIES

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        return self._transport.stream(request)

    async def aclose(self) -> None:
        await self._client.aclose()
