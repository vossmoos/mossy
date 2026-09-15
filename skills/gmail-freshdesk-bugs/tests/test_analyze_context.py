"""Context analysis of a mention -> Jira-ready problem (the contract was changed by measurement).

Freshdesk and the model are both mocked. The Freshdesk client is a fake with a fixed thread; the
model call (``_chat``) is patched to capture the exact prompt and return canned JSON.

**The contract these tests pin was rewritten because measurement invalidated the old one.**
``analyze`` used to take a ``--conversation-id`` and pick "the mentioned conversation" out of the
API. Measurement found four thread messages that produced a notification: exactly one was a
conversation, and only because it happened to be the note its thread hangs from. So the mention is
now passed in as text, the whole ticket is context, and no conversation is selected at all.

``--conversation-id`` is gone rather than deprecated, and a test asserts argparse rejects it: the
e-mail's ``X-FD-Note-Id`` is the thread anchor and repeats across notifications while the text
changes, so any surviving flag would eventually receive it and produce an issue about the wrong
content.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "analyze_context.py"
)
_spec = importlib.util.spec_from_file_location("analyze_context", _MODULE_PATH)
analyze_context = importlib.util.module_from_spec(_spec)
sys.modules["analyze_context"] = analyze_context  # the module defines a @dataclass
_spec.loader.exec_module(analyze_context)


class _FakeFreshdesk:
    """A fake AnalyzeFreshdeskClient: fixed ticket + a fixed conversation thread."""

    domain = "demo-account.freshdesk.com"

    def __init__(self, subject, conversations):
        self._subject = subject
        self._conversations = conversations

    def get_ticket(self, ticket_id):
        return {"id": ticket_id, "subject": self._subject}

    def get_ticket_conversations(self, ticket_id):
        return list(self._conversations)


#: The mention, as ``parse_mention_email.py`` hands it over: text, never an id.
MENTION = "MENTION-BODY please review the triage quality on this ticket"


def _thread():
    """Three conversations. None of them is the mention — that is the measured situation."""
    return [
        {"id": 111, "private": False, "body_text": "FIRST-BODY end customer describes the incident"},
        {"id": 222, "private": True, "body_text": "ANCHOR-BODY the note this thread hangs from"},
        {"id": 333, "private": False, "body_text": "LAST-BODY later reply"},
    ]


def test_an_empty_mention_is_refused_without_calling_the_model(monkeypatch):
    """An empty mention would become a Jira issue with no description that reads like a real one."""
    calls = {"n": 0}

    def _no_model(system_prompt, user_prompt):
        calls["n"] += 1
        return "{}"

    monkeypatch.setattr(analyze_context, "_chat", _no_model)
    fd = _FakeFreshdesk("Serviceauftrag 10462579", _thread())

    res = analyze_context.analyze(100, "", fd_client=fd)

    assert res["ok"] is False
    assert res["reason"] == "empty_mention_text"
    assert calls["n"] == 0


def test_whitespace_only_counts_as_empty(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(analyze_context, "_chat",
                        lambda s, u: calls.__setitem__("n", calls["n"] + 1) or "{}")
    fd = _FakeFreshdesk("subject", _thread())
    res = analyze_context.analyze(100, "   \n\t ", fd_client=fd)
    assert res["ok"] is False and res["reason"] == "empty_mention_text"
    assert calls["n"] == 0


def test_the_mention_is_what_gets_analysed_and_the_thread_is_only_context(monkeypatch):
    captured = {}

    def _capture(system_prompt, user_prompt):
        captured["system"] = system_prompt
        captured["user"] = user_prompt
        return '{"summary": "Review triage agent answer", "problem": "We are asked to review."}'

    monkeypatch.setattr(analyze_context, "_chat", _capture)
    fd = _FakeFreshdesk("Serviceauftrag 10462579", _thread())

    res = analyze_context.analyze(100, MENTION, thread_anchor_note_id=222, fd_client=fd)

    assert res["ok"] is True
    assert res["summary"] == "Review triage agent answer"
    assert res["ticket_subject"] == "Serviceauftrag 10462579"
    assert res["freshdesk_url"] == "https://demo-account.freshdesk.com/a/tickets/100"

    user = captured["user"]
    target = user.split("=== MENTIONED THREAD")[1].split("=== THE TICKET THREAD")[0]
    context = user.split("=== THE TICKET THREAD")[1]

    # MUTATION: if the analyser used the thread anchor note as the text to resolve instead of the
    # mention, ANCHOR-BODY would sit in the target section and MENTION-BODY would be absent. Both
    # assertions below go red.
    assert "MENTION-BODY" in target
    assert "ANCHOR-BODY" not in target
    assert "FIRST-BODY" not in target

    # The whole ticket is context — every conversation, not a slice of them.
    for body in ("FIRST-BODY", "ANCHOR-BODY", "LAST-BODY"):
        assert body in context, body


def test_the_anchor_note_is_marked_in_the_context_when_it_is_there(monkeypatch):
    captured = {}
    monkeypatch.setattr(analyze_context, "_chat",
                        lambda s, u: captured.setdefault("user", u) and None or '{"summary":"s","problem":"p"}')
    fd = _FakeFreshdesk("subject", _thread())
    res = analyze_context.analyze(100, MENTION, thread_anchor_note_id=222, fd_client=fd)
    assert res["anchor_note_in_thread"] is True
    assert "[the note this thread hangs from]" in captured["user"]


def test_an_anchor_that_is_not_in_the_thread_is_reported_not_invented(monkeypatch):
    """The measured normal case: the anchor is often not among the ticket's conversations."""
    monkeypatch.setattr(analyze_context, "_chat", lambda s, u: '{"summary":"s","problem":"p"}')
    fd = _FakeFreshdesk("subject", _thread())
    res = analyze_context.analyze(100, MENTION, thread_anchor_note_id=999999, fd_client=fd)
    assert res["ok"] is True, "a missing anchor is context we lack, not a reason to refuse"
    assert res["anchor_note_in_thread"] is False


