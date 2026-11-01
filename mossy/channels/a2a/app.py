"""A2A (Agent-to-Agent) channel for Mossy.

Lets another agent discover this one and ask it questions over the
[A2A protocol](https://a2a-protocol.org). Discovery is an agent card at
``GET /a2a/.well-known/agent-card.json``; tasks go to ``POST /a2a/``.

- **No shared capabilities.** The agent gets ``a2a_toolset()`` and nothing else, not
  ``runtime.shared_capabilities()``, which carries the skills and every script that writes.
  The built-in tools are read-only; ``MOSSY_A2A_TOOLS`` adds your own.
- **The key is checked here, not only by the HTTP middleware**, which leaves every route open
  when no key is set. Every request except the agent card needs ``Authorization: Bearer <key>``,
  and with no key configured every such request is refused.

If the model cannot be built, or ``fasta2a`` is not installed, the channel is not mounted and
stderr says why. Otherwise fasta2a's worker would resolve the model in its lifespan, and a bad
model would stop the whole HTTP app from starting.

``agent_to_a2a`` returns a Starlette app with a background worker whose lifespan Starlette does
not run for a mounted app, so ``register_a2a_routes`` returns it and ``create_app`` enters it.
"""

from __future__ import annotations

import hmac
import importlib
import os
import sys
from typing import TYPE_CHECKING, Any

from pydantic_ai import Agent, Tool
from pydantic_ai.models import Model, infer_model
from pydantic_ai.toolsets import FunctionToolset
from starlette.responses import JSONResponse

from mossy.runtime.models import TaskStatus

if TYPE_CHECKING:
    from fastapi import FastAPI
    from fasta2a.applications import FastA2A
    from starlette.types import ASGIApp, Receive, Scope, Send

    from mossy.runtime import Runtime


A2A_PATH = "/a2a"
AGENT_CARD_PATH = "/.well-known/agent-card.json"
#: The card's full URL path, which ``create_app`` exempts from ``ApiKeyMiddleware``.
A2A_CARD_URL_PATH = f"{A2A_PATH}{AGENT_CARD_PATH}"
TOOLS_ENV = "MOSSY_A2A_TOOLS"

_A2A_INSTRUCTIONS = """You are this Mossy agent's A2A endpoint. Other agents ask you questions.

Answer only from your tools. Do not guess: if no tool can answer, say so. You are talking to a
program, not a person: answer the question that was asked, return the result, and stop."""


def _agent_name() -> str:
    return (os.getenv("MOSSY_A2A_NAME") or "mossy").strip()


def agent_status_report(runtime: "Runtime") -> dict[str, Any]:
    """This agent's status, read from the runtime's memory. No network, no writes."""
    counts = {status.value: 0 for status in TaskStatus}
    for task in runtime.list_tasks():
        counts[task.status.value] += 1
    return {
        "agent": _agent_name(),
        "status": "up",
        "started_at": runtime.started_at.isoformat(),
        "tasks_by_status": counts,
        "tasks_total": sum(counts.values()),
        "queue_size": runtime.queue_depth(),
    }


def agent_status_tool(runtime: "Runtime") -> Tool[Any]:
    # Async on purpose: a sync tool runs in a worker thread, and reading ``runtime.tasks`` there
    # would race the event loop that adds to it.
    async def agent_status() -> dict[str, Any]:
        """Report this agent's status: up, start time, queued tasks by status, queue size.

        Counts are kept in memory and are lost when the agent restarts. They cover queued work,
        not chat conversations.
        """
        return agent_status_report(runtime)

    return Tool(agent_status)


def task_status_tool(runtime: "Runtime") -> Tool[Any]:
    async def task_status(task_id: str) -> dict[str, Any]:
        """Report one queued task by id: its status, result and error."""
        task = runtime.get_task(task_id)
        if task is None:
            return {"error": "unknown task", "id": task_id}
        return {
            "id": task.id,
            "status": task.status.value,
            "result": task.result,
            "error": task.error,
        }

    return Tool(task_status)


A2A_TOOLS = (agent_status_tool, task_status_tool)


def custom_tools(raw: str | None = None) -> list[Any]:
    """Functions named in ``MOSSY_A2A_TOOLS``, comma-separated ``package.module:function``.

    A name that cannot be loaded is reported on stderr and skipped: one bad entry must not take
    the channel down. Whatever is listed is reachable by anyone holding the key, so keep these
    read-only unless you mean otherwise.
    """
    text = os.getenv(TOOLS_ENV, "") if raw is None else raw
    tools: list[Any] = []
    for spec in (part.strip() for part in text.split(",")):
        if not spec:
            continue
        try:
            module_name, _, attr = spec.partition(":")
            tool = getattr(importlib.import_module(module_name), attr)
            if not callable(tool) and not isinstance(tool, Tool):
                raise TypeError("not a function")
        except Exception as exc:  # noqa: BLE001 - any failure means the same: this tool is out
            print(
                f"A2A tool {spec!r} NOT loaded ({type(exc).__name__}: {exc}).",
                file=sys.stderr,
                flush=True,
            )
            continue
        tools.append(tool)
    return tools


