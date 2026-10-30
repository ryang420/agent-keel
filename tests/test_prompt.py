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
        "Current date: Monday, October 12, 2026 (UTC, UTC+00:00)\nModel: m1\nProvider: bedrock",
        prompts.text("environment").format(workdir="/work"),
    ])


def test_no_skills_means_no_skills_block():
    prompt = build_system_prompt(soul="", model="m", provider="p", now=NOW, tools=True, workdir="/w")
    assert prompt.startswith(DEFAULT_IDENTITY + "\n\n")
    assert "<available_skills>" not in prompt and prompts.text("skills_header") not in prompt


def test_memory_guidance_follows_guidance_and_the_snapshot_follows_the_date():
    prompt = build_system_prompt(soul="SOUL", model="m1", provider="bedrock", now=NOW, tools=True,
                                 memory=True, memory_snapshot="SNAPSHOT", workdir="/work")
    guidance, memory = prompts.text("guidance"), prompts.text("memory")
    date = "Current date: Monday, October 12, 2026 (UTC, UTC+00:00)\nModel: m1\nProvider: bedrock"
    assert prompt.index(memory) == prompt.index(guidance) + len(guidance) + 2
    assert prompt.index("SNAPSHOT") == prompt.index(date) + len(date) + 2
    plain = build_system_prompt(soul="SOUL", model="m1", provider="bedrock", now=NOW, tools=True, workdir="/work")
    assert memory not in plain and "SNAPSHOT" not in plain


def test_history_guidance_follows_the_memory_guidance():
    from datetime import UTC, datetime
    from keel import prompts
    from keel.prompt import build_system_prompt

    text = build_system_prompt(soul="", model="m", provider="fake", now=datetime(2026, 10, 30, tzinfo=UTC),
                               tools=True, memory=True, history=True)
    assert text.index(prompts.text("memory")) < text.index(prompts.text("history")) < text.index(
        prompts.text("platform"))
    assert prompts.text("history") not in build_system_prompt(
        soul="", model="m", provider="fake", now=datetime(2026, 10, 30, tzinfo=UTC), tools=True, memory=True)
