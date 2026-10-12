"""Prompt texts and tool definitions, kept as files so they can be diffed against their source.

Adapted from Hermes (Nous Research hermes-agent, MIT, v2026.9.24 `f97608f178`): the guidance blocks of the
stable tier in agent/system_prompt.py, the api_server platform hint, the skills index header, the
iteration-cap request (agent/context_compressor.py) and the eight tool schemas as model_tools sends them.
Edits remove what keel does not have: background processes, a persistent kernel, skill authoring, document
extraction, other tools. See THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path

_DIR = Path(__file__).parent
TOOL_ORDER = ("terminal", "execute_code", "read_file", "write_file", "patch", "search_files", "skills_list",
              "skill_view")
MAX_ITERATIONS_REQUEST = (
    "You've reached the maximum number of tool-calling iterations allowed. Please provide a final response "
    "summarizing what you've found and accomplished so far, without calling any more tools."
)


@lru_cache(maxsize=None)
def text(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8").rstrip("\n")


@lru_cache(maxsize=None)
def tool_definitions() -> tuple[dict, ...]:
    return tuple(json.loads((_DIR / "tools" / f"{name}.json").read_text(encoding="utf-8")) for name in TOOL_ORDER)
