"""Microsoft Teams Bot Framework channel for conversational Mossy."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from html import unescape
from http import HTTPStatus
from typing import TYPE_CHECKING

from botbuilder.core import ActivityHandler, ShowTypingMiddleware, TurnContext
from botbuilder.integration.aiohttp import CloudAdapter, ConfigurationBotFrameworkAuthentication
from botbuilder.schema import Activity, ActivityTypes
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic_ai import Agent

from mossy.channels.slack.app import ConversationState, TTLStore
from mossy.channels.teams.config import bot_credentials, teams_path
from mossy.runtime.agent_run import run_agent_with_utc
from mossy.runtime.deps import RuntimeDeps
from mossy.runtime.message_history import trim_message_history

if TYPE_CHECKING:
    from mossy.runtime import Runtime

logger = logging.getLogger(__name__)

_TEAMS_INSTRUCTIONS = """You are Mossy's Microsoft Teams assistant.

You have access to agentic skills through the skills tools. Use skills immediately when they help
answer the user or perform an action. Use the system-queue skill when work should be queued,
inspected, cancelled, scheduled, or allowed to continue independently. Do not enqueue by default:
answer directly when the request can be resolved in the Teams turn.

Queued tasks do not automatically post their final result back to Teams. When you enqueue work,
include the task id in your reply and tell the user how to check on it.

Each user message is prefixed with `[System UTC now: ...]` — use it as the authoritative clock for
relative scheduling ("in 1 minute", "tomorrow"): compute scheduled_for in UTC from that line, not from
memory.

