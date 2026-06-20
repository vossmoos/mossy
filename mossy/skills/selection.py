"""Generic skill selection helpers for agent capability wiring."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from typing import Literal, TypeAlias

from pydantic_ai_skills import SkillsCapability, SkillsDirectory
from pydantic_ai_skills.types import Skill

SkillSelection: TypeAlias = Literal["all"] | Collection[str]


class FilteredSkillsDirectory(SkillsDirectory):
    """SkillsDirectory variant that exposes only selected skill names."""

    def __init__(
        self,
        *,
        path: str | Path,
        allow: SkillSelection = "all",
        exclude: Collection[str] | None = None,
        validate: bool = True,
        max_depth: int | None = 3,
    ) -> None:
        self._allow_names = _coerce_allow(allow)
        self._exclude_names = set(exclude or ())
        super().__init__(path=path, validate=validate, max_depth=max_depth)

    def get_skills(self) -> dict[str, Skill]:
        skills = super().get_skills()
        return {
            uri: skill
            for uri, skill in skills.items()
            if self._allows(skill.name)
        }

    def _allows(self, skill_name: str) -> bool:
        if self._allow_names is not None and skill_name not in self._allow_names:
            return False
        return skill_name not in self._exclude_names


def skills_capability(
    skills_roots: str | Path | Collection[str | Path],
    *,
    allow: SkillSelection = "all",
    exclude: Collection[str] | None = None,
    auto_reload: bool = True,
) -> SkillsCapability:
    """Build a SkillsCapability over one or more skill roots, with optional filtering.

    Accepts a single root or an ordered collection of roots. Later roots override
    earlier ones on a per-skill-name basis (the underlying SkillsToolset is
    last-directory-wins), so pass internal roots first and external/user roots last
    to let external skills override the built-in ones by name.
    """
    if isinstance(skills_roots, (str, Path)):
        roots: list[str | Path] = [skills_roots]
    else:
        roots = list(skills_roots)

    directories = [
        FilteredSkillsDirectory(path=root, allow=allow, exclude=exclude)
        for root in roots
    ]
    return SkillsCapability(directories=directories, auto_reload=auto_reload)


def _coerce_allow(allow: SkillSelection) -> set[str] | None:
    if allow == "all":
        return None
    if isinstance(allow, str):
        raise ValueError("allow must be 'all' or a collection of skill names")
    return set(allow)
