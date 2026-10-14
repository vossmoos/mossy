"""The Gmail reader of the Wrike flow (the Wrike → Jira flow, piece 1). Offline: nothing here reaches Google.

Loaded by file path, like the other skill-script tests. Every test runs with ``urlopen`` made to
explode and the ``GMAIL_*`` credentials removed, so a test that forgets its fake fails loudly
instead of reading a real mailbox.

What is pinned, and the mutation each part was written against (run on a green baseline before
these tests existed, all eight green then):

* the two exclusions travel **inside the query**, and nothing is filtered afterwards (M1);
* a refused refresh token is ``gmail_token_refused`` and never an empty list (M2), and the real
  client maps Google's refusal to that kind rather than to an ordinary HTTP error (M8);
* ``list`` writes nothing and does not even look labels up (M3);
* a missing label stops the marking and is never created (M4);
* a label counts only once a fresh read shows it (M5);
* an empty sender list reads nothing — no default opens the gate (M6);
* the sender restriction reaches the query (M7).

What these tests do NOT prove: that Gmail's ``format=metadata`` really returns
``X-Wrike-Metadata`` inside ``payload.headers``. The messages below are built by hand in that
shape. That was measured by hand against the real mailbox on 14 September 2026, and the module
records where.
"""

from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fetch = _load("fetch_wrike_notifications")
parser = _load("parse_wrike_notification")

SENDER = "wrike@wrike.com"
TASK_ID = "MAAAAAEXmain"
METADATA = {"userApiId": "KUEXAMP2", "taskApiId": TASK_ID, "entityType": "Task",
            "notificationType": "Regular", "watchReason": "Assignee"}


@pytest.fixture(autouse=True)
def _offline_and_configured(monkeypatch):
    """No network, no real credentials, and the sender list this flow is deployed with."""
    def _no_network(*args, **kwargs):
        raise AssertionError("a test reached for the network")

    monkeypatch.setattr(fetch.urllib.request, "urlopen", _no_network)
    for name in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_USER_ID",
                 "GMAIL_ALLOWED_SENDERS", "WRIKE_TASKS_PROCESSED_LABEL",
                 "WRIKE_TASKS_SKIPPED_LABEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WRIKE_TASKS_ALLOWED_SENDERS", SENDER)


def metadata_message(message_id: str, *, label_ids=("INBOX",)) -> dict:
    """A ``format=metadata`` message in the shape Gmail documents. Built, not captured."""
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "labelIds": list(label_ids),
        "payload": {"headers": [
            {"name": "From", "value": f"Wrike <{SENDER}>"},
            {"name": "Subject", "value": "Prepare sample import requests into service order"},
            {"name": "Message-ID", "value": f"<{message_id}@wrike.com>"},
            {"name": "X-Wrike-Metadata", "value": json.dumps(METADATA)},
        ]},
    }


class _FakeGmail:
    """Records every call. Nothing leaves the process."""

    def __init__(self, ids=None, *, label_exists=True, confirm=True, messages=None,
                 raise_at=None, error=None):
        self._ids = ["m1"] if ids is None else ids
        self._label_exists = label_exists
        self._confirm = confirm
        self._messages = messages or {}
        self._raise_at = raise_at
        self._error = error
        self.queries: list[str] = []
        self.fetched: list[str] = []
        self.label_lookups: list[str] = []
        self.writes: list[tuple[str, str]] = []
        self.readbacks: list[str] = []

    def _maybe_raise(self, step):
        if self._raise_at == step:
            raise self._error

    def list_ids(self, query, max_results):
        self.queries.append(query)
        self._maybe_raise("list")
        return list(self._ids)

    def metadata_message(self, message_id):
        self.fetched.append(message_id)
        self._maybe_raise("message")
        return self._messages.get(message_id) or metadata_message(message_id)

    def label_id(self, name):
        self.label_lookups.append(name)
        self._maybe_raise("label")
        return f"LBL_{name}" if self._label_exists else None

    def add_label(self, message_id, label_id):
        self.writes.append((message_id, label_id))
        return [label_id]

    def label_ids_of(self, message_id):
        self.readbacks.append(message_id)
        written = [lid for mid, lid in self.writes if mid == message_id]
        return (["INBOX"] + written) if self._confirm else ["INBOX"]


