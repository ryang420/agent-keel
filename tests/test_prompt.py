from datetime import UTC, datetime

from keel import prompts
from keel.prompt import DEFAULT_IDENTITY, build_system_prompt
from keel.skills import SkillInfo, render_index

NOW = datetime(2026, 10, 12, 9, 30, tzinfo=UTC)


def test_a_tool_less_session_is_just_its_identity():
    assert build_system_prompt(soul="", model="m", provider="p", now=NOW, tools=False) == DEFAULT_IDENTITY
    assert build_system_prompt(soul="  Be kind.\n", model="m", provider="p", now=NOW, tools=False) == "Be kind."


def test_the_tiers_come_in_hermes_order():
    skills = [SkillInfo("s", "d", None, "/x/s")]
    prompt = build_system_prompt(soul="SOUL", model="m1", provider="bedrock", now=NOW, tools=True, skills=skills,
                                 workdir="/work")
    assert prompt == "\n\n".join([
        "SOUL", prompts.text("guidance"), prompts.text("platform"),
        prompts.text("skills_header"), render_index(skills), prompts.text("skills_footer"),
        "Conversation started: Monday, October 12, 2026 (UTC, UTC+00:00)\nModel: m1\nProvider: bedrock",
        prompts.text("environment").format(workdir="/work"),
    ])


def test_no_skills_means_no_skills_block():
    prompt = build_system_prompt(soul="", model="m", provider="p", now=NOW, tools=True, workdir="/w")
    assert prompt.startswith(DEFAULT_IDENTITY + "\n\n")
    assert "<available_skills>" not in prompt and prompts.text("skills_header") not in prompt
