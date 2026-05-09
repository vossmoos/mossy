"""Mossy runtime: inbox, queue, worker agent, and task lifecycle."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic_ai import Agent
from pydantic_ai_skills import SkillsCapability

from mossy.capabilities.personality import personality_capability
from mossy.capabilities.system_queue import system_queue_capability
from mossy.capabilities.worker_state import worker_state_capability
from mossy.runtime.agent_run import run_agent_with_utc
from mossy.runtime.deps import RuntimeDeps
from mossy.runtime.models import Envelope, Priority, Task, TaskStatus
from mossy.runtime.queue import TaskQueue
from mossy.skills.selection import SkillSelection, skills_capability


class Runtime:
    def __init__(self, skills_root: Path | None = None) -> None:
        root = Path(__file__).resolve().parents[1]
        self.skills_root = skills_root or (root / "skills")
        self.queue = TaskQueue()
        self.inbox: asyncio.Queue[Envelope] = asyncio.Queue()
        self.tasks: dict[str, Task] = {}
        self._live_inbox = asyncio.Event()
        self._live_work = asyncio.Event()
        self._worker_model = os.getenv("PLATFORMER_SKILL_MODEL", "openai:gpt-5.4-mini")
        self._worker: Agent[RuntimeDeps, str] | None = None

    def skills_capability(
        self,
        *,
        allow_skills: SkillSelection = "all",
        exclude_skills: Collection[str] | None = None,
    ) -> SkillsCapability:
        return skills_capability(
            self.skills_root,
            allow=allow_skills,
            exclude=exclude_skills,
            auto_reload=True,
        )

    def shared_capabilities(
        self,
        *,
        allow_skills: SkillSelection = "all",
        exclude_skills: Collection[str] | None = None,
    ) -> list[Any]:
        capabilities: list[Any] = [
            personality_capability(),
            self.skills_capability(
                allow_skills=allow_skills,
                exclude_skills=exclude_skills,
            ),
        ]
        if self._allows_skill_tools(
            "system-queue",
            allow_skills=allow_skills,
            exclude_skills=exclude_skills,
        ):
            capabilities.append(system_queue_capability(self))
        return capabilities

    def _allows_skill_tools(
        self,
        skill_name: str,
        *,
        allow_skills: SkillSelection,
        exclude_skills: Collection[str] | None,
    ) -> bool:
        if exclude_skills and skill_name in exclude_skills:
            return False
        if allow_skills == "all":
            return True
        return skill_name in allow_skills

    @property
    def worker(self) -> Agent[RuntimeDeps, str]:
        if self._worker is None:
            self._worker = Agent(
                self._worker_model,
                deps_type=RuntimeDeps,
                instructions=(
                    "You are Mossy's queued task worker. Each run begins with a line "
                    "'[System UTC now: …]' — treat it as the authoritative current time in UTC. "
                    "Resolve the active task by using skills when useful. Skills are loaded progressively: "
                    "select a relevant skill, load it, follow its instructions, and call record_task_result "
                    "when there is structured result data to save. Enqueue follow-up work only when the task "
                    "truly requires it."
                ),
            )
        return self._worker

    async def submit(self, envelope: Envelope) -> Task:
        task = self.task_from_envelope(envelope)
        self.tasks[task.id] = task
        await self.queue.push(task)
        return task

    async def submit_goal(
        self,
        goal: str,
        *,
        priority: int = int(Priority.AUTONOMOUS),
        depends_on: list[str] | None = None,
        context: dict[str, Any] | None = None,
        not_before: datetime | None = None,
    ) -> Task:
        task = Task(
            id=str(uuid.uuid4()),
            goal=goal.strip(),
            priority=priority,
            depends_on=depends_on or [],
            not_before=not_before,
            context=context or {},
        )
        self.tasks[task.id] = task
        await self.queue.push(task)
        return task

    def task_from_envelope(self, envelope: Envelope) -> Task:
        priority = envelope.priority if envelope.priority is not None else int(Priority.USER_INPUT)
        context = dict(envelope.raw)
        context["source"] = envelope.source
        return Task(
            id=envelope.task_id or str(uuid.uuid4()),
            goal=envelope.payload.strip(),
            priority=int(priority),
            not_before=envelope.scheduled_for,
            context=context,
        )

    def get_task(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    def list_tasks(self, status: str | None = None) -> list[Task]:
        if status is None:
            return list(self.tasks.values())
        return [task for task in self.tasks.values() if task.status.value == status]

    def queue_depth(self) -> int:
        return self.queue.size()

    def cancel_task(self, task_id: str) -> Task | None:
        task = self.tasks.get(task_id)
        if task is None:
            return None
        if task.status in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED):
            return task
        task.status = TaskStatus.CANCELLED
        return task

    def healthy(self) -> bool:
        return self._live_inbox.is_set() and self._live_work.is_set()

    def _ready(self, task: Task) -> bool:
        if task.status == TaskStatus.CANCELLED:
            return True
        if task.not_before is not None and datetime.now(UTC) < task.not_before:
            return False
        for dep in task.depends_on:
            parent = self.tasks.get(dep)
            if parent is None or parent.status != TaskStatus.DONE:
                return False
        return True

    async def inbox_loop(self) -> None:
        self._live_inbox.set()
        while True:
            envelope = await self.inbox.get()
            await self.submit(envelope)

    async def work_loop(self) -> None:
        self._live_work.set()
        while True:
            task = await self.queue.pop(self._ready)
            if task.status == TaskStatus.CANCELLED:
                continue
            await self.execute(task)
            await self.think_next(task)

    async def execute(self, task: Task) -> None:
        task.status = TaskStatus.RUNNING
        task.error = None
        task.result = {}
        try:
            deps = RuntimeDeps(runtime=self, task=task)
            capabilities = [worker_state_capability(task), *self.shared_capabilities()]
            run = await run_agent_with_utc(
                self.worker, task.goal, deps=deps, capabilities=capabilities
            )
            if run.output is not None:
                task.result = {**(task.result or {}), "model_output": run.output}
            task.status = TaskStatus.DONE
        except Exception as exc:  # noqa: BLE001
            task.status = TaskStatus.FAILED
            task.error = str(exc)
            task.result = {**(task.result or {}), "error": str(exc)}

    async def think_next(self, task: Task) -> None:
        if os.getenv("PLATFORMER_DISABLE_AUTONOMOUS"):
            return
        data = task.result or {}
        if task.status == TaskStatus.DONE and data.get("follow_up_goal"):
            child = Task(
                id=str(uuid.uuid4()),
                goal=str(data["follow_up_goal"]),
                priority=int(data.get("follow_up_priority", Priority.AUTONOMOUS)),
                depends_on=list(data.get("follow_up_depends_on") or []),
                not_before=data.get("follow_up_not_before"),
                context=dict(data.get("follow_up_context") or {}),
            )
            self.tasks[child.id] = child
            await self.queue.push(child)
        elif self.queue.is_empty():
            idle_goal = os.getenv("PLATFORMER_IDLE_GOAL", "").strip()
            if idle_goal and task.priority != Priority.IDLE:
                idle = Task(id=str(uuid.uuid4()), goal=idle_goal, priority=Priority.IDLE)
                self.tasks[idle.id] = idle
                await self.queue.push(idle)

    async def start(self) -> None:
        await asyncio.gather(self.inbox_loop(), self.work_loop())
