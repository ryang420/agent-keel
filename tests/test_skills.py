import json
from pathlib import Path

import pytest

from keel.skills import SkillInfo, discover_skills, parse_front_matter, render_index
from keel.tools import run_tool
from keel.tools.base import ToolContext

CHART = "---\nname: making-charts\ndescription: Use whenever the user asks for a chart, graph, plot or visual of data\n---\n# Charts\nBody.\n"
FOLDED = "---\nname: fixing-builds\ndescription: >\n  Use when a build failed\n  or stopped.\n---\nSteps.\n"


def write_skills(root: Path) -> None:
    (root / "data" / "making-charts").mkdir(parents=True)
    (root / "data" / "making-charts" / "SKILL.md").write_text(CHART)
    (root / "devops" / "fixing-builds" / "scripts").mkdir(parents=True)
    (root / "devops" / "fixing-builds" / "SKILL.md").write_text(FOLDED)
    (root / "devops" / "fixing-builds" / "scripts" / "run.py").write_text("print('x')\n")
    (root / "solo").mkdir()
    (root / "solo" / "SKILL.md").write_text("---\nname: solo\ndescription: \"A top-level skill\"\n---\n")
    (root / "off").mkdir()
    (root / "off" / "SKILL.md").write_text("---\nname: off\ndescription: not enabled\n---\n")


def test_front_matter_reads_plain_quoted_and_folded_values():
    assert parse_front_matter(CHART)["name"] == "making-charts"
    assert parse_front_matter(FOLDED)["description"] == "Use when a build failed or stopped."
    assert parse_front_matter("no front matter") == {}


async def test_discovery_keeps_only_enabled_skills_with_their_categories(local_sandbox, tmp_path):
    backend, sandbox, _ = local_sandbox
    root = tmp_path / "skills"
    write_skills(root)
    skills = await discover_skills(backend, sandbox, str(root), ["making-charts", "fixing-builds", "solo"])
    assert [(s.name, s.category) for s in skills] == [
        ("solo", None), ("making-charts", "data"), ("fixing-builds", "devops")]
    assert skills[1].directory == str(root / "data" / "making-charts")
    assert await discover_skills(backend, sandbox, str(root), []) == []
    assert await discover_skills(backend, sandbox, str(tmp_path / "missing"), ["solo"]) == []


def test_the_index_groups_by_category_and_shortens_descriptions():
    skills = [SkillInfo("solo", "A top-level skill", None, "/s/solo"),
              SkillInfo("making-charts", "Use whenever the user asks for a chart, graph, plot or visual of data",
                         "data", "/s/data/making-charts")]
    assert render_index(skills) == (
        "<available_skills>\n"
        "  - solo: A top-level skill\n"
        "  data:\n"
        "    - making-charts: Use whenever the user asks for a chart, graph, plot or vi...\n"
        "</available_skills>")


@pytest.fixture
async def ctx(local_sandbox, tmp_path):
    backend, sandbox, work = local_sandbox
    work.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "skills"
    write_skills(root)
    skills = await discover_skills(backend, sandbox, str(root), ["making-charts", "fixing-builds"])
    return ToolContext(backend, sandbox, cwd=str(work), skills=tuple(skills))


async def call(ctx, tool_name, **args):
    outcome = await run_tool(tool_name, json.dumps(args), ctx)
    return json.loads(outcome.content), outcome.error


async def test_skills_list_and_category_filter(ctx):
    body, _ = await call(ctx, "skills_list")
    assert body["count"] == 2 and body["categories"] == ["data", "devops"]
    body, _ = await call(ctx, "skills_list", category="data")
    assert [s["name"] for s in body["skills"]] == ["making-charts"]


async def test_skill_view_returns_content_dir_and_linked_files(ctx):
    body, error = await call(ctx, "skill_view", name="fixing-builds")
    assert not error and body["content"].startswith("---") and body["skill_dir"].endswith("fixing-builds")
    assert body["linked_files"] == {"scripts": ["scripts/run.py"]} and "usage_hint" in body
    body, error = await call(ctx, "skill_view", name="fixing-builds", file_path="scripts/run.py")
    assert not error and body["content"] == "print('x')\n"


