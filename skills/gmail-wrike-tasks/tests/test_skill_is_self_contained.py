"""This skill must be self-contained: no import from outside its own folder.

The scripts run in a runtime that does not have ``mossy`` on the path, so importing ``mossy`` (or
pulling in ``python-dotenv``) there fails. The other tests exercise the scripts' logic, not their
imports, so this one reads the SOURCE instead — a re-added ``import mossy`` or ``dotenv`` turns it
red. It must read the file, never import it.

``test_it_finds_the_skills_that_exist`` is what stops that walk becoming a silent pass: a glob that
matches nothing checks nothing, and would go green the day the directory layout changes.
"""

from __future__ import annotations

import re
from pathlib import Path

_SKILL = Path(__file__).resolve().parents[1]
# Anchored at line start: only a real import statement matches, not the docstring phrase
# "no ``import mossy``" that documents the rule.
_FORBIDDEN = re.compile(r"^\s*(?:import|from)\s+(mossy|dotenv)\b", re.MULTILINE)


def _scripts() -> list[Path]:
    return sorted((_SKILL / "scripts").glob("*.py"))


def test_no_script_imports_mossy_or_dotenv():
    offenders: dict[str, list[str]] = {}
    for py in _scripts():
        hits = _FORBIDDEN.findall(py.read_text(encoding="utf-8"))
        if hits:
            offenders[f"{py.parent.parent.name}/{py.name}"] = hits
    assert not offenders, (
        "skill scripts must be self-contained (the script runtime has no mossy, and a transferable "
        f"skill carries no python-dotenv either); found: {offenders}"
    )


def test_it_finds_the_skills_that_exist():
    """Not a duplicate of the check above: it records that the walk found something.

    A glob that matches nothing passes the test above without reading a single file. This names
    the skills that must be covered, so deleting one — or moving scripts somewhere the glob does
    not reach — is reported rather than absorbed.
    """
    covered = {py.parent.parent.name for py in _scripts()}
    assert covered == {"gmail-wrike-tasks"}, covered
