# End-to-End Agent Tracing and Telemetry

- **Status:** Deferred — not scheduled for implementation
- **Recorded:** 2026-11-10
- **Design:** Not yet defined

## Motivation

The final output of an agent run is insufficient to diagnose unexpected behavior. Operators need a structured
record of the execution path: what the model received, what it generated, which tools it called, what those
tools returned, and where time, retries, and tokens were spent.

Provide end-to-end tracing through related spans so that a run can be inspected step by step, including failed
and interrupted work.

## Current capabilities

Keel already persists run lifecycle events, streamed text, conversation messages, tool calls and their arguments,
and tool results. Tool completion events include duration and error information. Runs retain aggregated token
counts, and provider retries appear in service logs.

These records do not yet form a complete trace:

- Events have a run ID and sequence number, but no span identity or parent-child hierarchy.
- Individual model requests are not saved as complete snapshots of the actual input sent to the provider.
- Model-call timing, retry attempts, and token usage are not consistently recorded per call.
- Tool results may be truncated; exception stacks are logged rather than persisted as associated span data.
- Token counts are available, but monetary cost estimates are not.
- Run events are pruned after a configurable retention period, currently seven days by default.

Relevant implementation: [runner](../../src/keel/runner.py), [event records](../../src/keel/store/base.py),
[retry handling](../../src/keel/providers/retry.py), [usage model](../../src/keel/models.py), and
[tool execution](../../src/keel/tools/__init__.py).

## Proposed scope

### Execution structure

- Represent a run and its model calls, tool executions, and context compaction steps with related spans.
- Record trace and span identities, parent relationships, ordering, timestamps, duration, status, and errors.
- Cover auxiliary model calls such as title generation and compaction summaries. Define how background memory
  reviews and learning jobs link to their originating work without assuming that each job belongs to one run.
- Preserve evidence of cancellation, timeout, and incomplete execution.

### Inputs, outputs, and failures

- Capture each model call's actual input after context preparation, including system prompt, instructions,
  messages, tool definitions, model identity, and relevant generation settings.
- Capture generated text and tool calls with their names, IDs, and arguments. Decide during design whether
  provider-native request and response payloads are also needed.
- Record tool results and structured errors, including exception stacks where available.
- Explicitly identify redacted, omitted, or truncated content; do not present partial records as complete.
- Record state transitions and context changes needed to explain subsequent model behavior.

### Timing, retries, and cost

- Record model-call and tool-execution durations independently of overall run duration.
- Record every retry attempt, its reason, outcome, and backoff delay, including request changes made for recovery.
- Record input, output, cache-read, and cache-write token counts per model call, with aggregate totals.
- Preserve unknown usage as unknown rather than zero, including failed attempts without reported usage.
- Provide cost estimates with currency, pricing source or version, and calculation basis. Distinguish estimated
  costs from provider-billed amounts and avoid double-counting cached input tokens.

### Access and operation

- Support retrieving or exporting a trace by run ID and following related background work.
- Define content-capture controls, redaction, size limits, retention, and deletion behavior.
- Enforce tenant ownership and authorized access to captured prompts, results, and errors.
- Keep tracing overhead bounded. Trace collection or export failures must not cause agent execution to fail.

## Acceptance criteria

- A run with multiple model calls, tool executions, and retries can be inspected as an ordered hierarchy with
  inputs, outputs, timing, outcomes, and failure causes associated with the correct steps.
- With content capture enabled, each model call's actual input remains inspectable after later context
  compaction; configured redactions or omissions are explicit.
- Cancellation, timeout, or worker termination leaves unfinished spans identifiable rather than reporting
  them as successful or silently dropping them.
- Per-call token counts reconcile with aggregate totals for the same scope. Missing usage and incomplete cost
  estimates remain visible.
- A consumer can retrieve or export a trace without manually joining ordinary service logs and conversation
  records. Cross-tenant access is rejected.
- Retention and deletion policies cover trace content, and tracing failures do not change the run's outcome.

## Open design questions

- Should Keel use OpenTelemetry, an internal span model, or an internal model with an OpenTelemetry exporter?
- Where should trace metadata and potentially large content payloads be stored?
- What content should be captured by default, and what sampling, redaction, and size limits are appropriate?
- How should trace retention relate to run-event retention and profile deletion?
- What query or export interface is needed first: API, CLI, or a visual trace viewer?
- How should pricing be maintained across providers, models, and cache billing rules?
- How should traces relate to durable lifecycle records when workers crash or ownership changes?

These questions should be resolved in a separate design document before implementation.
