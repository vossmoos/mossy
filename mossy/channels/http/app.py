"""FastAPI channel for Mossy."""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from mossy.capabilities.file_sharing import (
    list_shared,
    resolve_relative_shared_file_path,
    resolve_shared_path,
    share_relative_path,
    share_root,
)
from mossy.runtime.models import Envelope, TaskStatus

if TYPE_CHECKING:
    from mossy.runtime import Runtime


def _normalize_path(path: str) -> str:
    return path.rstrip("/") or "/"


def _configured_api_key() -> str:
    return (os.getenv("MOSSY_API_KEY") or os.getenv("HTTP_API_KEY", "")).strip()


def _auth_exempt_paths(*, extra: frozenset[str] = frozenset()) -> frozenset[str]:
    """Paths that skip API key auth. /ui is public so the browser can load the
    chat page before the user enters their key."""
    return frozenset({
        _normalize_path("/health"),
        _normalize_path("/ui"),
        _normalize_path("/ui/"),
    }) | extra


def _request_api_key(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("X-API-Key") or request.headers.get("X-Mossy-API-Key") or "").strip()


class ApiKeyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, api_key: str, exempt_paths: frozenset[str]) -> None:
        super().__init__(app)
        self.api_key = api_key
        self.exempt_paths = exempt_paths

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method == "OPTIONS":
            return await call_next(request)

        if _normalize_path(request.url.path) in self.exempt_paths:
            return await call_next(request)

        if _request_api_key(request) != self.api_key:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)

        return await call_next(request)


class RunBody(BaseModel):
    payload: str
    priority: int | None = None
    scheduled_for: datetime | None = None


def create_app(runtime: "Runtime", *, enable_agui: bool = True) -> FastAPI:
    app = FastAPI(title="mossy")

    from mossy.channels.jira_webhook import (
        configured_webhook_path,
        configured_webhook_secret,
        register_jira_webhook_routes,
    )

    jira_webhook_exempt = frozenset()
    if configured_webhook_secret():
        jira_path = _normalize_path(configured_webhook_path())
        jira_webhook_exempt = frozenset({jira_path})

    api_key = _configured_api_key()
    if api_key:
        app.add_middleware(
            ApiKeyMiddleware,
            api_key=api_key,
            exempt_paths=_auth_exempt_paths(extra=jira_webhook_exempt),
        )
        if jira_webhook_exempt:
            print(
                "HTTP API key auth enabled (MOSSY_API_KEY). /health and the Jira webhook are public.",
                file=sys.stderr,
                flush=True,
            )
        else:
            print("HTTP API key auth enabled (MOSSY_API_KEY). /health is public.", file=sys.stderr, flush=True)
    else:
        print(
            "HTTP API key auth disabled: set MOSSY_API_KEY to protect /run, /agui, /queue, and /files.",
            file=sys.stderr,
            flush=True,
        )

    agui_path = "/agui"
    if enable_agui:
        from mossy.channels.agui.app import register_agui_routes

        channel = register_agui_routes(app, runtime)
        agui_path = channel.path
        print(f"AG-UI channel enabled at POST {channel.path}", file=sys.stderr, flush=True)

    from mossy.channels.web.app import register_web_routes

    register_web_routes(app, agui_path=agui_path)
    print("Web UI channel enabled at GET /ui", file=sys.stderr, flush=True)

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

    shared_files_root = share_root(runtime.repo_root)

    @app.get("/files")
    async def shared_file_list(
        path: str = "",
        recursive: bool = False,
        limit: int = 200,
    ) -> dict:
        try:
            return list_shared(
                path,
                root=shared_files_root,
                recursive=recursive,
                limit=limit,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/files/{file_path:path}")
    async def shared_file_download(file_path: str) -> FileResponse:
        try:
            target = resolve_shared_path(file_path, root=shared_files_root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        return FileResponse(target, filename=target.name)

    @app.delete("/files/{file_path:path}")
    async def shared_file_delete(file_path: str) -> dict:
        try:
            target = resolve_relative_shared_file_path(
                file_path,
                root=shared_files_root,
                must_exist=True,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        stat = target.stat()
        relative_path = share_relative_path(target, root=shared_files_root)
        target.unlink()
        return {
            "ok": True,
            "path": relative_path,
            "bytes_deleted": stat.st_size,
        }

    @app.get("/health")
    async def health() -> dict[str, bool]:
        if not runtime.healthy():
            raise HTTPException(status_code=503, detail="starting")
        return {"ok": True}

    register_jira_webhook_routes(app, runtime)

    return app
