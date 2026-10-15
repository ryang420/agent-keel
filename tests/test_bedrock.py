import asyncio
import json
import os
import threading

import httpx
import pytest
import botocore.exceptions
from botocore.credentials import Credentials

from keel.models import Completed, Message, ModelRequest, TextDelta, TextPart
from keel.providers.base import ModelError
from keel.providers.bedrock import MANTLE_CAPABILITIES, BedrockMantleProvider, mantle_url
from keel.providers.sigv4 import SigV4Auth

REQUEST = ModelRequest("bedrock", "openai.gpt-6-luna", "You help.", (Message("user", (TextPart("hi"),)),),
                       reasoning_effort="low", cache_scope="s1")


def test_the_mantle_url_is_regional():
    assert mantle_url("us-west-2") == "https://bedrock-mantle.us-west-2.api.aws/openai/v1/responses"


async def test_requests_are_sigv4_signed_for_bedrock():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, content=b'event: response.completed\ndata: {"type": "response.completed", '
                                            b'"response": {"usage": {"input_tokens": 1, "output_tokens": 1}}}\n\n')

    auth = SigV4Auth("us-east-1", credentials=lambda: Credentials("AKIDEXAMPLE", "secret", "token"))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=auth)
    provider = BedrockMantleProvider("us-east-1", client=client)
    events = [e async for e in provider.stream(REQUEST)]
    await provider.aclose()
    assert isinstance(events[-1], Completed)
    headers = seen[0].headers
    assert headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/us-east-1/bedrock/aws4_request" in headers["authorization"]
    assert "x-amz-date" in headers and headers["x-amz-security-token"] == "token"
    assert json.loads(seen[0].content)["model"] == "openai.gpt-6-luna"
    assert str(seen[0].url) == mantle_url("us-east-1")


async def test_missing_credentials_are_an_auth_error():
    auth = SigV4Auth("us-east-1", credentials=lambda: None)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)), auth=auth)
    provider = BedrockMantleProvider("us-east-1", client=client)
    with pytest.raises(ModelError) as caught:
        [e async for e in provider.stream(REQUEST)]
    await provider.aclose()
    assert caught.value.code == "auth"


async def test_capabilities():
    provider = BedrockMantleProvider("us-east-1", client=httpx.AsyncClient())
    assert provider.name == "bedrock"
    assert provider.capabilities("openai.gpt-6-luna") == MANTLE_CAPABILITIES
    assert MANTLE_CAPABILITIES.context_window == 272_000 and MANTLE_CAPABILITIES.prompt_cache == "key"
    await provider.aclose()


async def test_botocore_errors_are_auth_errors():
    def broken():
        raise botocore.exceptions.NoCredentialsError()

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)),
                               auth=SigV4Auth("us-east-1", credentials=broken))
    provider = BedrockMantleProvider("us-east-1", client=client)
    with pytest.raises(ModelError) as caught:
        [e async for e in provider.stream(REQUEST)]
    await provider.aclose()
    assert caught.value.code == "auth"


async def test_credentials_resolve_off_the_event_loop():
    threads = []

    def lookup():
        threads.append(threading.get_ident())
        return Credentials("AKIDEXAMPLE", "secret", "token")

    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"")),
                               auth=SigV4Auth("us-east-1", credentials=lookup))
    provider = BedrockMantleProvider("us-east-1", client=client)
    try:
        [e async for e in provider.stream(REQUEST)]
    except ModelError:
        pass
    await provider.aclose()
    assert threads and threads[0] != threading.get_ident()


@pytest.mark.skipif(os.environ.get("KEEL_LIVE_BEDROCK") != "1", reason="set KEEL_LIVE_BEDROCK=1 to call Bedrock")
async def test_live_bedrock_mantle_answers():
    region = os.environ.get("AWS_REGION", "us-east-1")
    model = os.environ.get("KEEL_LIVE_MODEL", "openai.gpt-6-luna")
    provider = BedrockMantleProvider(region)
    request = ModelRequest("bedrock", model, "Reply tersely.", (Message("user", (TextPart("Reply with exactly: OK"),)),),
                           reasoning_effort="low", cache_scope="live")
    try:
        events = [e async for e in provider.stream(request)]
    finally:
        await provider.aclose()
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert "OK" in text and isinstance(events[-1], Completed) and events[-1].usage.input_tokens


async def test_the_default_provider_times_stalls_between_events():
    from keel.providers.base import StallLimits

    provider = BedrockMantleProvider("us-east-1")
    try:
        assert provider._client.timeout.read == 600.0
        assert provider._transport.stall == StallLimits()
    finally:
        await provider.aclose()
