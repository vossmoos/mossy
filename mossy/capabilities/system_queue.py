"""System queue tools exposed for the system-queue skill."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic_ai.capabilities.toolset import Toolset
from pydantic_ai.toolsets import FunctionToolset

from mossy.runtime.models import Priority, TaskStatus

if TYPE_CHECKING:
    from mossy.runtime.core import Runtime


def system_queue_capability(runtime: "Runtime") -> Toolset:
    """Expose queue and task lifecycle operations for the system-queue skill."""

    async def enqueue_task(
        goal: str,
        priority: int = int(Priority.AUTONOMOUS),
        depends_on: list[str] | None = None,
        scheduled_for: datetime | None = None,
    ) -> dict[str, Any]:
        """Create a queued task and return its id.

        Use scheduled_for only for future work. It must be an absolute UTC datetime.
        """
        task = await runtime.submit_goal(
            goal,
            priority=priority,
            depends_on=depends_on or [],
            not_before=scheduled_for,
        )
        return {
            "id": task.id,
            "status": task.status.value,
            "priority": task.priority,
            "goal": task.goal,
            "depends_on": task.depends_on,
            "not_before": task.not_before.isoformat() if task.not_before else None,
        }

    async def get_task_status(task_id: str) -> dict[str, Any]:
        """Return the current state of one task."""
        task = runtime.get_task(task_id)
        if task is None:
            return {"error": "unknown task", "id": task_id}
        return task.model_dump(mode="json")

    async def list_tasks(status: str | None = None) -> list[dict[str, Any]]:
        """List known tasks, optionally filtered by status."""
        return [task.model_dump(mode="json") for task in runtime.list_tasks(status=status)]

    async def queue_depth() -> int:
        """Return the approximate pending queue depth."""
        return runtime.queue_depth()

    async def cancel_task(task_id: str) -> dict[str, Any]:
        """Mark a task as cancelled if it has not already completed."""
        task = runtime.get_task(task_id)
        if task is None:
            return {"error": "unknown task", "id": task_id}
        if task.status == TaskStatus.DONE:
            return {
                "id": task.id,
                "status": task.status.value,
                "cancelled": False,
                "reason": "already_completed",
            }
        if task.status == TaskStatus.FAILED:
            return {
                "id": task.id,
                "status": task.status.value,
                "cancelled": False,
                "reason": "already_failed",
            }
        if task.status == TaskStatus.CANCELLED:
            return {
                "id": task.id,
                "status": task.status.value,
                "cancelled": False,
                "reason": "already_cancelled",
            }
        updated = runtime.cancel_task(task_id)
        if updated is None:
            return {"error": "unknown task", "id": task_id}
        return {
            "id": updated.id,
            "status": updated.status.value,
            "cancelled": updated.status == TaskStatus.CANCELLED,
            "reason": "cancelled",
        }

    return Toolset(
        FunctionToolset(
            [enqueue_task, get_task_status, list_tasks, queue_depth, cancel_task],
            id="system-queue",
            instructions=(
                "These tools implement the system-queue skill. Use them to enqueue background work "
                "and inspect or manage Mossy's task queue. "
                "For future tasks, pass scheduled_for as an absolute UTC datetime. "
                "For immediate user-facing answers, prefer answering directly or using a relevant skill."
            ),
        )
    )
