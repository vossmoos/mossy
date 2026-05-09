"""FastAPI channel for Mossy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from mossy.runtime.models import Envelope, TaskStatus

if TYPE_CHECKING:
    from mossy.runtime import Runtime


class RunBody(BaseModel):
    payload: str
    priority: int | None = None


def create_app(runtime: "Runtime") -> FastAPI:
    app = FastAPI(title="mossy")

    @app.post("/run")
    async def run_task(body: RunBody) -> dict[str, str]:
        task = await runtime.submit(
            Envelope(payload=body.payload, priority=body.priority, source="http")
        )
        return {"task_id": task.id}

    @app.get("/status/{task_id}")
    async def status(task_id: str) -> dict:
        task = runtime.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="unknown task")
        return {"status": task.status.value, "result": task.result, "error": task.error}

    @app.get("/queue")
    async def queue_view() -> dict:
        pending = [
            {"id": task.id, "priority": task.priority, "goal": task.goal}
            for task in runtime.list_tasks(status=TaskStatus.PENDING.value)
        ]
        return {"tasks": pending}

    @app.get("/health")
    async def health() -> dict[str, bool]:
        if not runtime.healthy():
            raise HTTPException(status_code=503, detail="starting")
        return {"ok": True}

    return app