# --------------------------------------------------------------------------------- the query
class TestTheExclusionIsInTheQuery:
    """Dedupe is server-side: a message already dealt with is never returned at all."""

    def test_the_query_is_the_sender_minus_both_labels(self):
        query = fetch.build_query([SENDER], "mossy-wrike-processed", "mossy-wrike-skipped")
        assert query == f"from:{SENDER} -label:mossy-wrike-processed -label:mossy-wrike-skipped"

    @pytest.mark.parametrize("label", ["mossy-wrike-processed", "mossy-wrike-skipped"])
    def test_neither_exclusion_may_go_missing(self, label):
        query = fetch.build_query([SENDER], "mossy-wrike-processed", "mossy-wrike-skipped")
        assert f"-label:{label}" in query

    def test_the_listing_sends_exactly_that_query_with_this_flows_labels(self):
        client = _FakeGmail()
        result = fetch.list_notifications(client=client)
        assert result["ok"] is True
        assert client.queries == [
            f"from:{SENDER} -label:mossy-wrike-processed -label:mossy-wrike-skipped"]
        assert result["processed_label"] == "mossy-wrike-processed"
        assert result["skipped_label"] == "mossy-wrike-skipped"

    def test_nothing_gmail_returns_is_dropped_afterwards(self):
        """The exclusion is Gmail's job. A result that happens to carry a label is still returned:
        filtering here would mean the query had stopped doing it, and every caller would have to
        remember to."""
        labelled = metadata_message(
            "m1", label_ids=("INBOX", "mossy-wrike-processed", "mossy-wrike-skipped"))
        client = _FakeGmail(ids=["m1"], messages={"m1": labelled})
        result = fetch.list_notifications(client=client)
        assert [m["id"] for m in result["messages"]] == ["m1"]

    def test_the_labels_are_configurable(self, monkeypatch):
        monkeypatch.setenv("WRIKE_TASKS_PROCESSED_LABEL", "done-here")
        monkeypatch.setenv("WRIKE_TASKS_SKIPPED_LABEL", "not-for-us")
        client = _FakeGmail()
        fetch.list_notifications(client=client)
        assert client.queries == [f"from:{SENDER} -label:done-here -label:not-for-us"]


# ------------------------------------------------------------------------------- list reads
class TestListOnlyReads:
    def test_it_writes_nothing(self):
        client = _FakeGmail(ids=["m1", "m2"])
        result = fetch.list_notifications(client=client)
        assert result["count"] == 2
        assert client.writes == [], "list must not label: the label means the work is done"

    def test_it_does_not_even_look_labels_up(self):
        client = _FakeGmail(ids=["m1", "m2"])
        fetch.list_notifications(client=client)
        assert client.label_lookups == [] and client.readbacks == []

    def test_it_returns_headers_as_metadata_messages(self):
        client = _FakeGmail(ids=["m1", "m2"])
        result = fetch.list_notifications(client=client)
        assert client.fetched == ["m1", "m2"]
        assert [m["id"] for m in result["messages"]] == ["m1", "m2"]
        assert all("payload" in m for m in result["messages"])

    def test_an_empty_mailbox_is_ok_only_because_gmail_answered(self):
        client = _FakeGmail(ids=[])
        result = fetch.list_notifications(client=client)
        assert result == {**result, "ok": True, "count": 0, "messages": []}
        assert client.queries, "an empty result is only ever reported after a real listing"

    def test_what_it_returns_is_what_the_parser_reads(self):
        """The two pieces fit — in the shape Gmail documents. That Gmail really sends
        ``X-Wrike-Metadata`` in ``format=metadata`` was measured on one real message (see the
        module); these messages are built by hand in that shape."""
        result = fetch.list_notifications(client=_FakeGmail(ids=["m1"]))
        verdict = parser.parse(result["messages"][0])
        assert verdict["ok"] is True and verdict["task_api_id"] == TASK_ID
        assert verdict["message_id"] == "m1" and verdict["thread_id"] == "thread-m1"