@pytest.mark.parametrize("args, message", [
    ({"name": "off"}, "not found"),
    ({"name": "fixing-builds", "file_path": "../x"}, "relative path"),
    ({"name": "fixing-builds", "file_path": "/etc/passwd"}, "relative path"),
    ({"name": "fixing-builds", "file_path": "nope.md"}, "not found"),
])
async def test_skill_view_refusals(ctx, args, message):
    body, error = await call(ctx, "skill_view", **args)
    assert error and body["success"] is False and message in body["error"]


# --- fix round 1 ---

import logging
import os
from types import SimpleNamespace

root_user = os.geteuid() == 0


def test_front_matter_handles_crlf_and_a_bom():
    text = CHART.replace("\n", "\r\n")
    assert parse_front_matter(text)["name"] == "making-charts"
    assert parse_front_matter("﻿" + CHART)["description"].startswith("Use whenever")
    assert parse_front_matter("﻿" + FOLDED.replace("\n", "\r\n"))["description"] == "Use when a build failed or stopped."


@pytest.mark.parametrize("indicator", [">", "|", ">-", "|-", ">+", "|+", ">2", "|2-"])
def test_front_matter_block_indicators(indicator):
    text = f"---\ndescription: {indicator}\n  one\n  two\nname: x\n---\n"
    assert parse_front_matter(text) == {"description": "one two", "name": "x"}


def test_front_matter_joins_a_plain_scalar_continued_on_indented_lines():
    text = "---\ndescription: Use when\n  continued here\nname: x\n---\n"
    assert parse_front_matter(text) == {"description": "Use when continued here", "name": "x"}


def test_front_matter_ignores_comments():
    text = "---\n# a: comment\nname: a # c\ndescription: \"keep # this\"\nother: b#c\n---\n"
    assert parse_front_matter(text) == {"name": "a", "description": "keep # this", "other": "b#c"}


async def test_duplicate_names_keep_the_first_in_sorted_order_and_warn(local_sandbox, tmp_path, caplog):
    backend, sandbox, _ = local_sandbox
    root = tmp_path / "skills"
    for category in ("b-cat", "a-cat"):
        (root / category / "dup").mkdir(parents=True)
        (root / category / "dup" / "SKILL.md").write_text("---\nname: dup\ndescription: from " + category + "\n---\n")
    with caplog.at_level(logging.WARNING):
        skills = await discover_skills(backend, sandbox, str(root), ["dup"])
    assert [(s.category, s.description) for s in skills] == [("a-cat", "from a-cat")]
    assert "dup" in caplog.text and "b-cat" in caplog.text


@pytest.mark.skipif(root_user, reason="permissions are not enforced for root")
async def test_an_unreadable_skill_is_logged_by_path(local_sandbox, tmp_path, caplog):
    backend, sandbox, _ = local_sandbox
    root = tmp_path / "skills"
    write_skills(root)
    bad = root / "solo" / "SKILL.md"
    bad.chmod(0)
    try:
        with caplog.at_level(logging.WARNING):
            skills = await discover_skills(backend, sandbox, str(root), ["solo", "making-charts"])
    finally:
        bad.chmod(0o644)
    assert [s.name for s in skills] == ["making-charts"]
    assert str(bad) in caplog.text


async def test_a_skill_file_directly_under_the_root_is_not_a_skill(local_sandbox, tmp_path):
    backend, sandbox, _ = local_sandbox
    root = tmp_path / "skills"
    write_skills(root)
    (root / "SKILL.md").write_text("---\nname: rootskill\ndescription: x\n---\n")
    assert await discover_skills(backend, sandbox, str(root), ["rootskill"]) == []


async def test_a_relative_skills_path_is_refused(local_sandbox, caplog):
    backend, sandbox, _ = local_sandbox
    with caplog.at_level(logging.WARNING):
        assert await discover_skills(backend, sandbox, "-skills", ["solo"]) == []
        assert await discover_skills(backend, sandbox, "skills", ["solo"]) == []
    assert "absolute" in caplog.text


@pytest.mark.skipif(root_user, reason="permissions are not enforced for root")
async def test_skill_view_tells_unreadable_from_missing(ctx):
    target = Path(ctx.skills[1].directory) / "scripts" / "run.py"
    target.chmod(0)
    try:
        body, error = await call(ctx, "skill_view", name="fixing-builds", file_path="scripts/run.py")
    finally:
        target.chmod(0o644)
    assert error and "could not be read" in body["error"] and "not found" not in body["error"]


