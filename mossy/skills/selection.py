"""Generic skill selection helpers for agent capability wiring."""

from __future__ import annotations

import os
import warnings
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypeAlias

from pydantic_ai_skills import SkillsCapability, SkillsDirectory
from pydantic_ai_skills.local import LocalSkillScriptExecutor
from pydantic_ai_skills.types import Skill

SkillSelection: TypeAlias = Literal["all"] | Collection[str]

# The library's 30 s is too short for scripts that call a vision model or read large files.
DEFAULT_SKILL_SCRIPT_TIMEOUT_SECONDS = 90
SKILL_SCRIPT_TIMEOUT_ENV = "SKILL_SCRIPT_TIMEOUT_SECONDS"

# Script arguments that max_script_results caps, when the model passes one.
_RESULT_LIMIT_ARGS = ("n", "limit", "top_k", "max_results")
_DIRECT_EXCLUDED_TOOLS = ("load_skill", "list_skills", "read_skill_resource")
_DIRECT_INSTRUCTIONS = """\
You have these skills. Their full instructions are below. Call `run_skill_script` directly with
the exact skill and script names. Do not call `load_skill`, `list_skills` or `read_skill_resource`.

<available_skills>
{{skills_list}}
</available_skills>

{details}
"""


def skill_script_timeout_seconds() -> int:
    """Seconds allowed for one ``run_skill_script`` call (env override, default 90)."""
    raw = (os.getenv(SKILL_SCRIPT_TIMEOUT_ENV) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SKILL_SCRIPT_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_SKILL_SCRIPT_TIMEOUT_SECONDS


class _SkillScriptExecutor(LocalSkillScriptExecutor):
    """The local executor with Mossy's timeout and an optional cap on result-count arguments."""

    def __init__(self, *, max_script_results: int | None = None) -> None:
        super().__init__(timeout=skill_script_timeout_seconds())
        self._max_script_results = max_script_results

    async def run(
        self, script: Any, args: dict[str, Any] | None = None, ctx: Any | None = None
    ) -> Any:
        return await super().run(script, self._capped(args), ctx)

    def _capped(self, args: dict[str, Any] | None) -> dict[str, Any] | None:
        if self._max_script_results is None or not args:
            return args
        capped = dict(args)
        for name in _RESULT_LIMIT_ARGS:
            try:
                capped[name] = min(int(capped[name]), self._max_script_results)
            except (KeyError, TypeError, ValueError):
                continue
        return capped


@dataclass(frozen=True)
class ChannelSkillOptions:
    """How one chat channel exposes skills, read from ``<CHANNEL>_*`` variables."""

    allow_skills: SkillSelection = "all"
    direct_scripts: bool = False
    max_script_results: int | None = None

    @classmethod
    def from_env(cls, channel: str) -> "ChannelSkillOptions":
        prefix = channel.upper()
        names = {
            name.strip()
            for name in os.getenv(f"{prefix}_ALLOW_SKILLS", "").split(",")
            if name.strip()
        }
        direct = os.getenv(f"{prefix}_DIRECT_SCRIPTS", "").strip().lower()
        try:
            limit = int(os.getenv(f"{prefix}_MAX_SCRIPT_RESULTS", "").strip())
        except ValueError:
            limit = 0
        return cls(
            allow_skills=names or "all",
            direct_scripts=direct in {"1", "true", "yes", "on"},
            max_script_results=limit if limit > 0 else None,
        )


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
        script_executor: LocalSkillScriptExecutor | None = None,
    ) -> None:
        self._allow_names = _coerce_allow(allow)
        self._exclude_names = set(exclude or ())
        super().__init__(
            path=path,
            validate=validate,
            max_depth=max_depth,
            script_executor=script_executor or _SkillScriptExecutor(),
        )

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
    direct_scripts: bool = False,
    max_script_results: int | None = None,
) -> SkillsCapability:
    """Build a SkillsCapability over one or more skill roots, with optional filtering.

    Accepts a single root or an ordered collection of roots. Later roots override
    earlier ones on a per-skill-name basis (the underlying SkillsToolset is
    last-directory-wins), so pass internal roots first and external/user roots last
    to let external skills override the built-in ones by name.

    ``direct_scripts`` skips the discover-and-load round trips for a faster reply: every exposed
    ``SKILL.md`` goes into the instructions up front and only ``run_skill_script`` stays. Use it
    with a short ``allow`` list, for skills whose ``SKILL.md`` is enough without its resources.
    ``max_script_results`` caps result-count arguments (``n``, ``limit``, ``top_k``,
    ``max_results``) the model passes to a script.
    """
    if isinstance(skills_roots, (str, Path)):
        roots: list[str | Path] = [skills_roots]
    else:
        roots = list(skills_roots)

    executor = _SkillScriptExecutor(max_script_results=max_script_results)
    directories = [
        FilteredSkillsDirectory(path=root, allow=allow, exclude=exclude, script_executor=executor)
        for root in roots
    ]
    if not direct_scripts:
        return SkillsCapability(directories=directories, auto_reload=auto_reload)

    with warnings.catch_warnings():
        # The library warns that excluding load_skill loses the instructions; these carry them.
        warnings.filterwarnings("ignore", message=".*load_skill.*", category=UserWarning)
        return SkillsCapability(
            directories=directories,
            auto_reload=auto_reload,
            exclude_tools=list(_DIRECT_EXCLUDED_TOOLS),
            instruction_template=_direct_instructions(directories),
        )


def _direct_instructions(directories: Collection[SkillsDirectory]) -> str:
    skills: dict[str, Skill] = {}
    for directory in directories:  # later roots override earlier ones, as in the toolset
        skills.update({skill.name: skill for skill in directory.get_skills().values()})
    sections = []
    for name in sorted(skills):
        scripts = ", ".join(script.name for script in skills[name].scripts) or "none"
        sections.append(
            f'<skill_details name="{name}" scripts="{scripts}">\n'
            f"{skills[name].content}\n</skill_details>"
        )
    details = "\n\n".join(sections).replace("{", "{{").replace("}", "}}")
    return _DIRECT_INSTRUCTIONS.format(details=details)


def _coerce_allow(allow: SkillSelection) -> set[str] | None:
    if allow == "all":
        return None
    if isinstance(allow, str):
        raise ValueError("allow must be 'all' or a collection of skill names")
    return set(allow)
