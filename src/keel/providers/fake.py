"""A scripted provider for tests and for running the service without a model."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

from keel.models import Completed, ModelEvent, ModelRequest, TextDelta, Usage
from keel.providers.base import Capabilities

DEFAULT_STEP = [TextDelta("ok"), Completed(Usage(10, 2, 0, 0))]


class FakeProvider:
    def __init__(self, name: str = "fake", script: list | None = None,
                 capabilities: Capabilities | None = None) -> None:
        self.name = name
        self.script = list(script or [])
        self.requests: list[ModelRequest] = []
        self._capabilities = capabilities or Capabilities(128_000, True, False, True, "none")

    def capabilities(self, model: str) -> Capabilities:
        return self._capabilities

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        self.requests.append(request)
        step = self.script.pop(0) if self.script else DEFAULT_STEP
        for item in step:
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, float):
                await asyncio.sleep(item)  # a pause of that many seconds
                continue
            if item == "hang":
                await asyncio.sleep(3600)
                continue
            yield item
