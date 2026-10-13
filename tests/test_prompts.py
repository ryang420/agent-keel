import json
import re
from pathlib import Path

from keel import prompts

PROMPTS = Path(prompts.__file__).parent
FORBIDDEN = [r"[Hh]ermes", r"skill_manage", r"web_search", r"vision_analyze", r"background=true", r"\bpty\b",
             r"\bnotify\b", r"heartbeat", r"persistent session kernel"]


def test_the_eight_tools_load_in_order_with_their_schemas():
    tools = prompts.tool_definitions()
    assert tuple(t["name"] for t in tools) == prompts.TOOL_ORDER == (
        "terminal", "execute_code", "read_file", "write_file", "patch", "search_files", "skills_list", "skill_view")
    for tool in tools:
        assert set(tool) == {"name", "description", "parameters"}
        assert tool["description"].strip()
        assert tool["parameters"]["type"] == "object"
        assert set(tool["parameters"]["required"]) <= set(tool["parameters"]["properties"])


def test_tool_parameters_offer_only_what_keel_does():
    props = {t["name"]: set(t["parameters"]["properties"]) for t in prompts.tool_definitions()}
    assert props["terminal"] == {"command", "timeout", "workdir"}
    assert props["execute_code"] == {"code"}
    assert props["search_files"] == {"pattern", "target", "path", "file_glob", "limit", "offset", "output_mode",
                                     "context"}
    assert props["patch"] == {"path", "old_string", "new_string", "replace_all"}


def test_memory_is_not_among_the_default_tools():
    assert "memory" not in {tool["name"] for tool in prompts.tool_definitions()}
    memory = prompts.tool_definitions(("memory",))
    assert [tool["name"] for tool in memory] == ["memory"]
    assert set(memory[0]["parameters"]["properties"]) >= {"action", "target", "content", "old_text", "operations"}


def test_texts_mention_nothing_keel_lacks():
    files = list(PROMPTS.glob("*.md")) + list((PROMPTS / "tools").glob("*.json"))
    assert len(files) == 21
    for path in files:
        content = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN:
            assert not re.search(pattern, content), f"{path.name} mentions {pattern}"


def test_texts_load_without_trailing_newline_and_the_environment_takes_a_workdir():
    for name in ("guidance", "platform", "skills_header", "skills_footer", "environment"):
        assert prompts.text(name) == prompts.text(name).rstrip("\n") and prompts.text(name)
    assert "/w" in prompts.text("environment").format(workdir="/w")
    assert prompts.MAX_ITERATIONS_REQUEST.startswith("You've reached the maximum number of tool-calling iterations")


def test_tool_files_are_valid_json_named_after_their_tool():
    for path in (PROMPTS / "tools").glob("*.json"):
        assert json.loads(path.read_text())["name"] == path.stem
