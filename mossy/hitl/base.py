"""HITL media — where a person approves or rejects a Mossy task.

A medium is the place a request is put in front of a person and the decision is read back: a
task in a project tool, a chat message with two buttons. The core ships none. A repo registers
its own the way it registers duties: a class with a ``name`` and ``@register``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mossy.hitl.store import ApprovalRequest

REGISTRY: dict[str, type[Medium]] = {}

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
CANCELLED = "cancelled"

#: What ``Medium.check`` may answer. Cancelling is something Mossy does, not something it reads.
CHECK_STATUSES = (PENDING, APPROVED, REJECTED)


@dataclass(frozen=True)
class Decision:
    """What a medium says about one request: its status, and who decided and with what words."""

    status: str
    by: str | None = None
    comment: str | None = None


class Medium:
    """Three operations against the place where people decide: ``open``, ``check``, ``cancel``."""

    name: str

    @classmethod
    def configured(cls) -> bool:
        """False when the medium cannot work here, e.g. its token is missing. It is then absent."""
        return True

    async def open(self, request: ApprovalRequest) -> str:
        """Put the request in front of a person. Returns the medium's own id for it."""
        raise NotImplementedError

    async def check(self, external_id: str) -> Decision:
        """Read the state of one request: pending, approved or rejected. Changes nothing."""
        raise NotImplementedError

    async def cancel(self, external_id: str) -> None:
        """Withdraw a request nobody has decided yet."""
        raise NotImplementedError

    def link(self, external_id: str) -> str | None:
        """Where a person can open the request, when that is not the id itself."""
        return None


def register(cls: type[Medium]) -> type[Medium]:
    REGISTRY[cls.name] = cls
    return cls
