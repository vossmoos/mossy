"""The HITL flow: a task is stored and shown to a person, and enters the queue only once approved.

- ``submit``: the task is saved, opened in a medium and **not queued**.
- ``review``: each pending request is checked in its medium. Approved, the stored task enters the
  queue unchanged, with the decision added to its context. Rejected, it does not: the agent gets
  a task with the request id, its short summary, the decision and the comment — and none of the
  stored task's own words, so there is nothing in it to carry out.
- ``cancel``: the request is withdrawn in the medium, and handled like a rejection.

Limits, declared:

- The queue is in memory. A request survives a restart while it is pending; a task already
  released to the queue and not yet run does not, like any other queued task.
- A rejected or cancelled task is never queued, and its words do not reach the agent again.
  The summary does, and it is written by whoever asked: it is cut to one short line, not checked.
- Nothing delivers a decision to the conversation it came from. The origin is stored for that;
  no channel can yet be asked to post to it.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from mossy.hitl import discover_media
from mossy.hitl.base import APPROVED, CANCELLED, CHECK_STATUSES, PENDING, REJECTED, Medium
from mossy.hitl.store import ApprovalRequest, ApprovalStore, database_path

if TYPE_CHECKING:
    # Not at import time: the runtime package imports this one.
    from mossy.runtime.core import Runtime
    from mossy.runtime.models import Envelope

logger = logging.getLogger(__name__)

#: Key of the decision in a task's context, on a task that went through an approval.
HITL_CONTEXT_KEY = "hitl"

#: A summary is one line of at most this many characters, however it was written.
SUMMARY_MAX = 200

_NOT_APPROVED_GOAL = """\
Approval request {request_id} was {verdict}{by}.
Summary: {summary}
Comment: {comment}
Nothing was carried out, and nothing is to be carried out for it. Tell the requester the decision."""


class HitlError(RuntimeError):
    """A task could not be sent for approval, or a request could not be handled."""


def serialize_envelope(envelope: Envelope) -> str:
    """The task as text, refused unless reading the text back gives the same task."""
    from mossy.runtime.models import Envelope as EnvelopeModel

    try:
        text = envelope.model_dump_json()
        same = EnvelopeModel.model_validate_json(text) == envelope
    except Exception as exc:
        raise HitlError(f"The task cannot be stored for approval: {exc}") from exc
    if not same:
        raise HitlError(
            "The task cannot be stored for approval: it would not be read back unchanged. "
            "Keep its context to plain JSON values."
        )
    return text


def short_summary(summary: str | None) -> str:
    """One line, at most ``SUMMARY_MAX`` characters."""
    return " ".join(str(summary or "").split())[:SUMMARY_MAX].rstrip()


def opaque(value: Any) -> Any:
    """A channel's own data, refused unless it comes back from JSON as it went in."""
    if value is None:
        return None
    try:
        same = json.loads(json.dumps(value)) == value
    except (TypeError, ValueError) as exc:
        raise HitlError(f"The origin of the request cannot be stored: {exc}") from exc
    if not same:
        raise HitlError("The origin of the request cannot be stored: it is not plain JSON.")
    return value


def resolved_envelope(
    request: ApprovalRequest, status: str, by: str | None, comment: str | None
) -> Envelope:
    """What enters the queue once a request is decided.

    Approved: the stored task, with the decision added to its context and nothing else touched.
    Rejected or cancelled: a new task with the request id, the summary, the decision and the
    comment. None of the stored task's words or context goes into it, except who asked
    (``caller_ids``), which is what keeps the agent to that person's access.
    """
    from mossy.runtime.models import Envelope as EnvelopeModel

    envelope = request.task_envelope()
    decision: dict[str, Any] = {
        "request_id": request.id,
        "status": status,
        "by": by,
        "comment": comment,
        "medium": request.medium,
        "external_id": request.external_id,
    }
    if status == APPROVED:
        return envelope.model_copy(update={"raw": {**envelope.raw, HITL_CONTEXT_KEY: decision}})
    summary = short_summary(request.summary)
    goal = _NOT_APPROVED_GOAL.format(
        request_id=request.id,
        verdict="cancelled" if status == CANCELLED else "rejected",
        by=f" by {by}" if by else "",
        summary=summary or "(none given)",
        comment=comment or "(none)",
    )
    context: dict[str, Any] = {
        HITL_CONTEXT_KEY: {
            "request_id": request.id,
            "status": status,
            "by": by,
            "comment": comment,
            "summary": summary,
        }
    }
    if "caller_ids" in envelope.raw:
        context["caller_ids"] = envelope.raw["caller_ids"]
    return EnvelopeModel(
        payload=goal, priority=envelope.priority, source=envelope.source, raw=context
    )


