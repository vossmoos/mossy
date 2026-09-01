"""MCP channel: expose Mossy to Claude (and other MCP clients) as `ask_mossy`.

Mounted on the existing FastAPI app as Streamable HTTP (default path `/mcp`).
Calling the tool is the same kind of turn as web chat, CLI, or Slack: a
conversational agent with shared capabilities, not `POST /run`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, TypeVar

from fastapi import FastAPI
from mcp.server.transport_security import TransportSecuritySettings
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage

try:
    from mcp.server.mcpserver import Context, MCPServer
except ModuleNotFoundError:  # mcp 1.x
    from mcp.server.fastmcp import Context, FastMCP as MCPServer

    _MCP_HTTP_KWARGS_ON_APP = False
else:
    _MCP_HTTP_KWARGS_ON_APP = True

from mossy.runtime.agent_run import run_agent_with_utc
from mossy.runtime.deps import RuntimeDeps

if TYPE_CHECKING:
    from mossy.runtime import Runtime

logger = logging.getLogger(__name__)

V = TypeVar("V")

_MCP_INSTRUCTIONS = """You are Mossy's assistant, answering a message sent through MCP
(typically from Claude Desktop, Claude Code, or another MCP client).

You have access to agentic skills through the skills tools. Use skills immediately when they help
answer the user or perform an action. Use the system-queue skill when work should be queued,
inspected, cancelled, scheduled, or allowed to continue independently. Do not enqueue by default:
answer directly when the request can be resolved in this turn.

Queued tasks do not automatically post their final result back to the MCP client yet. When you
enqueue work, include the task id in your reply and tell the user how to check on it.

Each user message is prefixed with `[System UTC now: …]` — use it as the authoritative clock for
relative scheduling ("in 1 minute", "tomorrow"): compute scheduled_for in UTC from that line, not from
memory.

Keep replies concise and self-contained: the caller sees only this text as the tool result."""

_ASK_MOSSY_DESCRIPTION = """Send a message to the Mossy agent and wait for its reply, as if the
message were typed in Mossy's web chat or CLI.

Mossy has skills (GitHub, Jira, Freshdesk, filesystem, shell, queue, and any installed skill) and
will use them to fulfill the request. Returns Mossy's final text response.

