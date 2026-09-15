"""Parsing the Freshdesk thread notification , against the three real e-mails, sanitised.

The fixtures in ``tests/fixtures/`` are real notifications from test ticket 101, sanitised (signatures, relay host, sender name
and note ids replaced; the headers and body the parser reads are kept):
two mentions (``X-FD-Type: 31``) and one reply (``30``). They are what established the facts this
parser is built on, so they are what it is tested against.

Three of those facts are load-bearing and each has a test that fails if it stops holding:

* **``X-FD-Note-Id`` is the thread's anchor, not the message.** Fixtures 2 and 3 carry the *same*
  note id with *different* text, so anything that treats it as "the conversation we were mentioned
  in" would analyse the wrong content. It is returned as ``thread_anchor_note_id`` and the test
  asserts the name.
* **The text must never come back empty.** If Freshdesk changes the template, an empty
  ``mention_text`` would become a Jira issue with no description that reads like a real one. A
  missing anchor is a named refusal instead, and the mutation below proves the difference is real.
* **The thread index is read twice and compared.** The link and the Message-ID each carry it. A
  parser that trusted only the link would not notice an e-mail shaped differently from these.
"""

from __future__ import annotations

import email
import email.policy
import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_MODULE_PATH = _ROOT / "scripts" / "parse_mention_email.py"
_spec = importlib.util.spec_from_file_location("parse_mention_email", _MODULE_PATH)
parse_mention_email = importlib.util.module_from_spec(_spec)
sys.modules["parse_mention_email"] = parse_mention_email
_spec.loader.exec_module(parse_mention_email)
parse = parse_mention_email.parse

FIXTURES = _ROOT / "tests" / "fixtures"
MENTION_1 = FIXTURES / "mention-notification.eml"
MENTION_2 = FIXTURES / "mention-notification-2.eml"
REPLY_3 = FIXTURES / "mention-notification-3.eml"
#: A mention written as a *reply inside an already open thread* — the case that
#: decided whether the type filter is safe.
MENTION_IN_OPEN_THREAD_4 = FIXTURES / "mention-notification-4.eml"

#: The long mention, measured on both sides: the e-mail body and the Freshdesk API returned
#: exactly this many characters, identical strings. It is the proof that notifications do not
#: truncate at two thousand characters over three paragraphs.
LONG_TEXT_LENGTH = 2005


def _rewrite(source: Path, target: Path, *, drop_header: str | None = None,
             replace: tuple[str, str] | None = None) -> Path:
    """A copy of a fixture with one header changed or removed. Originals stay untouched."""
    raw = source.read_text(encoding="utf-8", errors="surrogateescape")
    if drop_header:
        raw = "\n".join(l for l in raw.splitlines()
                        if not l.lower().startswith(drop_header.lower() + ":"))
    if replace:
        needle, value = replace
        assert needle in raw, f"the fixture no longer contains {needle!r}"
        raw = raw.replace(needle, value)
    target.write_text(raw, encoding="utf-8", errors="surrogateescape")
    return target


def _rewrite_html(source: Path, target: Path, needle: str, value: str) -> Path:
    """A copy with the HTML body edited.

    The body is quoted-printable, so the anchor is not a literal string in the file — it has to
    be decoded, changed, and re-encoded. Headers are left exactly as they were.
    """
    message = email.message_from_bytes(source.read_bytes(), policy=email.policy.default)
    body = message.get_content()
    assert needle in body, f"the fixture body no longer contains {needle!r}"
    message.set_content(body.replace(needle, value), subtype="html")
    target.write_bytes(message.as_bytes())
    return target


class TestTheThreeRealNotificationsParse:
    def test_the_first_mention(self):
        got = parse(MENTION_1)
        assert got["ok"] is True
        assert got["ticket_id"] == 101
        assert got["thread_id"] == 1
        assert got["thread_anchor_note_id"] == 900000000001
        assert got["notification_type"] == "mention"
        assert got["mention_text"] == "Test Agent can you check the triage quality here?"
        assert got["freshdesk_url"] == (
            "https://example.freshdesk.com/a/tickets/101?thread=1&follow=email")
        assert got["subject"] == "Mentioned in private in 101 - Test mention EX-135"

    def test_the_second_mention(self):
        got = parse(MENTION_2)
        assert got["ok"] is True
        assert got["ticket_id"] == 101
        assert got["thread_id"] == 2
        assert got["thread_anchor_note_id"] == 900000000002
        assert got["notification_type"] == "mention"
        assert got["freshdesk_url"].endswith("?thread=2&follow=email")

    def test_the_reply(self):
        got = parse(REPLY_3, require_type="any")
        assert got["ok"] is True
        assert got["ticket_id"] == 101
        assert got["thread_id"] == 2
        assert got["notification_type"] == "reply"
        assert got["mention_text"] == "Third mention, replying inside the existing thread"
        assert got["subject"] == "New reply in 101 - Test mention EX-135"

    def test_the_anchor_note_id_is_not_called_a_conversation_id(self):
        """The name is the guard: this id must never reach --conversation-id."""
        got = parse(MENTION_2)
        assert "thread_anchor_note_id" in got
        assert "conversation_id" not in got
        assert "note_id" not in got


