"""The Teams channel: reply formatting, caller ids, the message flow and startup wiring."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("botbuilder.core")

from fastapi.testclient import TestClient  # noqa: E402

from mossy.channels.http.app import create_app  # noqa: E402
from mossy.channels.slack.app import ConversationState, TTLStore  # noqa: E402
from mossy.channels.teams import app as teams  # noqa: E402
from mossy.channels.teams.config import teams_configured  # noqa: E402
from mossy.runtime.core import Runtime  # noqa: E402


def test_format_reply_converts_html(monkeypatch) -> None:
    monkeypatch.setenv("MOSSY_PUBLIC_BASE_URL", "https://bot.example/")
    text = teams.format_reply(
        '<p><b>Done</b></p><ul><li>one</li></ul><a href="/files/r.pdf">Report</a>'
        '<img src="/files/c.png" alt="Chart">\nSee /files/x.csv'
    )
    assert "**Done**" in text
    assert "- one" in text
    assert "[Report](https://bot.example/files/r.pdf)" in text
    assert "![Chart](https://bot.example/files/c.png)" in text
    assert "See https://bot.example/files/x.csv" in text
    assert "<" not in text


def test_format_reply_without_a_base_url_and_empty(monkeypatch) -> None:
    monkeypatch.delenv("MOSSY_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    assert "(/files/r.pdf)" in teams.format_reply('<a href="/files/r.pdf">R</a>')
    assert teams.format_reply("") == "(no output)"


def test_caller_ids_never_use_the_display_name() -> None:
    sender = SimpleNamespace(aad_object_id="aad-1", id="29:abc", name="Ada Lovelace")
    assert teams.caller_ids_from_activity(SimpleNamespace(from_property=sender)) == {
        "aad-1",
        "29:abc",
    }
    assert teams.caller_ids_from_activity(SimpleNamespace(from_property=None)) == frozenset()


def _channel() -> teams.TeamsChannel:
    channel = teams.TeamsChannel.__new__(teams.TeamsChannel)
    channel.runtime = MagicMock()
    channel.agent = MagicMock()
    channel.max_history_messages = 40
    channel.histories = TTLStore[ConversationState](ttl_seconds=60, max_entries=5)
    return channel


def _turn(role: str = "user") -> MagicMock:
    activity = teams.Activity(type="message", text="hello", id="m1", channel_id="msteams")
    activity.from_property = SimpleNamespace(role=role, aad_object_id="aad-1", id="29:abc")
    activity.conversation = SimpleNamespace(id="c1", conversation_type="personal")
    activity.entities = []
    activity.recipient = SimpleNamespace(id="bot")
    turn = MagicMock(activity=activity)
    turn.sent = []

    async def send(item):
        turn.sent.append(item)

    turn.send_activity = send
    return turn


def test_message_runs_the_agent_with_the_caller_and_replies(monkeypatch) -> None:
    monkeypatch.setenv("TEAMS_TYPING_PERIOD_SECONDS", "1")
    seen: dict = {}

    async def fake_run(agent, text, *, deps, capabilities, message_history):
        seen.update(text=text, deps=deps)
        return MagicMock(output="<b>hi</b>", new_messages=lambda: [])

    channel, turn = _channel(), _turn()
    with patch.object(teams, "run_agent_with_utc", fake_run):
        asyncio.run(channel.handle_message(turn))
    assert seen["text"] == "hello"
    assert seen["deps"].caller_ids == {"aad-1", "29:abc"}
    assert channel.runtime.channel_capabilities.call_args.args == ("TEAMS",)
    replies = [item for item in turn.sent if item.type == "message"]
    assert [reply.text for reply in replies] == ["**hi**"]


def test_bot_messages_are_ignored() -> None:
    channel, turn = _channel(), _turn(role="bot")
    asyncio.run(channel.handle_message(turn))
    assert turn.sent == []


def test_agent_failure_becomes_a_reply(monkeypatch) -> None:
    async def fake_run(*_args, **_kwargs):
        raise RuntimeError("boom")

    channel, turn = _channel(), _turn()
    with patch.object(teams, "run_agent_with_utc", fake_run):
        asyncio.run(channel.handle_message(turn))
    assert "boom" in [item for item in turn.sent if item.type == "message"][0].text


def _routes(app) -> set[str]:
    return {getattr(route, "path", "") for route in app.routes}


def test_not_mounted_without_credentials(monkeypatch) -> None:
    for name in ("MICROSOFT_APP_ID", "MICROSOFT_APP_PASSWORD", "MicrosoftAppId", "MicrosoftAppPassword"):
        monkeypatch.delenv(name, raising=False)
    assert not teams_configured()
    app = create_app(
        Runtime(enable_duties=False),
        enable_agui=False, enable_aui=False, enable_mcp=False, enable_a2a=False,
    )
    assert "/api/messages" not in _routes(app)


def test_mounted_and_key_exempt_with_credentials(monkeypatch) -> None:
    monkeypatch.setenv("MICROSOFT_APP_ID", "app")
    monkeypatch.setenv("MICROSOFT_APP_PASSWORD", "pw")
    monkeypatch.setenv("MOSSY_API_KEY", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "not-used")  # the agent is built but never run
    app = create_app(
        Runtime(enable_duties=False),
        enable_agui=False, enable_aui=False, enable_mcp=False, enable_a2a=False,
    )
    assert "/api/messages" in _routes(app)
    client = TestClient(app, raise_server_exceptions=False)
    # Past the API-key middleware: refused for its content type, not with the key's 401.
    assert client.post("/api/messages", content="x").status_code == 415
    assert client.get("/queue").status_code == 401
    # A request with no Bot Framework token is refused by the adapter, not a server error.
    response = client.post("/api/messages", json={"type": "message"})
    assert response.status_code == 401
