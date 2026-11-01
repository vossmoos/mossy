"""Helpers for bounded pydantic-ai conversation history."""

from __future__ import annotations

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, UserPromptPart


def _starts_a_turn(message: ModelMessage) -> bool:
    """True for a request carrying a user prompt, not only tool results."""
    return isinstance(message, ModelRequest) and any(
        isinstance(part, UserPromptPart) for part in message.parts
    )


def trim_message_history(messages: list[ModelMessage], max_messages: int) -> list[ModelMessage]:
    """Return at most the last *max_messages* entries without breaking tool-call sequences.

    Naive slicing can start the window on tool results without the assistant message that
    requested them, which providers reject. The window therefore starts on a user prompt and
    never ends on tool calls still waiting for results.
    """
    if max_messages <= 0:
        return []
    window = list(messages[-max_messages:])
    while window and not _starts_a_turn(window[0]):
        window = window[1:]
    while window and isinstance(window[-1], ModelResponse) and window[-1].tool_calls:
        window = window[:-1]
    return window
