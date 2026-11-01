"""Worker exclusion, worker concurrency, script timeout and bounded chat history."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from mossy.channels.slack.app import ConversationState, SlackChannel, TTLStore
from mossy.runtime import core
from mossy.runtime.core import Runtime, worker_concurrency, worker_excluded_skills
from mossy.runtime.message_history import trim_message_history
from mossy.runtime.models import Priority, Task, TaskStatus
from mossy.skills.selection import (
    DEFAULT_SKILL_SCRIPT_TIMEOUT_SECONDS,
    _SkillScriptExecutor,
    skill_script_timeout_seconds,
)


def _user(text: str = "hi") -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=text)])


def _call() -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool_name="t", args={}, tool_call_id="1")])


def _result() -> ModelRequest:
    return ModelRequest(parts=[ToolReturnPart(tool_name="t", content="ok", tool_call_id="1")])


def _text() -> ModelResponse:
    return ModelResponse(parts=[TextPart(content="done")])


def test_worker_exclusions_parse_and_default_to_none(monkeypatch) -> None:
    monkeypatch.delenv("PLATFORMER_WORKER_EXCLUDE_SKILLS", raising=False)
    assert worker_excluded_skills() == set()
    monkeypatch.setenv("PLATFORMER_WORKER_EXCLUDE_SKILLS", "shell, filesystem,")
    assert worker_excluded_skills() == {"shell", "filesystem"}


@pytest.mark.parametrize("excluded, expected", [("", True), ("shell", False)])
def test_worker_receives_shell_only_when_not_excluded(monkeypatch, excluded, expected) -> None:
    sentinel = object()
    monkeypatch.setattr(core, "shell_capability", lambda: sentinel)
    monkeypatch.setenv("PLATFORMER_WORKER_EXCLUDE_SKILLS", excluded)
    seen: dict = {}

    async def fake_run(agent, goal, *, deps, capabilities):
        seen["capabilities"] = list(capabilities)
        return MagicMock(output=None)

    runtime = Runtime(enable_duties=False)
    task = Task(id="t", goal="anything", priority=int(Priority.BACKGROUND))
    with patch.object(core, "run_agent_with_utc", fake_run), patch.object(
        Runtime, "worker", new=property(lambda self: MagicMock())
    ):
        asyncio.run(runtime.execute(task))
    assert (sentinel in seen["capabilities"]) is expected


@pytest.mark.parametrize("raw, expected", [(None, 1), ("3", 3), ("0", 1), ("x", 1)])
def test_worker_concurrency_values(monkeypatch, raw, expected) -> None:
    monkeypatch.delenv("PLATFORMER_WORKER_CONCURRENCY", raising=False)
    if raw is not None:
        monkeypatch.setenv("PLATFORMER_WORKER_CONCURRENCY", raw)
    assert worker_concurrency() == expected


@pytest.mark.parametrize("loops, expected", [("1", 1), ("2", 2)])
def test_tasks_run_side_by_side_only_with_more_loops(monkeypatch, loops, expected) -> None:
    monkeypatch.setenv("PLATFORMER_WORKER_CONCURRENCY", loops)
    monkeypatch.setenv("PLATFORMER_DISABLE_AUTONOMOUS", "1")

    async def scenario() -> int:
        running = peak = finished = 0
        runtime = Runtime(enable_duties=False)

        async def fake_execute(task: Task) -> None:
            nonlocal running, peak, finished
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1
            finished += 1
            task.status = TaskStatus.DONE

        runtime.execute = fake_execute  # type: ignore[method-assign]
        await runtime.submit_goal("a")
        await runtime.submit_goal("b")
        loop_task = asyncio.create_task(runtime.start())
        while finished < 2:
            await asyncio.sleep(0.01)
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)
        return peak

    assert asyncio.run(scenario()) == expected


def test_script_timeout_env(monkeypatch) -> None:
    monkeypatch.delenv("SKILL_SCRIPT_TIMEOUT_SECONDS", raising=False)
    assert skill_script_timeout_seconds() == DEFAULT_SKILL_SCRIPT_TIMEOUT_SECONDS == 90
    monkeypatch.setenv("SKILL_SCRIPT_TIMEOUT_SECONDS", "120")
    assert skill_script_timeout_seconds() == 120
    assert _SkillScriptExecutor().timeout == 120
    for bad in ("nope", "0", "-1"):
        monkeypatch.setenv("SKILL_SCRIPT_TIMEOUT_SECONDS", bad)
        assert skill_script_timeout_seconds() == 90


def test_real_script_runs_through_the_executor_with_capped_args(tmp_path: Path) -> None:
    script_path = tmp_path / "echo_args.py"
    script_path.write_text("import sys\nprint(sys.argv[1:])\n", encoding="utf-8")
    script = SimpleNamespace(name="echo_args.py", uri=str(script_path), skill_name="x")
    output = asyncio.run(
        _SkillScriptExecutor(max_script_results=5).run(script, {"limit": 50, "query": "q"})
    )
    assert "'--limit', '5'" in output
    assert "'--query', 'q'" in output


def test_trim_never_starts_on_orphan_tool_results() -> None:
    messages = [_user("old"), _call(), _result(), _text(), _user("new"), _text()]
    assert trim_message_history(messages, 4) == messages[4:]
    assert trim_message_history(messages, 5) == messages[4:]
    assert trim_message_history(messages, 6) == messages


def test_trim_edges() -> None:
    assert trim_message_history([_user()], 0) == []
    assert trim_message_history([_call(), _result()], 10) == []
    pending = [_user(), _call()]
    assert trim_message_history(pending, 10) == pending[:1]


def test_slack_keeps_a_valid_window_and_passes_the_caller() -> None:
    channel = SlackChannel.__new__(SlackChannel)
    channel.runtime = MagicMock()
    channel.agent = MagicMock()
    channel.max_history_messages = 4
    channel.histories = TTLStore[ConversationState](ttl_seconds=60, max_entries=5)
    channel._delivery_dedup_lock = asyncio.Lock()
    channel._delivered_event_keys = {}
    seen: dict = {}

    async def fake_run(agent, text, *, deps, capabilities, message_history):
        seen.update(deps=deps, history=list(message_history))
        return MagicMock(
            output="ok", new_messages=lambda: [_user(), _call(), _result(), _text()]
        )

    async def scenario() -> list:
        state = await channel.histories.get_or_create("im:D1", ConversationState)
        state.history[:] = [_user("a"), _text(), _user("b"), _call(), _result(), _text()]
        client = MagicMock()

        async def post(**_kwargs):
            return None

        client.chat_postMessage = post
        event = {"channel": "D1", "ts": "1.0", "user": "U1", "channel_type": "im"}
        with patch("mossy.channels.slack.app.run_agent_with_utc", fake_run):
            await channel._reply(
                event, "hello", SimpleNamespace(channel="D1", thread_ts=None), client
            )
        return state.history

    history = asyncio.run(scenario())
    assert isinstance(seen["history"][0], ModelRequest)
    assert isinstance(seen["history"][0].parts[0], UserPromptPart)
    assert seen["deps"].caller_ids == frozenset({"U1"})
    assert len(history) == 4 and isinstance(history[0].parts[0], UserPromptPart)
    assert channel.runtime.channel_capabilities.call_args.kwargs["caller_ids"] == {"U1"}
