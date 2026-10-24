# Providers

A provider turns a conversation into a stream of model events. A run names its provider and model; the profile's
`models` list says which pairs are allowed, and the service configuration decides which providers exist
(`provider_unavailable` otherwise).

## Bedrock Mantle (`bedrock`)

OpenAI-family models served by Amazon Bedrock through the OpenAI Responses API, at
`https://bedrock-mantle.<region>.api.aws/openai/v1/responses`. Requests are signed with SigV4 using the standard
AWS credential chain (for example `AWS_PROFILE`, environment keys or an instance role). The region is `AWS_REGION`
(default `us-east-1`). Model availability differs by region and account; use the model id your account exposes.

It reports a 272,000-token context window, vision, reasoning replay, parallel tool calls and prompt caching by key.
It is always registered.

### Claude models on Bedrock Mantle

A model id that starts with `anthropic.` (for example `anthropic.claude-opus-5-5`) is sent to the Anthropic
Messages API on the same host, `https://bedrock-mantle.<region>.api.aws/anthropic/v1/messages`, with the same
region and credentials. Nothing else needs to be configured: list `{"provider": "bedrock", "model": "anthropic.…"}`
in a profile. Which Claude ids Mantle serves differs by region and account; the signed, read-only
`GET https://bedrock-mantle.<region>.api.aws/v1/models` lists them. The first call to a model can start an AWS
Marketplace subscription, which answers `auth` ("Your subscription to the model is being set up") for a few
minutes.

- It reports a 200,000-token context window, vision, reasoning replay, parallel tool calls and prompt caching at
  breakpoints: one after the session's system prompt (per-run instructions follow it) and one on the newest
  message.
- Claude 4.6 and later get adaptive thinking, and `reasoning_effort` is sent as `output_config.effort`. These models
  cannot turn thinking off, so `none` is sent as `low`. Older ids get neither.
- Each assistant turn is replayed as the content blocks the model produced, thinking included, to the same model
  only. Requests set `thinking.block_binding.prefix_mismatch_behavior: "drop_block"`
  (`anthropic-beta: thinking-binding-controls-2026-08-01`), so thinking whose earlier conversation changed (after
  compression, compaction or new run instructions) is dropped for that request instead of failing it.
- `max_tokens` is the request's `max_output_tokens`, else 32,000. A `refusal` ends the call as `bad_request`.

## OpenAI-compatible (`openai`)

Any server that implements the Responses API (`POST <base>/responses`, streaming) with bearer-key auth. It is
registered when `KEEL_OPENAI_API_KEY` (or `OPENAI_API_KEY`) is set.

```bash
KEEL_API_KEY=dev KEEL_API_KEY_TENANT=1 KEEL_DATABASE_URL=sqlite:///keel.sqlite3 \
KEEL_OPENAI_API_KEY=<your-key> \
KEEL_OPENAI_BASE_URL=http://my-server:8000/v1 \
KEEL_OPENAI_COMPAT=1 \
KEEL_OPENAI_CONTEXT_WINDOW=32768 \
python -m keel
```

- `KEEL_OPENAI_BASE_URL` defaults to `https://api.openai.com/v1`.
- `KEEL_OPENAI_COMPAT=1` is for strict servers (vLLM, LiteLLM, Ollama): requests omit `include`,
  `prompt_cache_key` and `reasoning.summary`, and the provider reports no prompt cache.
- `KEEL_OPENAI_CONTEXT_WINDOW` (default 128,000) is what the provider reports; it does not change the server.
- It reports vision, reasoning replay and parallel tool calls for every model, so a server or model without
  them can fail at the server.
- Error messages are scrubbed of the key before they are stored or returned.

Except for Claude models, both providers share one wire implementation (`keel/providers/responses.py`): streaming SSE, tool calls assembled
from completed output items, encrypted reasoning returned and replayed only to the provider and model that produced
it, and `store: false`.

## Adding a provider

A provider is any object with a `name`, a `capabilities(model)` method and a `stream(request)` method
(`keel/providers/base.py`):

```python
class ModelProvider(Protocol):
    name: str
    def capabilities(self, model: str) -> Capabilities: ...
    def stream(self, request: ModelRequest) -> AsyncIterator[ModelEvent]: ...
```

- `Capabilities(context_window, vision, reasoning_replay, parallel_tool_calls, prompt_cache)`. `vision` decides
  whether image input is accepted (`model_not_vision_capable` otherwise). `prompt_cache` is `"key"`,
  `"breakpoints"` or `"none"`.
- `ModelRequest` carries `provider`, `model`, `system`, the neutral `messages` (`keel/models.py`), per-run
  `instructions`, `tools` (name, description, JSON-schema `parameters`), `reasoning_effort`, `cache_scope` (the
  session id) and `max_output_tokens`.
- `stream` yields `TextDelta`, `ToolCallReady`, `ReasoningReady` and finally `Completed(usage, status)`.
  Reasoning items are opaque and carry the provider and model that produced them; replay them only to the same
  pair.
- Raise only `ModelError(code, message, retry_after=None)` from a provider, with a code from
  `auth`, `rate_limited`, `server_error`, `timeout`, `context_overflow`, `invalid_reasoning`, `bad_request`.
  The loop retries `rate_limited`, `server_error` and `timeout` (up to 3 attempts, only while nothing was
  yielded), and retries `invalid_reasoning` once with reasoning stripped.
- An optional `aclose()` is called at shutdown.

Register it where the service builds its providers (`default_providers` in `keel/service.py`), or pass your own
`ProviderRegistry` to `create_service(settings, providers=…)`. `keel.providers.fake.FakeProvider` is a scripted
provider for tests.
