"""Current-task tools for queued worker agents."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic_ai.capabilities.toolset import Toolset
from pydantic_ai.toolsets import FunctionToolset

from mossy.runtime.models import Priority, Task


def worker_state_capability(task: Task) -> Toolset:
    """Expose the active task's mutable state to the worker agent."""

    async def current_task() -> dict[str, Any]:
        """Return the active task."""
        return task.model_dump(mode="json")

    async def record_task_result(result: dict[str, Any]) -> str:
        """Persist structured result data for the active task."""
        task.result = dict(result)
        return "recorded"

    async def set_follow_up_goal(
        goal: str,
        priority: int = int(Priority.AUTONOMOUS),
        depends_on: list[str] | None = None,
        scheduled_for: datetime | None = None,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Request one follow-up task after the current task completes.

        Use scheduled_for only for future work. It must be an absolute UTC datetime.
        """
        data = dict(task.result or {})
        data["follow_up_goal"] = goal
        data["follow_up_priority"] = priority
        data["follow_up_depends_on"] = depends_on or []
        data["follow_up_not_before"] = scheduled_for
        data["follow_up_context"] = context or {}
        task.result = data
        return "scheduled"

    return Toolset(
        FunctionToolset(
            [current_task, record_task_result, set_follow_up_goal],
            id="worker-state",
            instructions=(
                "Use these tools when running a queued task to inspect the active task, "
                "persist structured task results, or request a follow-up task."
            ),
        )
    )
