"""Duplicate flow over an existing Jira issue, offline.

The HTTP *transport* (``request_json``) is mocked with a stateful fake Jira issue, so the ADF
comment, the read-back gate, the target-status matching and the transition logic all run for real
— only the network I/O is canned. The fake can be told to make a comment POST succeed while the
comment never shows up on read-back: that is the case the read-back gate exists for.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "handle_duplicate.py"
)
_spec = importlib.util.spec_from_file_location("handle_duplicate", _MODULE_PATH)
handle_duplicate_mod = importlib.util.module_from_spec(_spec)
# Register before exec: the module defines a @dataclass, and dataclasses resolves field types via
# sys.modules[cls.__module__] — absent, that lookup raises at collection time.
sys.modules["handle_duplicate"] = handle_duplicate_mod
_spec.loader.exec_module(handle_duplicate_mod)

DuplicateJiraClient = handle_duplicate_mod.DuplicateJiraClient
handle_duplicate = handle_duplicate_mod.handle_duplicate
hd = handle_duplicate_mod

_TARGET = "To Do"  # the module default target status


class _FakeTransport:
    """Stateful stand-in for DuplicateJiraClient.request_json — one fake Jira issue."""

    def __init__(self, *, status="En curso", comment_lands=True, transition_succeeds=True):
        self.status = status
        self.comment_lands = comment_lands
        self.transition_succeeds = transition_succeeds
        self.transitions = [  # the shape /transitions really returns (measured on a real project)
            {"id": "11", "name": "Por hacer", "to": {"name": "To Do"}},
            {"id": "21", "name": "En curso", "to": {"name": "En curso"}},
            {"id": "31", "name": "Listo", "to": {"name": "Finalizada"}},
        ]
        self.comments: list[str] = []
        self._next = 1000
        self.transition_posts = 0

    def __call__(self, method, path, *, query=None, body=None):
        if path.endswith("/comment") and method == "POST":
            self._next += 1
            cid = str(self._next)
            if self.comment_lands:
                self.comments.append(cid)
            return 201, {"id": cid}
        if path.endswith("/comment") and method == "GET":
            return 200, {"comments": [{"id": c} for c in self.comments]}
        if path.endswith("/transitions") and method == "GET":
            return 200, {"transitions": self.transitions}
        if path.endswith("/transitions") and method == "POST":
            self.transition_posts += 1
            if not self.transition_succeeds:
                return 400, {"errorMessages": ["transition rejected"]}
            tid = str(((body or {}).get("transition") or {}).get("id"))
            for t in self.transitions:
                if str(t["id"]) == tid:
                    self.status = t["to"]["name"]
            return 204, None
        if "/issue/" in path and method == "GET":  # get_status: /issue/{key}?fields=status
            return 200, {"fields": {"status": {"name": self.status}}}
        raise AssertionError(f"unexpected transport call: {method} {path}")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Let the module default target status ("To Do") rule, unaffected by ambient env.
    monkeypatch.delenv("DUPLICATE_TARGET_STATUS", raising=False)


def _client(transport, monkeypatch):
    monkeypatch.setattr(DuplicateJiraClient, "request_json", transport)
    return DuplicateJiraClient(
        site="example.atlassian.net", space_name="PROJ", email="me@x.com", api_token="tok"
    )


def _run(client):
    return handle_duplicate(
        "PROJ-143", 100, 5, "https://support/tickets/100", "printer catches fire", client=client
    )



def test_happy_path_comments_confirms_and_transitions(monkeypatch):
    transport = _FakeTransport(status="En curso", comment_lands=True, transition_succeeds=True)
    res = _run(_client(transport, monkeypatch))

    assert res["ok"] is True
    assert res["transitioned"] is True
    assert res["comment_id"] is not None
    assert res["status_before"] == "En curso"
    assert res["status_after"] == _TARGET
    assert res["warnings"] == []
    assert transport.transition_posts == 1


def test_already_at_target_is_a_noop_not_an_error(monkeypatch):
    transport = _FakeTransport(status=_TARGET, comment_lands=True)
    res = _run(_client(transport, monkeypatch))

    assert res["ok"] is True
    assert res["transitioned"] is False
    assert res["status_before"] == _TARGET
    assert res["status_after"] == _TARGET
    assert transport.transition_posts == 0  # never attempted a transition
    assert res["comment_id"] is not None  # the context comment was still added


def test_comment_post_ok_but_not_confirmed_blocks_the_transition(monkeypatch):
    # THE KEY TEST: POST returns an id, but the comment never appears on read-back.
    transport = _FakeTransport(status="En curso", comment_lands=False, transition_succeeds=True)
    res = _run(_client(transport, monkeypatch))

    assert res["ok"] is False
    assert res["transitioned"] is False
    assert transport.transition_posts == 0  # the workflow was NOT advanced
    assert res["status_after"] == res["status_before"] == "En curso"
    assert res["comment_id"] is not None  # the POST did "succeed" — the read-back is what caught it
    # MUTATION: drop the read-back check in handle_duplicate and this goes red (it would transition).


def test_transition_fails_but_comment_landed_is_ok_with_warning(monkeypatch):
    transport = _FakeTransport(status="En curso", comment_lands=True, transition_succeeds=False)
    res = _run(_client(transport, monkeypatch))

    assert res["ok"] is True  # the context is safe; only the status move failed
    assert res["transitioned"] is False
    assert res["status_after"] == "En curso"
    assert res["warnings"] and any("transition" in w.lower() for w in res["warnings"])
    assert transport.transition_posts == 1  # it was attempted, and it failed


class TestTheFreshdeskReferenceNamesWhatTheIdActuallyIs:
    """Same audit finding as create_jira_issue: the comment said "Konversation" about a thread id."""

    def test_conversation_id_is_rejected_by_argparse(self):
        with pytest.raises(SystemExit):
            hd._build_parser().parse_args([
                "--jira-key", "PROJ-1", "--ticket-id", "100", "--freshdesk-url", "http://x",
                "--problem-text", "p", "--conversation-id", "5"])

    def test_the_anchor_flag_is_optional(self):
        args = hd._build_parser().parse_args([
            "--jira-key", "PROJ-1", "--ticket-id", "100", "--freshdesk-url", "http://x",
            "--problem-text", "p"])
        assert args.thread_anchor_note_id is None

    def test_the_comment_names_the_anchor_when_given(self):
        text = hd._duplicate_comment(100, 900000000002, "http://x", "problem")
        assert "Thread anchor note: 900000000002" in text
        assert "Konversation" not in text

    def test_the_comment_omits_the_line_when_absent(self):
        text = hd._duplicate_comment(100, None, "http://x", "problem")
        assert "Thread anchor note" not in text
        assert "Freshdesk-Ticket: 100" in text
