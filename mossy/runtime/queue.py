"""Priority queue with cooperative dependency handling."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from mossy.runtime.models import Task


class TaskQueue:
    def __init__(self) -> None:
        self._q: asyncio.PriorityQueue[tuple[int, float, int, Task]] = asyncio.PriorityQueue()
        self._defer = 0.0
        self._seq = 0

    async def push(self, task: Task) -> None:
        self._seq += 1
        await self._q.put((task.priority, task.created_at.timestamp(), self._seq, task))

    async def pop(self, ready: Callable[[Task], bool]) -> Task:
        while True:
            prio, ts, seq, task = await self._q.get()
            if ready(task):
                return task
            self._defer += 1e-6
            self._seq += 1
            await self._q.put((prio, ts + self._defer, self._seq, task))
            await asyncio.sleep(0.01)

    def size(self) -> int:
        return self._q.qsize()

    def is_empty(self) -> bool:
        return self._q.empty()
