"""The A2A channel: card, bearer check, read-only tools, custom tools, and app lifespans."""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic_ai.models.test import TestModel

from mossy.channels.a2a import app as a2a
from mossy.channels.http.app import create_app
from mossy.runtime.core import Runtime
from mossy.runtime.models import TaskStatus

CARD = a2a.A2A_CARD_URL_PATH
TASK_BODY = {"jsonrpc": "2.0", "id": "1", "method": "tasks/get", "params": {"id": "missing"}}


def sample_tool(name: str) -> str:
    """Say hello."""
    return f"hello {name}"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MOSSY_API_KEY", "secret")
    monkeypatch.delenv("HTTP_API_KEY", raising=False)
    monkeypatch.setenv("MOSSY_A2A_NAME", "probe")
    monkeypatch.setenv("OPENAI_API_KEY", "not-used")  # the MCP channel builds its agent at startup
    monkeypatch.setattr(a2a, "infer_model", lambda _name: TestModel())
    app = create_app(
        Runtime(enable_duties=False), enable_agui=False, enable_aui=False, enable_teams=False
    )
    with TestClient(app) as test_client:
        yield test_client


def test_card_is_public_and_generic(client) -> None:
    response = client.get(CARD)
    assert response.status_code == 200
    assert response.json()["name"] == "probe"


def test_task_endpoint_needs_the_key(client) -> None:
    assert client.post("/a2a/", json=TASK_BODY).status_code == 401
    bad = {"Authorization": "Bearer wrong"}
    assert client.post("/a2a/", json=TASK_BODY, headers=bad).status_code == 401
    good = {"Authorization": "Bearer secret"}
    assert client.post("/a2a/", json=TASK_BODY, headers=good).status_code == 200


def test_mcp_and_a2a_lifespans_run_together(client) -> None:
    assert client.get("/health").status_code in (200, 503)
    assert client.get(CARD).status_code == 200
    response = client.post(
        "/mcp/",
        json={},
        headers={"Authorization": "Bearer secret", "accept": "application/json, text/event-stream"},
    )
    assert response.status_code != 500


def test_without_a_key_every_task_request_is_refused(monkeypatch) -> None:
    monkeypatch.delenv("MOSSY_API_KEY", raising=False)
    monkeypatch.delenv("HTTP_API_KEY", raising=False)
    inner_called = []

    async def inner(scope, receive, send):
        inner_called.append(scope)

    app = FastAPI()
    app.mount("/a2a", a2a.RequireBearer(inner))
    response = TestClient(app).post(
        "/a2a/", json=TASK_BODY, headers={"Authorization": "Bearer "}
    )
    assert response.status_code == 401
    assert not inner_called


def test_a_model_that_cannot_be_built_mounts_nothing(monkeypatch, capsys) -> None:
    def boom(_name):
        raise ValueError("no key")

    monkeypatch.setattr(a2a, "infer_model", boom)
    assert a2a.register_a2a_routes(FastAPI(), Runtime(enable_duties=False)) is None
    assert "NOT enabled" in capsys.readouterr().err


def test_built_in_tools_are_read_only_and_report_state() -> None:
    runtime = Runtime(enable_duties=False)

    async def scenario():
        task = await runtime.submit_goal("work")
        task.status = TaskStatus.DONE
        status = await a2a.task_status_tool(runtime).function(task.id)
        missing = await a2a.task_status_tool(runtime).function("nope")
        return task, status, missing

    task, status, missing = asyncio.run(scenario())
    report = a2a.agent_status_report(runtime)
    assert report["tasks_by_status"]["done"] == 1
    assert report["started_at"] == runtime.started_at.isoformat()
    assert status["id"] == task.id and status["status"] == "done"
    assert missing["error"] == "unknown task"
    assert set(a2a.a2a_toolset(runtime).tools) == {"agent_status", "task_status"}


def test_custom_tools_load_and_bad_ones_are_skipped(capsys) -> None:
    tools = a2a.custom_tools(f"{__name__}:sample_tool, nope.missing:tool, {__name__}:CARD")
    assert tools == [sample_tool]
    err = capsys.readouterr().err
    assert "nope.missing:tool" in err and f"{__name__}:CARD" in err


def test_custom_tools_join_the_toolset(monkeypatch) -> None:
    monkeypatch.setenv(a2a.TOOLS_ENV, f"{__name__}:sample_tool")
    toolset = a2a.a2a_toolset(Runtime(enable_duties=False))
    assert "sample_tool" in toolset.tools


def test_a_message_runs_the_agent_end_to_end(client) -> None:
    headers = {"Authorization": "Bearer secret"}
    send = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "kind": "message",
                "messageId": "m1",
                "parts": [{"kind": "text", "text": "status?"}],
            }
        },
    }
    task = client.post("/a2a/", json=send, headers=headers).json()["result"]["task"]
    for _ in range(100):
        body = {"jsonrpc": "2.0", "id": "2", "method": "tasks/get", "params": {"id": task["id"]}}
        result = client.post("/a2a/", json=body, headers=headers).json()["result"]
        if result["status"]["state"] in ("completed", "failed"):
            break
        time.sleep(0.02)
    assert result["status"]["state"] == "completed"