# ---------------------------------------------------------------------------------- senders
class TestOnlyTheConfiguredSendersAreRead:
    def test_the_sender_restriction_reaches_the_query(self):
        client = _FakeGmail()
        fetch.list_notifications(client=client)
        assert client.queries[0].startswith(f"from:{SENDER} ")
        assert "noreply@wrike.com" not in client.queries[0], (
            "invitations and password resets are not work for this flow")

    def test_several_senders_all_reach_the_query(self, monkeypatch):
        monkeypatch.setenv("WRIKE_TASKS_ALLOWED_SENDERS", "a@example.com, B@Example.com")
        client = _FakeGmail()
        fetch.list_notifications(client=client)
        assert client.queries[0].startswith("{from:a@example.com from:b@example.com} ")

    @pytest.mark.parametrize("value", [None, "", "   ", " , ,  "])
    def test_an_empty_sender_list_reads_nothing(self, monkeypatch, value):
        """No default. A sender list is a gate, and a convenient default opens it without anybody
        deciding to — unlike the parser, which only judges what was already downloaded."""
        if value is None:
            monkeypatch.delenv("WRIKE_TASKS_ALLOWED_SENDERS", raising=False)
        else:
            monkeypatch.setenv("WRIKE_TASKS_ALLOWED_SENDERS", value)
        client = _FakeGmail(ids=["m1", "m2"])
        result = fetch.list_notifications(client=client)
        assert result["ok"] is False and result["reason"] == fetch.NO_ALLOWED_SENDERS
        assert client.queries == [] and client.fetched == [], "zero Gmail calls"
        assert "count" not in result and "messages" not in result

    def test_the_shared_freshdesk_variable_is_not_read(self, monkeypatch):
        monkeypatch.delenv("WRIKE_TASKS_ALLOWED_SENDERS", raising=False)
        monkeypatch.setenv("GMAIL_ALLOWED_SENDERS", SENDER)
        client = _FakeGmail()
        result = fetch.list_notifications(client=client)
        assert result["reason"] == fetch.NO_ALLOWED_SENDERS and client.queries == []

    def test_this_module_differs_from_the_parser_on_purpose(self, monkeypatch):
        monkeypatch.delenv("WRIKE_TASKS_ALLOWED_SENDERS", raising=False)
        assert parser.allowed_senders() == [SENDER]
        assert fetch.allowed_senders() == []
        assert "must not be \"unified\"" in fetch.__doc__


# ----------------------------------------------------------------------------------- labels
class TestMarkingWrites:
    @pytest.mark.parametrize("mark", ["processed", "skipped"])
    def test_a_missing_label_stops_and_is_not_created(self, mark):
        client = _FakeGmail(label_exists=False)
        out = (fetch.mark_processed("m1", client=client) if mark == "processed"
               else fetch.mark_skipped("m1", "no_wrike_metadata_header", client=client))
        assert out["ok"] is False and out["reason"] == fetch.LABEL_NOT_FOUND
        assert client.writes == [] and client.readbacks == []

    def test_a_label_counts_only_once_a_fresh_read_shows_it(self):
        client = _FakeGmail(confirm=False)
        out = fetch.mark_processed("m1", client=client)
        assert out["ok"] is False and out["reason"] == fetch.LABEL_NOT_CONFIRMED
        assert client.readbacks == ["m1"]

    def test_a_confirmed_label_is_reported_with_its_evidence(self):
        client = _FakeGmail()
        out = fetch.mark_processed("m1", client=client)
        assert out["ok"] is True and out["confirmed"] is True
        assert client.writes == [("m1", "LBL_mossy-wrike-processed")]
        assert client.readbacks == ["m1"]

    def test_skipping_uses_the_skipped_label_and_says_why(self):
        client = _FakeGmail()
        out = fetch.mark_skipped("m1", "no_wrike_metadata_header", client=client)
        assert out["ok"] is True and out["label"] == "mossy-wrike-skipped"
        assert out["marked"] == "skipped" and out["skip_reason"] == "no_wrike_metadata_header"


