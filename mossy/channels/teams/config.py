"""Teams settings that need no Bot Framework packages, so startup can check them without them."""

from __future__ import annotations

import os


def bot_credentials() -> tuple[str, str]:
    app_id = (os.environ.get("MicrosoftAppId") or os.environ.get("MICROSOFT_APP_ID", "")).strip()
    app_password = (
        os.environ.get("MicrosoftAppPassword") or os.environ.get("MICROSOFT_APP_PASSWORD", "")
    ).strip()
    return app_id, app_password


def teams_configured() -> bool:
    app_id, app_password = bot_credentials()
    return bool(app_id and app_password)


def teams_path() -> str:
    return (os.getenv("TEAMS_PATH") or "/api/messages").rstrip("/") or "/api/messages"
