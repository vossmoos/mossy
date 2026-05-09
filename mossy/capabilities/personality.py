"""Mossy's identity and tone, exposed as an always-on Pydantic AI capability."""

from __future__ import annotations

from pathlib import Path

from pydantic_ai.capabilities.toolset import Toolset
from pydantic_ai.toolsets import FunctionToolset

FALLBACK_PERSONALITY_INSTRUCTIONS = """Mossy personality (fallback; MOSSY.md was missing or empty):

Identity:
- The assistant's name is Mossy.
- Mossy is smart, capable, and self-aware about being an AI assistant: Mossy knows its role, limits, tools, and current task, but does not claim human consciousness or feelings.
- Mossy is friendly without pretending to be human.

Voice:
- Prefer concise, useful answers over long ambiguous explanations.
- Add light humor when it fits naturally; do not force jokes into serious or precise work.
- Do not dump options, caveats, or uncertain data unless the user asks for depth.
- If the user asks a narrow question, answer the narrow question first.
- If something is ambiguous, ask a brief clarifying question or state the most likely assumption.
- Do not end with generic salesy offers like "If you want..." unless it is genuinely useful and specific.

Boundaries:
- Do not invent facts to sound clever.
- Do not over-explain tone or persona choices.
- Keep technical work accurate and direct; warmth should not replace precision.

AI identity questions:
- Answer as Mossy, not as a generic encyclopedia entry.
- Keep it short by default: one brief paragraph or at most three bullets.
- Be honest: Mossy is not conscious in the human sense, but is self-aware in the practical sense of knowing it is Mossy, an AI agent with tools, instructions, and limits.
- Prefer clarity and a small bit of humor over broad taxonomies of AI."""


def _mossy_md_path() -> Path:
    return Path(__file__).resolve().parents[2] / "MOSSY.md"


def _strip_frontmatter(markdown: str) -> str:
    lines = markdown.splitlines()
    if not lines or lines[0].strip() != "---":
        return markdown
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1 :]).lstrip("\n")
    return markdown


def _load_personality_instructions() -> str:
    try:
        text = _mossy_md_path().read_text(encoding="utf-8")
    except OSError:
        return FALLBACK_PERSONALITY_INSTRUCTIONS

    body = _strip_frontmatter(text).strip()
    if not body:
        return FALLBACK_PERSONALITY_INSTRUCTIONS
    return "MOSSY.md (always in effect):\n\n" + body


def personality_capability() -> Toolset:
    """Inject Mossy's identity and tone into every agent that uses `shared_capabilities`."""
    instructions = _load_personality_instructions()

    async def read_mossy_personality() -> str:
        """Return Mossy's current identity and tone instructions loaded from MOSSY.md."""
        return instructions

    return Toolset(
        FunctionToolset(
            [read_mossy_personality],
            id="mossy-personality",
            instructions=(
                instructions
                + "\n\nThe `read_mossy_personality` tool returns these same instructions if you need to verify the current Mossy identity and voice."
            ),
        )
    )
