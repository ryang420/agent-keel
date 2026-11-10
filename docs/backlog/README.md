# Backlog

This directory records proposed improvements that are not yet scheduled for implementation. A backlog entry is
not an approved design or an implementation commitment.

## Entries

| Item | Status | Purpose |
| --- | --- | --- |
| [End-to-End Agent Tracing and Telemetry](agent-tracing.md) | Deferred | Inspect the complete execution path of an agent run. |

## Organization

- Keep one feature or problem per Markdown file, using a descriptive kebab-case filename.
- Add each entry to the table above. Include its status, motivation, current capabilities, proposed scope,
  acceptance criteria, and open questions.
- Use `Proposed`, `Deferred`, `In design`, `In progress`, or `Completed` to describe status. Record scheduling
  or priority only when agreed.
- When an item moves into design, create a dated document in `../design/` and link it from the backlog entry.
  Keep detailed implementation plans outside this directory, following the repository's conventions.
- When completed, update the entry and index, linking to the resulting design or implementation reference.
