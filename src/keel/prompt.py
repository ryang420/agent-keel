"""A session's system prompt (arch §4, M2 design §2): Hermes's three tiers in Hermes's order, stable -> context ->
volatile, built once per session so later turns reuse a byte-identical cached prefix."""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from keel import prompts
from keel.skills import SkillInfo, render_index

DEFAULT_IDENTITY = "You are a helpful assistant."


def build_system_prompt(*, soul: str, model: str, provider: str, now: datetime, tools: bool,
                        skills: Sequence[SkillInfo] = (), workdir: str | None = None) -> str:
    identity = soul.strip() or DEFAULT_IDENTITY
    if not tools:
        return identity
    parts = [identity, prompts.text("guidance"), prompts.text("platform")]
    if skills:
        parts += [prompts.text("skills_header"), render_index(skills), prompts.text("skills_footer")]
    parts.append(f"Current date: {now:%A}, {now:%B} {now.day}, {now.year} (UTC, UTC+00:00)\n"
                 f"Model: {model}\nProvider: {provider}")
    parts.append(prompts.text("environment").format(workdir=workdir or "/"))
    return "\n\n".join(parts)
