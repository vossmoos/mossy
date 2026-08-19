"""Duty framework — cadence checks, programmatic evaluate, then queued goals."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mossy.runtime.core import Runtime

REGISTRY: dict[str, type[Duty]] = {}


@dataclass
class EnqueueRequest:
    """Work to put on the Mossy queue.

    ``evaluate_duty`` — run this duty's ``evaluate()`` (no LLM).
    ``goal`` — a normal task the worker resolves with skills.
    """

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    goal: str = ""
    dedupe_key: str | None = None
    not_before: datetime | None = None
    # Default matches Priority.BACKGROUND so duties stay behind user work.
    priority: int = 3
    context: dict[str, Any] = field(default_factory=dict)


class Duty:
    """Clock-driven job: ``check`` is cadence; ``evaluate`` does code, then may enqueue goals."""

    name: str

    async def check(self, now: datetime) -> list[EnqueueRequest]:
        """Cadence only — fast, deterministic, no LLM. Runs in the sentinel loop."""
        raise NotImplementedError

    async def evaluate(
        self, payload: dict[str, Any], runtime: Runtime
    ) -> list[EnqueueRequest]:
        """Programmatic work (no LLM). Return goal tasks for the skill worker, or []."""
        return []


def register(cls: type[Duty]) -> type[Duty]:
    REGISTRY[cls.name] = cls
    return cls