class TestTheLongMentionArrivesWhole:
    def test_it_is_two_thousand_and_five_characters(self):
        got = parse(MENTION_2)
        assert len(got["mention_text"]) == LONG_TEXT_LENGTH

    def test_all_three_paragraphs_are_present_and_finished(self):
        text = parse(MENTION_2)["mention_text"]
        assert text.startswith("Test Agent The triage agent is misclassifying")
        assert "There is a second issue that may or may not be related." in text
        assert "Finally, and this is more of a request than a bug report" in text
        assert text.endswith("which signals in the document led to the decision.")

    def test_nothing_marks_a_truncation(self):
        text = parse(MENTION_2)["mention_text"]
        for marker in ("…", "...", "read more", "show more", "[...]"):
            assert marker not in text.lower() if marker.isalpha() else marker not in text


class TestTheTwoNotificationKindsAreToldApart:
    """Both land in the same mailbox. Filtering on "Mentioned in private" alone loses the reply."""

    def test_31_is_a_mention_and_30_is_a_reply(self):
        assert parse(MENTION_2)["notification_type"] == "mention"
        assert parse(REPLY_3, require_type="any")["notification_type"] == "reply"

    def test_the_same_thread_gives_the_same_anchor_with_different_text(self):
        """The measured fact behind the name: fixtures 2 and 3 share a note id."""
        second, third = parse(MENTION_2), parse(REPLY_3, require_type="any")
        assert second["thread_anchor_note_id"] == third["thread_anchor_note_id"]
        assert second["thread_id"] == third["thread_id"]
        assert second["mention_text"] != third["mention_text"]

    def test_an_unknown_type_is_refused_rather_than_assumed(self, tmp_path):
        bad = _rewrite(MENTION_2, tmp_path / "t.eml", replace=("X-FD-Type: 31", "X-FD-Type: 47"))
        got = parse(bad)
        assert got["ok"] is False
        assert got["reason"] == "unknown_notification_type"
        assert got["x_fd_type"] == "47"


class TestAMissingHeaderIsNamed:
    @pytest.mark.parametrize("header", ["X-FD-Ticket-Id", "X-FD-Note-Id", "X-FD-Type"])
    def test_it_says_which_one_is_missing(self, tmp_path, header):
        stripped = _rewrite(MENTION_2, tmp_path / "t.eml", drop_header=header)
        got = parse(stripped)
        assert got["ok"] is False
        assert got["reason"] == f"missing_header:{header}", got

    def test_no_ids_are_guessed_when_a_header_is_gone(self, tmp_path):
        stripped = _rewrite(MENTION_2, tmp_path / "t.eml", drop_header="X-FD-Ticket-Id")
        got = parse(stripped)
        assert "ticket_id" not in got and "mention_text" not in got


class TestTheTextIsNeverEmpty:
    """The mutation target: an empty mention would become a Jira issue with no description."""

    def test_a_body_without_the_anchor_is_a_named_refusal(self, tmp_path):
        broken = _rewrite_html(MENTION_2, tmp_path / "t.eml",
                               "Thread message", "Something Else Entirely")
        got = parse(broken)
        assert got["ok"] is False
        assert got["reason"] == "mention_text_not_found"

    def test_it_does_not_come_back_as_an_empty_string(self, tmp_path):
        """Explicitly: `""` is not an acceptable answer, and neither is a key that is there."""
        broken = _rewrite_html(MENTION_2, tmp_path / "t.eml",
                               "Thread message", "Something Else Entirely")
        got = parse(broken)
        assert got.get("mention_text") != ""
        assert "mention_text" not in got, (
            "a refusal must not carry a mention_text at all — an empty one reads like a real "
            "message that happened to be blank"
        )

    def test_the_helper_returns_none_not_empty(self):
        """Same rule one level down, where the fallback would be written."""
        assert parse_mention_email.extract_mention_text("<p>no anchor here</p>") is None


class TestTheThreadIndexIsReadTwiceAndCompared:
    def test_a_disagreement_between_link_and_message_id_is_refused(self, tmp_path):
        """The link says thread=2; the Message-ID is rewritten to say 1."""
        crossed = _rewrite(
            MENTION_2, tmp_path / "t.eml",
            replace=("1000000.101.2.900000000002", "1000000.101.1.900000000002"))
        got = parse(crossed)
        assert got["ok"] is False
        assert got["reason"] == "thread_id_mismatch"
        assert got["thread_id_from_url"] == "2"
        assert got["thread_id_from_message_id"] == "1"

    def test_an_unparseable_message_id_is_refused(self, tmp_path):
        broken = _rewrite(MENTION_2, tmp_path / "t.eml",
                          replace=("1000000.101.2.900000000002", "not-structured-like-this"))
        got = parse(broken)
        assert got["ok"] is False
        assert got["reason"] == "message_id_unparseable"


