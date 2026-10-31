"""run_tool enforces the context's allowlist and each tool's declared requirements."""

import json

from keel import prompts
from keel.tools import ToolContext, run_tool
from keel.tools.base import NEEDS_SANDBOX


async def test_a_tool_outside_the_allowlist_is_unknown():
    outcome = await run_tool("terminal", '{"command": "ls"}', ToolContext(allowed=frozenset({"memory"})))
    assert outcome.error and json.loads(outcome.content)["error"] == "Unknown tool: terminal"


async def test_memory_and_session_search_need_no_sandbox_and_the_sandbox_tools_do():
    assert "memory" not in NEEDS_SANDBOX
    assert "session_search" not in NEEDS_SANDBOX
    assert set(prompts.TOOL_ORDER) - {"skills_list", "skill_view"} <= NEEDS_SANDBOX
    assert not {"skills_list", "skill_view", "skill_manage"} & NEEDS_SANDBOX


async def test_a_sandbox_tool_without_a_sandbox_is_refused_even_when_allowed():
    outcome = await run_tool("read_file", '{"path": "x"}', ToolContext(allowed=frozenset({"read_file"})))
    assert outcome.error and "needs a sandbox" in json.loads(outcome.content)["error"]


async def test_memory_without_a_store_answers_not_available():
    outcome = await run_tool("memory", '{"action": "list"}', ToolContext(allowed=frozenset({"memory"})))
    assert outcome.error and json.loads(outcome.content)["error"] == "Memory is not available."
