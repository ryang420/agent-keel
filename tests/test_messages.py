import json

import httpx
import pytest

from keel.models import (
    Completed, ImagePart, Message, ModelRequest, ReasoningItem, ReasoningReady, TextDelta, TextPart, ToolCall,
    ToolCallReady,
)
from keel.providers.base import ModelError, StallLimits
from keel.providers.messages import (
    BINDING_BETA, MessagesTransport, error_from_http, parse_usage, to_messages,
)

URL = "https://model.test/anthropic/v1/messages"
MODEL = "anthropic.claude-opus-5-5"
TOOL = {"name": "terminal", "description": "Run a command", "parameters": {"type": "object"}}


def sse(*events: dict) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def transport_for(handler, **kwargs) -> tuple[MessagesTransport, list]:
    seen = []

    def record(request: httpx.Request):
        seen.append(request)
        return handler(request)

    return MessagesTransport("bedrock", URL, httpx.AsyncClient(transport=httpx.MockTransport(record)), **kwargs), seen


def request(**overrides) -> ModelRequest:
    base = dict(provider="bedrock", model=MODEL, system="SYSTEM", instructions="RUN",
                messages=(Message("user", (TextPart("hello"),)),), reasoning_effort="low", cache_scope="s1")
    base.update(overrides)
    return ModelRequest(**base)


async def collect(transport, req):
    return [event async for event in transport.stream(req)]


START = {"type": "message_start", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 1000,
                                                         "cache_creation_input_tokens": 200, "output_tokens": 1}}}
STOP = {"type": "message_stop"}


def delta(stop_reason="end_turn", output_tokens=30):
    return {"type": "message_delta", "delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": output_tokens}}


def text_block(index, *chunks):
    return [{"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
            *({"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": c}}
              for c in chunks),
            {"type": "content_block_stop", "index": index}]


