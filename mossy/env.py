"""Load `.env` into `os.environ` before Mossy reads configuration."""

from __future__ import annotations

from pathlib import Path


def load_mossy_env() -> None:
    """Load env files so `os.getenv` in Mossy sees them.

    Order:
    1. `<repo>/.env` next to the outer `mossy` package directory (works when CWD is wrong).
    2. `.env` from the current working directory (overrides; typical developer workflow).
    """
    from dotenv import load_dotenv

    pkg_dir = Path(__file__).resolve().parent
    repo_root = pkg_dir.parent
    repo_env = repo_root / ".env"
    if repo_env.is_file():
        load_dotenv(repo_env, override=False)
    load_dotenv(override=True)
