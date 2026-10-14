"""The SKILL.md Flow must be backed by real skill scripts and delegate nothing to the core.

Every executable (non-PENDING) Flow step must reference a ``scripts/<name>.py`` that exists in the
skill, and no such step may delegate a data step to a core capability. PENDING steps are the
explicitly-not-built-yet ones and are skipped — they are allowed to have no script.

The four checks are the same for every skill with a Flow — a script that exists, no core
delegation, flags the script accepts, steps in order — and here they are run against this skill.
A script with no command line (most of these are modules loaded by path) accepts no flag at all.
``test_it_walks_the_skills_that_have_a_flow`` stops the walk becoming a silent pass.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

_SKILL = Path(__file__).resolve().parents[1]

_STEP_RE = re.compile(r"^\d+\.\s", re.MULTILINE)
_SCRIPT_RE = re.compile(r"scripts/([A-Za-z0-9_]+\.py)")
# Phrases that would mean an executable step leans on a core capability instead of a skill script.
# Any "<something> capability" at all, not a list of names that goes stale the moment a new
# capability lands. Measured: the old alternation named jira and freshdesk, so "the gmail
# capability" sailed straight through after an earlier change added one.
_DELEGATION_RE = re.compile(
    r"\b\w+\s+capabilit(?:y|ies)\b|\bcapability\s+tool",
    re.IGNORECASE,
)


def _skills_with_a_flow() -> list[Path]:
    folder = _SKILL
    has_flow = ((folder / "SKILL.md").is_file() and (folder / "scripts").is_dir()
                and "## Flow" in (folder / "SKILL.md").read_text(encoding="utf-8"))
    return [folder] if has_flow else []


_WALKED = _skills_with_a_flow()


def _flow_section(skill: Path) -> str:
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    start = text.index("## Flow")
    rest = text[start + len("## Flow"):]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _steps(section: str) -> list[str]:
    starts = [m.start() for m in _STEP_RE.finditer(section)]
    starts.append(len(section))
    return [section[starts[i]:starts[i + 1]] for i in range(len(starts) - 1)]


def _is_pending(step: str) -> bool:
    return "PENDING" in step.upper()


def test_it_walks_the_skills_that_have_a_flow():
    """Not a duplicate of the checks below: it records that the walk found something.

    A walk that matches nothing passes every parametrised test by generating none of them.
    """
    walked = {skill.name for skill in _WALKED}
    assert walked == {"gmail-wrike-tasks"}, walked


@pytest.mark.parametrize("skill", _WALKED, ids=lambda s: s.name)
def test_every_executable_flow_step_runs_an_existing_skill_script(skill):
    steps = _steps(_flow_section(skill))
    assert steps, "no numbered Flow steps found in SKILL.md"

    problems: list[str] = []
    for step in steps:
        label = step.strip().splitlines()[0][:70]
        if _is_pending(step):
            continue  # explicitly not built yet — allowed to have no script
        scripts = _SCRIPT_RE.findall(step)
        if not scripts:
            problems.append(f"step {label!r} references no skill script (scripts/<name>.py)")
            continue
        for rel in scripts:
            if not (skill / "scripts" / rel).exists():
                problems.append(f"step {label!r} references scripts/{rel}, which does not exist")
    assert not problems, "Flow steps not backed by an existing script: " + "; ".join(problems)


@pytest.mark.parametrize("skill", _WALKED, ids=lambda s: s.name)
def test_no_executable_flow_step_delegates_to_a_core_capability(skill):
    problems: list[str] = []
    for step in _steps(_flow_section(skill)):
        if _is_pending(step):
            continue  # a PENDING step may explain why it must NOT call the core
        hit = _DELEGATION_RE.search(step)
        if hit:
            label = step.strip().splitlines()[0][:70]
            problems.append(f"step {label!r} delegates to the core ({hit.group(0)!r})")
    assert not problems, "; ".join(problems)


# --------------------------------------------------------------- flags declared vs accepted
_FLAG_RE = re.compile(r"--[a-z][a-z0-9-]+")

#: One inline-code span: `…`. A command in SKILL.md lives inside one of these, and so do the
#: bare flag names mentioned in prose — attributing flags per span is what tells them apart.
_CODE_SPAN_RE = re.compile(r"`([^`]+)`", re.DOTALL)

_NOT_ARGUMENTS = {"--help"}


def _script_parser(skill: Path, script_name: str):
    """Load one skill script and return its argparse parser, without running it.

    ``None`` for a script with no command line: a module another script loads by path, which
    therefore accepts no flag at all.
    """
    spec = importlib.util.spec_from_file_location(
        f"_flagcheck_{skill.name}_{script_name}", skill / "scripts" / script_name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    build = getattr(module, "_build_parser", None)
    return build() if build is not None else None


def _accepted_flags(parser) -> set[str]:
    """Every option string the parser takes, subparsers included.

    ``dedupe_cache.py`` and ``fetch_mention_emails.py`` put their real flags on subcommands, so
    a check that only read the top-level parser would pass them by default and prove nothing.
    """
    if parser is None:
        return set()
    accepted: set[str] = set()
    for action in parser._actions:
        accepted.update(action.option_strings)
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            for sub in choices.values():
                if hasattr(sub, "_actions"):
                    accepted.update(_accepted_flags(sub))
    return accepted


def _invocations(step: str) -> list[tuple[str, set[str]]]:
    """(script, flags) for every command span in a step.

    Flags are attributed to the script named in the *same* span. A step may run two different
    scripts, and a step may also name a flag in prose — step 3 says there is no
    ``--conversation-id`` — and neither should be read as an argument to the other's command.
    """
    found: list[tuple[str, set[str]]] = []
    for span in _CODE_SPAN_RE.findall(step):
        scripts = _SCRIPT_RE.findall(span)
        if len(scripts) != 1:
            continue  # not a single command: prose, a bare flag, or a filename on its own
        found.append((scripts[0], set(_FLAG_RE.findall(span)) - _NOT_ARGUMENTS))
    return found


@pytest.mark.parametrize("skill", _WALKED, ids=lambda s: s.name)
def test_every_flag_the_flow_mentions_is_one_its_script_accepts(skill):
    """The audit finding this exists for.

    A change removed ``--conversation-id`` from ``analyze_context.py``, but SKILL.md kept telling
    the agent to pass ``--conversation-id <conversation_id from step 3>`` to the two scripts that
    write to Jira. Following it literally, an agent would either die on argparse or improvise the
    thread anchor id into a permanent Jira issue as "Conversation". The existing guards did not
    catch it: they check that the script exists, not that the flags line up.
    """
    problems: list[str] = []
    checked = 0
    for step in _steps(_flow_section(skill)):
        if _is_pending(step):
            continue
        label = step.strip().splitlines()[0][:70]
        for script_name, flags in _invocations(step):
            if not (skill / "scripts" / script_name).exists():
                continue  # the other test reports this
            accepted = _accepted_flags(_script_parser(skill, script_name))
            checked += 1
            for flag in sorted(flags):
                if flag not in accepted:
                    problems.append(
                        f"step {label!r} passes {flag} to scripts/{script_name}, which does not "
                        f"accept it (it takes: {', '.join(sorted(accepted)) or 'no flags'})")
    assert checked, "no commands found in the Flow — the span parsing has stopped working"
    assert not problems, "SKILL.md flags that no script accepts: " + "; ".join(problems)


@pytest.mark.parametrize("skill", _WALKED, ids=lambda s: s.name)
def test_the_steps_appear_in_numeric_order_in_the_file(skill):
    """Reading order is execution order — an agent reads top to bottom.

    Measured on a review: the file ran 1, 2, 3, **7**, 4, 5, 6. Step 7 is "mark the
    e-mail processed — last, and only now", and it sat physically above steps 4-6. Its own
    prose warns that labelling before the issue exists loses the work silently; the layout
    invited exactly that. The flag guard cannot see this, because every flag was correct.
    """
    numbers = [
        int(step.split(".", 1)[0])
        for step in _steps(_flow_section(skill))
    ]
    assert numbers, "no numbered steps found — the Flow parsing has stopped working"
    assert numbers == sorted(numbers), (
        f"SKILL.md Flow steps are out of order: {numbers}. An agent reads the file top to "
        f"bottom, so a step that appears early is a step that runs early."
    )
    assert numbers == list(range(1, len(numbers) + 1)), (
        f"SKILL.md Flow steps are not 1..n with no gaps or repeats: {numbers}"
    )