def thinking_block(index, text, signature):
    return [{"type": "content_block_start", "index": index,
             "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
            {"type": "content_block_delta", "index": index, "delta": {"type": "thinking_delta", "thinking": text}},
            {"type": "content_block_delta", "index": index, "delta": {"type": "signature_delta",
                                                                      "signature": signature}},
            {"type": "content_block_stop", "index": index}]


def tool_block(index, call_id, name, *chunks):
    return [{"type": "content_block_start", "index": index,
             "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}}},
            *({"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta",
                                                                        "partial_json": c}} for c in chunks),
            {"type": "content_block_stop", "index": index}]


def test_the_body_caches_system_and_conversation_and_drops_rebound_thinking():
    transport, _ = transport_for(lambda r: httpx.Response(200))
    body = transport.build_body(request())
    assert body["model"] == MODEL and body["stream"] is True and body["max_tokens"] == 32_000
    assert body["system"] == [{"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}},
                              {"type": "text", "text": "RUN"}]
    assert body["messages"] == [{"role": "user", "content": [
        {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}]}]
    assert body["thinking"] == {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert body["output_config"] == {"effort": "low"}
    assert "tools" not in body and "tool_choice" not in body
    assert transport.build_body(request(instructions=None))["system"] == [
        {"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}]
    assert "system" not in transport.build_body(request(system="", instructions=None))
    assert transport.build_body(request(max_output_tokens=16))["max_tokens"] == 16


def test_effort_none_is_low_and_unset_effort_is_the_models_default():
    transport, _ = transport_for(lambda r: httpx.Response(200))
    assert transport.build_body(request(reasoning_effort="none"))["output_config"] == {"effort": "low"}
    assert "output_config" not in transport.build_body(request(reasoning_effort=None))
    assert transport.build_body(request(reasoning_effort="xhigh"))["output_config"] == {"effort": "xhigh"}


def test_claude_4_5_and_older_get_no_thinking_or_effort():
    transport, _ = transport_for(lambda r: httpx.Response(200))
    for model in ("anthropic.claude-haiku-4-5", "anthropic.claude-sonnet-4-5-20250929-v1:0"):
        body = transport.build_body(request(model=model))
        assert "thinking" not in body and "output_config" not in body
    assert "thinking" in transport.build_body(request(model="anthropic.claude-opus-4-6-v1"))


def test_tools_become_input_schemas():
    transport, _ = transport_for(lambda r: httpx.Response(200))
    body = transport.build_body(request(tools=(TOOL,)))
    assert body["tools"] == [{"name": "terminal", "description": "Run a command", "input_schema": {"type": "object"}}]
    assert "tool_choice" not in body


def test_tool_history_without_tools_declares_the_used_tools_and_forbids_calls():
    transport, _ = transport_for(lambda r: httpx.Response(200))
    messages = (Message("user", (TextPart("go"),)),
                Message("assistant", tool_calls=(ToolCall("toolu_1", "terminal", '{"cmd": "ls"}'),)),
                Message("tool", (TextPart("a.txt"),), tool_call_id="toolu_1", tool_name="terminal"),
                Message("user", (TextPart("Wrap up."),)))
    body = transport.build_body(request(messages=messages))
    assert body["tools"] == [{"name": "terminal", "description": "", "input_schema": {"type": "object"}}]
    assert body["tool_choice"] == {"type": "none"}


def test_messages_convert_images_tool_calls_and_merged_tool_results():
    messages = (
        Message("user", (TextPart("look"), ImagePart("image/png", "AAAA"))),
        Message("assistant", (TextPart("Checking."),), tool_calls=(ToolCall("toolu_1", "terminal", '{"cmd": "ls"}'),
                                                                   ToolCall("toolu_2", "terminal", "not json"))),
        Message("tool", (TextPart("a.txt"),), tool_call_id="toolu_1", tool_name="terminal"),
        Message("tool", (), tool_call_id="toolu_2", tool_name="terminal"),
        Message("assistant"),  # an empty turn is left out
    )
    assert to_messages(messages, "bedrock", MODEL) == [
        {"role": "user", "content": [
            {"type": "text", "text": "look"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "toolu_1", "name": "terminal", "input": {"cmd": "ls"}},
            {"type": "tool_use", "id": "toolu_2", "name": "terminal", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.txt"},
            {"type": "tool_result", "tool_use_id": "toolu_2", "content": "(no output)"}]},
    ]


def test_stored_layout_replays_the_original_blocks_only_to_its_own_model_and_calls():
    thinking = {"type": "thinking", "thinking": "", "signature": "sig"}
    layout = [thinking, {"type": "text", "chars": 4}, thinking, {"type": "text", "chars": 5},
              {"type": "tool_use", "id": "toolu_1"}]
    own = ReasoningItem("bedrock", MODEL, "anthropic_content", {"blocks": layout})
    other = ReasoningItem("bedrock", "anthropic.claude-sonnet-5", "anthropic_content", {"blocks": layout})
    call = ToolCall("toolu_1", "terminal", '{"cmd": "ls"}')
    tool_use = {"type": "tool_use", "id": "toolu_1", "name": "terminal", "input": {"cmd": "ls"}}

    def assistant(item, text="Hi. Look.", calls=(call,)):
        return to_messages((Message("assistant", (TextPart(text),), tool_calls=calls, reasoning=(item,)),),
                           "bedrock", MODEL)[0]["content"]

    assert assistant(own) == [thinking, {"type": "text", "text": "Hi. "}, thinking,
                              {"type": "text", "text": "Look."}, tool_use]
    rebuilt = [{"type": "text", "text": "Hi. Look."}, tool_use]
    assert assistant(other) == rebuilt
    # A message that no longer matches its layout (edited text or calls) is rebuilt without thinking.
    assert assistant(own, text="Hi. Looked.") == [{"type": "text", "text": "Hi. Looked."}, tool_use]
    assert assistant(own, calls=(ToolCall("toolu_9", "terminal", "{}"),))[-1]["id"] == "toolu_9"


def test_usage_counts_the_whole_prompt():
    usage = parse_usage({"input_tokens": 10, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 200},
                        {"output_tokens": 30})
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (
        1210, 30, 1000, 200)
    assert parse_usage(None, None).input_tokens is None


async def test_the_stream_yields_text_tool_calls_content_and_usage():
    events = [START, *thinking_block(0, "plan", "sig-1"), *text_block(1, "Let me ", "look."),
              *tool_block(2, "toolu_1", "terminal", '{"cmd":', ' "ls"}'), delta("tool_use"), STOP]
    transport, seen = transport_for(lambda r: httpx.Response(200, content=sse(*events)))
    out = await collect(transport, request(tools=(TOOL,)))
    assert [e.text for e in out if isinstance(e, TextDelta)] == ["Let me ", "look."]
    assert [e.call for e in out if isinstance(e, ToolCallReady)] == [ToolCall("toolu_1", "terminal",
                                                                              '{"cmd": "ls"}')]
    [ready] = [e for e in out if isinstance(e, ReasoningReady)]
    assert ready.item.kind == "anthropic_content" and ready.item.model == MODEL
    assert ready.item.payload["blocks"] == [
        {"type": "thinking", "thinking": "plan", "signature": "sig-1"},
        {"type": "text", "chars": len("Let me look.")},
        {"type": "tool_use", "id": "toolu_1"}]
    assert isinstance(out[-1], Completed) and out[-1].status == "completed"
    assert out[-1].usage.input_tokens == 1210 and out[-1].usage.output_tokens == 30
    sent = seen[0]
    assert str(sent.url) == URL
    assert sent.headers["anthropic-version"] == "2023-06-01" and sent.headers["anthropic-beta"] == BINDING_BETA


async def test_a_redacted_thinking_block_is_kept_for_replay():
    events = [START, {"type": "content_block_start", "index": 0,
                      "content_block": {"type": "redacted_thinking", "data": "opaque"}},
              {"type": "content_block_stop", "index": 0}, *text_block(1, "Hi"), delta(), STOP]
    transport, _ = transport_for(lambda r: httpx.Response(200, content=sse(*events)))
    out = await collect(transport, request())
    [ready] = [e for e in out if isinstance(e, ReasoningReady)]
    assert ready.item.payload["blocks"][0] == {"type": "redacted_thinking", "data": "opaque"}


async def test_max_tokens_is_incomplete_and_a_refusal_is_bad_request():
    transport, _ = transport_for(lambda r: httpx.Response(200, content=sse(START, *text_block(0, "x"),
                                                                           delta("max_tokens"), STOP)))
    assert (await collect(transport, request()))[-1].status == "incomplete"
    transport, _ = transport_for(lambda r: httpx.Response(200, content=sse(START, delta("refusal"), STOP)))
    with pytest.raises(ModelError) as caught:
        await collect(transport, request())
    assert caught.value.code == "bad_request"


async def test_a_stream_error_event_and_a_cut_stream_are_errors():
    error = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    transport, _ = transport_for(lambda r: httpx.Response(200, content=sse(START, error)))
    with pytest.raises(ModelError) as caught:
        await collect(transport, request())
    assert caught.value.code == "server_error"
    transport, _ = transport_for(lambda r: httpx.Response(200, content=sse(START, *text_block(0, "x"))))
    with pytest.raises(ModelError) as caught:
        await collect(transport, request())
    assert caught.value.code == "server_error"


@pytest.mark.parametrize("status,kind,message,code", [
    (400, "invalid_request_error", "prompt is too long: 250000 tokens > 200000 maximum", "context_overflow"),
    (400, "invalid_request_error", "messages.1.content.0: Invalid `signature` in `thinking` block.",
     "invalid_reasoning"),
    (400, "invalid_request_error", "max_tokens: Field required", "bad_request"),
    (403, "permission_error", "Your subscription to the model is being set up.", "auth"),
    (429, "rate_limit_error", "slow down", "rate_limited"),
    (529, "overloaded_error", "Overloaded", "server_error"),
])
def test_http_errors_map_to_model_errors(status, kind, message, code):
    body = json.dumps({"type": "error", "error": {"type": kind, "message": message}})
    error = error_from_http(status, body, "7" if status == 429 else None)
    assert error.code == code and message[:20] in str(error)
    if status == 429:
        assert error.retry_after == 7.0


async def test_an_http_error_response_raises():
    body = json.dumps({"type": "error", "error": {"type": "permission_error", "message": "no access"}})
    transport, _ = transport_for(lambda r: httpx.Response(403, content=body.encode()))
    with pytest.raises(ModelError) as caught:
        await collect(transport, request())
    assert caught.value.code == "auth"


async def test_pings_do_not_hold_off_the_stall_limit():
    async def stream_body():
        yield sse(START)
        while True:
            yield b"event: ping\ndata: {\"type\": \"ping\"}\n\n"
            import asyncio
            await asyncio.sleep(0.01)

    transport, _ = transport_for(lambda r: httpx.Response(200, content=stream_body()),
                                 stall=StallLimits(first_event=1.0, between_events=0.1, high_effort=1.0))
    with pytest.raises(ModelError) as caught:
        await collect(transport, request())
    assert caught.value.code == "timeout"
