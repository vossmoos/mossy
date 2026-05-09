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
            skipped: list[tuple[int, float, int, Task]] = []
            item = await self._q.get()

            while True:
                _prio, _ts, _seq, task = item
                if ready(task):
                    await self._push_skipped(skipped)
                    return task

                skipped.append(item)
                if self._q.empty():
                    break
                item = await self._q.get()

            await self._push_skipped(skipped)
            await asyncio.sleep(0.01)

    async def _push_skipped(self, skipped: list[tuple[int, float, int, Task]]) -> None:
        for prio, ts, _seq, task in skipped:
            self._defer += 1e-6
            self._seq += 1
            await self._q.put((prio, ts + self._defer, self._seq, task))

    def size(self) -> int:
        return self._q.qsize()

    def is_empty(self) -> bool:
        return self._q.empty()
