"""Minimal .env reader, so the skill stays dependency-free and still runs by hand.

Under Mossy the runtime loads the repository's ``.env`` into the environment before any skill
script starts, so this does nothing there. Run from a shell, though, a self-contained skill has
no ``python-dotenv`` to lean on — and asking someone to export six variables before a read-only
script is a good way to have them export the wrong ones.

Values already present in the environment always win: the file fills gaps, it never overrides.
"""

from __future__ import annotations

import os
from pathlib import Path

_MAX_LEVELS = 6


def load_dotenv_if_present() -> str | None:
    """Fill missing variables from the nearest ``.env`` above this file. Returns its path."""
    here = Path(__file__).resolve().parent
    for parent in [here, *here.parents][:_MAX_LEVELS]:
        candidate = parent / ".env"
        if not candidate.is_file():
            continue
        try:
            for raw in candidate.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except OSError:
            return None
        return str(candidate)
    return None


__all__ = ["load_dotenv_if_present"]
