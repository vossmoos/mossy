"""Skill manager capability — install/remove agent skills from the mossy-skills repo.

Exposes two tools, intended for the interactive CLI channel:

- ``install_skill(name)``  Fetch the folder ``<name>`` from the private
  ``mossy-skills`` GitHub repo and (re)create it under the repo-root ``skills/``
  folder, i.e. ``skills/<name>``. An existing ``skills/<name>`` is removed first and
  copied fresh, so install is always a clean replace.
- ``delete_skill(name)``  Remove ``skills/<name>`` from the repo-root ``skills/`` folder.

Wiring: skills are discovered from the repo-root ``skills/`` directory by the
``SkillsCapability`` (see ``Runtime.skills_capability``), which runs with
``auto_reload=True``. That toolset re-scans its directories before every agent run,
so dropping a ``<name>/SKILL.md`` folder in (install) wires the skill to the agent,
and removing it (delete) un-wires it — both take effect on the next message. No code
change is needed per skill: this is the same mechanism that already serves the
external ``skills/python-code-writer`` skill.

Env:
    MOSSY_SKILLS_REPO       Source repo (default: https://github.com/vossmoos/mossy-skills).
    MOSSY_SKILLS_REPO_REF   Branch/tag/commit to fetch (default: the repo's default branch).
    GITHUB_PERSONAL_ACCESS_TOKEN (or GITHUB_TOKEN)  Used to clone the private repo.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from pydantic_ai.capabilities.toolset import Toolset
from pydantic_ai.toolsets import FunctionToolset

if TYPE_CHECKING:
    from mossy.runtime import Runtime

DEFAULT_SKILLS_REPO = "https://github.com/vossmoos/mossy-skills"


def _is_safe_skill_name(name: str) -> bool:
    """A skill name must be a single, plain path segment (no traversal/separators)."""
    name = name.strip()
    if not name or name in (".", ".."):
        return False
    if "/" in name or "\\" in name or os.sep in name:
        return False
    return Path(name).name == name


def _authed_repo_url(repo: str) -> str:
    """Inject a GitHub token into an https repo URL so private clones work."""
    token = (
        os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or ""
    ).strip()
    if not token:
        return repo
    parts = urlsplit(repo)
    if parts.scheme != "https" or "@" in parts.netloc:
        return repo  # ssh URL, or credentials already present — leave as-is
    netloc = f"x-access-token:{token}@{parts.netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def _run_git(args: list[str], cwd: str | Path | None = None) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "output": out.decode("utf-8", errors="replace").strip(),
    }


def _skill_description(skill_dir: Path) -> str:
    """Best-effort read of the ``description`` field from a skill's SKILL.md frontmatter."""
    skill_md = skill_dir / "SKILL.md"
    try:
        lines = skill_md.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    if not lines or lines[0].strip() != "---":
        return ""
    for i in range(1, len(lines)):
        line = lines[i]
        if line.strip() == "---":
            break
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return ""


def _is_skill_dir(path: Path) -> bool:
    return path.is_dir() and not path.name.startswith(".") and (path / "SKILL.md").is_file()


