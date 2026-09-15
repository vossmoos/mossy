"""Local time for the log line. Standard library only.

Freshdesk timestamps are UTC and the people reading a contact log are in Switzerland, so a
line stamped in UTC is wrong for half the year by an hour and wrong for the other half by two.
``zoneinfo`` ships with Python and reads the system's tz database.

If that database is missing — a stripped container, most often — the fallback is explicit: the
time is stamped in UTC and the caller is told, rather than silently writing a time that is off
by an hour or two and looks perfectly plausible.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

DEFAULT_TIMEZONE = "Europe/Zurich"


def local_now() -> tuple[datetime, str | None]:
    """(now in the configured zone, a note when it could not be used)."""
    name = (os.environ.get("LINKING_TIMEZONE") or DEFAULT_TIMEZONE).strip()
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(name)), None
    except Exception:  # noqa: BLE001 - no tz database, or an unknown zone name
        return (
            datetime.now(timezone.utc),
            f"The timezone {name!r} is not available on this machine; the log line is stamped "
            "in UTC.",
        )


def log_timestamp() -> tuple[str, str | None]:
    """``DD.MM.YYYY / HH:MM`` in the reader's own time, plus a note when that failed."""
    moment, note = local_now()
    return moment.strftime("%d.%m.%Y / %H:%M"), note


__all__ = ["DEFAULT_TIMEZONE", "local_now", "log_timestamp"]
