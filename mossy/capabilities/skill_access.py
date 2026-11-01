"""Per-user skill access: limit a skill to named callers on channels that identify them.

``MOSSY_SKILL_ACCESS`` lists the restricted skills, separated by semicolons::

    MOSSY_SKILL_ACCESS="billing=U012ABC,00000000-aaaa-bbbb-cccc-000000000000;shell=U012ABC"

Ids match case-insensitively. A skill not named here is open to everyone. A named skill with no
ids is denied to every chat caller. A capability gated on a skill (``shell``, ``jira``) goes with it.
"""

from __future__ import annotations

import os
from collections.abc import Collection

SKILL_ACCESS_ENV = "MOSSY_SKILL_ACCESS"


def skill_allowlists(raw: str | None = None) -> dict[str, frozenset[str]]:
    """Parse ``MOSSY_SKILL_ACCESS`` into skill name -> allowed ids (casefolded)."""
    text = os.getenv(SKILL_ACCESS_ENV, "") if raw is None else raw
    allowlists: dict[str, frozenset[str]] = {}
    for entry in text.split(";"):
        name, _, ids = entry.partition("=")
        name = name.strip()
        if name:
            allowlists[name] = frozenset(
                item.strip().casefold() for item in ids.split(",") if item.strip()
            )
    return allowlists


def denied_skills(caller_ids: Collection[str] | None) -> frozenset[str]:
    """Skills this caller may not use. ``None`` is a trusted operator, denied nothing."""
    if caller_ids is None:
        return frozenset()
    caller = {item.strip().casefold() for item in caller_ids if item and item.strip()}
    return frozenset(
        name for name, allowed in skill_allowlists().items() if not caller & allowed
    )
