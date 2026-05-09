"""Runtime control tools exposed as a Pydantic AI capability."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic_ai.capabilities.toolset import Toolset
from pydantic_ai.toolsets import FunctionToolset

from mossy.runtime.models import Priority

if TYPE_CHECKING:
    from mossy.runtime.core import Runtime


def runtime_control_capability(runtime: "Runtime") -> Toolset:
    """Expose queue and task lifecycle operations to any agent."""

    async def enqueue_task(
        goal: str,
        priority: int = int(Priority.AUTONOMOUS),
        depends_on: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a queued task and return its id."""
        task = await runtime.submit_goal(goal, priority=priority, depends_on=depends_on or [])
        return {
            "id": task.id,
            "status": task.status.value,
            "priority": task.priority,
            "goal": task.goal,
            "depends_on": task.depends_on,
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
        task = runtime.cancel_task(task_id)
        if task is None:
            return {"error": "unknown task", "id": task_id}
        return {"id": task.id, "status": task.status.value}

    return Toolset(
        FunctionToolset(
            [enqueue_task, get_task_status, list_tasks, queue_depth, cancel_task],
            id="runtime-control",
            instructions=(
                "Use these tools to enqueue background work and inspect or manage Mossy's task queue. "
                "For immediate user-facing answers, prefer answering directly or using a relevant skill."
            ),
        )
    )

