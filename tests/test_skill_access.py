"""Per-user skill access, per-channel options and direct-script mode."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from mossy.capabilities.skill_access import denied_skills, skill_allowlists
from mossy.capabilities.system_queue import system_queue_capability
from mossy.runtime import core
from mossy.runtime.core import Runtime
from mossy.runtime.models import Priority, Task, TaskStatus
from mossy.skills.selection import ChannelSkillOptions, skills_capability


def _skill(root: Path, name: str, body: str = "Use it.") -> None:
    folder = root / name
    (folder / "scripts").mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: The {name} skill.\n---\n\n{body}\n", encoding="utf-8"
    )
    (folder / "scripts" / "go.py").write_text("print('ok')\n", encoding="utf-8")


def test_allowlists_parse() -> None:
    parsed = skill_allowlists(" billing = U1, Ada@X.io ;shell=;; ")
    assert parsed == {"billing": {"u1", "ada@x.io"}, "shell": frozenset()}


def test_denied_skills_by_caller(monkeypatch) -> None:
    monkeypatch.setenv("MOSSY_SKILL_ACCESS", "billing=U1;shell=")
    assert denied_skills(None) == frozenset()
    assert denied_skills(frozenset()) == {"billing", "shell"}
    assert denied_skills({"other"}) == {"billing", "shell"}
    assert denied_skills({"u1"}) == {"shell"}


def test_nothing_is_denied_when_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv("MOSSY_SKILL_ACCESS", raising=False)
    assert denied_skills(frozenset()) == frozenset()


def test_channel_capabilities_withhold_restricted_tools(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setattr(core, "shell_capability", lambda: sentinel)
    monkeypatch.setenv("MOSSY_SKILL_ACCESS", "shell=U1")
    runtime = Runtime(enable_duties=False)
    assert sentinel not in runtime.channel_capabilities("SLACK")
    assert sentinel not in runtime.channel_capabilities("SLACK", caller_ids=frozenset({"U2"}))
    assert sentinel in runtime.channel_capabilities("SLACK", caller_ids=frozenset({"U1"}))


def test_channel_capabilities_are_cached_per_access(monkeypatch) -> None:
    monkeypatch.setenv("MOSSY_SKILL_ACCESS", "shell=U1")
    runtime = Runtime(enable_duties=False)
    first = runtime.channel_capabilities("SLACK", caller_ids=frozenset({"U2"}))
    assert runtime.channel_capabilities("SLACK", caller_ids=frozenset({"U3"})) is first
    assert runtime.channel_capabilities("SLACK", caller_ids=frozenset({"U1"})) is not first


def _enqueue_tool(runtime: Runtime):
    toolset = system_queue_capability(runtime).toolset
    return toolset.tools["enqueue_task"].function


def test_a_queued_task_cannot_widen_a_callers_access(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setattr(core, "shell_capability", lambda: sentinel)
    monkeypatch.setenv("MOSSY_SKILL_ACCESS", "shell=U1")
    runtime = Runtime(enable_duties=False)
    seen: dict = {}

    async def fake_run(agent, goal, *, deps, capabilities):
        seen.update(capabilities=list(capabilities), deps=deps)
        return MagicMock(output=None)

    async def scenario() -> Task:
        ctx = SimpleNamespace(deps=SimpleNamespace(caller_ids=frozenset({"U2"})))
        result = await _enqueue_tool(runtime)(ctx, "use the shell")
        task = runtime.tasks[result["id"]]
        with patch.object(core, "run_agent_with_utc", fake_run), patch.object(
            Runtime, "worker", new=property(lambda self: MagicMock())
        ):
            await runtime.execute(task)
        return task

    task = asyncio.run(scenario())
    assert task.context["caller_ids"] == ["U2"]
    assert sentinel not in seen["capabilities"]
    assert seen["deps"].caller_ids == frozenset({"U2"})


def test_trusted_tasks_keep_every_skill(monkeypatch) -> None:
    sentinel = object()
    monkeypatch.setattr(core, "shell_capability", lambda: sentinel)
    monkeypatch.setenv("MOSSY_SKILL_ACCESS", "shell=U1")
    runtime = Runtime(enable_duties=False)
    seen: dict = {}

    async def fake_run(agent, goal, *, deps, capabilities):
        seen["capabilities"] = list(capabilities)
        return MagicMock(output=None)

    task = Task(id="t", goal="duty work", priority=int(Priority.BACKGROUND))
    with patch.object(core, "run_agent_with_utc", fake_run), patch.object(
        Runtime, "worker", new=property(lambda self: MagicMock())
    ):
        asyncio.run(runtime.execute(task))
    assert sentinel in seen["capabilities"]


@pytest.mark.parametrize(
    "parent_context, expected",
    [({"caller_ids": ["U2"]}, ["U2"]), ({}, None)],
)
def test_follow_up_caller_comes_from_the_parent_not_the_model(
    monkeypatch, parent_context, expected
) -> None:
    monkeypatch.delenv("PLATFORMER_DISABLE_AUTONOMOUS", raising=False)
    runtime = Runtime(enable_duties=False)
    parent = Task(
        id="p",
        goal="g",
        priority=int(Priority.AUTONOMOUS),
        status=TaskStatus.DONE,
        context=parent_context,
        result={"follow_up_goal": "next", "follow_up_context": {"caller_ids": ["U1"], "a": 1}},
    )
    asyncio.run(runtime.think_next(parent))
    child = next(task for task in runtime.tasks.values() if task.goal == "next")
    assert child.context.get("caller_ids") == expected
    assert child.context["a"] == 1


def test_channel_options_from_env(monkeypatch) -> None:
    monkeypatch.setenv("TEAMS_ALLOW_SKILLS", "a, b")
    monkeypatch.setenv("TEAMS_DIRECT_SCRIPTS", "true")
    monkeypatch.setenv("TEAMS_MAX_SCRIPT_RESULTS", "5")
    options = ChannelSkillOptions.from_env("teams")
    assert options == ChannelSkillOptions({"a", "b"}, True, 5)
    assert ChannelSkillOptions.from_env("SLACK") == ChannelSkillOptions("all", False, None)
    monkeypatch.setenv("TEAMS_MAX_SCRIPT_RESULTS", "many")
    assert ChannelSkillOptions.from_env("TEAMS").max_script_results is None


def test_direct_mode_carries_instructions_and_only_the_script_tool(tmp_path, recwarn) -> None:
    _skill(tmp_path, "alpha", "Call go.py with {braces} intact.")
    _skill(tmp_path, "beta")
    capability = skills_capability(tmp_path, allow={"alpha"}, direct_scripts=True, auto_reload=False)
    toolset = capability.get_toolset()
    assert set(toolset.tools) == {"run_skill_script"}
    instructions = asyncio.run(toolset.get_instructions(MagicMock()))
    assert "Call go.py with {braces} intact." in instructions
    assert "scripts/go.py" in instructions
    assert "beta" not in instructions
    assert not [w for w in recwarn if "load_skill" in str(w.message)]


def test_normal_mode_keeps_the_library_tools(tmp_path) -> None:
    _skill(tmp_path, "alpha")
    tools = skills_capability(tmp_path).get_toolset().tools
    assert {"load_skill", "list_skills", "run_skill_script"} <= set(tools)


def _tools_offered(capabilities: list, deps_runtime: Runtime) -> set[str]:
    seen: set[str] = set()

    def respond(_messages, info: AgentInfo) -> ModelResponse:
        seen.update(tool.name for tool in info.function_tools)
        return ModelResponse(parts=[TextPart(content="ok")])

    agent = Agent(FunctionModel(respond))
    asyncio.run(agent.run("hi", capabilities=capabilities))
    return seen


def test_a_real_agent_run_offers_tools_by_caller(monkeypatch) -> None:
    monkeypatch.setenv("MOSSY_SKILL_ACCESS", "system-queue=U1")
    runtime = Runtime(enable_duties=False)
    allowed = runtime.channel_capabilities("SLACK", caller_ids=frozenset({"U1"}))
    denied = runtime.channel_capabilities("SLACK", caller_ids=frozenset({"U2"}))
    assert "enqueue_task" in _tools_offered(allowed, runtime)
    tools = _tools_offered(denied, runtime)
    assert "enqueue_task" not in tools and "load_skill" in tools


def test_a_real_agent_run_in_direct_mode_offers_only_the_script_tool(monkeypatch) -> None:
    monkeypatch.setenv("TEAMS_DIRECT_SCRIPTS", "true")
    monkeypatch.setenv("TEAMS_ALLOW_SKILLS", "echo")
    runtime = Runtime(enable_duties=False)
    tools = _tools_offered(runtime.channel_capabilities("TEAMS"), runtime)
    assert tools == {"run_skill_script", "read_mossy_personality"}
