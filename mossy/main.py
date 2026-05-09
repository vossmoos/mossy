"""Concurrent runtime plus optional HTTP and CLI channels."""

from __future__ import annotations

import argparse
import asyncio
import os

import uvicorn

from mossy.env import load_mossy_env

load_mossy_env()

from mossy.channels.cli.chat import stdin_loop
from mossy.channels.http.app import create_app
from mossy.runtime import Runtime


async def _http(runtime: Runtime, host: str, port: int) -> None:
    app = create_app(runtime)
    cfg = uvicorn.Config(app, host=host, port=port, loop="asyncio", log_level="warning")
    server = uvicorn.Server(cfg)
    await server.serve()


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=os.getenv("PLATFORMER_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.getenv("PORT", "8765")))
    p.add_argument("--no-http", action="store_true")
    p.add_argument("--no-cli", action="store_true")
    args = p.parse_args()

    runtime = Runtime()

    tasks = [runtime.start()]
    if not args.no_http:
        tasks.append(_http(runtime, args.host, args.port))
    if not args.no_cli:
        tasks.append(stdin_loop(runtime))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
