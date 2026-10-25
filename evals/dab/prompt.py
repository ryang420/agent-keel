"""Query instructions for ``dab-score-v1`` and the guard that keeps evaluator material out of them.

A prompt is exactly the query text, the dataset's description without hints, a list of sanctioned stores and fixed
boilerplate. Gold answers, ``validate.py``, the hint file and DAB's agent scaffold are never part of it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .suite import Case

ANSWER_MARKER = "ANSWER:"
BOILERPLATE = """You are completing a data-analysis task in a sandbox workspace.

The sandbox provides bash and Python 3.12 with the duckdb, sqlite3, psycopg (PostgreSQL) and pymongo (MongoDB)
libraries. Work only from the sanctioned stores listed below; they are the complete set of inputs for this task.

When you have finished, end your final message with exactly one line of this form and nothing after it:

ANSWER: <your answer>

The answer is plain text containing only the value, list or table the question asks for, with no commentary.
"""

# Markers that must never appear in an instruction: they identify evaluator-only material.
FORBIDDEN = ("ground_truth", "validate.py", "def validate(", "db_description_withhint", "HINTS:")


@dataclass(frozen=True)
class Prompt:
    """Instructions plus their hash and any detected leakage."""

    text: str
    sha256: str
    leaks: tuple[str, ...]

    def clean(self) -> bool:
        return not self.leaks


def question(case: Case, repo: Path) -> str:
    """Read the pinned question text (``query.json`` holds a JSON string)."""
    raw = json.loads((case.query_dir(repo) / "query.json").read_text(encoding="utf-8"))
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{case.case_id}: query.json must contain a non-empty string")
    return raw.strip()


def description(case: Case, repo: Path) -> str:
    """The sanitized dataset description (never ``db_description_withhint.txt``)."""
    return (case.dataset.directory(repo) / "db_description.txt").read_text(encoding="utf-8").strip()


def build(case: Case, repo: Path, stores: list[tuple[str, str]]) -> Prompt:
    """Build and inspect one case's instructions.

    ``stores`` pairs a logical database name with a one-line description of where the sandbox finds it. The caller
    builds it from the sanctioned policy, so the prompt cannot promise a store the sandbox does not have.
    """
    listed = "\n".join(f"- {name}: {where}" for name, where in stores)
    text = (
        f"{BOILERPLATE}\n"
        f"QUESTION\n{question(case, repo)}\n\n"
        f"DATABASE DESCRIPTION\n{description(case, repo)}\n\n"
        f"SANCTIONED STORES\n{listed}\n"
    )
    leaks = inspect(text, case, repo)
    return Prompt(text=text, sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), leaks=leaks)


def inspect(text: str, case: Case, repo: Path) -> tuple[str, ...]:
    """Return the evaluator-material leaks found in an instruction.

    Structural markers catch validator or hint text pasted into a prompt. The gold file is checked as content: a
    gold value appearing in an instruction would let a model pass without computing anything, whatever its source.
    """
    found = [marker for marker in FORBIDDEN if marker in text]
    gold = case.query_dir(repo) / "ground_truth.csv"
    if gold.is_file():
        for line in gold.read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if len(value) >= 4 and value.lower() in text.lower():
                found.append(f"ground_truth value {value!r}")
    hint = case.dataset.directory(repo) / "db_description_withhint.txt"
    if hint.is_file():
        hint_text = hint.read_text(encoding="utf-8").strip()
        if hint_text and hint_text.splitlines()[0] in text:
            found.append("hint description")
    return tuple(sorted(set(found)))
