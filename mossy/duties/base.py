"""Duty framework — cadence checks and queued evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mossy.runtime.core import Runtime

REGISTRY: dict[str, type[Duty]] = {}


@dataclass
class EnqueueRequest:
    """Work the sentinel or a duty's evaluate() wants put on the Mossy queue."""

    kind: str
    payload: dict[str, Any]
    dedupe_key: str | None = None
    not_before: datetime | None = None
    # Default matches Priority.BACKGROUND so duties stay behind user work.
    priority: int = 3


class Duty:
    """Proactive job: `check` is clock-only; `evaluate` runs as a queued task."""

    name: str

    async def check(self, now: datetime) -> list[EnqueueRequest]:
        """Cadence only — fast, deterministic, no LLM. Runs in the sentinel loop."""
        raise NotImplementedError

    async def evaluate(
        self, payload: dict[str, Any], runtime: Runtime
    ) -> list[EnqueueRequest]:
        """Queued work. May enqueue follow-up tasks (including LLM goals)."""
        return []


def register(cls: type[Duty]) -> type[Duty]:
    REGISTRY[cls.name] = cls
    return cls
