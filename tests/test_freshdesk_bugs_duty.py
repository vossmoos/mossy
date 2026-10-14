"""The freshdesk-bugs duty, offline. Nothing here reaches Gmail.

Three properties are load-bearing and each has a mutation:

* **``check()`` makes no network call.** It runs on every tick of the duty loop, so a call there
  would be made constantly and would fail the loop when the mailbox is unreachable. Asserted by
  making sockets explode rather than by reading the code.
* **It ships disabled.** A duty that starts polling a live mailbox the moment the branch merges
  is a surprise nobody asked for.
* **The processed label goes on last.** It is the only dedupe: an e-mail carrying it is never
  returned by the listing again. Setting it before the Jira issue exists would drop the work on
  the floor and hide it — so ``list`` labels nothing, and marking is a separate call.
"""

from __future__ import annotations

import asyncio
import importlib.util
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import duties.freshdesk_bugs as duty_module  # noqa: E402

_FETCH_PATH = (_ROOT / "skills" / "gmail-freshdesk-bugs" / "scripts"
               / "fetch_mention_emails.py")
_spec = importlib.util.spec_from_file_location("fetch_mention_emails", _FETCH_PATH)
fetch = importlib.util.module_from_spec(_spec)
sys.modules["fetch_mention_emails"] = fetch
_spec.loader.exec_module(fetch)


def _at(minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 1, 10, minute, second, tzinfo=timezone.utc)


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setenv("FRESHDESK_BUGS_ENABLED", "true")
    monkeypatch.delenv("FRESHDESK_BUGS_INTERVAL_MINUTES", raising=False)
    monkeypatch.delenv("FRESHDESK_BUGS_PROCESSED_LABEL", raising=False)


class TestCheckNeverTouchesTheNetwork:
    """It runs on every tick. A call here would be made all day and break on a bad network."""

    @pytest.fixture(autouse=True)
    def _no_outbound_calls(self, monkeypatch):
        """Block the ways out, not sockets as a class.

        Patching ``socket.socket`` itself breaks asyncio: the event loop builds a self-pipe out
        of one, so ``asyncio.run`` would fail for a reason that has nothing to do with the duty.
        These four are what an outbound call actually goes through.
        """
        def explode(*args, **kwargs):
            raise AssertionError("check() tried to reach the network; it must only read the clock")
        monkeypatch.setattr(socket, "getaddrinfo", explode)
        monkeypatch.setattr(socket, "create_connection", explode)
        monkeypatch.setattr(socket.socket, "connect", explode)
        import urllib.request
        monkeypatch.setattr(urllib.request, "urlopen", explode)

    def test_it_is_silent_when_disabled(self, monkeypatch):
        monkeypatch.setenv("FRESHDESK_BUGS_ENABLED", "false")
        assert asyncio.run(duty_module.FreshdeskBugs().check(_at(0))) == []

    def test_it_enqueues_on_the_interval_without_any_call(self, enabled):
        out = asyncio.run(duty_module.FreshdeskBugs().check(_at(10)))
        assert len(out) == 1
        assert out[0].kind == "evaluate_duty"

    def test_it_is_silent_off_the_interval_without_any_call(self, enabled):
        assert asyncio.run(duty_module.FreshdeskBugs().check(_at(11))) == []


class TestItShipsDisabled:
    def test_no_variable_means_off(self, monkeypatch):
        monkeypatch.delenv("FRESHDESK_BUGS_ENABLED", raising=False)
        assert duty_module._enabled() is False
        assert asyncio.run(duty_module.FreshdeskBugs().check(_at(0))) == []

    @pytest.mark.parametrize("value", ["false", "False", "0", "yes", "", "  "])
    def test_only_an_explicit_true_switches_it_on(self, monkeypatch, value):
        monkeypatch.setenv("FRESHDESK_BUGS_ENABLED", value)
        assert duty_module._enabled() is False

    @pytest.mark.parametrize("value", ["true", "TRUE", " True "])
    def test_true_in_any_case_switches_it_on(self, monkeypatch, value):
        monkeypatch.setenv("FRESHDESK_BUGS_ENABLED", value)
        assert duty_module._enabled() is True