def skill_manager_capability(runtime: "Runtime") -> Toolset:
    """Return a toolset that installs/removes skills under the external skills root."""

    # External (repo-root) `skills/` folder — the last-wins, user-overridable root.
    skills_root: Path = Path(runtime.external_skills_root)

    def _redact(text: str) -> str:
        token = (
            os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN")
            or os.environ.get("GITHUB_TOKEN")
            or ""
        ).strip()
        return text.replace(token, "***") if token else text

    async def install_skill(name: str) -> dict[str, Any]:
        """Install (or reinstall) a skill from the mossy-skills repo.

        Fetches the folder ``name`` from the private ``mossy-skills`` GitHub repo and
        recreates it at ``skills/<name>``: an existing folder is removed and replaced
        with a fresh copy. The skill is auto-wired to the agent on the next message
        (the skills toolset re-scans ``skills/`` before each run).

        Args:
            name: Skill/folder name, identical in the repo and locally (e.g. "python-code-writer").

        Returns a dict with keys: ok (bool), name, path, action ("installed"|"reinstalled"),
        message, and (on failure) error.
        """
        name = name.strip()
        if not _is_safe_skill_name(name):
            return {"ok": False, "name": name, "error": "Invalid skill name: must be a single folder name with no path separators."}

        repo = (os.environ.get("MOSSY_SKILLS_REPO") or DEFAULT_SKILLS_REPO).strip()
        ref = (os.environ.get("MOSSY_SKILLS_REPO_REF") or "").strip()
        clone_url = _authed_repo_url(repo)

        skills_root.mkdir(parents=True, exist_ok=True)
        dest = skills_root / name
        already = dest.exists()

        tmp = Path(tempfile.mkdtemp(prefix="mossy-skills-"))
        try:
            clone_args = ["clone", "--depth", "1"]
            if ref:
                clone_args += ["--branch", ref]
            clone_args += [clone_url, str(tmp / "repo")]
            clone = await _run_git(clone_args)
            if not clone["ok"]:
                return {
                    "ok": False,
                    "name": name,
                    "error": f"Failed to clone {repo}: {_redact(clone['output'])}",
                }

            src = tmp / "repo" / name
            if not src.is_dir():
                return {
                    "ok": False,
                    "name": name,
                    "error": f"Skill '{name}' not found in {repo}. Expected a top-level folder named '{name}'.",
                }
            if not (src / "SKILL.md").is_file():
                return {
                    "ok": False,
                    "name": name,
                    "error": f"Folder '{name}' in {repo} has no SKILL.md; it is not a valid skill.",
                }

            # Recreate from scratch: remove any existing copy, then copy fresh.
            if already:
                shutil.rmtree(dest)
            shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        action = "reinstalled" if already else "installed"
        return {
            "ok": True,
            "name": name,
            "path": str(dest),
            "action": action,
            "message": (
                f"Skill '{name}' {action} at {dest}. It is wired to the agent and "
                f"available from the next message (skills are re-scanned before each run)."
            ),
        }

    async def delete_skill(name: str) -> dict[str, Any]:
        """Delete a locally installed skill folder (skills/<name>).

        Removes the folder and un-wires the skill from the agent: the skills toolset
        re-scans ``skills/`` before each run, so the skill disappears on the next message.

        Args:
            name: Skill/folder name under skills/ (e.g. "python-code-writer").

        Returns a dict with keys: ok (bool), name, path, message, and (on failure) error.
        """
        name = name.strip()
        if not _is_safe_skill_name(name):
            return {"ok": False, "name": name, "error": "Invalid skill name: must be a single folder name with no path separators."}

        dest = skills_root / name
        if not dest.exists():
            return {"ok": False, "name": name, "path": str(dest), "error": f"Skill '{name}' is not installed (no folder at {dest})."}
        if not dest.is_dir():
            return {"ok": False, "name": name, "path": str(dest), "error": f"{dest} is not a directory."}

        shutil.rmtree(dest)
        return {
            "ok": True,
            "name": name,
            "path": str(dest),
            "message": (
                f"Skill '{name}' deleted. It is un-wired from the agent and gone from "
                f"the next message onward."
            ),
        }

    async def list_available_skills() -> dict[str, Any]:
        """List skills available for install from the configured skills repository.

        Clones the repo (``MOSSY_SKILLS_REPO`` / ``MOSSY_SKILLS_REPO_REF``) and returns
        every top-level folder that contains a SKILL.md — i.e. everything that can be
        passed to install_skill — each marked with whether it is already installed locally.

        Returns a dict with keys: ok (bool), repo, ref, count, skills (list of
        {name, description, installed}), and (on failure) error.
        """
        repo = (os.environ.get("MOSSY_SKILLS_REPO") or DEFAULT_SKILLS_REPO).strip()
        ref = (os.environ.get("MOSSY_SKILLS_REPO_REF") or "").strip()
        clone_url = _authed_repo_url(repo)

        tmp = Path(tempfile.mkdtemp(prefix="mossy-skills-"))
        try:
            clone_args = ["clone", "--depth", "1"]
            if ref:
                clone_args += ["--branch", ref]
            clone_args += [clone_url, str(tmp / "repo")]
            clone = await _run_git(clone_args)
            if not clone["ok"]:
                return {"ok": False, "repo": repo, "ref": ref or None, "error": f"Failed to clone {repo}: {_redact(clone['output'])}"}

            repo_dir = tmp / "repo"
            skills = [
                {
                    "name": child.name,
                    "description": _skill_description(child),
                    "installed": (skills_root / child.name).is_dir(),
                }
                for child in sorted(repo_dir.iterdir())
                if _is_skill_dir(child)
            ]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        return {"ok": True, "repo": repo, "ref": ref or None, "count": len(skills), "skills": skills}

    async def list_enabled_skills() -> dict[str, Any]:
        """List skills currently installed (enabled) under the local skills/ folder.

        These are the user/downloadable skills wired to the agent from the repo-root
        ``skills/`` directory. Built-in package skills (mossy/skills/) are not listed.

        Returns a dict with keys: ok (bool), root, count, skills (list of {name, description}).
        """
        if not skills_root.is_dir():
            return {"ok": True, "root": str(skills_root), "count": 0, "skills": []}
        skills = [
            {"name": child.name, "description": _skill_description(child)}
            for child in sorted(skills_root.iterdir())
            if _is_skill_dir(child)
        ]
        return {"ok": True, "root": str(skills_root), "count": len(skills), "skills": skills}

    return Toolset(
        FunctionToolset(
            [install_skill, delete_skill, list_available_skills, list_enabled_skills],
            id="skill-manager",
            instructions=(
                "Skill management tools for the operator. Map the user's intent to a tool: "
                "'install skill X' -> install_skill(name='X'); 'delete/remove/uninstall skill X' "
                "-> delete_skill(name='X'); 'list skills available for install' (what's in the repo) "
                "-> list_available_skills(); 'list skills enabled/installed' (what's installed locally) "
                "-> list_enabled_skills(). 'X' is the skill's folder name. install_skill fetches X "
                "from the skills repo and recreates skills/X (clean replace); delete_skill removes "
                "skills/X. Both auto-wire/un-wire the skill, effective from the next message. For the "
                "list tools, present the skills as a concise name + description list. Report the "
                "returned message; on failure, report the error verbatim."
            ),
        )
    )
