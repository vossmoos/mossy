"""The gmail-wrike-tasks SKILL.md names what exists, and nothing that exists goes unnamed (the Wrike → Jira flow, piece 4).

A SKILL.md ages on its own: a script is renamed, a variable is added, and the file keeps describing
the old shape. One that names a script that is no longer there is worse than none, because it is
believed. So this holds it to the code in **both** directions:

* every ``scripts/<name>.py`` it names exists, and every script in the folder is named;
* every environment variable it names is read by the code, and every variable the code reads is
  named — "the code" being the skill's scripts.

The variables the code reads are found by walking the source's syntax tree (``os.environ.get``,
``os.getenv``, ``os.environ[...]``), never by importing and never by a text search that a line break
would defeat. ``test_the_walk_finds_what_it_must`` stops that walk turning into a silent pass.

And the file is only worth anything if Mossy loads it: measured before it existed, the library Mossy
uses did not discover ``gmail-wrike-tasks`` at all.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SKILL = _ROOT
_SKILL_MD = _SKILL / "SKILL.md"

_SCRIPT_RE = re.compile(r"scripts/([A-Za-z0-9_]+\.py)")
#: A variable written as code: `UPPER_CASE_WITH_UNDERSCORES`.
_VARIABLE_RE = re.compile(r"`([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)`")


def _text() -> str:
    return _SKILL_MD.read_text(encoding="utf-8")


def _section(title: str) -> str:
    text = _text()
    start = text.index(f"## {title}")
    rest = text[start + len(title) + 3:]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _env_reads(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        name = None
        if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr == "get"
                    and isinstance(func.value, ast.Attribute) and func.value.attr == "environ"):
                name = node.args[0].value
            elif isinstance(func, ast.Attribute) and func.attr == "getenv":
                name = node.args[0].value
        elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
              and node.value.attr == "environ" and isinstance(node.slice, ast.Constant)):
            name = node.slice.value
        if isinstance(name, str):
            found.add(name)
    return found


def _variables_the_code_reads() -> set[str]:
    sources = sorted((_SKILL / "scripts").glob("*.py"))
    return set().union(*(_env_reads(path) for path in sources))


# ------------------------------------------------------------------------------ scripts
class TestTheScriptsItNames:
    def test_every_script_it_names_exists(self):
        named = set(_SCRIPT_RE.findall(_text()))
        missing = sorted(n for n in named if not (_SKILL / "scripts" / n).exists())
        assert not missing, f"SKILL.md names scripts that do not exist: {missing}"

    def test_every_script_the_skill_has_is_named(self):
        named = set(_SCRIPT_RE.findall(_text()))
        real = {path.name for path in (_SKILL / "scripts").glob("*.py")}
        assert real, "no scripts found — the walk has stopped working"
        unnamed = sorted(real - named)
        assert not unnamed, f"scripts SKILL.md does not name: {unnamed}"


# ------------------------------------------------------------------------------ variables
class TestTheVariablesItNames:
    def test_the_walk_finds_what_it_must(self):
        """Not a duplicate of the two below: an extractor that matches nothing passes them both.
        Two variables, read by two different scripts."""
        reads = _variables_the_code_reads()
        assert {"WRIKE_TOKEN", "WRIKE_TASK_LOCK_DIR"} <= reads, reads

    def test_every_variable_it_names_is_read_by_the_code(self):
        named = set(_VARIABLE_RE.findall(_text()))
        invented = sorted(named - _variables_the_code_reads())
        assert not invented, f"SKILL.md names variables no code reads: {invented}"

    def test_every_variable_the_code_reads_is_in_the_configuration_table(self):
        documented = set(_VARIABLE_RE.findall(_section("Configuration")))
        undocumented = sorted(_variables_the_code_reads() - documented)
        assert not undocumented, (
            f"variables the code reads that the Configuration table does not name: {undocumented}")


# ------------------------------------------------------------------------------ loading
class TestMossyLoadsIt:
    def test_the_library_mossy_uses_discovers_the_skill_and_its_scripts(self):
        from pydantic_ai_skills import discover_skills

        skills = {skill.name: skill for skill in discover_skills(_ROOT.parent)}
        assert "gmail-wrike-tasks" in skills, sorted(skills)
        exposed = {Path(str(script.name)).name for script in skills["gmail-wrike-tasks"].scripts}
        assert exposed == {path.name for path in (_SKILL / "scripts").glob("*.py")}

    def test_it_says_it_is_not_the_orchestrator(self):
        """Whatever runs the flow calls the scripts in Python. Read as an orchestrator, this file would
        invite an agent to run a write that was built to be kept away from agents."""
        text = _text()
        assert "this file does not run the flow" in text
        assert "enqueues no goal for an agent" in text
