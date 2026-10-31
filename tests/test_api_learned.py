"""Learned skills over the API (procedural learning design §2, §6)."""

import httpx
import pytest

from keel.api import Keel, create_app
from keel.config import ServiceCredential
from keel.events import InMemoryEventBus
from keel.providers.base import ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.runner import RunExecutor

PROFILE = {"models": [{"provider": "fake", "model": "m"}], "sandbox": {}, "soul": "", "skills": [], "memory": True}


@pytest.fixture
async def client(store):
    registry = ProviderRegistry()
    registry.register(FakeProvider())
    bus = InMemoryEventBus()
    keel = Keel(store=store, bus=bus, providers=registry, executor=RunExecutor(store, bus, registry),
                credentials=(ServiceCredential(1, "key-one"), ServiceCredential(2, "key-two")), max_running=1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(keel)), base_url="http://keel",
                                 headers={"Authorization": "Bearer key-one"}) as http:
        assert (await http.put("/v1/profiles/p1", json=PROFILE)).status_code == 200
        yield http, store


async def test_learning_requires_memory_and_is_returned(client):
    http, store = client
    bad = await http.put("/v1/profiles/p2", json={**PROFILE, "memory": False, "learning": True})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_input"
    assert (await http.put("/v1/profiles/p2", json={**PROFILE, "learning": "yes"})).status_code == 400
    ok = await http.put("/v1/profiles/p2", json={**PROFILE, "learning": True})
    assert ok.status_code == 200 and ok.json()["learning"] is True
    assert (await store.get_profile("p2")).learning is True
    off = await http.put("/v1/profiles/p2", json=PROFILE)  # omitted means false, as for memory
    assert off.json()["learning"] is False