Pass conversation_id to continue a named Mossy thread. If omitted, history is kept for this MCP
session. Long-running work may be queued; Mossy will include a task id in the reply."""


class TTLStore(Generic[V]):
    """Idle-TTL cache with a hard entry cap."""

    def __init__(self, *, ttl_seconds: float, max_entries: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._data: dict[str, tuple[float, V]] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, key: str, factory: Callable[[], V]) -> V:
        async with self._lock:
            self._sweep()
            entry = self._data.get(key)
            if entry is not None:
                value = entry[1]
                self._data[key] = (time.monotonic(), value)
                return value
            self._evict_if_full()
            value = factory()
            self._data[key] = (time.monotonic(), value)
            return value

    async def touch(self, key: str) -> None:
        async with self._lock:
            entry = self._data.get(key)
            if entry is not None:
                self._data[key] = (time.monotonic(), entry[1])

    def _evict_if_full(self) -> None:
        if len(self._data) < self.max_entries:
            return
        oldest = min(self._data, key=lambda item: self._data[item][0])
        self._data.pop(oldest, None)

    def _sweep(self) -> None:
        cutoff = time.monotonic() - self.ttl_seconds
        stale = [key for key, (last_used, _) in self._data.items() if last_used < cutoff]
        for key in stale:
            self._data.pop(key, None)


@dataclass
class ConversationState:
    history: list[ModelMessage] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def mcp_path() -> str:
    path = (os.getenv("MCP_PATH") or "/mcp").strip() or "/mcp"
    if not path.startswith("/"):
        path = f"/{path}"
    return path.rstrip("/") or "/mcp"


def _env_flag(name: str) -> bool:
    raw = os.getenv(name, "").strip().lower()
    return raw not in {"", "0", "false", "no", "off"}


def _csv_env(name: str) -> list[str]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _mcp_model() -> str:
    return (
        os.getenv("PLATFORMER_MCP_MODEL")
        or os.getenv("PLATFORMER_CLI_MODEL")
        or os.getenv("PLATFORMER_SKILL_MODEL", "openai:gpt-5.4-mini")
    )


def _transport_security() -> TransportSecuritySettings:
    if _env_flag("MCP_DISABLE_DNS_REBINDING"):
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", *_csv_env("MCP_ALLOWED_HOSTS")]
    origins = [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
        *_csv_env("MCP_ALLOWED_ORIGINS"),
    ]
    bind_host = (os.getenv("PLATFORMER_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    if bind_host not in {"0.0.0.0", "::", "127.0.0.1", "localhost", "::1"}:
        hosts.append(f"{bind_host}:*")
        origins.append(f"http://{bind_host}:*")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def _mcp_session_id(ctx: Context) -> str | None:
    try:
        request = ctx.request_context.request
    except ValueError:
        return None
    if request is None:
        return None
    headers = getattr(request, "headers", None)
    if headers is None:
        return None
    session_id = headers.get("mcp-session-id")
    if session_id:
        return str(session_id)
    return None


def _history_key(conversation_id: str | None, ctx: Context) -> str:
    if conversation_id and conversation_id.strip():
        return conversation_id.strip()
    session_id = _mcp_session_id(ctx)
    if session_id:
        return f"mcp:{session_id}"
    return f"session:{id(ctx.session)}"


class McpChannel:
    """Expose a conversational Mossy agent as the MCP tool `ask_mossy`."""

    def __init__(self, runtime: "Runtime", *, path: str | None = None) -> None:
        self.runtime = runtime
        self.path = path or mcp_path()
        self.max_history_messages = int(os.getenv("MCP_HISTORY_MAX_MESSAGES", "40"))
        self.histories = TTLStore[ConversationState](
            ttl_seconds=float(os.getenv("MCP_HISTORY_TTL_SECONDS", str(2 * 60 * 60))),
            max_entries=int(os.getenv("MCP_HISTORY_MAX_CONVERSATIONS", "500")),
        )
        self.agent = Agent(
            _mcp_model(),
            deps_type=RuntimeDeps,
            instructions=_MCP_INSTRUCTIONS,
            capabilities=runtime.shared_capabilities(),
            retries=3,
        )
        self.deps = RuntimeDeps(runtime=runtime)
        bind_host = (os.getenv("PLATFORMER_HOST") or "127.0.0.1").strip() or "127.0.0.1"
        http_kwargs = dict(
            streamable_http_path="/",
            json_response=_env_flag("MCP_JSON_RESPONSE"),
            transport_security=_transport_security(),
            host=bind_host,
        )
        instructions = (
            "Mossy is an agent with skills. Call ask_mossy to send it a message "
            "the same way you would in Mossy's web chat or CLI."
        )
        if _MCP_HTTP_KWARGS_ON_APP:
            self.mcp = MCPServer("mossy", instructions=instructions)
            self._register_tools()
            self.http_app = self.mcp.streamable_http_app(**http_kwargs)
        else:
            self.mcp = MCPServer("mossy", instructions=instructions, **http_kwargs)
            self._register_tools()
            self.http_app = self.mcp.streamable_http_app()

    def session_lifespan(self) -> AbstractAsyncContextManager[None]:
        """FastAPI must run this; mounted Starlette apps do not start their own lifespan."""
        return self.mcp.session_manager.run()

    def _register_tools(self) -> None:
        channel = self

        @self.mcp.tool(name="ask_mossy", description=_ASK_MOSSY_DESCRIPTION, structured_output=False)
        async def ask_mossy(
            message: str,
            ctx: Context,
            conversation_id: str | None = None,
        ) -> str:
            return await channel.handle_ask(message, ctx, conversation_id=conversation_id)

    async def handle_ask(
        self,
        message: str,
        ctx: Context,
        *,
        conversation_id: str | None = None,
    ) -> str:
        text = (message or "").strip()
        if not text:
            return "message is required."

        key = _history_key(conversation_id, ctx)
        state = await self.histories.get_or_create(key, ConversationState)
        async with state.lock:
            try:
                run = await run_agent_with_utc(
                    self.agent,
                    text,
                    deps=self.deps,
                    message_history=state.history[-self.max_history_messages :],
                )
                state.history += run.new_messages()
                del state.history[:-self.max_history_messages]
                body = str(run.output or "").strip() or "(no output)"
            except Exception as exc:  # noqa: BLE001
                logger.exception("MCP ask_mossy failed")
                body = f"Sorry, I hit an error while handling that: {exc}"
            finally:
                await self.histories.touch(key)
        return body


class _PrefixedASGI:
    """Rewrite `/mcp` to `/` so the mounted Streamable HTTP app matches."""

    def __init__(self, app, prefix: str) -> None:
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            scope = dict(scope)
            if path == self.prefix or path == f"{self.prefix}/":
                scope["path"] = "/"
            elif path.startswith(f"{self.prefix}/"):
                rest = path[len(self.prefix) :]
                scope["path"] = rest if rest.startswith("/") else f"/{rest}"
        await self.app(scope, receive, send)


def register_mcp_routes(app: FastAPI, channel: McpChannel) -> None:
    """Mount Streamable HTTP at `/mcp` and `/mcp/`.

    FastAPI's slash-redirect would 307 POST `/mcp` → `/mcp/`, and MCP clients
    typically do not replay the body. An exact `/mcp` route avoids that.
    Caller must run `channel.session_lifespan()`.
    """
    from starlette.routing import Route

    prefix = channel.path.rstrip("/") or "/mcp"
    inner = channel.http_app
    asgi = _PrefixedASGI(inner, prefix)
    app.router.routes.insert(
        0,
        Route(prefix, endpoint=asgi, methods=["GET", "POST", "DELETE", "OPTIONS"]),
    )
    app.mount(prefix, inner)
