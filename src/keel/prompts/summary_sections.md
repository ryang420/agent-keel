## Historical Task Snapshot
[THE SINGLE MOST IMPORTANT FIELD. Capture the user's most recent unfulfilled
input verbatim — the exact words they used. This includes:
- Explicit task assignments ("<specific user task>")
- Questions awaiting an answer ("<specific user question>")
- Decisions awaiting input ("<option A or B?>")
- Ongoing discussions where the assistant owes the next substantive reply
A conversation where the user just asked a question IS an active task — the
task is "answer that question with full context". Do NOT write "None" merely
because the user did not issue an imperative command; reserve "None" for the
rare case where the last exchange was fully resolved and the user said
something like "thanks, that's all".
If multiple items are outstanding, list only the ones NOT yet completed.
This historical snapshot must identify the latest unresolved user input precisely. Examples:
"User asked: '<exact latest user request>'"
"User asked: '<exact latest user question>' — needs investigation + answer"
"User chose <option>; awaiting implementation of <specific next step>"
If the user's most recent message was a reverse signal (stop, undo, roll
back, never mind, just verify, change of topic) that supersedes earlier
work, write the reverse signal verbatim and DO NOT carry forward the
cancelled task. Example: "User asked: '<exact reverse signal>' — earlier
in-flight work is cancelled."
If no outstanding task exists, write "None."]

## Goal
[What the user is trying to accomplish overall]

## Constraints & Preferences
[User preferences, coding style, constraints, important decisions. Any security or safety constraint the user stated (files/data to avoid, operations that must not be performed, credential-handling rules) MUST be quoted VERBATIM here so it continues to apply after compaction — never paraphrase those.]

## Completed Actions
[Numbered list of concrete actions taken — include tool used, target, and outcome.
Format each as: N. ACTION target — outcome [tool: name]
Example:
1. READ config.py:45 — found `==` should be `!=` [tool: read_file]
2. PATCH config.py:45 — changed `==` to `!=` [tool: patch]
3. TEST `pytest tests/` — 3/50 failed: test_parse, test_validate, test_edge [tool: terminal]
Be specific with file paths, commands, line numbers, and results.]

## Active State
[Current working state — include:
- Working directory and branch (if applicable)
- Modified/created files with brief note on each
- Test status (X/Y passing)
- Any running processes or servers
- Environment details that matter]

## Blocked
[Any blockers, errors, or issues not yet resolved. Include exact error messages.]

## Key Decisions
[Important technical decisions and WHY they were made]

## Errors & Fixes
[Errors hit during the compacted turns and how each was resolved — include the
exact error text. Pay special attention to corrections the USER gave; quote
the user's correction and record what changed as a result.]

## Resolved Questions
[Questions the user asked that were ALREADY answered — include the answer so it is not repeated]

## Relevant Files
[Files read, modified, or created — with brief note on each]

## Critical Context
[Any specific values, error messages, configuration details, or data that would be lost without explicit preservation. NEVER include API keys, tokens, passwords, or credentials — write [REDACTED] instead.]

Target ~{target} tokens. Be CONCRETE — include file paths, command outputs, error messages, line numbers, and specific values. Avoid vague descriptions like "made some changes" — say exactly what changed.

TEMPORAL ANCHORING: The current date is {today}. When an action has already been carried out, phrase it as a completed, dated, past-tense fact rather than an open instruction. For example, rewrite "email John about the proposal" as "Sent the proposal email to John on {today}." Never leave a finished action worded as if it still needs doing, and never invent a date for work that has not happened yet.

Write only the summary body. Do not include any preamble or prefix.
