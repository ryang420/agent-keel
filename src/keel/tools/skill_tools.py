"""skills_list and skill_view, with Hermes's result shapes (tools/skills_tool.py). Only the profile's enabled
deployment skills discovered at run start through the sandbox, and the profile's learned skills read from the store,
are visible."""

from __future__ import annotations

import posixpath

from keel.skills import MAX_SKILL_BYTES, read_text
from keel.store.base import ProfileIncarnationMismatch
from keel.tools.base import ToolOutcome, require, tool, tool_json

_MAX_FILE_BYTES = MAX_SKILL_BYTES
_MAX_LINKED = 200
_HINT = ("To view linked files, call skill_view(name, file_path) where file_path is e.g. 'references/api.md' or "
         "'assets/config.yaml'")


def _fail(message: str) -> ToolOutcome:
    return ToolOutcome(tool_json({"success": False, "error": message}), error=True)


async def _learned(ctx, name: str | None = None):
    """The profile's learned skills when learning is enabled: every active one, or the one named (any state).
    A deployment skill of the same name hides it."""
    if ctx.store is None or not ctx.profile_id:
        return [] if name is None else None
    profile = await ctx.store.get_profile(ctx.profile_id)
    if profile is None or not profile.learning or (
            ctx.profile_incarnation is not None and profile.incarnation != ctx.profile_incarnation):
        return [] if name is None else None
    shipped = {skill.name for skill in ctx.skills}
    if name is not None:
        return None if name in shipped else await ctx.store.learned_skill_named(ctx.profile_id, name)
    return [skill for skill in await ctx.store.learned_skills(ctx.profile_id, states=("active",))
            if skill.name not in shipped]


async def _view_learned(name: str, args: dict, ctx) -> ToolOutcome:
    from keel.learned import document  # keel.learned imports keel.memory, which imports keel.tools.base

    skill = await _learned(ctx, name)
    if skill is None:
        return _fail(f"Skill '{name}' not found.")
    if skill.state != "active":
        return _fail(f"Skill '{name}' is {skill.state} and no longer available.")
    if args.get("file_path"):
        return _fail(f"Skill '{name}' is a learned skill and has no linked files.")
    if ctx.run_id and ctx.session_id:
        try:
            await ctx.store.record_skill_load(ctx.profile_id, ctx.session_id, ctx.run_id, skill.id, skill.version,
                                              profile_incarnation=ctx.profile_incarnation)
        except ProfileIncarnationMismatch:
            return _fail(f"Skill '{name}' not found.")
    return ToolOutcome(tool_json({"success": True, "name": skill.name, "description": skill.description,
                                  "content": document(skill), "source": "learned", "version": skill.version,
                                  "linked_files": None}))


@tool("skills_list", sandbox=False)
async def skills_list(args: dict, ctx) -> ToolOutcome:
    from keel.learned import CATEGORY  # keel.learned imports keel.memory, which imports keel.tools.base

    category = args.get("category") if isinstance(args.get("category"), str) else None
    entries = [{"name": s.name, "description": s.description, "category": s.category} for s in ctx.skills]
    entries += [{"name": s.name, "description": s.description, "category": CATEGORY} for s in await _learned(ctx)]
    skills = [s for s in entries if not category or s["category"] == category]
    if not skills:
        return ToolOutcome(tool_json({"success": True, "skills": [], "categories": [], "message": "No skills found."}))
    return ToolOutcome(tool_json({
        "success": True,
        "skills": skills,
        "categories": sorted({s["category"] for s in skills if s["category"]}),
        "count": len(skills),
        "hint": "Use skill_view(name) to see full content, tags, and linked files",
    }))


@tool("skill_view", sandbox=False)
async def skill_view(args: dict, ctx) -> ToolOutcome:
    name = require(args, "name", str)
    skill = next((s for s in ctx.skills if s.name == name), None)
    if skill is None:
        return await _view_learned(name, args, ctx)
    file_path = args.get("file_path")
    if file_path:
        if (not isinstance(file_path, str) or file_path.startswith("/") or ".." in file_path.split("/")
                or "\0" in file_path):
            return _fail("file_path must be a relative path inside the skill.")
        target = posixpath.join(skill.directory, file_path)
    else:
        target = posixpath.join(skill.directory, "SKILL.md")
    label = f"File '{file_path}'" if file_path else "SKILL.md"
    try:
        content, truncated = await read_text(ctx.backend, ctx.sandbox, target, _MAX_FILE_BYTES)
    except (FileNotFoundError, IsADirectoryError):
        return _fail(f"{label} not found in skill '{name}'.")
    except ValueError:
        return _fail("file_path must be a relative path inside the skill.")
    except OSError as exc:
        return _fail(f"{label} of skill '{name}' could not be read: {exc}")
    cut = {"truncated": True, "note": f"The file is larger than {_MAX_FILE_BYTES} bytes; only the start is shown."}
    if file_path:
        return ToolOutcome(tool_json({"success": True, "name": name, "file": file_path, "content": content,
                                      **(cut if truncated else {})}))
    listing = await ctx.exec(["find", skill.directory, "-type", "f", "-not", "-name", "SKILL.md"], timeout_s=30)
    linked: dict[str, list[str]] = {}
    extra: dict = {}
    if listing.exit_code != 0:
        extra["warning"] = "Linked files could not be listed."
    else:
        found = sorted(line for line in listing.output.splitlines() if line.startswith(skill.directory + "/"))
        if len(found) > _MAX_LINKED:
            extra["linked_files_omitted"] = len(found) - _MAX_LINKED
            extra["warning"] = f"Only the first {_MAX_LINKED} linked files are listed."
            found = found[:_MAX_LINKED]
        for line in found:
            relative = line[len(skill.directory) + 1:]
            linked.setdefault(relative.split("/", 1)[0] if "/" in relative else "files", []).append(relative)
    body = {"success": True, "name": name, "description": skill.description, "content": content,
            "path": f"{skill.directory}/SKILL.md", "skill_dir": skill.directory, "linked_files": linked or None,
            **extra, **(cut if truncated else {})}
    if linked:
        body["usage_hint"] = _HINT
    return ToolOutcome(tool_json(body))