class HitlFlow:
    """Approval requests of one runtime. The requests live in DuckDB, not here."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        # Requests being opened right now, and requests whose outcome is already on this queue.
        self._opening: set[str] = set()
        self._released: set[str] = set()

    @property
    def store(self) -> ApprovalStore:
        return ApprovalStore(database_path(self.runtime.repo_root))

    def media(self) -> dict[str, type[Medium]]:
        return discover_media(self.runtime.repo_root)

    def medium(self, name: str | None = None) -> Medium:
        """The medium called ``name``, or the only one configured when no name is given."""
        media = self.media()
        if not media:
            raise HitlError("No HITL medium is configured.")
        if name is None:
            if len(media) > 1:
                raise HitlError(f"Several HITL media are configured; name one of: {', '.join(media)}.")
            name = next(iter(media))
        cls = media.get(name)
        if cls is None:
            raise HitlError(f"Unknown HITL medium: {name}")
        return cls()

    # ------------------------------------------------------------------ submit
    async def submit(
        self,
        envelope: Envelope,
        *,
        medium: str | None = None,
        summary: str | None = None,
        requester: str | None = None,
        origin_channel: str | None = None,
        origin_conversation: Any = None,
        dedupe_key: str | None = None,
    ) -> ApprovalRequest:
        """Store the task, open it in the medium, and leave it out of the queue.

        The medium is given the request, whose ``task_text`` is the stored task word for word.
        ``origin_conversation`` is whatever the channel needs to find the conversation again; it
        is kept as it is given and never read here. With a ``dedupe_key`` that a pending request
        already has, that request is returned and nothing new is stored or opened.
        """
        target = self.medium(medium)
        text = serialize_envelope(envelope)
        origin_conversation = opaque(origin_conversation)
        store = self.store
        if dedupe_key:
            existing = store.find_pending(dedupe_key)
            if existing is not None:
                if existing.external_id or existing.id in self._opening:
                    return existing
                # Stored but never opened: the process stopped in between. Open it now.
                return await self._open(self.medium(existing.medium), existing, discard=False)
        now = datetime.now(UTC)
        request = ApprovalRequest(
            id=str(uuid.uuid4()),
            envelope=text,
            medium=target.name,
            summary=short_summary(summary) or None,
            requester=requester,
            origin_channel=origin_channel or envelope.source,
            origin_conversation=origin_conversation,
            dedupe_key=dedupe_key or None,
            created_at=now,
            updated_at=now,
        )
        store.add(request)
        return await self._open(target, request, discard=True)

    async def _open(self, medium: Medium, request: ApprovalRequest, *, discard: bool) -> ApprovalRequest:
        self._opening.add(request.id)
        try:
            external_id = str(await medium.open(request) or "").strip()
            if not external_id:
                raise HitlError("the medium returned no id")
        except Exception as exc:
            if discard:
                # Nobody can decide a request that was never opened: it must not stay pending.
                self.store.delete(request.id)
            raise HitlError(
                f"The approval request could not be opened in {medium.name}: {exc}"
            ) from exc
        finally:
            self._opening.discard(request.id)
        self.store.set_external_id(request.id, external_id)
        return self.store.get(request.id) or request

    # ------------------------------------------------------------------ review
    async def review(self) -> list[ApprovalRequest]:
        """Check every pending request in its medium and act on each decision.

        Returns the requests decided in this pass. A medium that fails, or answers something that
        is not a decision, leaves its request pending: an error is never an approval or a rejection.
        """
        decided: list[ApprovalRequest] = []
        for request in self.store.pending():
            if not request.external_id:
                logger.warning("HITL request %s was never opened in %s", request.id, request.medium)
                continue
            try:
                decision = await self.medium(request.medium).check(request.external_id)
                status = getattr(decision, "status", None)
                if status not in CHECK_STATUSES:
                    raise HitlError(f"{request.medium} answered an unknown status: {status!r}")
            except Exception:
                logger.exception("HITL request %s could not be checked", request.id)
                continue
            if status == PENDING:
                continue
            decided.append(await self._resolve(request, status, decision.by, decision.comment))
        return decided

    async def cancel(
        self, request_id: str, *, by: str | None = None, comment: str | None = None
    ) -> ApprovalRequest:
        """Withdraw a pending request in its medium. The task is handled as not approved."""
        request = self.store.get(request_id)
        if request is None:
            raise HitlError(f"Unknown approval request: {request_id}")
        if request.status != PENDING:
            return request
        if request.external_id:
            await self.medium(request.medium).cancel(request.external_id)
        return await self._resolve(request, CANCELLED, by, comment)

    async def _resolve(
        self, request: ApprovalRequest, status: str, by: str | None, comment: str | None
    ) -> ApprovalRequest:
        # Queue first, record second. If the record fails the request stays pending and is looked
        # at again; ``_released`` keeps that second look from queueing the task twice.
        if request.id not in self._released:
            await self.runtime.submit(resolved_envelope(request, status, by, comment))
            self._released.add(request.id)
        self.store.decide(request.id, status, by, comment)
        return self.store.get(request.id) or request


__all__ = [
    "APPROVED",
    "CANCELLED",
    "HITL_CONTEXT_KEY",
    "HitlError",
    "HitlFlow",
    "PENDING",
    "REJECTED",
    "SUMMARY_MAX",
    "resolved_envelope",
    "serialize_envelope",
    "short_summary",
]
