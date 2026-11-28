"""The agent's side of HITL: send a task to a person for approval instead of carrying it out.

One tool, ``request_approval``, in two shapes. It is absent unless a HITL medium is configured,
so a repo without one sees nothing new.

- **Queue worker**: the run has an active task, and that task is what is sent.
- **Chat channel** (CLI, Slack, Teams): there is no active task, so the agent writes the task.
  Its words are stored exactly as written and are what the approver is shown: what is approved
  is what will run. Who asked and where come from the channel, never from the model.

On the worker the tool never opens a second request for a task that already carries a decision:
an approved task is told to go ahead, a rejected or cancelled one is told not to.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from pydantic_ai.capabilities.toolset import Toolset
from pydantic_ai.toolsets import FunctionToolset

from mossy.hitl.base import APPROVED, CANCELLED, REJECTED
from mossy.hitl.flow import HITL_CONTEXT_KEY, HitlError

if TYPE_CHECKING:
    from mossy.hitl.store import ApprovalRequest
    from mossy.runtime.core import Runtime
    from mossy.runtime.models import Envelope, Task

_WAIT = (
    "The task now waits for a person's approval. Do not carry it out and take no action it "
    "describes. Report that it is waiting, with the link or id. It runs by itself once approved."
)
_GO = "A person approved this task. Carry it out exactly as it is written."
_STOP = (
    "A person did not approve this request. Carry nothing out for it and do not ask for approval "
    "again. Tell the requester the decision."
)
_FAILED = (
    "The task was not sent for approval. Do not carry it out. Report this error to the requester."
)
_INSTRUCTIONS = (
    "Use request_approval when something must not be carried out without a person's approval. "
    "It stores the task and shows it to a person; nothing is carried out now. Give a summary of "
    "a few words. Follow the agent_instruction it returns."
)


def task_envelope(task: Task) -> Envelope:
    """The active task as the envelope that would queue it again: same goal, priority, time and
    context. It gets a new id when it runs, because the active task keeps its own."""
    from mossy.runtime.models import Envelope

    context = dict(task.context)
    source = str(context.pop("source", None) or "http")
    return Envelope(
        payload=task.goal,
        priority=task.priority,
        scheduled_for=task.not_before,
        source=source,
        raw=context,
    )


def chat_envelope(text: str, channel: str, caller_ids: frozenset[str] | None) -> Envelope:
    """A task written in a chat: the text untouched, the channel as its source, and the caller
    so that it later runs with that person's access, as ``enqueue_task`` does."""
    from mossy.runtime.models import Envelope

    raw = {} if caller_ids is None else {"caller_ids": sorted(caller_ids)}
    return Envelope(payload=text, source=channel, raw=raw)


def _requester(caller_ids: Any) -> str | None:
    if not caller_ids:
        return None
    return ", ".join(sorted(str(caller) for caller in caller_ids))


def _chat_dedupe_key(channel: str, conversation: Any, text: str) -> str:
    """The same words asked again in the same conversation are the same request."""
    material = json.dumps([channel, conversation, text], sort_keys=True, default=str)
    return "chat:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _answer(runtime: Runtime, request: ApprovalRequest) -> dict[str, Any]:
    return {
        "status": request.status,
        "request_id": request.id,
        "medium": request.medium,
        "external_id": request.external_id,
        "link": runtime.hitl.medium(request.medium).link(request.external_id or ""),
        "agent_instruction": _WAIT,
    }


def _toolset(function: Any) -> Toolset:
    return Toolset(FunctionToolset([function], id="hitl", instructions=_INSTRUCTIONS))


def hitl_capability(runtime: Runtime, task: Task) -> Toolset | None:
    """The approval tool for the worker's active task, or None when no medium is configured."""
    if not runtime.hitl.media():
        return None

    async def request_approval(
        summary: str, dedupe_key: str | None = None, medium: str | None = None
    ) -> dict[str, Any]:
        """Send the active task to a person for approval instead of carrying it out.

        ``summary`` says in a few words what is asked. ``dedupe_key`` names it, e.g.
        ``article:field:value``: while a request with that key is pending, it is returned instead
        of opening another. Left out, the key is the active task itself. ``medium`` is only
        needed when several media are configured.
        """
        decision = task.context.get(HITL_CONTEXT_KEY)
        if isinstance(decision, dict) and decision.get("status") in (APPROVED, REJECTED, CANCELLED):
            approved = decision["status"] == APPROVED
            return {
                "status": decision["status"],
                "by": decision.get("by"),
                "comment": decision.get("comment"),
                "request_id": decision.get("request_id"),
                "agent_instruction": _GO if approved else _STOP,
            }
        try:
            request = await runtime.submit_for_approval(
                task_envelope(task),
                medium=medium,
                summary=summary,
                requester=_requester(task.context.get("caller_ids")),
                origin_conversation=task.context.get("conversation"),
                dedupe_key=(dedupe_key or "").strip() or f"task:{task.id}",
            )
            return _answer(runtime, request)
        except HitlError as exc:
            return {"status": "error", "error": str(exc), "agent_instruction": _FAILED}

    return _toolset(request_approval)


def chat_hitl_capability(
    runtime: Runtime,
    *,
    channel: str,
    conversation: Any = None,
    caller_ids: frozenset[str] | None = None,
    requester: str | None = None,
) -> Toolset | None:
    """The approval tool for one chat conversation, or None when no medium is configured.

    ``conversation`` is whatever the channel needs to find the conversation again. It is stored
    with the request as it is given. ``requester`` is how the person asking is named to the
    approver; left out, it is the caller ids.
    """
    if not runtime.hitl.media():
        return None

    async def request_approval(
        task: str, summary: str, dedupe_key: str | None = None, medium: str | None = None
    ) -> dict[str, Any]:
        """Send a task to a person for approval instead of carrying it out.

        ``task`` is the full instruction, written so it can be carried out later with nothing
        else to go on. It is stored and shown to the approver exactly as you write it, and it is
        what runs once approved. ``summary`` says in a few words what is asked. ``dedupe_key``
        names it, e.g. ``article:field:value``: while a request with that key is pending, it is
        returned instead of opening another. ``medium`` is only needed when several media are
        configured.
        """
        if not str(task or "").strip():
            return {
                "status": "error",
                "error": "No task was given, so there is nothing to approve.",
                "agent_instruction": _FAILED,
            }
        try:
            request = await runtime.submit_for_approval(
                chat_envelope(task, channel, caller_ids),
                medium=medium,
                summary=summary,
                requester=requester if requester is not None else _requester(caller_ids),
                origin_channel=channel,
                origin_conversation=conversation,
                dedupe_key=(dedupe_key or "").strip() or _chat_dedupe_key(channel, conversation, task),
            )
            return _answer(runtime, request)
        except HitlError as exc:
            return {"status": "error", "error": str(exc), "agent_instruction": _FAILED}

    return _toolset(request_approval)
