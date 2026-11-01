"""A2A (Agent-to-Agent) channel: exposes a read-only agent over the A2A protocol."""

from mossy.channels.a2a.app import A2A_CARD_URL_PATH, A2A_PATH, register_a2a_routes

__all__ = ["A2A_CARD_URL_PATH", "A2A_PATH", "register_a2a_routes"]
