"""AG-UI (Agent-User Interaction) channel with SSE streaming."""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from typing import TYPE_CHECKING

from ag_ui.core import RunAgentInput, UserMessage
from ag_ui.core.types import TextInputContent
from fastapi import FastAPI
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.ui import SSE_CONTENT_TYPE
from pydantic_ai.ui.ag_ui import AGUIAdapter
from starlette.requests import Request
from starlette.responses import Response

from mossy.runtime.clock import obligate_agent_user_message, utc_context_line
from mossy.runtime.deps import RuntimeDeps

if TYPE_CHECKING:
    from mossy.runtime import Runtime

_AGUI_INSTRUCTIONS = """You are Mossy's web chat assistant.

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


def _agui_model() -> str:
    return (
        os.getenv("PLATFORMER_AGUI_MODEL")
        or os.getenv("PLATFORMER_CLI_MODEL")
        or os.getenv("PLATFORMER_SKILL_MODEL", "openai:gpt-5.4-mini")
    )


def _agui_path() -> str:
    path = os.getenv("AGUI_PATH", "/agui").strip() or "/agui"
    if not path.startswith("/"):
        path = f"/{path}"
    return path.rstrip("/") or "/agui"


def _cors_origins() -> list[str]:
    raw = os.getenv("AGUI_CORS_ORIGINS", "").strip()
    if not raw:
        return []
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def inject_utc_into_run_input(run_input: RunAgentInput) -> RunAgentInput:
    """Prefix the latest user message with Mossy's mandatory UTC context."""
    utc_marker = utc_context_line()
    messages = list(run_input.messages)
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if not isinstance(msg, UserMessage):
            continue
        content = msg.content
        if isinstance(content, str):
            if utc_marker in content:
                return run_input
            messages[idx] = msg.model_copy(
                update={"content": obligate_agent_user_message(content)}
            )
        elif isinstance(content, list):
            new_parts: list[TextInputContent | object] = []
            prefixed = False
            for part in content:
                if not prefixed and isinstance(part, TextInputContent):
                    if utc_marker in part.text:
                        return run_input
                    new_parts.append(
                        part.model_copy(update={"text": obligate_agent_user_message(part.text)})
                    )
                    prefixed = True
                else:
                    new_parts.append(part)
            if prefixed:
                messages[idx] = msg.model_copy(update={"content": new_parts})
        break
    return run_input.model_copy(update={"messages": messages})


class AguiChannel:
    """Expose Mossy over the AG-UI protocol (SSE event stream)."""

    def __init__(self, runtime: "Runtime") -> None:
        self.runtime = runtime
        self.path = _agui_path()
        self.agent = Agent(
            _agui_model(),
            deps_type=RuntimeDeps,
            instructions=_AGUI_INSTRUCTIONS,
            capabilities=runtime.shared_capabilities(exclude_skills={"filesystem"}),
        )
        self.deps = RuntimeDeps(runtime=runtime)

    async def handle_request(self, request: Request) -> Response:
        accept = request.headers.get("accept", SSE_CONTENT_TYPE)
        try:
            run_input = inject_utc_into_run_input(
                AGUIAdapter.build_run_input(await request.body())
            )
        except ValidationError as exc:
            return Response(
                content=json.dumps(exc.errors()),
                media_type="application/json",
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            )

        adapter = AGUIAdapter(
            agent=self.agent,
            run_input=run_input,
            accept=accept,
        )
        return adapter.streaming_response(adapter.run_stream(deps=self.deps))


def register_agui_routes(app: FastAPI, runtime: "Runtime") -> AguiChannel:
    """Mount AG-UI POST endpoint (and optional CORS) on an existing FastAPI app."""
    channel = AguiChannel(runtime)
    route_path = channel.path

    @app.post(route_path)
    @app.post(f"{route_path}/")
    async def agui_run(request: Request) -> Response:
        return await channel.handle_request(request)

    origins = _cors_origins()
    if origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["POST", "OPTIONS"],
            allow_headers=["*"],
        )

    return channel
