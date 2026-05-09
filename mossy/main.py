"""Concurrent runtime plus optional HTTP, CLI, and Slack channels."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

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
    p.add_argument("--no-slack", action="store_true")
    args = p.parse_args()

    runtime = Runtime()

    tasks = [runtime.start()]
    if not args.no_http:
        tasks.append(_http(runtime, args.host, args.port))
    if not args.no_cli:
        tasks.append(stdin_loop(runtime))
    if not args.no_slack:
        bot_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
        app_token = os.getenv("SLACK_APP_TOKEN", "").strip()
        if bot_token and app_token:
            from mossy.channels.slack.app import SlackChannel

            print("Slack channel enabled (Socket Mode).", file=sys.stderr)
            tasks.append(SlackChannel(runtime, bot_token=bot_token, app_token=app_token).start())
        elif bot_token or app_token:
            print(
                "Slack channel disabled: set both SLACK_BOT_TOKEN and SLACK_APP_TOKEN.",
                file=sys.stderr,
            )
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
