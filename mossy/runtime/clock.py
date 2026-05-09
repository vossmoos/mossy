"""Authoritative wall clock for agent prompts (UTC)."""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def utc_context_line() -> str:
    return f"[System UTC now: {utc_now_iso()}]"


def obligate_agent_user_message(message: str) -> str:
    """Mandatory UTC context for every agent user message (CLI, worker, etc.)."""
    text = message.strip()
    return f"{utc_context_line()}\n\n{text}"
