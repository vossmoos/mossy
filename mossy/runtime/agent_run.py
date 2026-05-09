"""Single entry points for agent execution — UTC context is always applied."""

from __future__ import annotations

from typing import Any

from pydantic_ai import Agent

from mossy.runtime.clock import obligate_agent_user_message


async def run_agent_with_utc(agent: Agent[Any, Any], user_message: str, **kwargs: Any) -> Any:
    """Call `agent.run` with mandatory `[System UTC now: …]` prefix on the user message."""
    return await agent.run(obligate_agent_user_message(user_message), **kwargs)
