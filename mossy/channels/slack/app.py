"""Slack Socket Mode channel for conversational Mossy."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, TypeVar

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from mossy.runtime.agent_run import run_agent_with_utc
from mossy.runtime.deps import RuntimeDeps

if TYPE_CHECKING:
    from mossy.runtime import Runtime

V = TypeVar("V")
logger = logging.getLogger(__name__)

_SLACK_INSTRUCTIONS = """You are Mossy's Slack assistant.

You have access to agentic skills through the skills tools. Use skills immediately when they help
answer the user or perform an action. Use the system-queue skill when work should be queued,
inspected, cancelled, scheduled, or allowed to continue independently. Do not enqueue by default:
answer directly when the request can be resolved in the Slack turn.

Queued tasks do not automatically post their final result back to Slack yet. When you enqueue work,
include the task id in your Slack reply and tell the user how to check on it.

Each user message is prefixed with `[System UTC now: ...]` — use it as the authoritative clock for
relative scheduling ("in 1 minute", "tomorrow"): compute scheduled_for in UTC from that line, not from
memory.

Keep Slack replies concise and readable."""


class TTLStore(Generic[V]):
    """Idle-TTL cache with a hard entry cap."""

    def __init__(self, *, ttl_seconds: float, max_entries: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._data: dict[str, tuple[float, V]] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(self, key: str, factory: Callable[[], V]) -> V:
        async with self._lock:
            self._sweep()
            entry = self._data.get(key)
            if entry is not None:
                value = entry[1]
                self._data[key] = (time.monotonic(), value)
                return value
            self._evict_if_full()
            value = factory()
            self._data[key] = (time.monotonic(), value)
            return value

    async def touch(self, key: str) -> None:
        async with self._lock:
            entry = self._data.get(key)
            if entry is not None:
                self._data[key] = (time.monotonic(), entry[1])

    def _evict_if_full(self) -> None:
        if len(self._data) < self.max_entries:
            return
        oldest = min(self._data, key=lambda item: self._data[item][0])
        self._data.pop(oldest, None)

    def _sweep(self) -> None:
        cutoff = time.monotonic() - self.ttl_seconds
        stale = [key for key, (last_used, _) in self._data.items() if last_used < cutoff]
        for key in stale:
            self._data.pop(key, None)


@dataclass
class ConversationState:
    history: list[ModelMessage] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(frozen=True)
class ReplyTarget:
    channel: str
    thread_ts: str | None


class SlackChannel:
    """Run a CLI-like Mossy agent over Slack Socket Mode."""

    def __init__(
        self,
        runtime: "Runtime",
        *,
        bot_token: str | None = None,
        app_token: str | None = None,
        history_ttl_seconds: float | None = None,
        max_conversations: int | None = None,
        max_history_messages: int | None = None,
    ) -> None:
        self.runtime = runtime
        self.bot_token = bot_token or os.environ["SLACK_BOT_TOKEN"]
        self.app_token = app_token or os.environ["SLACK_APP_TOKEN"]
        self.max_history_messages = max_history_messages or int(
            os.getenv("SLACK_HISTORY_MAX_MESSAGES", "40")
        )
        self.histories = TTLStore[ConversationState](
            ttl_seconds=history_ttl_seconds
            or float(os.getenv("SLACK_HISTORY_TTL_SECONDS", str(2 * 60 * 60))),
            max_entries=max_conversations
            or int(os.getenv("SLACK_HISTORY_MAX_CONVERSATIONS", "500")),
        )
        model = os.getenv("PLATFORMER_SLACK_MODEL") or os.getenv(
            "PLATFORMER_CLI_MODEL",
            os.getenv("PLATFORMER_SKILL_MODEL", "openai:gpt-5.4-mini"),
        )
        self.agent = Agent(
            model,
            deps_type=RuntimeDeps,
            instructions=_SLACK_INSTRUCTIONS,
            capabilities=runtime.shared_capabilities(exclude_skills={"filesystem"}),
        )
        self.deps = RuntimeDeps(runtime=runtime)
        # Same Slack message can yield both `app_mention` and `message`; dedupe replies.
        self._delivery_dedup_lock = asyncio.Lock()
        self._delivered_event_keys: dict[str, float] = {}
        self.debug = _env_flag("SLACK_DEBUG")
        self.app = AsyncApp(token=self.bot_token)
        self.app.event("app_mention")(self._on_app_mention)
        self.app.event("message")(self._on_message)
        if self.debug:
            self._install_debug_middleware()

    def _install_debug_middleware(self) -> None:
        """Log raw Slack payloads when SLACK_DEBUG is set (no tokens printed here)."""

        @self.app.middleware  # type: ignore[misc]
        async def _log_slack_payload(logger, body, next):  # noqa: ANN001
            try:
                event = body.get("event") if isinstance(body, dict) else None
                if isinstance(event, dict):
                    msg = (
                        "[slack debug] incoming event "
                        f"type={event.get('type')} subtype={event.get('subtype')} "
                        f"channel={event.get('channel')} channel_type={event.get('channel_type')}"
                    )
                    print(msg, flush=True)
                    logger.info(msg)
                else:
                    keys = list(body.keys()) if isinstance(body, dict) else type(body)
                    print(f"[slack debug] incoming payload keys={keys}", flush=True)
                    logger.info("[slack debug] incoming payload keys=%s", keys)
            except Exception:
                logger.exception("[slack debug] middleware logging failed")
            return await next()

    async def _on_app_mention(self, event: dict, client: AsyncWebClient) -> None:
        if self._should_ignore(event):
            return
        logger.info(
            "Slack app mention received: channel=%s thread_ts=%s ts=%s",
            event.get("channel"),
            event.get("thread_ts"),
            event.get("ts"),
        )
        if self.debug:
            print("Slack app mention received.", flush=True)
        text = _strip_leading_mentions(str(event.get("text") or "")).strip()
        if not text:
            # Mention-only pings were previously dropped; still answer briefly.
            text = "The user @-mentioned you but did not add any other text. Greet them and ask what they need."
        target = ReplyTarget(
            channel=str(event["channel"]),
            thread_ts=str(event.get("thread_ts") or event["ts"]),
        )
        await self._reply(event, text, target, client)

    async def _on_message(self, event: dict, client: AsyncWebClient) -> None:
        if self._should_ignore(event):
            return
        # Socket Mode payloads often omit `channel_type`; 1:1 DM channels use ids
        # starting with `D`. Rely on that plus `channel_type == "im"` when present.
        if not self._is_direct_message(event):
            return
        logger.info(
            "Slack DM received: channel=%s thread_ts=%s ts=%s",
            event.get("channel"),
            event.get("thread_ts"),
            event.get("ts"),
        )
        if self.debug:
            print("Slack DM received.", flush=True)
        text = str(event.get("text") or "").strip()
        if not text:
            return
        target = ReplyTarget(
            channel=str(event["channel"]),
            thread_ts=str(event["thread_ts"]) if event.get("thread_ts") else None,
        )
        await self._reply(event, text, target, client)

    async def _reply(
        self,
        event: dict,
        text: str,
        target: ReplyTarget,
        client: AsyncWebClient,
    ) -> None:
        if await self._is_duplicate_delivery(event):
            return
        key = self._conversation_key(event)
        state = await self.histories.get_or_create(key, ConversationState)
        async with state.lock:
            try:
                run = await run_agent_with_utc(
                    self.agent,
                    text,
                    deps=self.deps,
                    message_history=state.history[-self.max_history_messages :],
                )
                state.history += run.new_messages()
                del state.history[:-self.max_history_messages]
                body = str(run.output or "").strip() or "(no output)"
            except Exception as exc:  # noqa: BLE001
                logger.exception("Slack agent failed while handling event")
                body = f"Sorry, I hit an error while handling that: {exc}"
            finally:
                await self.histories.touch(key)

        message: dict[str, str] = {"channel": target.channel, "text": body}
        if target.thread_ts is not None:
            message["thread_ts"] = target.thread_ts
        try:
            await client.chat_postMessage(**message)
        except SlackApiError as exc:
            err = exc.response.get("error") if exc.response is not None else str(exc)
            print(
                f"Slack chat.postMessage failed: error={err} channel={target.channel}",
                flush=True,
            )
            logger.exception("Slack reply failed: channel=%s thread_ts=%s", target.channel, target.thread_ts)
        except Exception:
            logger.exception("Slack reply failed: channel=%s thread_ts=%s", target.channel, target.thread_ts)

    def _conversation_key(self, event: dict) -> str:
        channel = str(event["channel"])
        if self._is_direct_message(event):
            return f"im:{channel}"
        return f"thread:{channel}:{event.get('thread_ts') or event['ts']}"

    def _should_ignore(self, event: dict) -> bool:
        if event.get("bot_id"):
            return True
        # app_mention payloads should never be dropped just because `subtype` is set.
        if event.get("type") == "app_mention":
            return False
        return bool(event.get("subtype"))

    async def _is_duplicate_delivery(self, event: dict) -> bool:
        """Skip if we already replied for this channel + message ts (Bolt can invoke twice)."""
        channel = str(event.get("channel") or "")
        ts = str(event.get("event_ts") or event.get("ts") or "")
        if not channel or not ts:
            return False
        dedup_key = f"{channel}:{ts}"
        window = 120.0
        async with self._delivery_dedup_lock:
            now = time.monotonic()
            cutoff = now - window
            stale = [k for k, seen_at in self._delivered_event_keys.items() if seen_at < cutoff]
            for k in stale:
                self._delivered_event_keys.pop(k, None)
            if dedup_key in self._delivered_event_keys:
                logger.debug("Skipping duplicate Slack delivery for %s", dedup_key)
                return True
            self._delivered_event_keys[dedup_key] = now
            return False

    @staticmethod
    def _is_direct_message(event: dict) -> bool:
        """True for 1:1 DM. Slack often omits channel_type on message events."""
        if event.get("channel_type") == "im":
            return True
        channel = str(event.get("channel") or "")
        # One-to-one DM channel ids start with D; public/private channels use C/G.
        return channel.startswith("D")

    async def start(self) -> None:
        logger.info("Starting Slack Socket Mode channel")
        print("Starting Slack Socket Mode channel.", flush=True)
        await self._verify_bot_token()
        handler = AsyncSocketModeHandler(self.app, self.app_token)
        await handler.start_async()

    async def _verify_bot_token(self) -> None:
        """Call auth.test so misconfigured tokens fail loudly before Socket Mode."""
        client = AsyncWebClient(token=self.bot_token)
        try:
            auth = await client.auth_test()
        except SlackApiError as exc:
            err = exc.response.get("error") if exc.response is not None else str(exc)
            print(f"Slack auth.test failed: {err}. Check SLACK_BOT_TOKEN in .env.", flush=True)
            return
        except OSError as exc:
            print(f"Slack auth.test network error: {exc}", flush=True)
            return
        team = auth.get("team")
        user = auth.get("user")
        user_id = auth.get("user_id")
        url = auth.get("url")
        print(
            f"Slack auth.test ok: team={team!r} bot_user={user!r} bot_user_id={user_id!r} url={url!r}",
            flush=True,
        )
        print(
            "Channels: type @ and pick THIS app from the list (not a human with the same name). "
            "Plain channel messages are ignored. Tip: SLACK_DEBUG=1 logs incoming event types.",
            flush=True,
        )


def _env_flag(name: str) -> bool:
    """Parse a boolean-ish env var. Treat 0/false/no/off/empty as off."""
    raw = os.getenv(name, "").strip().lower()
    return raw not in {"", "0", "false", "no", "off"}


def _strip_leading_mentions(text: str) -> str:
    """Remove one or more leading user mentions (Slack may send several tokens)."""
    s = text.strip()
    while True:
        next_s = re.sub(r"^<@[^>]+>\s*", "", s)
        if next_s == s:
            break
        s = next_s
    return s
