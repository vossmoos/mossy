"""Microsoft Teams channel. Needs ``pip install -r requirements-teams.txt``."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from mossy.channels.teams.config import teams_configured, teams_path

if TYPE_CHECKING:
    from fastapi import FastAPI

    from mossy.runtime import Runtime

__all__ = ["register_teams_routes", "teams_configured", "teams_path"]


def register_teams_routes(app: "FastAPI", runtime: "Runtime") -> Any | None:
    """Mount the Teams endpoint when credentials are set and the Bot Framework is installed."""
    if not teams_configured():
        return None
    try:
        from mossy.channels.teams.app import TeamsChannel
    except ImportError as exc:
        print(
            f"Teams channel NOT enabled ({exc}): pip install -r requirements-teams.txt",
            file=sys.stderr,
            flush=True,
        )
        return None
    channel = TeamsChannel(runtime)
    channel.register(app)
    print(f"Teams channel enabled at POST {channel.path}", file=sys.stderr, flush=True)
    return channel
