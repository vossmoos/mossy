"""The trigger: is this Gmail message a Wrike task notification, and which task (the notification parser).

Loaded by file path, like the other skill-script tests. **Nothing here touches the network,
because the module under test cannot**: it has no client, no OAuth and no socket. That is the
point of the design, not a property of the tests.

Two classes carry the weight.

``TestTheIdComesFromTheHeaderAndNeverTheBody`` is the one to read first. ``taskApiId`` lives in
``X-Wrike-Metadata`` and nowhere else — measured, ``MAAAAA`` appears zero times in the body of
every notification in the mailbox. What the body *does* carry is ``open.htm?id=1234567890``, the
permalink number, which is a different identifier that the Wrike API refuses with
``400 Invalid Task ID`` (the Wrike reader). A parser that fell back to the body would pick up something
that looks like an id, is not one, and fails a step later instead of here.

``TestTheEventTypeIsNotClassified`` pins an absence, which is unusual and deliberate. The module
is not allowed to grow body-sniffing later: the only signal that separates an assignment from a
comment is the first line of the body, in the language of the recipient's Wrike account, and a
parser built on that breaks silently when somebody changes their locale.
"""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "parse_wrike_notification.py"
)
_spec = importlib.util.spec_from_file_location("parse_wrike_notification", _MODULE_PATH)
parser = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(parser)

#: The header off a real notification, verbatim.
METADATA = {
    "userApiId": "KUEXAMP2",
    "taskApiId": "MAAAAAEXmain",
    "accountApiId": "IEAAUIYL",
    "taskId": 1234567890,
    "accountId": 664331,
    "notificationType": "Regular",
    "entityType": "Task",
    "watchReason": "Assignee",
    "priority": "Normal",
    "lastChangeTime": 1788774840925,
}
GMAIL_ID = "0a1b2c3d4e5f6072"
#: What the body of a real notification carries. A number, not an API id.
BODY_PERMALINK = "https://www.wrike.com/open.htm?id=1234567890&utm_source=email"


def message(
    *,
    sender: str = "wrike@wrike.com",
    metadata: dict | None = METADATA,
    raw_metadata: str | None = None,
    body: str | None = None,
    gmail_id: str = GMAIL_ID,
    thread_id: str = GMAIL_ID,
) -> dict:
    """A Gmail API message in ``format=full`` shape, as the fetch step will hand it over."""
    headers = [
        {"name": "From", "value": f"=?UTF-8?Q?Anna_Beispiel?= <{sender}>"},
        {"name": "To", "value": "operator@example.com"},
        {"name": "Subject", "value": "Prepare sample import requests into service order"},
        {"name": "Message-ID",
         "value": "<task-update-notification.10000001.1234567890.1788774455839@wrike.com>"},
        {"name": "Auto-Submitted", "value": "auto-generated"},
    ]
    if raw_metadata is not None:
        headers.append({"name": "X-Wrike-Metadata", "value": raw_metadata})
    elif metadata is not None:
        headers.append({"name": "X-Wrike-Metadata", "value": json.dumps(metadata)})
    payload: dict = {"headers": headers}
    if body is not None:
        payload["body"] = {"data": base64.urlsafe_b64encode(body.encode()).decode()}
    return {"id": gmail_id, "threadId": thread_id, "payload": payload}


class TestARealNotification:
    def test_it_is_recognised(self):
        result = parser.parse(message())
        assert result["ok"] is True
        assert result["is_wrike_task_notification"] is True

    def test_it_carries_the_task_api_id(self):
        assert parser.parse(message())["task_api_id"] == "MAAAAAEXmain"

    def test_it_carries_what_stops_it_being_processed_twice(self):
        """Gmail's own ids. They are not in the MIME — they come from the API envelope — and the
        labelling step needs the message id to mark the exact message."""
        result = parser.parse(message())
        assert result["message_id"] == GMAIL_ID
        assert result["thread_id"] == GMAIL_ID

    def test_it_keeps_the_whole_header_for_later(self):
        """So a future ticket wanting watchReason or lastChangeTime re-parses nothing."""
        assert parser.parse(message())["metadata"] == METADATA

    def test_the_permalink_number_is_carried_but_is_not_the_task_id(self):
        """Two identifiers in one header, and only one of them is a task id: the API answers
        400 "Invalid Task ID" to the number (the Wrike reader)."""
        result = parser.parse(message())
        assert result["permalink_id"] == "1234567890"
        assert result["task_api_id"] != result["permalink_id"]

    def test_a_raw_format_message_parses_the_same_way(self):
        """``format=RAW`` is base64url MIME, which is also what an .eml on disk is. The caller
        should not have to convert one shape into the other to ask this question."""
        mime = (
            "From: =?UTF-8?Q?Anna?= <wrike@wrike.com>\r\n"
            "Subject: Prepare sample import requests into service order\r\n"
            f"X-Wrike-Metadata: {json.dumps(METADATA)}\r\n\r\nbody text\r\n"
        ).encode("utf-8")
        result = parser.parse({
            "id": GMAIL_ID, "threadId": GMAIL_ID,
            "raw": base64.urlsafe_b64encode(mime).decode("ascii"),
        })
        assert result["ok"] is True and result["task_api_id"] == "MAAAAAEXmain"


