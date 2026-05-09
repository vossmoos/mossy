"""Conversational CLI channel."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import TYPE_CHECKING

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage
from rich.console import Console

from mossy.runtime.agent_run import run_agent_with_utc
from mossy.runtime.deps import RuntimeDeps

if TYPE_CHECKING:
    from mossy.runtime import Runtime


_CLI_INSTRUCTIONS = """You are Mossy's interactive terminal assistant.

You have access to agentic skills through the skills tools. Use skills immediately when they help
answer the user or perform an action. Use the system-queue skill when work should be queued,
inspected, cancelled, or allowed to continue independently. Do not enqueue by default: answer
directly when the request can be resolved in the chat turn.

Each user message is prefixed with `[System UTC now: …]` — use it as the authoritative clock for
relative scheduling ("in 1 minute", "tomorrow"): compute scheduled_for in UTC from that line, not from
memory.

Keep terminal output concise."""


def _ansi_code_is_green(code: str) -> bool:
    try:
        value = int(code)
    except ValueError:
        return False
    return value in {2, 10, 22, 28, 34, 40, 46, 48, 76, 82, 83, 84, 118, 119, 120, 154, 155, 156}


def _terminal_background_is_green() -> bool:
    colorfgbg = os.getenv("COLORFGBG", "")
    if colorfgbg:
        background = colorfgbg.split(";")[-1]
        return _ansi_code_is_green(background)

    background = os.getenv("TERMINAL_BACKGROUND", "").lower()
    return "green" in background


def _terminal_foreground_is_green() -> bool:
    colorfgbg = os.getenv("COLORFGBG", "")
    if colorfgbg:
        foreground = colorfgbg.split(";")[0]
        return _ansi_code_is_green(foreground)

    foreground = os.getenv("TERMINAL_FOREGROUND", "").lower()
    return "green" in foreground


def _mossy_output_style() -> str:
    if _terminal_background_is_green():
        return "#556b2f"  # dark olive green, readable on green terminal backgrounds
    return "bright_green"


def _user_input_ansi_prefix() -> str:
    """ANSI SGR for echoed stdin text + prompt (Rich cannot style typed characters)."""
    if _terminal_foreground_is_green():
        return "\033[38;5;39m"  # vivid blue — readable when terminal fg is already green
    # Mid green: brighter than the old forest tone (readable on black), still calmer than Mossy’s bright_green.
    return "\033[38;2;58;160;98m"


def _read_cli_line_sync() -> str:
    sys.stdout.write(_user_input_ansi_prefix())
    sys.stdout.write("> ")
    sys.stdout.flush()
    try:
        return input()
    finally:
        sys.stdout.write("\033[0m")
        sys.stdout.flush()


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
    console = Console()
    mossy_style = _mossy_output_style()

    console.print("Mossy CLI — chat mode. Use /quit to exit.\n", style=mossy_style)

    while True:
        try:
            line = await asyncio.to_thread(_read_cli_line_sync)
        except (KeyboardInterrupt, EOFError):
            console.print("\nbye.", style=mossy_style)
            return
        text = line.strip()
        if not text:
            continue
        if text.lower() in ("/quit", "/exit", "exit", "quit"):
            console.print("bye.", style=mossy_style)
            return

        out = await run_agent_with_utc(cli, text, deps=deps, message_history=history)
        history += out.new_messages()
        console.print()
        console.print(str(out.output), style=mossy_style)
        console.print()
