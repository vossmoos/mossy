"""Conversational CLI channel."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import TYPE_CHECKING

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage

from mossy.runtime.deps import RuntimeDeps

if TYPE_CHECKING:
    from mossy.runtime import Runtime


_CLI_INSTRUCTIONS = """You are Mossy's interactive terminal assistant.

You have access to agentic skills through the skills tools. Use skills immediately when they help
answer the user or perform an action. Use runtime-control tools when work should be queued,
inspected, cancelled, or allowed to continue independently. Do not enqueue by default: answer
directly when the request can be resolved in the chat turn.

Keep terminal output concise."""


async def stdin_loop(runtime: "Runtime") -> None:
    model = os.getenv("PLATFORMER_CLI_MODEL") or os.getenv(
        "PLATFORMER_SKILL_MODEL", "openai:gpt-5.4-mini"
    )
    cli = Agent(
        model,
        deps_type=RuntimeDeps,
        instructions=_CLI_INSTRUCTIONS,
        capabilities=runtime.shared_capabilities(exclude_skills={"filesystem"}),
    )
    deps = RuntimeDeps(runtime=runtime)
    history: list[ModelMessage] = []

    print(
        "Mossy CLI — chat mode. Use /quit to exit.\n",
        flush=True,
    )

    while True:
        try:
            line = await asyncio.to_thread(sys.stdin.readline)
        except (KeyboardInterrupt, EOFError):
            print("\nbye.", flush=True)
            return
        if not line:
            await asyncio.sleep(0.05)
            continue
        text = line.strip()
        if not text:
            continue
        if text.lower() in ("/quit", "/exit", "exit", "quit"):
            print("bye.", flush=True)
            return

        out = await cli.run(text, deps=deps, message_history=history)
        history += out.new_messages()
        print(f"\n{out.output}\n", flush=True)
