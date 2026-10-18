"""POST /v1/models/verify: two tiny calls decide whether a model id works and whether it reads images."""

import httpx
import pytest

from keel.api import Keel, create_app
from keel.config import ServiceCredential
from keel.events import InMemoryEventBus
from keel.models import Completed, ImagePart, TextDelta, Usage
from keel.providers.base import ModelError, ProviderRegistry
from keel.providers.fake import FakeProvider
from keel.verify import VerifyError, verify_model

OK = [TextDelta("OK"), Completed(Usage(5, 1, 0, 0))]


@pytest.fixture
async def api(store):
    provider = FakeProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    keel = Keel(store=store, bus=InMemoryEventBus(), providers=registry, executor=None,
                credentials=(ServiceCredential(1, "key-one"), ServiceCredential(2, "key-two")), max_running=1)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(keel)), base_url="http://keel")
    yield client, provider, store
    await client.aclose()


def auth(key="key-one"):
    return {"Authorization": f"Bearer {key}"}


async def test_a_model_that_answers_both_calls_reads_images(api):
    client, provider, _ = api
    provider.script = [OK, OK]
    response = await client.post("/v1/models/verify", json={"provider": "fake", "model": "m1"}, headers=auth())
    assert response.status_code == 200
    assert response.json() == {"supports_vision": True}
    text_call, image_call = provider.requests
    assert text_call.model == "m1" and text_call.max_output_tokens == 16
    assert not any(isinstance(p, ImagePart) for m in text_call.messages for p in m.parts)
    assert any(isinstance(p, ImagePart) for m in image_call.messages for p in m.parts)


async def test_an_image_refusal_means_no_vision(api):
    client, provider, _ = api
    provider.script = [OK, [ModelError("bad_request", "This model does not support image input.")]]
    response = await client.post("/v1/models/verify", json={"provider": "fake", "model": "m1"}, headers=auth())
    assert response.status_code == 200 and response.json() == {"supports_vision": False}


@pytest.mark.parametrize("error, status, code", [
    (ModelError("auth", "not authorized to invoke"), 422, "access_denied"),
    (ModelError("bad_request", "The model `m1` does not exist."), 422, "not_found"),
    (ModelError("bad_request", "Unsupported parameter: input"), 422, "unsupported"),
    (ModelError("server_error", "upstream 502"), 503, "unavailable"),
    (ModelError("rate_limited", "slow down"), 503, "unavailable"),
    (ModelError("timeout", "no answer"), 503, "unavailable"),
])
async def test_a_failed_text_call_is_classified(api, error, status, code):
    client, provider, _ = api
    provider.script = [[error]]
    response = await client.post("/v1/models/verify", json={"provider": "fake", "model": "m1"}, headers=auth())
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert len(provider.requests) == 1  # no image call after a failed text call


async def test_a_failed_image_call_for_another_reason_is_not_a_vision_answer(api):
    client, provider, _ = api
    provider.script = [OK, [ModelError("server_error", "upstream 502")]]
    response = await client.post("/v1/models/verify", json={"provider": "fake", "model": "m1"}, headers=auth())
    assert response.status_code == 503 and response.json()["error"]["code"] == "unavailable"


async def test_any_tenant_key_may_verify_and_none_is_refused(api):
    client, provider, _ = api
    provider.script = [OK, OK]
    assert (await client.post("/v1/models/verify", json={"provider": "fake", "model": "m1"},
                              headers=auth("key-two"))).status_code == 200
    bare = await client.post("/v1/models/verify", json={"provider": "fake", "model": "m1"})
    assert bare.status_code == 401


@pytest.mark.parametrize("body", [{}, {"provider": "fake"}, {"provider": "nope", "model": "m1"},
                                  {"provider": "fake", "model": ""}, {"provider": "fake", "model": "x" * 200}])
async def test_bad_input_is_400(api, body):
    client, _, _ = api
    response = await client.post("/v1/models/verify", json=body, headers=auth())
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_input"


async def test_verification_writes_nothing(api):
    client, provider, store = api
    provider.script = [OK, OK]
    await client.post("/v1/models/verify", json={"provider": "fake", "model": "m1"}, headers=auth())
    assert await store.get_profile("m1") is None
    assert await store.profile_sandboxes() == []


async def test_a_call_that_hangs_times_out_as_unavailable():
    provider = FakeProvider(script=[["hang"]])
    with pytest.raises(VerifyError) as caught:
        await verify_model(provider, "m1", timeout=0.05)
    assert caught.value.code == "unavailable"