def a2a_toolset(runtime: "Runtime") -> FunctionToolset[Any]:
    """The explicit toolset the A2A agent gets: the built-in tools plus ``MOSSY_A2A_TOOLS``."""
    return FunctionToolset([*(make(runtime) for make in A2A_TOOLS), *custom_tools()])


def _a2a_model() -> str | Model:
    return os.getenv("PLATFORMER_A2A_MODEL") or os.getenv(
        "PLATFORMER_SKILL_MODEL", "openai:gpt-5.4-mini"
    )


def _public_url() -> str:
    """The URL the agent card advertises for itself. Informational."""
    base = (os.getenv("MOSSY_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if base:
        return f"{base}{A2A_PATH}"
    host = (os.getenv("PLATFORMER_HOST") or "127.0.0.1").strip()
    port = (os.getenv("PORT") or "8765").strip()
    return f"http://{host}:{port}{A2A_PATH}"


def configured_api_key() -> str:
    """The HTTP API key, read per request. Empty when unset."""
    return (os.getenv("MOSSY_API_KEY") or os.getenv("HTTP_API_KEY") or "").strip()


def build_a2a_agent(model: Model, runtime: "Runtime") -> Agent[None, str]:
    return Agent(
        model,
        instructions=_A2A_INSTRUCTIONS,
        toolsets=[a2a_toolset(runtime)],
        retries=3,
    )


def _route_path(scope: "Scope") -> str:
    """The path inside the mount. Starlette keeps the full path and sets ``root_path``."""
    path = scope.get("path", "")
    root = scope.get("root_path", "")
    if root and path.startswith(root):
        path = path[len(root):]
    return path.rstrip("/") or "/"


def _bearer(scope: "Scope") -> str:
    for name, value in scope.get("headers") or []:
        if name == b"authorization":
            text = value.decode("latin-1")
            if text[:7].lower() == "bearer ":
                return text[7:].strip()
    return ""


class RequireBearer:
    """Refuses every request but the agent card unless it carries the configured bearer key."""

    def __init__(self, app: "ASGIApp") -> None:
        self.app = app

    async def __call__(self, scope: "Scope", receive: "Receive", send: "Send") -> None:
        if scope["type"] == "http" and not self._is_card(scope) and not self._authorized(scope):
            await JSONResponse({"detail": "Unauthorized"}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _is_card(scope: "Scope") -> bool:
        return scope.get("method") in ("GET", "HEAD") and _route_path(scope) == AGENT_CARD_PATH

    @staticmethod
    def _authorized(scope: "Scope") -> bool:
        expected = configured_api_key()
        if not expected:
            return False
        return hmac.compare_digest(_bearer(scope).encode(), expected.encode())


def build_a2a_app(model: Model, runtime: "Runtime") -> "FastA2A":
    """Build the FastA2A app around the A2A agent. Does not mount it."""
    from fasta2a.pydantic_ai import agent_to_a2a

    return agent_to_a2a(
        build_a2a_agent(model, runtime),
        name=_agent_name(),
        description=(
            os.getenv("MOSSY_A2A_DESCRIPTION")
            or "A Mossy agent. Answers questions from other agents, read-only."
        ),
        version="1.0.0",
        url=_public_url(),
        skills=[],
    )


def register_a2a_routes(app: "FastAPI", runtime: "Runtime") -> "FastA2A | None":
    """Mount the A2A app at ``/a2a`` behind ``RequireBearer`` and return the FastA2A app.

    The returned app carries the worker lifespan; ``create_app`` must enter it. Returns ``None``,
    mounting nothing, when the model cannot be built or ``fasta2a`` is missing.
    """
    try:
        model = infer_model(_a2a_model())
        sub_app = build_a2a_app(model, runtime)
    except Exception as exc:  # noqa: BLE001 - any provider or import error means the same
        print(
            f"A2A channel NOT enabled ({type(exc).__name__}: {exc}).",
            file=sys.stderr,
            flush=True,
        )
        return None
    app.mount(A2A_PATH, RequireBearer(sub_app))
    print(
        f"A2A channel enabled at POST {A2A_PATH} (agent card at GET "
        f"{A2A_CARD_URL_PATH}; POST needs Authorization: Bearer <MOSSY_API_KEY>)",
        file=sys.stderr,
        flush=True,
    )
    if not configured_api_key():
        print(
            "A2A: MOSSY_API_KEY is not set, so every A2A task request is refused.",
            file=sys.stderr,
            flush=True,
        )
    return sub_app
