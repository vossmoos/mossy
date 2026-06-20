"""FastAPI channel for Mossy."""

from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from mossy.channels.slack.app import ConversationState, TTLStore
from mossy.capabilities.archives import (
    archive_relative_path,
    archive_root,
    ensure_zip_file_path,
    list_archive_files,
    resolve_archive_path,
    resolve_relative_archive_file_path,
)
from mossy.runtime.agent_run import run_agent_with_utc
from mossy.runtime.deps import RuntimeDeps
from mossy.runtime.models import Envelope, TaskStatus

if TYPE_CHECKING:
    from mossy.runtime import Runtime


_CHAT_INSTRUCTIONS = """You are Mossy's browser chat assistant.

You have access to agentic skills through the skills tools. Use skills immediately when they help
answer the user or perform an action. Use the system-queue skill when work should be queued,
inspected, cancelled, scheduled, or allowed to continue independently. Do not enqueue by default:
answer directly when the request can be resolved in the chat turn.

Queued tasks do not automatically post their final result back to the web client yet. When you enqueue
work, include the task id in your reply and tell the user how to check on it.

Each user message is prefixed with `[System UTC now: …]` — use it as the authoritative clock for
relative scheduling ("in 1 minute", "tomorrow"): compute scheduled_for in UTC from that line, not from
memory.

Keep replies concise and readable in a chat UI."""


def _chat_model() -> str:
    return (
        os.getenv("PLATFORMER_CHAT_MODEL")
        or os.getenv("PLATFORMER_CLI_MODEL")
        or os.getenv("PLATFORMER_SKILL_MODEL", "openai:gpt-5.4-mini")
    )


def _normalize_path(path: str) -> str:
    return path.rstrip("/") or "/"


def _configured_api_key() -> str:
    return (os.getenv("MOSSY_API_KEY") or os.getenv("HTTP_API_KEY", "")).strip()


def _auth_exempt_paths() -> frozenset[str]:
    """Paths that skip API key auth. /ui is public so the browser can load the
    chat page before the user enters their key."""
    return frozenset({
        _normalize_path("/health"),
        _normalize_path("/ui"),
        _normalize_path("/ui/"),
    })


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


class ChatBody(BaseModel):
    message: str
    thread_id: str | None = None
    context: dict | None = None


def _trim_message_history(history: list[ModelMessage], max_messages: int) -> list[ModelMessage]:
    if max_messages <= 0:
        return []
    return history[-max_messages:]


def _last_assistant_reply(history: list[ModelMessage]) -> str | None:
    """Return the latest assistant text from pydantic-ai message history."""
    for msg in reversed(history):
        if not isinstance(msg, ModelResponse):
            continue
        text = "".join(part.content for part in msg.parts if isinstance(part, TextPart))
        text = text.strip()
        if text:
            return text
    return None


def create_app(runtime: "Runtime", *, enable_agui: bool = True) -> FastAPI:
    app = FastAPI(title="mossy")

    api_key = _configured_api_key()
    if api_key:
        app.add_middleware(
            ApiKeyMiddleware,
            api_key=api_key,
            exempt_paths=_auth_exempt_paths(),
        )
        print("HTTP API key auth enabled (MOSSY_API_KEY). /health is public.", file=sys.stderr, flush=True)
    else:
        print(
            "HTTP API key auth disabled: set MOSSY_API_KEY to protect /run, /chat, /agui, /queue, and /archive/files.",
            file=sys.stderr,
            flush=True,
        )

    _chat_agent = Agent(
        _chat_model(),
        deps_type=RuntimeDeps,
        instructions=_CHAT_INSTRUCTIONS,
        capabilities=runtime.shared_capabilities(exclude_skills={"filesystem"}),
    )
    _chat_deps = RuntimeDeps(runtime=runtime)
    _chat_histories: TTLStore[ConversationState] = TTLStore(
        ttl_seconds=float(os.getenv("CHAT_HISTORY_TTL_SECONDS", str(2 * 60 * 60))),
        max_entries=int(os.getenv("CHAT_HISTORY_MAX_THREADS", "500")),
    )
    _chat_max_history = int(os.getenv("CHAT_HISTORY_MAX_MESSAGES", "40"))

    @app.post("/chat")
    async def chat(body: ChatBody) -> dict:
        thread_id = body.thread_id or str(uuid.uuid4())
        state = await _chat_histories.get_or_create(thread_id, ConversationState)
        async with state.lock:
            run = await run_agent_with_utc(
                _chat_agent,
                body.message,
                deps=_chat_deps,
                message_history=_trim_message_history(state.history, _chat_max_history),
            )
            state.history.extend(run.new_messages())
            state.history[:] = _trim_message_history(state.history, _chat_max_history)
        return {"reply": run.output, "thread_id": thread_id}

    @app.get("/chat/last")
    async def chat_last(thread_id: str = Query(...)) -> dict:
        state = await _chat_histories.get(thread_id)
        if state is None:
            raise HTTPException(status_code=404, detail="unknown thread")
        async with state.lock:
            reply = _last_assistant_reply(state.history)
        return {"reply": reply, "thread_id": thread_id}

    if enable_agui:
        from mossy.channels.agui.app import register_agui_routes

        channel = register_agui_routes(app, runtime)
        print(f"AG-UI channel enabled at POST {channel.path}", file=sys.stderr, flush=True)

    from mossy.channels.web.app import register_web_routes

    register_web_routes(app)
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

    archive_files_root = archive_root(runtime.repo_root)

    @app.get("/archive/files")
    async def archive_file_list(
        path: str = "",
        recursive: bool = False,
        limit: int = 200,
    ) -> dict:
        try:
            return list_archive_files(
                path,
                root=archive_files_root,
                recursive=recursive,
                limit=limit,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/archive/files/{file_path:path}")
    async def archive_file_download(file_path: str) -> FileResponse:
        try:
            target = resolve_archive_path(file_path, root=archive_files_root)
            ensure_zip_file_path(target)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        return FileResponse(target, filename=target.name)

    @app.delete("/archive/files/{file_path:path}")
    async def archive_file_delete(file_path: str) -> dict:
        try:
            target = resolve_relative_archive_file_path(
                file_path,
                root=archive_files_root,
                must_exist=True,
            )
            ensure_zip_file_path(target)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        stat = target.stat()
        relative_path = archive_relative_path(target, root=archive_files_root)
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

    return app
