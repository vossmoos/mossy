"""MCP channel: Streamable HTTP server exposing `ask_mossy`."""

from mossy.channels.mcp.app import McpChannel, mcp_path, register_mcp_routes

__all__ = ["McpChannel", "mcp_path", "register_mcp_routes"]
