"""The MCP client capability: configuration, gating, a real MCP server, and outages."""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from mossy.capabilities import mcp_client
from mossy.capabilities.mcp_client import McpConfigError, McpServerConfig, configured_servers, mcp_capabilities
from mossy.runtime.core import Runtime

KEY = "test-myco-agent-key"


@pytest.fixture(autouse=True)
def _fresh_cache():
    mcp_client._CACHE.clear()
    yield
    mcp_client._CACHE.clear()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def myco_like_server():
    """A tiny MCP server that, like Myco, refuses calls without the agent key."""
    server = FastMCP(
        "fake-myco",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    seen: list[dict] = []

    @server.tool()
    def memory_recall(query: str) -> list[dict]:
        """Recall beliefs."""
        seen.append({"query": query})
        return [{"id": "company_acme::tier", "value": "gold"}]

    class RequireKey(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.headers.get("authorization") != f"Bearer {KEY}":
                return JSONResponse({"detail": "bad key"}, status_code=401)
            return await call_next(request)

    inner = server.streamable_http_app()
    app = Starlette(routes=inner.routes, middleware=[Middleware(RequireKey)], lifespan=inner.router.lifespan_context)
    port = _free_port()
    uv = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if uv.started:
            break
        time.sleep(0.05)
    yield {"url": f"http://127.0.0.1:{port}/mcp", "seen": seen}
    uv.should_exit = True
    thread.join(timeout=5)


def test_configuration_is_read_per_server():
    env = {
        "MOSSY_MCP_SERVERS": "myco, vendor",
        "MOSSY_MCP__MYCO__URL": "http://myco:8080/mcp",
        "MOSSY_MCP__MYCO__TOKEN": KEY,
        "MOSSY_MCP__VENDOR__URL": "https://vendor.test/mcp",
        "MOSSY_MCP__VENDOR__HEADERS": '{"X-Tenant": "acme"}',
        "MOSSY_MCP__VENDOR__TOOL_PREFIX": "vendor_",
        "MOSSY_MCP__VENDOR__SKILL": "vendor",
    }
    myco, vendor = configured_servers(env)
    assert myco.headers == {"Authorization": f"Bearer {KEY}"} and myco.skill == "mcp"
    assert vendor.headers == {"X-Tenant": "acme"} and vendor.tool_prefix == "vendor_" and vendor.skill == "vendor"
    assert configured_servers({}) == []


def test_bad_configuration_is_refused_without_taking_mossy_down():
    with pytest.raises(McpConfigError, match="URL is required"):
        McpServerConfig.from_env("myco", {})
    with pytest.raises(McpConfigError, match="JSON object"):
        McpServerConfig.from_env("x", {"MOSSY_MCP__X__URL": "http://x", "MOSSY_MCP__X__HEADERS": "nope"})
    assert mcp_capabilities({"MOSSY_MCP_SERVERS": "myco"}) == []  # logged, not raised


def test_servers_are_gated_on_their_skill_and_built_once(monkeypatch):
    monkeypatch.setenv("MOSSY_MCP_SERVERS", "myco")
    monkeypatch.setenv("MOSSY_MCP__MYCO__URL", "http://127.0.0.1:9/mcp")
    runtime = Runtime(enable_duties=False)
    every = runtime.shared_capabilities()
    mcp_caps = [c for c in every if isinstance(getattr(c, "toolset", None), mcp_client.OptionalToolset)]
    assert len(mcp_caps) == 1
    # The worker rebuilds its list per task: the same connection is reused.
    again = [c for c in runtime.shared_capabilities() if c in mcp_caps]
    assert again == mcp_caps
    without = runtime.shared_capabilities(exclude_skills={"mcp"})
    assert not any(c in mcp_caps for c in without)
    only_echo = runtime.shared_capabilities(allow_skills={"echo"})
    assert not any(c in mcp_caps for c in only_echo)


def test_a_real_mcp_server_is_called_with_the_agent_key(myco_like_server):
    env = {
        "MOSSY_MCP_SERVERS": "myco",
        "MOSSY_MCP__MYCO__URL": myco_like_server["url"],
        "MOSSY_MCP__MYCO__TOKEN": KEY,
    }
    [(skill, capability)] = mcp_capabilities(env)
    agent = Agent(TestModel(call_tools=["memory_recall"]), capabilities=[capability])
    result = asyncio.run(agent.run("What tier is Acme?"))
    assert myco_like_server["seen"], "the tool was never called"
    assert "company_acme::tier" in result.output


def test_a_wrong_key_or_a_dead_server_removes_the_tools_instead_of_failing(myco_like_server):
    for url, token in ((myco_like_server["url"], "wrong-key"), ("http://127.0.0.1:9/mcp", KEY)):
        mcp_client._CACHE.clear()
        env = {"MOSSY_MCP_SERVERS": "myco", "MOSSY_MCP__MYCO__URL": url, "MOSSY_MCP__MYCO__TOKEN": token}
        [(_, capability)] = mcp_capabilities(env)
        agent = Agent(TestModel(), capabilities=[capability])
        result = asyncio.run(agent.run("hello"))  # no exception: the run goes on without those tools
        assert isinstance(result.output, str)
    assert myco_like_server["seen"] == []