async def test_skill_view_missing_skill_md_and_nul_path(ctx):
    body, error = await call(ctx, "skill_view", name="fixing-builds", file_path="a\x00b")
    assert error and "relative path" in body["error"]
    (Path(ctx.skills[1].directory) / "SKILL.md").unlink()
    body, error = await call(ctx, "skill_view", name="fixing-builds")
    assert error and "not found" in body["error"]


async def test_skill_view_warns_when_the_listing_fails(ctx):
    async def failing(argv, **kwargs):
        return SimpleNamespace(exit_code=1, output="find: boom", timed_out=False)
    ctx.exec = failing
    body, error = await call(ctx, "skill_view", name="fixing-builds")
    assert not error and body["linked_files"] is None and "warning" in body and "usage_hint" not in body


async def test_skill_view_caps_the_linked_file_listing(ctx):
    many = Path(ctx.skills[1].directory) / "references"
    many.mkdir()
    for number in range(205):
        (many / f"f{number:03}.md").write_text("x")
    body, _ = await call(ctx, "skill_view", name="fixing-builds")
    shown = sum(len(files) for files in body["linked_files"].values())
    assert shown == 200 and body["linked_files_omitted"] == 206 - 200


async def test_skill_view_flags_a_truncated_read(ctx, monkeypatch):
    from keel.tools import skill_tools
    monkeypatch.setattr(skill_tools, "_MAX_FILE_BYTES", 11)
    body, error = await call(ctx, "skill_view", name="fixing-builds")
    assert not error and body["truncated"] is True and len(body["content"].encode()) == 11 and "note" in body
    body, _ = await call(ctx, "skill_view", name="fixing-builds", file_path="scripts/run.py")
    assert "truncated" not in body


# --- fix round 2 ---

def test_a_long_whitespace_run_parses_fast():
    import time
    text = "---\nname: x" + " " * 100_000 + "y\ndescription: d\n---\n"
    started = time.monotonic()
    fields = parse_front_matter(text)
    assert time.monotonic() - started < 1
    assert fields["description"] == "d" and fields["name"].startswith("x")


def test_a_quoted_value_followed_by_a_comment():
    assert parse_front_matter("---\nname: \"a\" # c\nother: 'b'  # d\n---\n") == {"name": "a", "other": "b"}


def test_an_empty_value_does_not_take_an_indented_block():
    fields = parse_front_matter("---\nmetadata:\n  v: 1\nname: x\n---\n")
    assert fields["name"] == "x" and fields["metadata"] == ""


# --- final fix wave ---

class SpyBackend:
    """Counts exec and read_file calls and forwards them."""

    def __init__(self, backend):
        self.backend, self.execs, self.reads = backend, 0, 0

    async def exec(self, *args, **kwargs):
        self.execs += 1
        return await self.backend.exec(*args, **kwargs)

    async def read_file(self, *args, **kwargs):
        self.reads += 1
        return await self.backend.read_file(*args, **kwargs)


async def test_discovery_is_one_exec_however_many_skills(local_sandbox, tmp_path):
    backend, sandbox, _ = local_sandbox
    root = tmp_path / "skills"
    names = []
    for number in range(25):
        (root / "cat" / f"s{number}").mkdir(parents=True)
        (root / "cat" / f"s{number}" / "SKILL.md").write_text(f"---\nname: s{number}\ndescription: d{number}\n---\n")
        names.append(f"s{number}")
    spy = SpyBackend(backend)
    skills = await discover_skills(spy, sandbox, str(root), names)
    assert len(skills) == 25 and spy.execs == 1 and spy.reads == 0
    assert skills[3].description.startswith("d")


async def test_a_skill_text_cannot_forge_a_boundary(local_sandbox, tmp_path):
    backend, sandbox, _ = local_sandbox
    root = tmp_path / "skills"
    (root / "a").mkdir(parents=True)
    (root / "a" / "SKILL.md").write_text("---\nname: a\ndescription: real\n---\n__KEEL_SKILL_x__/evil/SKILL.md\n")
    skills = await discover_skills(backend, sandbox, str(root), ["a", "evil"])
    assert [s.name for s in skills] == ["a"]
