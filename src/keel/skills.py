"""Skills: read-only directories with a SKILL.md, read through the sandbox. The index format follows Hermes's
skills prompt (agent/prompt_builder.py): categories as headings, descriptions cut to 60 characters."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import groupby
import logging
import posixpath
import re
import uuid

from keel.sandbox.base import Sandbox, SandboxExecutor

log = logging.getLogger(__name__)
_FRONT = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.S)
_DESCRIPTION_CHARS = 60
MAX_SKILL_BYTES = 1_000_000


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    category: str | None
    directory: str  # absolute path inside the sandbox


_BLOCK = re.compile(r"[>|][+-]?\d*[+-]?")
_COMMENT = re.compile(r"[ \t]#")
_MAX_LINE_CHARS = 10_000


def parse_front_matter(text: str) -> dict[str, str]:
    """Top-level `key: value` pairs; folded, literal and continued plain values are joined into one line.
    Tolerates a BOM and CRLF line endings; `#` comments are ignored."""
    text = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    match = _FRONT.match(text)
    if not match:
        return {}
    fields: dict[str, str] = {}
    lines = [line[:_MAX_LINE_CHARS] for line in match.group(1).splitlines()]
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line or line[0].isspace() or line[0] == "#" or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        block = _BLOCK.fullmatch(value) is not None
        closed = False
        if value[:1] in ("\"", "'"):
            end = value.find(value[0], 1)
            if end > 0:  # a closed quoted value: whatever follows (a comment) is ignored
                value, closed = value[1:end], True
        elif not block:
            cut = _COMMENT.search(value)
            value = (value[:cut.start()] if cut else value).strip()
        if closed or not (block or value):
            pass  # nothing continues this key; an indented block under an empty value is not its value
        else:
            parts = [] if block else [value]
            while index < len(lines) and (not lines[index] or lines[index][0].isspace()):
                parts.append(lines[index].strip())
                index += 1
            value = " ".join(part for part in parts if part and not (not block and part.startswith("#")))
            if not block and len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
        fields[key.strip()] = value
    return fields


async def read_text(backend: SandboxExecutor, sandbox: Sandbox, path: str, limit: int) -> tuple[str, bool]:
    """The file's text and whether it was cut at `limit` bytes. Raises OSError (or ValueError for a bad path)."""
    data = await backend.read_file(sandbox, path, limit + 1)
    return data[:limit].decode("utf-8", errors="replace"), len(data) > limit


# One shell run reads every SKILL.md's head: each file is preceded by a line holding a per-call random token and its
# path, so the text of a skill (which cannot know the token) can never forge a boundary. `!` after the token marks a
# file that could not be read.
_DISCOVER = ('find "$1" -maxdepth 3 -type f -name SKILL.md -exec sh -c \'t=$1; shift; for f; do '
             'printf "\\n%s%s\\n" "$t" "$f"; head -c 8192 "$f" 2>/dev/null || printf "\\n%s!\\n" "$t"; done\' '
             'keel-skills "$2" {} +')


def _sections(output: str, token: str) -> list[tuple[str, str | None]]:
    """(path, head of the file or None when unreadable) for each section of the discovery output."""
    found: list[tuple[str, str | None]] = []
    path: str | None = None
    body: list[str] = []
    failed = False

    def close() -> None:
        if path is not None:
            found.append((path, None if failed else "\n".join(body)))

    for line in output.split("\n"):
        if line.startswith(token):
            rest = line[len(token):]
            if rest == "!":
                failed = True
            elif rest.startswith("/"):
                close()
                path, body, failed = rest, [], False
            continue
        if path is not None:
            body.append(line)
    close()
    return found


async def discover_skills(backend: SandboxExecutor, sandbox: Sandbox, skills_path: str,
                          enabled: list[str]) -> list[SkillInfo]:
    """The enabled skills under `skills_path`, found with a single exec that reads the first 8 KiB of every SKILL.md
    (the full file is read on demand by skill_view)."""
    if not enabled:
        return []
    root = posixpath.normpath(skills_path)
    if not root.startswith("/"):
        log.warning("skills path %r is not absolute; no skills are loaded", skills_path)
        return []
    token = f"__KEEL_SKILL_{uuid.uuid4().hex}__"
    result = await backend.exec(sandbox, ["sh", "-c", _DISCOVER, "keel-discover", root, token], cwd="/",
                                timeout_s=60, exec_id="s" + uuid.uuid4().hex[:16])
    sections = _sections(result.output, token)
    if result.exit_code != 0:
        log.warning("listing skills under %s failed: %s", root, result.output.strip()[:200] if not sections else
                    f"exit code {result.exit_code}; using the {len(sections)} files read")
        if not sections:
            return []
    wanted = set(enabled)
    skills: dict[str, SkillInfo] = {}
    for path, text in sorted(sections):
        if not path.endswith("/SKILL.md"):
            continue
        directory = posixpath.dirname(path)
        if directory == root:
            continue  # a skill is a directory
        if text is None:
            log.warning("skipping skill %s: could not read it", path)
            continue
        meta = parse_front_matter(text)
        name = meta.get("name") or posixpath.basename(directory)
        if name not in wanted:
            continue
        if name in skills:
            log.warning("skill %s at %s is a duplicate of %s and is ignored", name, directory, skills[name].directory)
            continue
        parent = posixpath.dirname(directory)
        category = posixpath.basename(parent) if parent != root else None
        skills[name] = SkillInfo(name, meta.get("description", ""), category, directory)
    return sorted(skills.values(), key=lambda s: (s.category or "", s.name))


def _short(description: str) -> str:
    return description if len(description) <= _DESCRIPTION_CHARS else description[:_DESCRIPTION_CHARS - 3] + "..."


def render_index(skills) -> str:
    lines = ["<available_skills>"]
    for category, group in groupby(sorted(skills, key=lambda s: (s.category or "", s.name)),
                                   key=lambda s: s.category):
        indent = "  "
        if category:
            lines.append(f"  {category}:")
            indent = "    "
        lines.extend(f"{indent}- {skill.name}: {_short(skill.description)}" for skill in group)
    lines.append("</available_skills>")
    return "\n".join(lines)