class TestTheOtherWrikeSenderIsNoise:
    def test_noreply_is_not_a_task_notification(self):
        """Same company, not this flow's work: invitations and password resets — four of the
        seven Wrike messages in the measured mailbox."""
        result = parser.parse(message(sender="noreply@wrike.com"))
        assert result["is_wrike_task_notification"] is False
        assert result["reason"] == parser.NOT_A_SENDER

    def test_it_is_refused_even_carrying_a_perfect_header(self):
        """The sender is checked first and on purpose. A password-reset e-mail that happened to
        carry task metadata would still not be a task event."""
        result = parser.parse(message(sender="noreply@wrike.com", metadata=METADATA))
        assert result["reason"] == parser.NOT_A_SENDER
        assert "task_api_id" not in result

    def test_an_unrelated_sender_is_refused_too(self):
        assert parser.parse(message(sender="jira@example.atlassian.net"))["reason"] == (
            parser.NOT_A_SENDER
        )

    def test_the_allowlist_is_this_flows_own_variable(self, monkeypatch):
        """Not GMAIL_ALLOWED_SENDERS: that one is shared with the Freshdesk flow, which builds
        its search from it, so widening it here would widen that."""
        monkeypatch.setenv("GMAIL_ALLOWED_SENDERS", "noreply@wrike.com")
        monkeypatch.delenv("WRIKE_TASKS_ALLOWED_SENDERS", raising=False)
        assert parser.parse(message(sender="noreply@wrike.com"))["reason"] == parser.NOT_A_SENDER

    def test_the_allowlist_can_be_configured(self, monkeypatch):
        monkeypatch.setenv("WRIKE_TASKS_ALLOWED_SENDERS", "notify@wrike.example")
        assert parser.parse(message(sender="notify@wrike.example"))["ok"] is True
        assert parser.parse(message(sender="wrike@wrike.com"))["reason"] == parser.NOT_A_SENDER

    def test_the_default_is_the_measured_sender(self, monkeypatch):
        monkeypatch.delenv("WRIKE_TASKS_ALLOWED_SENDERS", raising=False)
        assert parser.allowed_senders() == ["wrike@wrike.com"]

    def test_the_module_never_reads_the_shared_variable(self):
        """Checked on the reads themselves, not on the word.

        An earlier version of this asserted the string was absent from the source and went red
        against a docstring that *explains* why the variable is not used. Naming a thing to say
        you do not touch it is the opposite of touching it, and a test that cannot tell those
        apart is matching text rather than behaviour.
        """
        import re

        body_source = _MODULE_PATH.read_text(encoding="utf-8").split('"""', 2)[2]
        read = set(re.findall(r'os\.environ\.get\(\s*"([A-Z_]+)"', body_source))
        assert "GMAIL_ALLOWED_SENDERS" not in read, (
            "widening this flow's senders must not widen the Freshdesk flow's search"
        )


class TestAMessageThatIsNotWrikeAtAll:
    def test_no_metadata_header_does_not_raise(self):
        result = parser.parse(message(metadata=None))
        assert result["is_wrike_task_notification"] is False
        assert result["reason"] == parser.NO_METADATA_HEADER

    def test_a_message_with_no_headers_at_all_does_not_raise(self):
        assert parser.parse({"id": "x"})["is_wrike_task_notification"] is False

    def test_an_empty_dict_does_not_raise(self):
        assert parser.parse({})["is_wrike_task_notification"] is False

    def test_an_empty_header_counts_as_absent(self):
        assert parser.parse(message(raw_metadata="   "))["reason"] == parser.NO_METADATA_HEADER

    def test_unparseable_raw_does_not_raise(self):
        assert parser.parse({"id": "x", "raw": "!!!not base64!!!"})[
            "is_wrike_task_notification"] is False


