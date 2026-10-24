# Claude models on Bedrock Mantle (Anthropic Messages API)

Status: implemented on `feat/bedrock-anthropic-provider`, unreleased.

## Goal

The `bedrock` provider serves only OpenAI-family models, over the Responses API at
`https://bedrock-mantle.<region>.api.aws/openai/v1/responses`. The same Mantle host also serves Anthropic's Claude
models over the Anthropic Messages API at `/anthropic/v1/messages`, signed with the same SigV4 credentials. A consumer
wants to offer those models without running a second provider configuration.

## Decision: one provider, routed by model id

`bedrock` stays one provider. A model id that starts with `anthropic.` goes to a new Messages transport
(`keel/providers/messages.py`); every other id keeps using the Responses transport. Routing by id means:

- a consumer lists `{"provider": "bedrock", "model": "anthropic.claude-…"}` in a profile, and nothing else changes in
  its configuration, credentials or region;
- `POST /v1/models/verify` works unchanged, because it calls the provider's own `stream`;
- reasoning items stay keyed by `(provider, model)`, so replay never crosses models or wire formats.

`capabilities(model)` depends on the id: Claude ids report a 200,000-token window (every Claude model Mantle lists
accepts at least that much; a larger window is a later, per-model change), vision, reasoning replay, parallel tool
calls and `prompt_cache="breakpoints"`.

## Request

- `system`: the session's system prompt as the first text block, with a `cache_control` breakpoint. It does not
  change between compactions (a keel invariant), so tools + system cache across runs. Per-run `instructions` follow
  as a second block, after the breakpoint.
- `messages`: user text and base64 images; an assistant turn as its stored Anthropic content (below) or, without
  it, text then `tool_use` blocks; consecutive tool messages merged into one user message of `tool_result` blocks.
  A second breakpoint goes on the last block of the last message, so the conversation so far is cached for the next
  call. Moving a `cache_control` marker does not change the conversation the model sees.
- `tools`: `name`, `description`, `input_schema`. When a call offers no tools but the history holds tool blocks (the
  final call at the iteration cap), the request still declares the tools named in the history, with minimal schemas,
  and `tool_choice: {"type": "none"}`. The Messages API rejects tool blocks with no tool definitions.
- `max_tokens`: `max_output_tokens` when set, else 32,000. The Messages API requires it.
- Thinking: Claude 4.6+ models take `thinking: {"type": "adaptive"}` and `output_config.effort` from
  `reasoning_effort`. These models cannot turn thinking off, so `none` is sent as `low`. Claude 4.5 and older ids
  get neither field, because they reject adaptive thinking and effort.

## Replay and preserved thinking

Current Claude models bind a thinking block's signature to the exact conversation before it: the system prompt, the
tools and every earlier message. keel changes that prefix on purpose when it compresses or compacts a session, and
when per-run instructions change. So every request sends
`thinking.block_binding.prefix_mismatch_behavior: "drop_block"`, under the beta
`anthropic-beta: thinking-binding-controls-2026-08-01`. A block whose prefix changed is dropped for that request
and the call goes on. It does not fail with a 400 on accounts where the check is enforced.

An assistant turn is stored as one `ReasoningItem(kind="anthropic_content")` whose payload is the response's
content blocks in order (thinking, text and `tool_use` interleaved as the model produced them). Replaying the exact
blocks keeps them byte-identical, which keeps thinking valid and the cache warm. The stored blocks are used only
when the item's provider and model match the request and its `tool_use` ids equal the message's tool calls.
Otherwise the turn is rebuilt from text and tool calls without thinking. Stripping reasoning (the loop's
`invalid_reasoning` retry, compression) falls back to the same rebuild. A signature error maps to
`invalid_reasoning`, so the loop retries once without reasoning.

## Stream

`message_start` (usage) → `content_block_start` / `_delta` / `_stop` → `message_delta` (stop reason, output
tokens) → `message_stop`. Text deltas stream as `TextDelta`. A `tool_use` block becomes `ToolCallReady` at its
stop. The whole content becomes one `ReasoningReady` before `Completed`. `max_tokens` is `incomplete`. A `refusal`
ends the call as `bad_request`, because the model declined and a retry would decline again. `ping` and other events
do not count as progress for the stall limits. Usage: `input_tokens` = uncached + cache read + cache write (the
whole prompt), with the cache read and write counts reported as such.

Errors: `overloaded_error`/`api_error`/5xx → `server_error`, `rate_limit_error`/429 → `rate_limited`,
`authentication_error`/`permission_error`/401/403 → `auth`, "prompt is too long" → `context_overflow`, a thinking
signature error → `invalid_reasoning`, other 4xx → `bad_request`.

## Verified live

Against `anthropic.claude-opus-5-5` on Mantle: the beta header and `block_binding` are accepted; a turn with a
thinking block and a tool call replays with `input_transformations` empty (kept) and the prefix read from cache; the
same history with new run instructions answers with `thinking_dropped` / `prefix_binding_mismatch` instead of a
400, and still reads tools + system from cache. Against `anthropic.claude-haiku-4-5`: tool call and replay, model
verification (text and image), and the final call with tool history and no tools offered.

## Not in scope

Larger context windows per model, server-side compaction, per-message effort, prompt-cache TTLs, `display`
summaries of thinking, and Claude models served only through `bedrock-runtime` inference profiles.