# ------------------------------------------------------------------ a dead token is not empty
class TestARefusedTokenIsNotAnEmptyMailbox:
    """While the Google Cloud app is in Testing, the refresh token dies every 7 days. That must
    never read as 'nothing to process'."""

    def test_the_listing_reports_it_in_its_own_reason(self):
        error = fetch.GmailFetchError("refused", kind="token", status=400,
                                      google_error="invalid_grant")
        result = fetch.list_notifications(client=_FakeGmail(raise_at="list", error=error))
        assert result["ok"] is False
        assert result["reason"] == fetch.GMAIL_TOKEN_REFUSED
        assert result["status"] == 400 and result["google_error"] == "invalid_grant"
        assert result["detail"] == fetch.TOKEN_REFUSED_IS_NOT_EMPTY
        assert "count" not in result and "messages" not in result

    def test_the_real_client_maps_googles_refusal_to_that_reason(self, monkeypatch):
        """Through ``GmailFetchClient`` itself, not a double — the refusal is decided in
        ``access_token``, and a fake would never exercise it."""
        body = json.dumps({"error": "invalid_grant",
                           "error_description": "Token has been expired or revoked."}).encode()

        def _refuse(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(body))

        monkeypatch.setattr(fetch.urllib.request, "urlopen", _refuse)
        client = fetch.GmailFetchClient("client-id", "client-secret", "REFRESH-SECRET-VALUE")
        result = fetch.list_notifications(client=client)
        assert result["reason"] == fetch.GMAIL_TOKEN_REFUSED
        assert result["google_error"] == "invalid_grant" and result["status"] == 400
        dumped = json.dumps(result)
        assert "REFRESH-SECRET-VALUE" not in dumped and "client-secret" not in dumped

    def test_marking_reports_it_too(self):
        error = fetch.GmailFetchError("refused", kind="token", status=400)
        out = fetch.mark_processed("m1", client=_FakeGmail(raise_at="label", error=error))
        assert out["ok"] is False and out["reason"] == fetch.GMAIL_TOKEN_REFUSED

    def test_an_unreachable_google_is_not_an_empty_mailbox_either(self, monkeypatch):
        def _down(req, timeout=None):
            raise urllib.error.URLError("nodename nor servname provided")

        monkeypatch.setattr(fetch.urllib.request, "urlopen", _down)
        client = fetch.GmailFetchClient("client-id", "client-secret", "refresh")
        result = fetch.list_notifications(client=client)
        assert result["reason"] == fetch.GMAIL_UNREACHABLE and result["status"] is None
        assert "count" not in result

    def test_gmail_refusing_the_listing_is_its_own_reason(self):
        error = fetch.GmailFetchError("forbidden", kind="http", status=403)
        result = fetch.list_notifications(client=_FakeGmail(raise_at="list", error=error))
        assert result["reason"] == fetch.GMAIL_LISTING_REFUSED and result["status"] == 403

    def test_a_message_that_cannot_be_read_stops_the_listing(self):
        error = fetch.GmailFetchError("gone", kind="http", status=404)
        result = fetch.list_notifications(
            client=_FakeGmail(ids=["m1", "m2"], raise_at="message", error=error))
        assert result["reason"] == fetch.GMAIL_MESSAGE_REFUSED and result["message_id"] == "m1"
        assert "messages" not in result


# --------------------------------------------------------------------------------- the CLI
class TestTheCommandRuns:
    """``main`` is run, not imported and inspected (the lesson of an earlier change)."""

    def test_list_prints_the_envelope_and_exits_zero(self, monkeypatch, capsys):
        client = _FakeGmail(ids=["m1"])
        monkeypatch.setattr(fetch.GmailFetchClient, "from_env", classmethod(lambda cls: client))
        assert fetch.main(["list", "--max-results", "5"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True and out["count"] == 1 and client.writes == []

    def test_a_refusal_exits_non_zero(self, monkeypatch, capsys):
        monkeypatch.delenv("WRIKE_TASKS_ALLOWED_SENDERS", raising=False)
        assert fetch.main(["list"]) == 1
        assert json.loads(capsys.readouterr().out)["reason"] == fetch.NO_ALLOWED_SENDERS

    def test_without_credentials_nothing_is_read(self, capsys):
        assert fetch.main(["list"]) == 1
        assert json.loads(capsys.readouterr().out)["reason"] == fetch.GMAIL_NOT_CONFIGURED


# -------------------------------------------------------------------- what the module declares
class TestTheDeclarationsStayWritten:
    def test_the_measured_format_is_declared_with_its_date_and_message(self):
        """The fixtures here are hand-built in the metadata shape; this pins where the real
        shape was measured, so nobody mistakes the fixtures for the measurement."""
        doc = fetch.__doc__
        assert "has not been measured" not in doc
        assert "14 September 2026" in doc and "one real notification" in doc
        assert "no ``body`` and no ``parts``" in doc

    def test_the_seven_day_expiry_is_declared(self):
        doc = fetch.__doc__
        assert "Testing" in doc and "expires every 7 days" in doc
        assert "indistinguishable" in doc and "the label search" in doc