class TestABrokenHeaderIsNotAMissingOne:
    """Present-and-unreadable means Wrike changed something. Absent means it is not a task
    notification. Somebody should look at the first and nobody needs to look at the second, so
    they cannot share a reason."""

    def test_invalid_json_is_its_own_reason(self):
        result = parser.parse(message(raw_metadata='{"taskApiId": "MAAAA'))
        assert result["reason"] == parser.METADATA_UNREADABLE
        assert result["reason"] != parser.NO_METADATA_HEADER

    def test_valid_json_that_is_not_an_object_is_unreadable_too(self):
        assert parser.parse(message(raw_metadata='"a string"'))["reason"] == (
            parser.METADATA_UNREADABLE
        )
        assert parser.parse(message(raw_metadata="[1, 2]"))["reason"] == (
            parser.METADATA_UNREADABLE
        )

    def test_it_keeps_the_raw_header_so_somebody_can_see_what_arrived(self):
        result = parser.parse(message(raw_metadata='{"broken'))
        assert result["raw_metadata"] == '{"broken'

    def test_a_readable_header_with_no_task_is_a_third_reason(self):
        without = {k: v for k, v in METADATA.items() if k != "taskApiId"}
        result = parser.parse(message(metadata=without))
        assert result["reason"] == parser.NO_TASK_API_ID
        assert result["metadata"] == without

    def test_an_empty_task_api_id_counts_as_none(self):
        assert parser.parse(message(metadata={**METADATA, "taskApiId": "  "}))["reason"] == (
            parser.NO_TASK_API_ID
        )

    def test_ok_and_the_task_id_always_agree(self):
        """The invariant underneath all four reasons, and the one that was missing.

        Written because a mutation survived: setting ``ok`` to True on the "readable header, no
        task" branch left every test green. Each of them asserted its own ``reason`` and none
        asserted the flag a caller actually branches on, so a result could say "fine" and carry
        no task id — and the next step would go looking for ``task_api_id`` and find nothing.

        ``ok`` true means exactly "there is a task id in here". Nothing else may set it.
        """
        without = {k: v for k, v in METADATA.items() if k != "taskApiId"}
        for case in (
            message(sender="noreply@wrike.com"),
            message(metadata=None),
            message(raw_metadata="{broken"),
            message(metadata=without),
            {},
        ):
            result = parser.parse(case)
            assert result["ok"] is False, result.get("reason")
            assert result["is_wrike_task_notification"] is False
            assert "task_api_id" not in result
            assert result.get("reason")

    def test_ok_is_true_only_when_there_is_a_task_id(self):
        """The other direction, so the pair pins the sign rather than one end."""
        result = parser.parse(message())
        assert result["ok"] is True
        assert result["task_api_id"]

    def test_the_four_reasons_are_four_different_strings(self):
        reasons = {parser.NOT_A_SENDER, parser.NO_METADATA_HEADER,
                   parser.METADATA_UNREADABLE, parser.NO_TASK_API_ID}
        assert len(reasons) == 4


class TestTheIdComesFromTheHeaderAndNeverTheBody:
    """The mutation this class exists for: a parser that falls back to the body."""

    def test_a_task_id_in_the_body_alone_is_not_enough(self):
        """No header, a perfectly good-looking id in the body — and the answer is still no."""
        result = parser.parse(message(
            metadata=None,
            body=f"Anna asignó la tarea MAAAAAEXmain a Bernd {BODY_PERMALINK}",
        ))
        assert result["is_wrike_task_notification"] is False
        assert "task_api_id" not in result

    def test_the_header_wins_when_the_body_disagrees(self):
        """The direction that catches a fallback added "just in case": if the body were ever
        consulted, this returns the wrong task and the next step acts on it."""
        result = parser.parse(message(
            body="MAAAAAWRONG999 was mentioned here " + BODY_PERMALINK,
        ))
        assert result["task_api_id"] == "MAAAAAEXmain"

    def test_the_permalink_number_in_the_body_never_becomes_a_task_id(self):
        result = parser.parse(message(metadata=None, body=BODY_PERMALINK))
        assert result["is_wrike_task_notification"] is False

    def test_the_module_never_reads_the_message_body(self):
        """Read off the source: nothing in it reaches for ``payload.body`` or ``parts``."""
        body_source = _MODULE_PATH.read_text(encoding="utf-8").split('"""', 2)[2]
        for reached_for in ('"body"', "'body'", '"parts"', "'parts'", "get_payload"):
            assert reached_for not in body_source, (
                f"the module reaches for {reached_for}; the task id must come from the header"
            )

    def test_the_module_never_parses_a_permalink(self):
        body_source = _MODULE_PATH.read_text(encoding="utf-8").split('"""', 2)[2]
        assert "open.htm" not in body_source


