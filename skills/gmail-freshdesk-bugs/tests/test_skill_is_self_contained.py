"""The gmail-freshdesk-bugs skill must be self-contained: no import from outside its own folder.

The scripts run in a runtime that does not have ``mossy`` on the path,
so importing ``mossy`` (or pulling in ``python-dotenv``) there fails. The other tests exercise the
scripts' logic, not their imports, so this one reads the SOURCE instead — a re-added ``import
mossy`` or ``dotenv`` turns it red. It must read the file, never import it.
"""

from __future__ import annotations

import re
from pathlib import Path

_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / "scripts"
)
# Anchored at line start: only a real import statement matches, not the docstring phrase
# "no ``import mossy``" that documents the rule.
_FORBIDDEN = re.compile(r"^\s*(?:import|from)\s+(mossy|dotenv)\b", re.MULTILINE)


def test_no_script_imports_mossy_or_dotenv():
    offenders: dict[str, list[str]] = {}
    for py in sorted(_SCRIPTS.glob("*.py")):
        hits = _FORBIDDEN.findall(py.read_text(encoding="utf-8"))
        if hits:
            offenders[py.name] = hits
    assert not offenders, (
        "skill scripts must be self-contained (the script runtime has no mossy, and a transferable "
        f"skill carries no python-dotenv either); found: {offenders}"
    )
