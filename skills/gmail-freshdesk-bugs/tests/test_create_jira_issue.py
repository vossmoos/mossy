"""Create-and-remember for a new Jira bug (flow step 6), offline.

The HTTP transport (``request_json``) is mocked with a stateful fake Jira, and ``remember`` is
spied on (not the whole cache), so the test proves the behaviour that matters: the issue is cached
only after a successful read-back. The fake can make the create POST return a key while the issue
does not resolve on read-back — the phantom case the gate exists for.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "create_jira_issue.py"
)
_spec = importlib.util.spec_from_file_location("create_jira_issue", _MODULE_PATH)
cji = importlib.util.module_from_spec(_spec)
sys.modules["create_jira_issue"] = cji  # the module defines a @dataclass
_spec.loader.exec_module(cji)

CreateJiraClient = cji.CreateJiraClient
create_and_remember = cji.create_and_remember


class _FakeTransport:
    """Stateful stand-in for CreateJiraClient.request_json."""

    def __init__(self, *, users, created_key="PROJ-500", issue_resolves=True):
        self.users = users
        self.created_key = created_key
        self.issue_resolves = issue_resolves
        self.create_posts = 0
        self.created_payload = None

    def __call__(self, method, path, *, query=None, body=None):
        if path == "/user/assignable/search" and method == "GET":
            return 200, self.users
        if path == "/issue" and method == "POST":
            self.create_posts += 1
            self.created_payload = body
            return 201, {"id": "10500", "key": self.created_key}
        if path.startswith("/issue/") and method == "GET":  # read-back
            if self.issue_resolves:
                return 200, {"key": self.created_key, "id": "10500"}
            return 404, {"errorMessages": ["Issue does not exist"]}
        raise AssertionError(f"unexpected transport call: {method} {path}")


class _RememberSpy:
    def __init__(self):
        self.calls = []

    def __call__(self, jira_key, jira_url, ticket_id, anchor_note_id, summary, problem, *,
                 db_path=None):
        self.calls.append({"jira_key": jira_key, "jira_url": jira_url, "ticket_id": ticket_id,
                           "anchor_note_id": anchor_note_id})
        return {"ok": True, "jira_key": jira_key}


def _client(transport, monkeypatch):
    monkeypatch.setattr(CreateJiraClient, "request_json", transport)
    return CreateJiraClient(
        site="example.atlassian.net", space_name="PROJ", email="me@x.com", api_token="tok"
    )


def _run(client, monkeypatch):
    spy = _RememberSpy()
    monkeypatch.setattr(cji.dedupe_cache, "remember", spy)
    monkeypatch.setenv("JIRA_DEFAULT_ASSIGNEE", "Ana Beispiel")
    res = create_and_remember(
        "Fix the extraction", "The JSON must be validated.", 100, 5,
        "https://support/tickets/100", client=client,
    )
    return res, spy


def test_happy_path_creates_confirms_and_caches(monkeypatch):
    users = [{"accountId": "5b:ana", "displayName": "Ana Beispiel", "emailAddress": "ana@x.com"}]
    transport = _FakeTransport(users=users, issue_resolves=True)
    res, spy = _run(_client(transport, monkeypatch), monkeypatch)

    assert res["ok"] is True
    assert res["jira_key"] == "PROJ-500"
    assert res["jira_url"] == "https://example.atlassian.net/browse/PROJ-500"
    assert res["assignee_set"] is True
    assert res["cached"] is True
    assert res["warnings"] == []
    assert len(spy.calls) == 1 and spy.calls[0]["jira_key"] == "PROJ-500"


def test_issue_not_confirmed_on_readback_is_not_cached(monkeypatch):
    # THE KEY TEST: the create POST returns a key, but read-back cannot find the issue.
    users = [{"accountId": "5b:ana", "displayName": "Ana Beispiel", "emailAddress": "ana@x.com"}]
    transport = _FakeTransport(users=users, issue_resolves=False)
    res, spy = _run(_client(transport, monkeypatch), monkeypatch)

    assert res["ok"] is False
    assert res["cached"] is False
    assert transport.create_posts == 1  # the issue POST did "succeed"...
    assert spy.calls == []              # ...but nothing was written to the cache
    # MUTATION: drop the read-back gate in create_and_remember and this goes red (it would cache).


def test_unresolved_assignee_still_creates_and_caches_with_warning(monkeypatch):
    # Two "Ana" candidates, neither an exact match -> ambiguous -> no assignee.
    users = [
        {"accountId": "1", "displayName": "Ana Eins", "emailAddress": "a1@x.com"},
        {"accountId": "2", "displayName": "Ana Zwei", "emailAddress": "a2@x.com"},
    ]
    transport = _FakeTransport(users=users, issue_resolves=True)
    res, spy = _run(_client(transport, monkeypatch), monkeypatch)

    assert res["ok"] is True
    assert res["assignee_set"] is False
    assert res["cached"] is True             # the issue exists, so it IS cached
    assert res["warnings"] and any("assignee" in w.lower() for w in res["warnings"])
    assert len(spy.calls) == 1


class TestTheFreshdeskReferenceNamesWhatTheIdActuallyIs:
    """The audit finding: the flow stopped emitting a conversation id and this kept demanding one.

    Following SKILL.md literally, an agent would have passed the thread's anchor note to
    ``--conversation-id`` and written "Conversation: <anchor>" into a Jira issue this account
    cannot delete. The flag is renamed, optional, and the line says what the id is.
    """

    def test_conversation_id_is_rejected_by_argparse(self):
        with pytest.raises(SystemExit):
            cji._build_parser().parse_args([
                "--summary", "s", "--problem", "p", "--ticket-id", "100",
                "--freshdesk-url", "http://x", "--conversation-id", "5"])

    def test_the_anchor_flag_is_optional(self):
        args = cji._build_parser().parse_args([
            "--summary", "s", "--problem", "p", "--ticket-id", "100",
            "--freshdesk-url", "http://x"])
        assert args.thread_anchor_note_id is None

    def test_it_works_without_the_anchor(self, monkeypatch):
        transport = _FakeTransport(users=[], issue_resolves=True)
        monkeypatch.setattr(cji.dedupe_cache, "remember", _RememberSpy())
        res = create_and_remember("s", "p", 100, "https://support/tickets/100",
                                  client=_client(transport, monkeypatch))
        assert res["ok"] is True

    def test_the_description_names_the_anchor_when_given(self, monkeypatch):
        transport = _FakeTransport(users=[], issue_resolves=True)
        monkeypatch.setattr(cji.dedupe_cache, "remember", _RememberSpy())
        create_and_remember("s", "p", 100, "https://support/tickets/100",
                            thread_anchor_note_id=900000000002,
                            client=_client(transport, monkeypatch))
        body = json.dumps(transport.created_payload)
        assert "Thread anchor note: 900000000002" in body
        assert "Conversation:" not in body, (
            "the id identifies the thread, not a conversation; the old wording claimed otherwise"
        )

    def test_the_description_omits_the_line_when_absent(self, monkeypatch):
        transport = _FakeTransport(users=[], issue_resolves=True)
        monkeypatch.setattr(cji.dedupe_cache, "remember", _RememberSpy())
        create_and_remember("s", "p", 100, "https://support/tickets/100",
                            client=_client(transport, monkeypatch))
        body = json.dumps(transport.created_payload)
        assert "Thread anchor note" not in body and "Conversation" not in body