class TestTheEventTypeIsNotClassified:
    """An absence, pinned on purpose. See the module docstring for why."""

    def test_the_three_events_parse_identically_apart_from_nothing(self):
        """Assignment, comment-with-mention and attachments-added: measured to have identical
        headers. So all three must produce the same verdict, and the module must not pretend
        otherwise."""
        assigned = parser.parse(message(body="Anna asignó la tarea a Bernd"))
        commented = parser.parse(message(body="@Mia Muster Würdest du einmal prüfen"))
        attached = parser.parse(message(body="Image-2026-09-08 23:18:44.png (novedad)"))
        assert assigned == commented == attached
        assert assigned["task_api_id"] == "MAAAAAEXmain"

    def test_no_event_type_is_reported(self):
        result = parser.parse(message())
        assert "event_type" not in result and "notification_kind" not in result

    def test_notification_type_is_carried_but_not_acted_on(self):
        """It is in ``metadata`` because it is in the header, and it is useless for telling the
        events apart — "Regular" on all three."""
        assert parser.parse(message())["metadata"]["notificationType"] == "Regular"

    def test_the_module_matches_no_localised_phrase(self):
        body_source = _MODULE_PATH.read_text(encoding="utf-8").split('"""', 2)[2]
        for phrase in ("asignó", "asigno", "assigned", "Würdest", "novedad", "comment"):
            assert phrase not in body_source, (
                f"{phrase!r} appears in the code: a parser tied to the account's language "
                "breaks silently when somebody changes their locale"
            )


class TestItHasNoSideEffects:
    def test_it_imports_nothing_that_could_reach_out_or_write(self):
        """The precise version of "no side effects": the import list.

        Substring hunting for "send" or "write" is a charade — "send" is inside "sender", and a
        module can call something without spelling the verb. What it cannot do is reach the
        network or the disk without importing something that can, so the allowlist is the
        assertion, and anything new has to be argued for here.
        """
        import re

        source = _MODULE_PATH.read_text(encoding="utf-8")
        imported = set(re.findall(r"^\s*import\s+([a-z_.0-9]+)", source, re.MULTILINE))
        assert imported == {
            "base64", "binascii", "email", "email.policy", "email.utils", "json", "os"
        }, imported

    def test_it_opens_no_file_and_calls_no_transport(self):
        """Named calls rather than words: ``open(`` with a bracket, and the two functions that
        would actually fetch. This is what an accidental "just read the attachment" would trip
        on."""
        import re

        body_source = _MODULE_PATH.read_text(encoding="utf-8").split('"""', 2)[2]
        for pattern in (r"\bopen\s*\(", r"\burlopen\b", r"\bRequest\s*\(", r"\bsocket\b"):
            assert not re.search(pattern, body_source), f"{pattern} in a parser that only reads"

    def test_it_reads_one_environment_variable_and_no_more(self):
        import re

        body_source = _MODULE_PATH.read_text(encoding="utf-8").split('"""', 2)[2]
        names = set(re.findall(r'os\.environ\.get\(\s*"([A-Z_]+)"', body_source))
        assert names == {"WRIKE_TASKS_ALLOWED_SENDERS"}

    def test_it_is_self_contained(self):
        import re

        source = _MODULE_PATH.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(?:import|from)\s+(mossy|dotenv)\b", source, re.MULTILINE)

    def test_it_does_not_import_the_wrike_client_either(self):
        """A parser that could call the API would be a parser somebody makes call the API. The
        two are separate modules and the flow joins them, not the imports."""
        source = _MODULE_PATH.read_text(encoding="utf-8")
        assert "wrike_api" not in source.split('"""', 2)[2]