Keep replies concise and readable. Use markdown."""

_HTML_ANCHOR_RE = re.compile(
    r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_HTML_IMG_RE = re.compile(
    r'<img\b[^>]*\bsrc=["\']([^"\']+)["\'][^>]*/?>',
    re.IGNORECASE,
)
_FILES_HREF_RE = re.compile(r"(?<![\w/])/files/[A-Za-z0-9._~%/-]+", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def _public_base_url() -> str:
    return (
        os.getenv("MOSSY_PUBLIC_BASE_URL")
        or os.getenv("PUBLIC_BASE_URL")
        or ""
    ).strip().rstrip("/")


def _absolutize_files_url(url: str) -> str:
    """Teams has no page origin to resolve a relative ``/files/...`` link against."""
    raw = (url or "").strip()
    base = _public_base_url()
    if not raw.startswith("/files/") or not base:
        return raw
    return f"{base}{raw}"


def _strip_tags(text: str) -> str:
    return unescape(_TAG_RE.sub("", text or "")).strip()


def _img_alt(tag: str) -> str:
    match = re.search(r'\balt=["\']([^"\']*)["\']', tag or "", flags=re.IGNORECASE)
    return _strip_tags(match.group(1)) if match else ""


def format_reply(body: str) -> str:
    """Convert HTML (download anchors and images especially) into Teams-friendly markdown."""
    text = str(body or "").strip() or "(no output)"

    def replace_anchor(match: re.Match[str]) -> str:
        href = _absolutize_files_url(unescape(match.group(1).strip()))
        label = _strip_tags(match.group(2)) or href
        return f"[{label}]({href})"

    def replace_img(match: re.Match[str]) -> str:
        src = _absolutize_files_url(unescape(match.group(1).strip()))
        # A short alt keeps the markdown image on one line, so clients do not soft-wrap the URL.
        alt = _img_alt(match.group(0)) or "image"
        if len(alt) > 40:
            alt = alt[:37] + "..."
        return f"![{alt}]({src})\n"

    # Images first so nested markup inside alt text is not an issue.
    text = _HTML_IMG_RE.sub(replace_img, text)
    text = _HTML_ANCHOR_RE.sub(replace_anchor, text)
    text = _FILES_HREF_RE.sub(lambda match: _absolutize_files_url(match.group(0)), text)

    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</li\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(ul|ol)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?h[1-6][^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(strong|b)(\s[^>]*)?>", "**", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(em|i)(\s[^>]*)?>", "_", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    text = unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def caller_ids_from_activity(activity: Activity) -> frozenset[str]:
    """Stable caller ids for skill access. Never the display name: anyone can share one."""
    sender = getattr(activity, "from_property", None)
    ids = {
        value.strip()
        for value in (
            getattr(sender, "aad_object_id", None),
            getattr(sender, "id", None),
        )
        if isinstance(value, str) and value.strip()
    }
    return frozenset(ids)


class _TeamsConfig:
    """Bot Framework auth settings (supports Mossy and Microsoft env var names)."""

    def __init__(self) -> None:
        self.PORT = 8765
        self.APP_ID, self.APP_PASSWORD = bot_credentials()
        self.APP_TYPE = os.environ.get("MicrosoftAppType", "MultiTenant")
        self.APP_TENANTID = os.environ.get("MicrosoftAppTenantId", "")


class MossyTeamsBot(ActivityHandler):
    """Route Bot Framework activities into the Mossy Teams channel."""

    def __init__(self, channel: "TeamsChannel") -> None:
        self._channel = channel

    async def on_message_activity(self, turn_context: TurnContext) -> None:
        await self._channel.handle_message(turn_context)


class TeamsChannel:
    """Run a CLI-like Mossy agent over Microsoft Teams / Bot Framework."""

    def __init__(
        self,
        runtime: "Runtime",
        *,
        app_id: str | None = None,
        app_password: str | None = None,
        history_ttl_seconds: float | None = None,
        max_conversations: int | None = None,
        max_history_messages: int | None = None,
        path: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.path = (path or teams_path()).rstrip("/") or "/api/messages"
        config = _TeamsConfig()
        self.app_id = (app_id or config.APP_ID).strip()
        self.app_password = (app_password or config.APP_PASSWORD).strip()
        if not self.app_id or not self.app_password:
            raise ValueError("MICROSOFT_APP_ID and MICROSOFT_APP_PASSWORD are required for Teams")
        config.APP_ID = self.app_id
        config.APP_PASSWORD = self.app_password

        self.max_history_messages = max_history_messages or int(
            os.getenv("TEAMS_HISTORY_MAX_MESSAGES", "40")
        )
        self.histories = TTLStore[ConversationState](
            ttl_seconds=history_ttl_seconds
            or float(os.getenv("TEAMS_HISTORY_TTL_SECONDS", str(2 * 60 * 60))),
            max_entries=max_conversations
            or int(os.getenv("TEAMS_HISTORY_MAX_CONVERSATIONS", "500")),
        )
        model = os.getenv("PLATFORMER_TEAMS_MODEL") or os.getenv(
            "PLATFORMER_CLI_MODEL",
            os.getenv("PLATFORMER_SKILL_MODEL", "openai:gpt-5.4-mini"),
        )
        self.agent = Agent(
            model,
            deps_type=RuntimeDeps,
            instructions=_TEAMS_INSTRUCTIONS,
            retries=3,  # recover from a failed tool call instead of breaking the run
        )
        self.adapter = CloudAdapter(ConfigurationBotFrameworkAuthentication(config))
        self.adapter.on_turn_error = self._on_turn_error
        # First indicator quickly; the middleware alone is unreliable for long runs, so
        # handle_message also runs an explicit typing loop.
        self.adapter.use(ShowTypingMiddleware(delay=0.5, period=2.0))
        self.bot = MossyTeamsBot(self)

    async def _on_turn_error(self, turn_context: TurnContext, error: Exception) -> None:
        logger.error("Teams agent failed while handling activity", exc_info=error)
        await turn_context.send_activity(f"Sorry, I hit an error while handling that: {error}")

    async def _typing_loop(self, turn_context: TurnContext, stop: asyncio.Event) -> None:
        """Keep the Teams typing indicator alive during long skill runs."""
        period = float(os.getenv("TEAMS_TYPING_PERIOD_SECONDS", "2.5"))
        while not stop.is_set():
            try:
                await turn_context.send_activity(Activity(type=ActivityTypes.typing))
            except Exception:  # noqa: BLE001 - typing must never fail the turn
                logger.debug("Teams typing indicator send failed", exc_info=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(1.0, period))
            except asyncio.TimeoutError:
                continue

    async def handle_message(self, turn_context: TurnContext) -> None:
        activity = turn_context.activity
        if activity.from_property and getattr(activity.from_property, "role", None) == "bot":
            return

        text = (TurnContext.remove_recipient_mention(activity) or activity.text or "").strip()
        if not text:
            text = (
                "The user @-mentioned you but did not add any other text. "
                "Greet them and ask what they need."
            )

        key = self._conversation_key(activity)
        state = await self.histories.get_or_create(key, ConversationState)
        caller_ids = caller_ids_from_activity(activity)
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(self._typing_loop(turn_context, stop_typing))
        try:
            async with state.lock:
                try:
                    run = await run_agent_with_utc(
                        self.agent,
                        text,
                        deps=RuntimeDeps(runtime=self.runtime, caller_ids=caller_ids),
                        capabilities=self.runtime.channel_capabilities(
                            "TEAMS", caller_ids=caller_ids, exclude_skills={"filesystem"}
                        ),
                        message_history=trim_message_history(
                            state.history, self.max_history_messages
                        ),
                    )
                    state.history[:] = trim_message_history(
                        state.history + run.new_messages(), self.max_history_messages
                    )
                    body = str(run.output or "").strip() or "(no output)"
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Teams agent failed while handling message")
                    body = f"Sorry, I hit an error while handling that: {exc}"
                finally:
                    await self.histories.touch(key)
        finally:
            stop_typing.set()
            await typing_task

        await turn_context.send_activity(
            Activity(type=ActivityTypes.message, text=format_reply(body), text_format="markdown")
        )

    def _conversation_key(self, activity: Activity) -> str:
        conv_id = str(activity.conversation.id)
        if activity.conversation.conversation_type == "personal":
            return f"im:{conv_id}"
        root = activity.reply_to_id or activity.id
        return f"thread:{conv_id}:{root}"

    async def handle_request(self, request: Request) -> Response:
        content_type = request.headers.get("content-type", "")
        if "application/json" not in content_type:
            return Response(status_code=HTTPStatus.UNSUPPORTED_MEDIA_TYPE)

        activity = Activity().deserialize(await request.json())
        auth_header = request.headers.get("Authorization", "")

        async def call_bot(turn_context: TurnContext) -> None:
            await self.bot.on_turn(turn_context)

        try:
            invoke_response = await self.adapter.process_activity(auth_header, activity, call_bot)
        except PermissionError:  # the adapter's answer to a missing or invalid token
            return Response(status_code=HTTPStatus.UNAUTHORIZED)
        if invoke_response:
            return JSONResponse(content=invoke_response.body, status_code=invoke_response.status)
        return Response(status_code=HTTPStatus.OK)

    def register(self, app: FastAPI) -> None:
        @app.post(self.path)
        @app.post(f"{self.path}/")
        async def teams_messages(request: Request) -> Response:
            return await self.handle_request(request)