def test_a_missing_file_is_reported_rather_than_raised(tmp_path):
    got = parse(tmp_path / "nope.eml")
    assert got["ok"] is False and got["reason"] == "file_not_found"


def test_the_script_is_self_contained():
    """No mossy, no dotenv: the skill script runtime has neither."""
    source = _MODULE_PATH.read_text(encoding="utf-8")
    imports = "\n".join(l for l in source.splitlines()
                        if l.startswith(("import ", "from ")) and "__future__" not in l)
    assert "mossy" not in imports and "dotenv" not in imports, imports


class TestOnlyMentionsAreProcessedByDefault:
    """A reply belongs to a thread that already produced its own mention.

    Measured: the reply on thread 2 came back with a ``problem`` almost identical to the mention
    that opened that thread, because both draw their context from the same ticket thread. Acting
    on each reply would file the same work again on every round of the conversation. The filter
    is a flag rather than a hardcoded rule so that turning replies on later is a configuration
    change, not a code change.
    """

    def test_a_reply_is_skipped_by_default(self):
        got = parse(REPLY_3)
        assert got["ok"] is False
        assert got["reason"] == "notification_type_skipped"

    def test_the_skip_says_what_was_dropped(self):
        """Visible in a log: which kind it was, what was required, and on which ticket."""
        got = parse(REPLY_3)
        assert got["notification_type"] == "reply"
        assert got["required"] == "mention"
        assert got["ticket_id"] == 101

    def test_a_skipped_reply_carries_no_text_to_act_on(self):
        got = parse(REPLY_3)
        assert "mention_text" not in got

    def test_the_same_reply_parses_with_require_type_any(self):
        got = parse(REPLY_3, require_type="any")
        assert got["ok"] is True
        assert got["notification_type"] == "reply"
        assert got["mention_text"] == "Third mention, replying inside the existing thread"

    @pytest.mark.parametrize("fixture", [MENTION_1, MENTION_2])
    @pytest.mark.parametrize("mode", ["mention", "any"])
    def test_mentions_pass_in_both_modes(self, fixture, mode):
        got = parse(fixture, require_type=mode)
        assert got["ok"] is True
        assert got["notification_type"] == "mention"

    def test_an_unknown_require_type_is_refused(self):
        got = parse(MENTION_2, require_type="everything")
        assert got["ok"] is False
        assert got["reason"] == "unknown_require_type"

    def test_the_cli_defaults_to_mention_and_accepts_any(self):
        parser = parse_mention_email._build_parser()
        assert parser.parse_args(["--eml-path", "x.eml"]).require_type == "mention"
        assert parser.parse_args(
            ["--eml-path", "x.eml", "--require-type", "any"]).require_type == "any"
        with pytest.raises(SystemExit):
            parser.parse_args(["--eml-path", "x.eml", "--require-type", "replies-only"])


class TestAMentionInsideAnAlreadyOpenThreadIsStillAMention:
    """The finding the type filter rests on, pinned so it cannot regress.

    Before this was measured, the risk was that Freshdesk classified anything written inside an
    existing thread as a reply (type 30). If it did, filtering to mentions would silently drop
    real mentions — the worst possible failure for this skill. It does not: fixture 4 was written
    inside thread 2, which fixture 2 had already opened, and it still arrives as **type 31**. The
    ``@`` decides the type, not where it is written.

    Fixtures 3 and 4 are the pair that proves it: same ticket, same thread, same anchor note,
    different types, because one tagged someone and the other did not.
    """

    def test_it_arrives_as_a_mention(self):
        got = parse(MENTION_IN_OPEN_THREAD_4)
        assert got["ok"] is True
        assert got["notification_type"] == "mention"

    def test_it_passes_the_default_filter(self):
        """The point: the filter that drops replies does not drop this."""
        assert parse(MENTION_IN_OPEN_THREAD_4)["ok"] is True
        assert parse(REPLY_3)["ok"] is False

    def test_it_is_the_same_thread_as_the_reply(self):
        mention, reply = parse(MENTION_IN_OPEN_THREAD_4), parse(REPLY_3, require_type="any")
        assert mention["thread_id"] == reply["thread_id"] == 2
        assert mention["thread_anchor_note_id"] == reply["thread_anchor_note_id"]
        assert mention["ticket_id"] == reply["ticket_id"] == 101

    def test_only_the_type_and_the_text_differ(self):
        mention, reply = parse(MENTION_IN_OPEN_THREAD_4), parse(REPLY_3, require_type="any")
        assert mention["notification_type"] == "mention"
        assert reply["notification_type"] == "reply"
        assert mention["mention_text"] != reply["mention_text"]
        assert mention["freshdesk_url"] == reply["freshdesk_url"]

    def test_its_text_is_read_whole(self):
        got = parse(MENTION_IN_OPEN_THREAD_4)
        assert got["mention_text"] == (
            "Test Agent Fourth test, first mention inside an existing thread")