class TestTheOutputCarriesTheAnchorAndNotAConversation:
    def test_conversation_id_is_gone_from_the_output(self, monkeypatch):
        monkeypatch.setattr(analyze_context, "_chat", lambda s, u: '{"summary":"s","problem":"p"}')
        fd = _FakeFreshdesk("subject", _thread())
        res = analyze_context.analyze(100, MENTION, thread_anchor_note_id=222, fd_client=fd)
        assert "conversation_id" not in res
        assert res["thread_anchor_note_id"] == 222


class TestTheThreadLinkSurvivesTheAnalysis:
    """The notification url points at the thread (``?thread=N``); rebuilding it loses that.

    Measured on an end-to-end run: ``parse_mention_email.py`` emitted
    ``.../a/tickets/101?thread=2&follow=email`` and the Jira issue was filed with
    ``.../a/tickets/101`` — whoever opened it landed on the ticket and had to hunt for the
    thread we were actually mentioned in.
    """

    URL_FROM_EMAIL = "https://example.freshdesk.com/a/tickets/101?thread=2&follow=email"

    def test_a_url_from_the_email_comes_back_untouched(self, monkeypatch):
        monkeypatch.setattr(analyze_context, "_chat", lambda s, u: '{"summary":"s","problem":"p"}')
        fd = _FakeFreshdesk("subject", _thread())
        res = analyze_context.analyze(101, MENTION, thread_anchor_note_id=222,
                                      freshdesk_url=self.URL_FROM_EMAIL, fd_client=fd)
        assert res["freshdesk_url"] == self.URL_FROM_EMAIL
        # The part that gets lost by rebuilding, named explicitly so the mutation is legible.
        assert "?thread=2" in res["freshdesk_url"]
        # And it is not the fake client's domain: the url was carried, not composed.
        assert fd.domain not in res["freshdesk_url"]

    def test_without_one_the_ticket_url_is_rebuilt_as_before(self, monkeypatch):
        monkeypatch.setattr(analyze_context, "_chat", lambda s, u: '{"summary":"s","problem":"p"}')
        fd = _FakeFreshdesk("subject", _thread())
        res = analyze_context.analyze(101, MENTION, thread_anchor_note_id=222, fd_client=fd)
        assert res["freshdesk_url"] == "https://demo-account.freshdesk.com/a/tickets/101"

    def test_a_blank_url_falls_back_rather_than_shipping_an_empty_link(self, monkeypatch):
        monkeypatch.setattr(analyze_context, "_chat", lambda s, u: '{"summary":"s","problem":"p"}')
        fd = _FakeFreshdesk("subject", _thread())
        res = analyze_context.analyze(101, MENTION, freshdesk_url="   ", fd_client=fd)
        assert res["freshdesk_url"] == "https://demo-account.freshdesk.com/a/tickets/101"


class TestArgparseRefusesTheOldFlag:
    """Gone, not deprecated. A surviving flag would eventually receive the thread anchor."""

    def test_conversation_id_is_not_accepted(self):
        parser = analyze_context._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--ticket-id", "100", "--mention-text", "x",
                               "--conversation-id", "900000000001"])

    def test_the_flags_it_does_accept(self):
        parser = analyze_context._build_parser()
        url = "https://example.freshdesk.com/a/tickets/102?thread=3&follow=email"
        args = parser.parse_args(["--ticket-id", "102", "--mention-text", MENTION,
                                  "--thread-anchor-note-id", "900000000003",
                                  "--freshdesk-url", url])
        assert args.ticket_id == 102
        assert args.mention_text == MENTION
        assert args.thread_anchor_note_id == 900000000003
        assert args.freshdesk_url == url

    def test_the_url_is_optional(self):
        parser = analyze_context._build_parser()
        args = parser.parse_args(["--ticket-id", "102", "--mention-text", MENTION])
        assert args.freshdesk_url is None

    def test_mention_text_is_required(self):
        parser = analyze_context._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--ticket-id", "100"])


def test_the_system_prompt_states_the_client_rule(monkeypatch):
    # The whole point: solve what is asked OF US, not the end customer's incident.
    system = analyze_context._SYSTEM_PROMPT.lower()
    assert "not the end customer's incident" in system or "not the end customer" in system
    assert "asked of us" in system or "asked of us" in system.replace("  ", " ")
