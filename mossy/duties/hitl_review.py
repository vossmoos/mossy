"""Reviews pending approval requests: once a minute, and only while there are any.

``check`` reads the clock, the registered media and the local DuckDB file. It never calls a
medium: every call to a medium is in ``evaluate``, which runs on the queue.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mossy.duties.base import Duty, EnqueueRequest, register
from mossy.hitl import discover_media
from mossy.hitl.store import ApprovalStore, database_path

if TYPE_CHECKING:
    from mossy.runtime.core import Runtime

#: Layout: ``<repo>/mossy/duties/<this file>`` — the repo root is two up, as in the runtime.
_REPO_ROOT = Path(__file__).resolve().parents[2]

POLL_SECONDS = 60


@register
class HitlReview(Duty):
    name = "hitl_review"

    def __init__(self) -> None:
        self._window: int | None = None

    async def check(self, now: datetime) -> list[EnqueueRequest]:
        window = int(now.timestamp()) // POLL_SECONDS
        if window == self._window:
            return []
        self._window = window
        if not discover_media(_REPO_ROOT):
            return []
        if not ApprovalStore(database_path(_REPO_ROOT)).has_pending():
            return []
        return [
            EnqueueRequest(
                kind="evaluate_duty",
                payload={"duty": self.name},
                dedupe_key=f"hitl_review:{window}",
            )
        ]

    async def evaluate(
        self, payload: dict[str, Any], runtime: Runtime
    ) -> list[EnqueueRequest]:
        await runtime.hitl.review()
        return []
