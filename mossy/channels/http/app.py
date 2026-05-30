"""FastAPI channel for Mossy."""

from __future__ import annotations

import sys
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from mossy.runtime.models import Envelope, TaskStatus

if TYPE_CHECKING:
    from mossy.runtime import Runtime


class RunBody(BaseModel):
    payload: str
    priority: int | None = None
    scheduled_for: datetime | None = None


def create_app(runtime: "Runtime", *, enable_agui: bool = True) -> FastAPI:
    app = FastAPI(title="mossy")

    if enable_agui:
        from mossy.channels.agui.app import register_agui_routes

        channel = register_agui_routes(app, runtime)
        print(f"AG-UI channel enabled at POST {channel.path}", file=sys.stderr, flush=True)

    @app.post("/run")
    async def run_task(body: RunBody) -> dict[str, str | None]:
        task = await runtime.submit(
            Envelope(
                payload=body.payload,
                priority=body.priority,
                scheduled_for=body.scheduled_for,
                source="http",
            )
        )
        return {
            "task_id": task.id,
            "not_before": task.not_before.isoformat() if task.not_before else None,
        }

    @app.get("/status/{task_id}")
    async def status(task_id: str) -> dict:
        task = runtime.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="unknown task")
        return {
            "status": task.status.value,
            "not_before": task.not_before.isoformat() if task.not_before else None,
            "result": task.result,
            "error": task.error,
        }

    @app.get("/queue")
    async def queue_view() -> dict:
        pending = [
            {
                "id": task.id,
                "priority": task.priority,
                "goal": task.goal,
                "not_before": task.not_before.isoformat() if task.not_before else None,
            }
            for task in runtime.list_tasks(status=TaskStatus.PENDING.value)
        ]
        return {"tasks": pending}

    @app.get("/health")
    async def health() -> dict[str, bool]:
        if not runtime.healthy():
            raise HTTPException(status_code=503, detail="starting")
        return {"ok": True}

    return app
