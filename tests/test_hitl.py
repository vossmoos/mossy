"""HITL: a task waits on disk for a person, and enters the queue only once approved.

The medium here is a fake, defined in this file and used nowhere else: the core ships no medium.
It stands in for the place where people decide, so its state outlives a runtime the way a real
one would. Nothing reaches the network, and the DuckDB file is the test's own (``conftest.py``).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from mossy.capabilities.hitl import chat_hitl_capability, hitl_capability, task_envelope
from mossy.channels.cli import chat as cli_chat
from mossy.channels.slack.app import ConversationState, SlackChannel, TTLStore
from mossy.duties import discover_duties
from mossy.duties.hitl_review import POLL_SECONDS, HitlReview
from mossy.hitl import REGISTRY, Decision, Medium, discover_media
from mossy.hitl.base import APPROVED, CANCELLED, PENDING, REJECTED
from mossy.hitl.flow import HITL_CONTEXT_KEY, SUMMARY_MAX, HitlError
from mossy.hitl.store import ApprovalStore, database_path
from mossy.runtime.core import Runtime
from mossy.runtime.deps import RuntimeDeps
from mossy.runtime.models import Envelope, Priority, Task, TaskStatus

GOAL = "Set the retention period of report 42 to 8 days"
NOW = datetime(2026, 11, 27, 10, 0, 5, tzinfo=UTC)


class FakeMedium(Medium):
    """Tests only. Records every call, and answers ``check`` from ``decisions``."""

    name = "fake"
    is_configured = True
    calls: list[tuple[str, str]] = []
    shown: list[str] = []
    decisions: dict[str, Decision] = {}
    fail_open: Exception | None = None
    fail_check: Exception | None = None

    @classmethod
    def configured(cls) -> bool:
        return cls.is_configured

    @classmethod
    def opened(cls) -> list[str]:
        return [request_id for operation, request_id in cls.calls if operation == "open"]

    async def open(self, request) -> str:
        type(self).calls.append(("open", request.id))
        type(self).shown.append(request.task_text)
        if self.fail_open is not None:
            raise self.fail_open
        return f"fake-{len(self.opened())}"

    async def check(self, external_id: str) -> Decision:
        type(self).calls.append(("check", external_id))
        if self.fail_check is not None:
            raise self.fail_check
        return self.decisions.get(external_id, Decision(PENDING))

    async def cancel(self, external_id: str) -> None:
        type(self).calls.append(("cancel", external_id))

    def link(self, external_id: str) -> str | None:
        return f"https://approvals.invalid/{external_id}"


@pytest.fixture
def medium(monkeypatch):
    """The fake medium, registered for one test and with nothing left from the last one."""
    FakeMedium.is_configured = True
    FakeMedium.calls = []
    FakeMedium.shown = []
    FakeMedium.decisions = {}
    FakeMedium.fail_open = None
    FakeMedium.fail_check = None
    monkeypatch.setitem(REGISTRY, FakeMedium.name, FakeMedium)
    return FakeMedium


def _envelope(**changes) -> Envelope:
    values = {
        "payload": GOAL,
        "priority": int(Priority.AUTONOMOUS),
        "scheduled_for": datetime(2026, 12, 1, 9, 0, tzinfo=UTC),
        "source": "teams",
        "task_id": "task-1",
        "raw": {"caller_ids": ["user-1"], "conversation": "c-9", "nested": {"values": [1, 2]}},
    }
    values.update(changes)
    return Envelope(**values)


def _store() -> ApprovalStore:
    return ApprovalStore(database_path(Path("unused")))


def _runtime() -> Runtime:
    return Runtime(enable_duties=False)


# ---------------------------------------------------------------- 1. not queued before approval


def test_a_task_sent_for_approval_is_stored_and_opened_and_not_queued(medium) -> None:
    envelope = _envelope()

    async def scenario():
        runtime = _runtime()
        request = await runtime.submit_for_approval(envelope, requester="user-1")
        return runtime, request

    runtime, request = asyncio.run(scenario())
    assert runtime.queue_depth() == 0
    assert runtime.tasks == {}
    stored = _store().get(request.id)
    assert stored == request
    assert stored.status == PENDING
    assert stored.envelope == envelope.model_dump_json()
    assert (stored.medium, stored.external_id) == ("fake", "fake-1")
    assert (stored.requester, stored.origin_channel) == ("user-1", "teams")
    assert stored.decided_at is None
    assert medium.opened() == [request.id]


def test_a_pending_request_is_checked_and_stays_out_of_the_queue(medium) -> None:
    async def scenario():
        runtime = _runtime()
        request = await runtime.submit_for_approval(_envelope())
        decided = await runtime.hitl.review()
        return runtime, request, decided

    runtime, request, decided = asyncio.run(scenario())
    assert decided == []
    assert runtime.queue_depth() == 0
    assert ("check", "fake-1") in medium.calls
    assert _store().get(request.id).status == PENDING


# ---------------------------------------------------------------- 2. approved = as stored


def test_an_approved_task_enters_the_queue_exactly_as_it_was_stored(medium) -> None:
    envelope = _envelope()

    async def scenario():
        runtime = _runtime()
        request = await runtime.submit_for_approval(envelope)
        medium.decisions[request.external_id] = Decision(APPROVED, by="anna", comment="ok")
        await runtime.hitl.review()
        # Bounded: with nothing queued, pop() would wait for ever instead of failing.
        popped = await asyncio.wait_for(runtime.queue.pop(lambda task: True), timeout=5)
        return runtime, request, popped

    runtime, request, popped = asyncio.run(scenario())
    [task] = runtime.tasks.values()
    assert popped is task
    expected = _runtime().task_from_envelope(envelope)
    context = dict(task.context)
    decision = context.pop(HITL_CONTEXT_KEY)
    assert (task.id, task.goal, task.priority, task.not_before, context) == (
        "task-1", GOAL, expected.priority, expected.not_before, expected.context
    )
    assert decision == {
        "request_id": request.id, "status": APPROVED, "by": "anna", "comment": "ok",
        "medium": "fake", "external_id": "fake-1",
    }
    stored = _store().get(request.id)
    assert (stored.status, stored.decision_by, stored.decision_comment) == (APPROVED, "anna", "ok")
    assert stored.decided_at is not None
    # The stored text is still the text that was written.
    assert stored.envelope == envelope.model_dump_json()


def test_a_task_that_would_not_be_read_back_unchanged_is_refused(medium) -> None:
    # A set comes back from JSON as a list: the approved task would not be the one that was sent.
    envelope = _envelope(raw={"callers": {"user-1"}})

    async def scenario():
        runtime = _runtime()
        with pytest.raises(HitlError):
            await runtime.submit_for_approval(envelope)
        return runtime

    runtime = asyncio.run(scenario())
    assert _store().pending() == []
    assert medium.calls == []
    assert runtime.queue_depth() == 0


# ---------------------------------------------------------------- 3. not approved = not run


@pytest.mark.parametrize(
    "verdict, where",
    [(REJECTED, "in the medium"), (CANCELLED, "by Mossy"), (CANCELLED, "in the medium")],
)
def test_a_task_that_is_not_approved_goes_back_as_a_decision_without_its_instructions(
    medium, verdict, where
) -> None:
    envelope = _envelope()

    async def scenario():
        runtime = _runtime()
        request = await runtime.submit_for_approval(envelope, summary="Retention of report 42")
        if where == "in the medium":
            # Read back by the review, like any decision the people in the medium take.
            medium.decisions[request.external_id] = Decision(verdict, by="anna", comment="wrong value")
            await runtime.hitl.review()
        else:
            await runtime.hitl.cancel(request.id, by="anna", comment="wrong value")
        return runtime, request

    runtime, request = asyncio.run(scenario())
    [task] = runtime.tasks.values()
    # The stored task is not what was queued: not its id, not its schedule, and none of its words.
    assert task.id != "task-1"
    assert task.not_before is None
    everything = json.dumps(task.model_dump(mode="json"))
    assert GOAL not in everything
    # Nor anything of its context. ("c-9" itself is not searched for: a random id can contain it.)
    for word in ("period of report", "8 days", "nested", "conversation"):
        assert word not in everything
    assert task.goal == (
        f"Approval request {request.id} was {verdict} by anna.\n"
        "Summary: Retention of report 42\n"
        "Comment: wrong value\n"
        "Nothing was carried out, and nothing is to be carried out for it. "
        "Tell the requester the decision."
    )
    # Id, summary, decision and comment. Plus who asked, which is access and not instruction.
    assert task.context == {
        HITL_CONTEXT_KEY: {
            "request_id": request.id, "status": verdict, "by": "anna",
            "comment": "wrong value", "summary": "Retention of report 42",
        },
        "caller_ids": ["user-1"],
        "source": "teams",
    }
    assert _store().get(request.id).status == verdict
    # Mossy withdraws it in the medium only when Mossy is the one cancelling.
    assert (("cancel", "fake-1") in medium.calls) is (where == "by Mossy")


def test_a_summary_is_one_short_line_however_it_was_written(medium) -> None:
    # The summary is the one thing of the task a rejection hands back, so the task cannot hide in it.
    long = "Set the retention period\nof report 42 to 8 days. " * 20

    async def scenario():
        runtime = _runtime()
        request = await runtime.submit_for_approval(_envelope(), summary=long)
        medium.decisions[request.external_id] = Decision(REJECTED)
        await runtime.hitl.review()
        return runtime, request

    runtime, request = asyncio.run(scenario())
    [task] = runtime.tasks.values()
    summary = task.context[HITL_CONTEXT_KEY]["summary"]
    assert summary == _store().get(request.id).summary
    assert 150 < len(summary) <= SUMMARY_MAX and "\n" not in summary
    assert len(task.goal) < SUMMARY_MAX + 300


def test_a_medium_that_fails_or_answers_nonsense_decides_nothing(medium) -> None:
    async def scenario():
        runtime = _runtime()
        request = await runtime.submit_for_approval(_envelope())
        medium.fail_check = RuntimeError("medium down")
        first = await runtime.hitl.review()
        medium.fail_check = None
        medium.decisions[request.external_id] = Decision("yes")
        second = await runtime.hitl.review()
        return runtime, request, first, second

    runtime, request, first, second = asyncio.run(scenario())
    assert first == [] and second == []
    assert runtime.queue_depth() == 0
    assert _store().get(request.id).status == PENDING


def test_a_request_that_cannot_be_opened_is_not_left_pending(medium) -> None:
    medium.fail_open = RuntimeError("medium down")

    async def scenario():
        runtime = _runtime()
        with pytest.raises(HitlError, match="could not be opened in fake"):
            await runtime.submit_for_approval(_envelope())
        return runtime

    runtime = asyncio.run(scenario())
    assert _store().pending() == []
    assert runtime.queue_depth() == 0


# ---------------------------------------------------------------- 4. dedupe


def test_the_same_dedupe_key_returns_the_pending_request(medium) -> None:
    async def scenario():
        runtime = _runtime()
        first = await runtime.submit_for_approval(_envelope(), dedupe_key="42:retention:8")
        again = await runtime.submit_for_approval(_envelope(payload="other words"), dedupe_key="42:retention:8")
        other = await runtime.submit_for_approval(_envelope(), dedupe_key="42:retention:10")
        return first, again, other

    first, again, other = asyncio.run(scenario())
    assert again == first
    assert other.id != first.id
    assert medium.opened() == [first.id, other.id]
    assert [request.id for request in _store().pending()] == [first.id, other.id]


def test_a_decided_request_no_longer_blocks_its_dedupe_key(medium) -> None:
    # Control: only a pending request is returned. Once decided, the same key opens a new one.
    async def scenario():
        runtime = _runtime()
        first = await runtime.submit_for_approval(_envelope(), dedupe_key="k")
        medium.decisions[first.external_id] = Decision(REJECTED)
        await runtime.hitl.review()
        second = await runtime.submit_for_approval(_envelope(), dedupe_key="k")
        return first, second

    first, second = asyncio.run(scenario())
    assert second.id != first.id
    assert len(medium.opened()) == 2


def test_without_a_dedupe_key_every_submission_is_its_own_request(medium) -> None:
    async def scenario():
        runtime = _runtime()
        return [await runtime.submit_for_approval(_envelope()) for _ in range(2)]

    first, second = asyncio.run(scenario())
    assert first.id != second.id


# ---------------------------------------------------------------- 5. survives a restart


def test_a_pending_request_survives_a_restart_and_is_released_by_the_duty(medium) -> None:
    envelope = _envelope()

    async def before() -> str:
        runtime = _runtime()
        return (await runtime.submit_for_approval(envelope, dedupe_key="k")).id

    async def after(request_id: str):
        # A new runtime: nothing in memory, the same DuckDB file.
        runtime = _runtime()
        assert runtime.tasks == {} and runtime.queue_depth() == 0
        medium.decisions["fake-1"] = Decision(APPROVED, by="anna")
        [due] = await HitlReview().check(NOW)
        duty_task = await runtime.enqueue_request(due)
        await runtime.execute(duty_task)
        again = await runtime.submit_for_approval(envelope, dedupe_key="other")
        return runtime, duty_task, again

    request_id = asyncio.run(before())
    runtime, duty_task, again = asyncio.run(after(request_id))
    assert duty_task.status == TaskStatus.DONE, duty_task.error
    [released] = [task for task in runtime.tasks.values() if task.id != duty_task.id]
    assert (released.id, released.goal) == ("task-1", GOAL)
    assert released.context[HITL_CONTEXT_KEY]["request_id"] == request_id
    assert _store().get(request_id).status == APPROVED
    assert again.id != request_id


def test_an_approved_request_is_released_once(medium) -> None:
    async def scenario():
        runtime = _runtime()
        request = await runtime.submit_for_approval(_envelope())
        medium.decisions[request.external_id] = Decision(APPROVED)
        await runtime.hitl.review()
        await runtime.hitl.review()
        return runtime

    runtime = asyncio.run(scenario())
    assert runtime.queue_depth() == 1


# ---------------------------------------------------------------- 6. check() never calls a medium


def test_check_reads_the_file_and_calls_no_medium(medium) -> None:
    async def scenario():
        runtime = _runtime()
        await runtime.submit_for_approval(_envelope())
        medium.calls.clear()
        duty = HitlReview()
        first = await duty.check(NOW)
        same_minute = await duty.check(NOW + timedelta(seconds=20))
        next_minute = await duty.check(NOW + timedelta(seconds=POLL_SECONDS))
        return runtime, first, same_minute, next_minute

    runtime, first, same_minute, next_minute = asyncio.run(scenario())
    assert medium.calls == []
    assert [(request.kind, request.payload) for request in first] == [
        ("evaluate_duty", {"duty": "hitl_review"})
    ]
    assert same_minute == []
    assert len(next_minute) == 1 and next_minute[0].dedupe_key != first[0].dedupe_key
    assert runtime.queue_depth() == 0


def test_evaluate_is_where_the_medium_is_called(medium) -> None:
    # The other half of the rule above: the review does reach the medium, from evaluate().
    async def scenario():
        runtime = _runtime()
        await runtime.submit_for_approval(_envelope())
        medium.calls.clear()
        return await HitlReview().evaluate({"duty": "hitl_review"}, runtime)

    assert asyncio.run(scenario()) == []
    assert medium.calls == [("check", "fake-1")]


def test_check_does_nothing_and_creates_no_file_when_nothing_waits(medium, tmp_path) -> None:
    assert asyncio.run(HitlReview().check(NOW)) == []
    assert not (tmp_path / "mossy.duckdb").exists()


def test_check_does_nothing_without_a_medium(medium) -> None:
    async def scenario():
        await _runtime().submit_for_approval(_envelope())
        medium.is_configured = False
        return await HitlReview().check(NOW)

    assert asyncio.run(scenario()) == []


def test_the_duty_is_discovered_with_the_others() -> None:
    assert "hitl_review" in {duty.name for duty in discover_duties()}


# ---------------------------------------------------------------- media registry


def test_the_core_ships_no_medium() -> None:
    assert discover_media() == {}


def test_a_medium_that_is_not_configured_is_absent(medium) -> None:
    assert list(discover_media()) == ["fake"]
    medium.is_configured = False
    assert discover_media() == {}

    async def scenario():
        with pytest.raises(HitlError, match="No HITL medium is configured"):
            await _runtime().submit_for_approval(_envelope())

    asyncio.run(scenario())
    assert _store().pending() == []


def test_a_repo_registers_its_medium_the_way_it_registers_a_duty(tmp_path, monkeypatch) -> None:
    package = tmp_path / "hitl"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "paper.py").write_text(
        "from mossy.hitl import Medium, register\n\n\n"
        "@register\nclass Paper(Medium):\n    name = 'paper'\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        assert list(discover_media(tmp_path)) == ["paper"]
    finally:
        REGISTRY.pop("paper", None)
        for name in [name for name in sys.modules if name == "hitl" or name.startswith("hitl.")]:
            del sys.modules[name]


# ---------------------------------------------------------------- the agent's tool


def _run_on_the_worker(runtime: Runtime, task: Task, arguments: dict | None = None) -> list:
    arguments = {"summary": "Retention of report 42", **(arguments or {})}
    """Run ``task`` through the real ``execute`` with a model that calls ``request_approval`` once.

    Returns what the tool answered, or ``["no tool"]`` when the worker was not given the tool.
    """
    answers: list = []

    def respond(messages, info) -> ModelResponse:
        returned = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if returned:
            answers.append(returned[-1].content)
            return ModelResponse(parts=[TextPart(content="reported")])
        if not any(tool.name == "request_approval" for tool in info.function_tools):
            answers.append("no tool")
            return ModelResponse(parts=[TextPart(content="reported")])
        return ModelResponse(
            parts=[ToolCallPart(tool_name="request_approval", args=arguments, tool_call_id="1")]
        )

    agent = Agent(FunctionModel(respond), deps_type=RuntimeDeps)

    async def scenario() -> None:
        with patch.object(Runtime, "worker", new=property(lambda self: agent)), patch.object(
            Runtime, "shared_capabilities", lambda self, **options: []
        ):
            await runtime.execute(task)

    asyncio.run(scenario())
    assert task.status == TaskStatus.DONE, task.error
    return answers


def _task(**changes) -> Task:
    values = {
        "id": "active-1",
        "goal": GOAL,
        "priority": int(Priority.USER_INPUT),
        "context": {"source": "teams", "caller_ids": ["user-2", "user-1"], "conversation": "c-9"},
    }
    values.update(changes)
    return Task(**values)


def test_the_worker_has_the_tool_only_when_a_medium_is_configured(medium) -> None:
    runtime = _runtime()
    assert hitl_capability(runtime, _task()) is not None
    medium.is_configured = False
    assert hitl_capability(runtime, _task()) is None
    assert _run_on_the_worker(runtime, _task()) == ["no tool"]


def test_the_tool_sends_the_active_task_for_approval_and_returns_the_link(medium) -> None:
    runtime, task = _runtime(), _task()
    [answer] = _run_on_the_worker(runtime, task)
    [request] = _store().pending()
    assert answer["status"] == PENDING
    assert (answer["request_id"], answer["external_id"]) == (request.id, "fake-1")
    assert answer["link"] == "https://approvals.invalid/fake-1"
    assert "Do not carry it out" in answer["agent_instruction"]
    assert request.envelope == task_envelope(task).model_dump_json()
    assert (request.requester, request.origin_channel, request.origin_conversation) == (
        "user-1, user-2", "teams", "c-9"
    )
    assert request.dedupe_key == "task:active-1"
    assert request.summary == "Retention of report 42"
    assert medium.shown == [GOAL]
    assert runtime.queue_depth() == 0


def test_the_tool_asked_twice_for_one_task_opens_one_request(medium) -> None:
    runtime, task = _runtime(), _task()
    [first] = _run_on_the_worker(runtime, task)
    [second] = _run_on_the_worker(runtime, task)
    assert second["request_id"] == first["request_id"]
    assert len(medium.opened()) == 1


def test_the_tool_uses_the_dedupe_key_it_is_given(medium) -> None:
    runtime = _runtime()
    [first] = _run_on_the_worker(runtime, _task(id="a"), {"dedupe_key": "42:retention:8"})
    [second] = _run_on_the_worker(runtime, _task(id="b"), {"dedupe_key": "42:retention:8"})
    assert second["request_id"] == first["request_id"]


@pytest.mark.parametrize("verdict, word", [(APPROVED, "Carry it out"), (REJECTED, "Carry nothing out")])
def test_a_decided_task_is_told_the_decision_and_opens_no_new_request(medium, verdict, word) -> None:
    runtime, task = _runtime(), _task()
    _run_on_the_worker(runtime, task)

    async def decide() -> None:
        medium.decisions["fake-1"] = Decision(verdict, by="anna", comment="seen")
        await runtime.hitl.review()

    asyncio.run(decide())
    [back] = runtime.tasks.values()
    [answer] = _run_on_the_worker(runtime, back)
    assert (answer["status"], answer["by"], answer["comment"]) == (verdict, "anna", "seen")
    assert word in answer["agent_instruction"]
    assert len(medium.opened()) == 1
    assert _store().pending() == []


def test_the_tool_reports_a_request_that_could_not_be_opened(medium) -> None:
    medium.fail_open = RuntimeError("medium down")
    runtime = _runtime()
    [answer] = _run_on_the_worker(runtime, _task())
    assert answer["status"] == "error"
    assert "could not be opened" in answer["error"]
    assert "Do not carry it out" in answer["agent_instruction"]
    assert _store().pending() == []
    assert runtime.queue_depth() == 0


# ---------------------------------------------------------------- the tool in a chat


def _ask_in_chat(capabilities: list, arguments: dict) -> list:
    """Run a chat agent that calls ``request_approval`` once with ``arguments``.

    Returns what the tool answered, or ``["no tool"]`` when the agent was not given the tool.
    """
    answers: list = []

    def respond(messages, info) -> ModelResponse:
        returned = [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]
        if returned:
            answers.append(returned[-1].content)
            return ModelResponse(parts=[TextPart(content="reported")])
        if not any(tool.name == "request_approval" for tool in info.function_tools):
            answers.append("no tool")
            return ModelResponse(parts=[TextPart(content="reported")])
        return ModelResponse(
            parts=[ToolCallPart(tool_name="request_approval", args=arguments, tool_call_id="1")]
        )

    agent = Agent(FunctionModel(respond), deps_type=RuntimeDeps, capabilities=capabilities)
    asyncio.run(agent.run("please", deps=RuntimeDeps(runtime=None)))
    return answers


#: Written by the model, with its own spacing: it must come back untouched.
CHAT_TASK = "  Set the retention period of report 42 to 8 days.\n\nThen tell the requester.\n"
THREAD = {"channel": "C1", "thread_ts": "1700000000.000100"}


def test_a_chat_has_the_tool_only_when_a_medium_is_configured(medium) -> None:
    runtime = _runtime()
    assert len(runtime.approval_capabilities("slack")) == 1
    medium.is_configured = False
    assert runtime.approval_capabilities("slack") == []
    assert _ask_in_chat(runtime.approval_capabilities("slack"), {"task": CHAT_TASK, "summary": "s"}) == ["no tool"]


def test_the_task_written_in_a_chat_is_stored_and_shown_exactly_as_written(medium) -> None:
    runtime = _runtime()
    capabilities = runtime.approval_capabilities(
        "slack", conversation=THREAD, caller_ids=frozenset({"U2", "U1"})
    )
    [answer] = _ask_in_chat(capabilities, {"task": CHAT_TASK, "summary": "Retention of report 42"})
    [request] = _store().pending()
    assert (answer["status"], answer["request_id"]) == (PENDING, request.id)
    assert answer["link"] == "https://approvals.invalid/fake-1"
    # Word for word, spacing included: in the file, and in front of the approver.
    assert request.task_text == CHAT_TASK
    assert json.loads(request.envelope)["payload"] == CHAT_TASK
    assert medium.shown == [CHAT_TASK]
    # Who and where come from the channel, not from the model.
    assert request.task_envelope().raw == {"caller_ids": ["U1", "U2"]}
    assert (request.requester, request.origin_channel) == ("U1, U2", "slack")
    assert request.origin_conversation == THREAD
    assert request.summary == "Retention of report 42"
    assert runtime.queue_depth() == 0 and runtime.tasks == {}


def test_a_chat_task_runs_as_written_and_with_the_callers_access_once_approved(medium) -> None:
    runtime = _runtime()
    capabilities = runtime.approval_capabilities("teams", caller_ids=frozenset({"aad-1"}))
    _ask_in_chat(capabilities, {"task": CHAT_TASK, "summary": "s"})

    async def approve() -> None:
        medium.decisions["fake-1"] = Decision(APPROVED, by="anna")
        await runtime.hitl.review()

    asyncio.run(approve())
    [task] = runtime.tasks.values()
    # The runtime trims the ends of every goal it queues; the words are the stored ones.
    assert task.goal == CHAT_TASK.strip()
    assert task.context["caller_ids"] == ["aad-1"]
    assert task.context["source"] == "teams"
    assert task.context[HITL_CONTEXT_KEY]["status"] == APPROVED


@pytest.mark.parametrize(
    "requester, stored", [("Anna Example", "Anna Example"), ("", ""), (None, "aad-1, u-1")]
)
def test_a_channel_can_name_the_requester_instead_of_the_caller_ids(medium, requester, stored) -> None:
    # A channel that knows a visible name passes it, and "" when it knows none: neither is replaced
    # by the ids. Left out, the requester is the caller ids, as for every channel here.
    runtime = _runtime()
    capability = chat_hitl_capability(
        runtime, channel="teams", caller_ids=frozenset({"u-1", "aad-1"}), requester=requester
    )
    _ask_in_chat([capability], {"task": CHAT_TASK, "summary": "s"})
    [request] = _store().pending()
    assert request.requester == stored
    # Who asks is only a name for the approver: the access the task runs with is still the ids.
    assert request.task_envelope().raw == {"caller_ids": ["aad-1", "u-1"]}


def test_the_same_words_in_the_same_conversation_are_one_request(medium) -> None:
    runtime = _runtime()
    here = runtime.approval_capabilities("slack", conversation=THREAD, caller_ids=frozenset({"U1"}))
    there = runtime.approval_capabilities(
        "slack", conversation={**THREAD, "thread_ts": "2"}, caller_ids=frozenset({"U1"})
    )
    [first] = _ask_in_chat(here, {"task": CHAT_TASK, "summary": "s"})
    [again] = _ask_in_chat(here, {"task": CHAT_TASK, "summary": "s"})
    [elsewhere] = _ask_in_chat(there, {"task": CHAT_TASK, "summary": "s"})
    assert again["request_id"] == first["request_id"]
    assert elsewhere["request_id"] != first["request_id"]
    assert len(medium.opened()) == 2


def test_a_chat_request_without_a_task_or_with_an_origin_that_is_not_plain_data_is_refused(medium) -> None:
    runtime = _runtime()
    [empty] = _ask_in_chat(runtime.approval_capabilities("slack"), {"task": "  \n", "summary": "s"})
    bad_origin = runtime.approval_capabilities("slack", conversation={"when": NOW})
    [refused] = _ask_in_chat(bad_origin, {"task": CHAT_TASK, "summary": "s"})
    assert (empty["status"], refused["status"]) == ("error", "error")
    assert "origin" in refused["error"]
    assert _store().pending() == [] and medium.calls == []


# ---------------------------------------------------------------- the three chat channels


def test_slack_gives_the_agent_the_tool_with_its_channel_and_thread(medium) -> None:
    runtime = _runtime()
    channel = SlackChannel.__new__(SlackChannel)
    channel.runtime = runtime
    channel.agent = MagicMock()
    channel.max_history_messages = 4
    channel.histories = TTLStore[ConversationState](ttl_seconds=60, max_entries=5)
    channel._delivery_dedup_lock = asyncio.Lock()
    channel._delivered_event_keys = {}
    seen: dict = {}

    async def fake_run(agent, text, *, deps, capabilities, message_history):
        seen["capabilities"] = list(capabilities)
        return MagicMock(output="ok", new_messages=lambda: [])

    async def scenario() -> None:
        client = MagicMock()

        async def post(**_kwargs):
            return None

        client.chat_postMessage = post
        event = {"channel": "C1", "ts": "1700000000.000100", "user": "U1"}
        target = SimpleNamespace(channel="C1", thread_ts="1700000000.000100")
        with patch("mossy.channels.slack.app.run_agent_with_utc", fake_run), patch.object(
            Runtime, "channel_capabilities", lambda self, *args, **options: []
        ):
            await channel._reply(event, "hello", target, client)

    asyncio.run(scenario())
    [answer] = _ask_in_chat(seen["capabilities"], {"task": CHAT_TASK, "summary": "s"})
    request = _store().get(answer["request_id"])
    assert (request.origin_channel, request.origin_conversation, request.requester) == ("slack", THREAD, "U1")


def test_teams_gives_the_agent_the_tool_with_its_conversation(medium) -> None:
    pytest.importorskip("botbuilder.core")
    from mossy.channels.teams import app as teams

    runtime = _runtime()
    channel = teams.TeamsChannel.__new__(teams.TeamsChannel)
    channel.runtime = runtime
    channel.agent = MagicMock()
    channel.max_history_messages = 40
    channel.histories = TTLStore[ConversationState](ttl_seconds=60, max_entries=5)
    activity = teams.Activity(
        type="message", text="hello", id="m1", channel_id="msteams",
        service_url="https://smba.invalid/emea/",
    )
    activity.from_property = SimpleNamespace(role="user", aad_object_id="aad-1", id="29:abc")
    activity.conversation = SimpleNamespace(id="c1", conversation_type="personal", tenant_id="t1")
    activity.entities = []
    activity.recipient = SimpleNamespace(id="bot")
    turn = MagicMock(activity=activity)

    async def send(_item):
        return None

    turn.send_activity = send
    seen: dict = {}

    async def fake_run(agent, text, *, deps, capabilities, message_history):
        seen["capabilities"] = list(capabilities)
        return MagicMock(output="ok", new_messages=lambda: [])

    async def scenario() -> None:
        with patch.object(teams, "run_agent_with_utc", fake_run), patch.object(
            Runtime, "channel_capabilities", lambda self, *args, **options: []
        ):
            await channel.handle_message(turn)

    asyncio.run(scenario())
    [answer] = _ask_in_chat(seen["capabilities"], {"task": CHAT_TASK, "summary": "s"})
    request = _store().get(answer["request_id"])
    assert request.origin_channel == "teams"
    assert request.origin_conversation == {
        "service_url": "https://smba.invalid/emea/", "channel_id": "msteams",
        "conversation_id": "c1", "conversation_type": "personal", "tenant_id": "t1",
        "activity_id": "m1", "reply_to_id": None, "bot_id": "bot", "user_id": "29:abc",
    }
    assert request.requester == "29:abc, aad-1"


def test_the_cli_gives_its_agent_the_tool(medium) -> None:
    runtime = _runtime()
    built: dict = {}

    def fake_agent(model, **options):
        built["capabilities"] = list(options["capabilities"])
        return MagicMock()

    def quit_at_once() -> str:
        return "/quit"

    async def scenario() -> None:
        with patch.object(cli_chat, "Agent", fake_agent), patch.object(
            cli_chat, "_read_cli_line_sync", quit_at_once
        ), patch.object(cli_chat, "skill_manager_capability", lambda runtime: None), patch.object(
            Runtime, "shared_capabilities", lambda self, **options: []
        ):
            await cli_chat.stdin_loop(runtime)

    asyncio.run(scenario())
    capabilities = [capability for capability in built["capabilities"] if capability is not None]
    [answer] = _ask_in_chat(capabilities, {"task": CHAT_TASK, "summary": "s"})
    request = _store().get(answer["request_id"])
    # A trusted operator in one conversation: no caller, nothing to find again.
    assert (request.origin_channel, request.origin_conversation, request.requester) == ("cli", None, None)
    assert request.task_envelope().raw == {}


# ---------------------------------------------------------------- the file is shared

_OTHER_PROCESS = """
import sys, duckdb
connection = duckdb.connect(sys.argv[1])
connection.execute("CREATE TABLE IF NOT EXISTS other_writer (n INTEGER)")
connection.execute("INSERT INTO other_writer VALUES (1)")
connection.close()
"""


def test_another_process_can_write_the_file_while_the_runtime_runs(medium, tmp_path, monkeypatch) -> None:
    # No connection is kept: a skill script, which is another process, must be able to write.
    monkeypatch.setenv("PLATFORMER_DISABLE_AUTONOMOUS", "1")
    path = tmp_path / "mossy.duckdb"

    async def scenario():
        runtime = Runtime(enable_duties=True)
        loops = asyncio.create_task(runtime.start())
        try:
            request = await runtime.submit_for_approval(_envelope())
            await asyncio.sleep(0.2)  # the loops are running: inbox, worker and duties
            other = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-c", _OTHER_PROCESS, str(path)],
                capture_output=True, text=True, timeout=60,
            )
            # And the runtime still reads and writes its own table afterwards.
            still_pending = await runtime.hitl.review()
            second = await runtime.submit_for_approval(_envelope(task_id="task-2"))
        finally:
            loops.cancel()
            await asyncio.gather(loops, return_exceptions=True)
        return runtime, request, other, still_pending, second

    runtime, request, other, still_pending, second = asyncio.run(scenario())
    assert other.returncode == 0, other.stderr
    assert still_pending == []
    assert [item.id for item in _store().pending()] == [request.id, second.id]
    import duckdb

    connection = duckdb.connect(str(path), read_only=True)
    try:
        assert connection.execute("SELECT n FROM other_writer").fetchall() == [(1,)]
    finally:
        connection.close()
