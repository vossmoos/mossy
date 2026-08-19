"""Daily ops digest: evaluate() counts the queue, then enqueues a print task."""

from __future__ import annotations

import os
from datetime import datetime
from typing import TYPE_CHECKING, Any

from mossy.duties.base import Duty, EnqueueRequest, register
from mossy.runtime.models import TaskStatus

if TYPE_CHECKING:
    from mossy.runtime.core import Runtime


def _enabled() -> bool:
    return os.environ.get("OPS_DIGEST_ENABLED", "false").strip().lower() == "true"


def _hour_utc() -> int:
    try:
        return max(0, min(23, int(os.environ.get("OPS_DIGEST_HOUR_UTC", "13"))))
    except ValueError:
        return 13


@register
class OpsDigest(Duty):
    name = "ops_digest"

    async def check(self, now: datetime) -> list[EnqueueRequest]:
        if not _enabled():
            return []
        is_due = now.hour == _hour_utc() and now.minute == 0 and now.second < 8
        if not is_due:
            return []
        day = now.strftime("%Y-%m-%d")
        return [
            EnqueueRequest(
                kind="evaluate_duty",
                payload={"duty": self.name, "day": day},
                dedupe_key=f"ops_digest:{day}",
            )
        ]

    async def evaluate(
        self, payload: dict[str, Any], runtime: Runtime
    ) -> list[EnqueueRequest]:
        day = str(payload.get("day") or "")
        counts = {
            TaskStatus.PENDING: 0,
            TaskStatus.RUNNING: 0,
            TaskStatus.FAILED: 0,
            TaskStatus.DONE: 0,
        }
        skip_id = getattr(runtime, "_active_task_id", None)
        for task in runtime.list_tasks():
            if skip_id and task.id == skip_id:
                continue
            if task.status in counts:
                counts[task.status] += 1
        line = (
            f"Mossy ops {day}: {counts[TaskStatus.PENDING]} pending, "
            f"{counts[TaskStatus.RUNNING]} running, "
            f"{counts[TaskStatus.FAILED]} failed, "
            f"{counts[TaskStatus.DONE]} done"
        )
        return [
            EnqueueRequest(
                kind="goal",
                goal=(
                    f"Print this one-line ops digest to stderr exactly, and nothing else: {line}"
                ),
                dedupe_key=f"ops_digest:print:{day}",
                context={"duty": self.name, "day": day, "digest": line},
            )
        ]
