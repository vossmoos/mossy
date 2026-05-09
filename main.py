"""Run from repo root: `python main.py` (requires cwd on PYTHONPATH)."""

from __future__ import annotations

import asyncio

from mossy.env import load_mossy_env

load_mossy_env()

from mossy.main import main

if __name__ == "__main__":
    asyncio.run(main())