class TestTheIntervalIsRespected:
    def test_the_default_is_five_minutes(self, enabled):
        assert duty_module._interval_minutes() == 5
        fired = [m for m in range(60)
                 if asyncio.run(duty_module.FreshdeskBugs().check(_at(m)))]
        assert fired == [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]

    def test_a_configured_interval_is_used(self, enabled, monkeypatch):
        monkeypatch.setenv("FRESHDESK_BUGS_INTERVAL_MINUTES", "15")
        fired = [m for m in range(60)
                 if asyncio.run(duty_module.FreshdeskBugs().check(_at(m)))]
        assert fired == [0, 15, 30, 45]

    def test_it_does_not_fire_late_in_the_minute(self, enabled):
        assert asyncio.run(duty_module.FreshdeskBugs().check(_at(10, 0))) != []
        assert asyncio.run(duty_module.FreshdeskBugs().check(_at(10, 30))) == []

    def test_one_pass_per_window(self, enabled):
        first = asyncio.run(duty_module.FreshdeskBugs().check(_at(10, 0)))
        second = asyncio.run(duty_module.FreshdeskBugs().check(_at(10, 5)))
        assert first[0].dedupe_key == second[0].dedupe_key

    def test_a_nonsense_interval_falls_back_to_the_default(self, enabled, monkeypatch):
        monkeypatch.setenv("FRESHDESK_BUGS_INTERVAL_MINUTES", "not-a-number")
        assert duty_module._interval_minutes() == 5


class TestEvaluateEnqueuesTheSkillRun:
    def test_it_returns_one_goal(self, enabled):
        out = asyncio.run(duty_module.FreshdeskBugs().evaluate(
            {"duty": "freshdesk_bugs", "window": "2026-09-01T10:10"}, runtime=None))
        assert len(out) == 1 and out[0].kind == "goal"

    def test_the_goal_names_the_skill_and_the_ordering_rule(self, enabled):
        out = asyncio.run(duty_module.FreshdeskBugs().evaluate({"window": "w"}, runtime=None))
        goal = out[0].goal
        assert "gmail-freshdesk-bugs" in goal
        assert "fetch_mention_emails.py list" in goal
        assert "mark-processed only" in goal, "the goal must state that marking comes last"

    def test_the_context_carries_the_label(self, enabled, monkeypatch):
        monkeypatch.setenv("FRESHDESK_BUGS_PROCESSED_LABEL", "custom-label")
        out = asyncio.run(duty_module.FreshdeskBugs().evaluate({"window": "w"}, runtime=None))
        assert out[0].context["processed_label"] == "custom-label"


