"""Tools from external MCP servers — Myco, a vendor's MCP server, etchmem — as capabilities.

Each configured server becomes one toolset, gated on a skill like every other capability
(default ``mcp``), so channel allow lists, ``PLATFORMER_WORKER_EXCLUDE_SKILLS`` and
``MOSSY_SKILL_ACCESS`` narrow it the usual way. Nothing else in Mossy changes: the built-in
Jira, Wrike, Freshdesk and GitHub capabilities stay as they are.

Environment (``<NAME>`` is the upper-case server name):

    MOSSY_MCP_SERVERS                comma-separated server names, e.g. myco
    MOSSY_MCP__<NAME>__URL           Streamable HTTP endpoint, e.g. http://myco:8080/mcp
    MOSSY_MCP__<NAME>__TOKEN         sent as ``Authorization: Bearer <token>`` (Myco: the agent key)
    MOSSY_MCP__<NAME>__HEADERS       extra headers as a JSON object
    MOSSY_MCP__<NAME>__TOOL_PREFIX   prefix for this server's tool names, to avoid collisions
    MOSSY_MCP__<NAME>__SKILL         the skill that gates these tools (default: mcp)
    MOSSY_MCP__<NAME>__TIMEOUT       seconds per request (default 30)

Behind Myco, Mossy is one agent with one key: every call runs as that agent's service user
in Myco's ``users.yaml``, and Myco decides which tools and scopes it sees. Mossy does not
forward a person's SSO token yet: connections are shared between the runs on a channel, so a
per-user header could reach another user's concurrent run. That needs one connection per run.

A server that cannot be reached is skipped for that run (logged), so an outage removes its
tools instead of failing every chat.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from pydantic_ai.capabilities.toolset import Toolset
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.toolsets import AbstractToolset, WrapperToolset

logger = logging.getLogger(__name__)

DEFAULT_SKILL = "mcp"


class McpConfigError(ValueError):
    """A ``MOSSY_MCP__*`` setting is invalid."""


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    tool_prefix: str | None = None
    skill: str = DEFAULT_SKILL
    timeout: float = 30.0

    @classmethod
    def from_env(cls, name: str, env: dict[str, str] | None = None) -> McpServerConfig:
        env = dict(os.environ if env is None else env)
        key = name.strip().upper().replace("-", "_")
        prefix = f"MOSSY_MCP__{key}__"

        def get(setting: str) -> str:
            return (env.get(prefix + setting) or "").strip()

        url = get("URL")
        if not url:
            raise McpConfigError(f"{prefix}URL is required for MCP server {name!r}")
        headers: dict[str, str] = {}
        raw_headers = get("HEADERS")
        if raw_headers:
            try:
                parsed = json.loads(raw_headers)
            except json.JSONDecodeError as exc:
                raise McpConfigError(f"{prefix}HEADERS must be a JSON object: {exc}") from exc
            if not isinstance(parsed, dict) or not all(isinstance(v, str) for v in parsed.values()):
                raise McpConfigError(f"{prefix}HEADERS must map header names to strings")
            headers.update(parsed)
        token = get("TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            timeout = float(get("TIMEOUT") or 30)
        except ValueError as exc:
            raise McpConfigError(f"{prefix}TIMEOUT must be a number of seconds") from exc
        return cls(
            name=name.strip(),
            url=url,
            headers=headers,
            tool_prefix=get("TOOL_PREFIX") or None,
            skill=get("SKILL") or DEFAULT_SKILL,
            timeout=timeout,
        )


def configured_servers(env: dict[str, str] | None = None) -> list[McpServerConfig]:
    """The servers named in ``MOSSY_MCP_SERVERS``, in order. Empty when unset."""
    source = os.environ if env is None else env
    names = [n.strip() for n in (source.get("MOSSY_MCP_SERVERS") or "").split(",") if n.strip()]
    return [McpServerConfig.from_env(name, dict(source)) for name in dict.fromkeys(names)]


@dataclass
class OptionalToolset(WrapperToolset[Any]):
    """A server that is down contributes no tools for the run instead of failing it."""

    server_name: str = ""
    _down: bool = field(default=False, repr=False)

    async def __aenter__(self) -> OptionalToolset:
        try:
            await self.wrapped.__aenter__()
            self._down = False
        except Exception as exc:  # noqa: BLE001 — any connection failure means "no tools this run"
            self._down = True
            logger.warning("MCP server %s unreachable, its tools are skipped: %s", self.server_name, exc)
        return self

    async def __aexit__(self, *args: Any) -> bool | None:
        if self._down:
            return None
        return await self.wrapped.__aexit__(*args)

    async def get_tools(self, ctx: Any) -> dict[str, Any]:
        if self._down:
            return {}
        try:
            return await self.wrapped.get_tools(ctx)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP server %s did not list its tools: %s", self.server_name, exc)
            return {}


def mcp_server_capability(config: McpServerConfig) -> Toolset:
    server: AbstractToolset[Any] = MCPToolset(
        config.url,
        id=f"mcp_{config.name}",
        headers=config.headers or None,
        init_timeout=config.timeout,
        read_timeout=config.timeout,
    )
    if config.tool_prefix:
        server = server.prefixed(config.tool_prefix.rstrip("_"))
    return Toolset(OptionalToolset(wrapped=server, server_name=config.name))


#: One capability (one HTTP client, one MCP connection) per server configuration and process.
#: The worker rebuilds its capability list for every task; without this each task would open
#: a client that is never closed.
_CACHE: dict[tuple[Any, ...], Toolset] = {}


def _cache_key(config: McpServerConfig) -> tuple[Any, ...]:
    return (config.name, config.url, tuple(sorted(config.headers.items())), config.tool_prefix,
            config.skill, config.timeout)


def mcp_capabilities(env: dict[str, str] | None = None) -> list[tuple[str, Toolset]]:
    """``(gating skill, capability)`` for every configured server.

    A broken configuration is logged and every server left out, so a typo shows up in the
    log instead of taking Mossy down."""
    try:
        servers = configured_servers(env)
    except McpConfigError as exc:
        logger.error("MCP client: %s", exc)
        return []
    out = []
    for config in servers:
        key = _cache_key(config)
        if key not in _CACHE:
            _CACHE[key] = mcp_server_capability(config)
        out.append((config.skill, _CACHE[key]))
    return out