class TestProcessedMessagesAreExcludedFromTheListing:
    """The dedupe. Server-side in the query, so a processed e-mail is never even returned."""

    def test_the_query_excludes_both_labels(self):
        query = fetch.build_query(
            ["support@example.freshdesk.com"], "mossy-processed", "mossy-skipped")
        assert query == (
            "from:support@example.freshdesk.com -label:mossy-processed -label:mossy-skipped")

    def test_the_exclusions_are_there_even_without_a_sender_filter(self):
        # ``build_query`` stays permissive; the empty allowlist is refused one level up, in
        # ``list_mentions`` — see TestTheMailboxIsNotReadWithoutAnAllowlist.
        assert fetch.build_query([], "mossy-processed", "mossy-skipped") == (
            "-label:mossy-processed -label:mossy-skipped")

    @pytest.mark.parametrize("label", ["mossy-processed", "mossy-skipped"])
    def test_neither_exclusion_may_go_missing(self, label):
        """Mutation: drop one and the mailbox starts handing that class back every pass."""
        query = fetch.build_query(["s@example.com"], "mossy-processed", "mossy-skipped")
        assert f"-label:{label}" in query, f"the {label} exclusion is gone from the query"

    def test_a_configured_label_name_is_used(self, monkeypatch):
        monkeypatch.setenv("FRESHDESK_BUGS_PROCESSED_LABEL", "done-by-mossy")
        assert fetch._processed_label() == "done-by-mossy"

    def test_the_listing_sends_that_query(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GMAIL_ALLOWED_SENDERS", "support@example.freshdesk.com")
        monkeypatch.delenv("FRESHDESK_BUGS_PROCESSED_LABEL", raising=False)
        client = _FakeGmail()
        result = fetch.list_mentions(str(tmp_path), client=client)
        assert result["ok"] is True
        assert client.queries == [
            "from:support@example.freshdesk.com -label:mossy-processed -label:mossy-skipped"]
        assert result["processed_label"] == "mossy-processed"
        assert result["skipped_label"] == "mossy-skipped"


class TestTheMailboxIsNotReadWithoutAnAllowlist:
    """No configured senders means read nothing — never read everything.

    An empty ``GMAIL_ALLOWED_SENDERS`` used to produce ``-label:… -label:…``, a query that
    matches the **whole mailbox**. With the duty enabled that turns every unlabelled personal
    e-mail into input for the analyser, and into Jira issues this account cannot delete. The
    listing now refuses instead, before it has a client to call Gmail with.
    """

    @pytest.mark.parametrize("value", [None, "", "   ", " , ,  "])
    def test_an_empty_allowlist_refuses_and_never_touches_gmail(self, tmp_path, monkeypatch,
                                                                value):
        if value is None:
            monkeypatch.delenv("GMAIL_ALLOWED_SENDERS", raising=False)
        else:
            monkeypatch.setenv("GMAIL_ALLOWED_SENDERS", value)
        client = _FakeGmail(ids=["m1", "m2"])
        result = fetch.list_mentions(str(tmp_path), client=client)
        assert result["ok"] is False
        assert result["reason"] == "no_allowed_senders"
        assert client.queries == [], "the mailbox must not be queried without an allowlist"
        assert "count" not in result and "emails" not in result
        assert list(tmp_path.iterdir()) == [], "nothing may be downloaded either"

    def test_with_senders_configured_it_runs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GMAIL_ALLOWED_SENDERS", "support@example.freshdesk.com")
        client = _FakeGmail(ids=["m1", "m2"])
        result = fetch.list_mentions(str(tmp_path), client=client)
        assert result["ok"] is True and result["count"] == 2
        assert client.queries == [
            "from:support@example.freshdesk.com -label:mossy-processed -label:mossy-skipped"]

    def test_several_senders_all_reach_the_query(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GMAIL_ALLOWED_SENDERS", "a@example.com, b@example.com")
        client = _FakeGmail()
        result = fetch.list_mentions(str(tmp_path), client=client)
        assert result["ok"] is True
        assert "a@example.com" in client.queries[0] and "b@example.com" in client.queries[0]


class TestTheLabelGoesOnLastOrNotAtAll:
    """Mutation target: labelling during the listing loses any run that fails afterwards."""

    def test_listing_labels_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GMAIL_ALLOWED_SENDERS", "support@example.freshdesk.com")
        client = _FakeGmail(ids=["m1", "m2"])
        result = fetch.list_mentions(str(tmp_path), client=client)
        assert result["count"] == 2
        assert client.labelled == [], (
            "list must not label: the marker means the Jira issue exists, and setting it here "
            "would skip the e-mail forever if the rest of the flow failed"
        )

    def test_marking_is_a_separate_call_that_labels(self, monkeypatch):
        client = _FakeGmail()
        out = fetch.mark_processed("m1", client=client)
        assert out["ok"] is True
        assert client.labelled == [("m1", "LBL_PROC")]

    def test_marking_confirms_by_read_back(self, monkeypatch):
        """A label that does not show up on a fresh read does not count."""
        client = _FakeGmail(confirm=False)
        out = fetch.mark_processed("m1", client=client)
        assert out["ok"] is False
        assert out["reason"] == "label_not_confirmed"

    def test_a_missing_label_is_reported_not_created(self):
        client = _FakeGmail(label_exists=False)
        out = fetch.mark_processed("m1", client=client)
        assert out["ok"] is False and out["reason"] == "label_not_found"
        assert client.labelled == []


class TestTheTwoLabelsCannotBeConfused:
    """The important mutation: a marker must not claim work that never happened.

    ``mossy-processed`` says a Jira issue exists. ``mossy-skipped`` says none does and none
    will. If skipping reached for the processed label, an unparseable notification would be
    recorded as handled and nobody reading the mailbox could tell it apart from real work.
    """

    def test_skipping_uses_the_skipped_label(self, monkeypatch):
        monkeypatch.delenv("FRESHDESK_BUGS_SKIPPED_LABEL", raising=False)
        client = _FakeGmail()
        out = fetch.mark_skipped("m1", "unknown_notification_type", client=client)
        assert out["ok"] is True
        assert out["label"] == "mossy-skipped"
        assert out["label"] != fetch._processed_label(), (
            "skipping must never use the processed label: it would claim a Jira issue that "
            "does not exist"
        )
        assert out["marked"] == "skipped"
        assert out["skip_reason"] == "unknown_notification_type"

    def test_processing_uses_the_processed_label(self, monkeypatch):
        monkeypatch.delenv("FRESHDESK_BUGS_PROCESSED_LABEL", raising=False)
        out = fetch.mark_processed("m1", client=_FakeGmail())
        assert out["label"] == "mossy-processed"
        assert out["label"] != fetch._skipped_label()
        assert out["marked"] == "processed"

    def test_the_two_labels_are_never_the_same_string(self, monkeypatch):
        monkeypatch.delenv("FRESHDESK_BUGS_PROCESSED_LABEL", raising=False)
        monkeypatch.delenv("FRESHDESK_BUGS_SKIPPED_LABEL", raising=False)
        assert fetch._processed_label() != fetch._skipped_label()

    def test_the_skipped_label_is_configurable(self, monkeypatch):
        monkeypatch.setenv("FRESHDESK_BUGS_SKIPPED_LABEL", "not-for-us")
        assert fetch._skipped_label() == "not-for-us"

    def test_skipping_confirms_by_read_back(self):
        client = _FakeGmail(confirm=False)
        out = fetch.mark_skipped("m1", "reply", client=client)
        assert out["ok"] is False
        assert out["reason"] == "label_not_confirmed"

    def test_a_missing_skipped_label_is_reported_not_created(self):
        client = _FakeGmail(label_exists=False)
        out = fetch.mark_skipped("m1", "reply", client=client)
        assert out["ok"] is False and out["reason"] == "label_not_found"
        assert client.labelled == []


class _FakeGmail:
    """Records what was asked of Gmail. Nothing leaves the process."""

    def __init__(self, ids=None, label_exists=True, confirm=True):
        self._ids = ids if ids is not None else ["m1"]
        self._label_exists = label_exists
        self._confirm = confirm
        self.queries: list[str] = []
        self.labelled: list[tuple[str, str]] = []

    def list_ids(self, query, max_results):
        self.queries.append(query)
        return list(self._ids)

    def raw_message(self, message_id):
        return f"Subject: {message_id}\r\n\r\nbody\r\n".encode()

    def label_id(self, name):
        """A distinct id per label name, so a test can tell which one was applied."""
        if not self._label_exists:
            return None
        return {"mossy-processed": "LBL_PROC", "mossy-skipped": "LBL_SKIP"}.get(name, "LBL")

    def add_label(self, message_id, label_id):
        self.labelled.append((message_id, label_id))
        return ["INBOX", label_id]

    def label_ids_of(self, message_id):
        if self._confirm:
            return ["INBOX"] + [lid for m, lid in self.labelled if m == message_id]
        return ["INBOX"]
