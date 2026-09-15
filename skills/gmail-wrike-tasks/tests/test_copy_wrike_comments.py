"""Copying a Wrike task's new comments onto the Jira issue it already has.

the comment copy. Loaded by file path. **Nothing here touches the network**: the Jira reader/writer and
the Wrike client are injected, and the writer double counts every call so "wrote nothing" is an
assertion and not a hope. The register is a real DuckDB file under ``tmp_path`` — never
``data/mossy.duckdb``, and never the operator's own register.

Two classes carry the weight, and neither may be relaxed:

``TestWithoutApplyNothingIsWritten`` — the same gate as the create step. The default describes and stops.

``TestTheAnchorIsReadBeforeAnythingIsWritten`` — the one that is specific to this subtask. A
duplicate comment cannot be deleted from this Jira and no search will ever flag it, so the
anchors already on the issue are read **before** the first POST, and a read-back that fails
stops the pass instead of being read as "no comments".

The comment corpus is the 29 real ones, verbatim from the Wrike API
(``tests/fixtures/wrike_task_comments.json``) — not invented markup.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


copier = _load("copy_wrike_comments")
links = _load("wrike_jira_links")
decide_module = _load("decide_wrike_action")
run = copier.run
plan = copier.plan

TASK = "MAAAAAEXmain"
ISSUE = "PROJ-193"
METADATA = {"taskApiId": TASK, "taskId": 1234567890, "notificationType": "Regular",
            "entityType": "Task"}

REAL_COMMENTS = json.loads((_FIXTURES / "wrike_task_comments.json").read_text(encoding="utf-8"))


def message(*, sender="wrike@wrike.com"):
    return {"id": "gmail-1", "threadId": "thread-1", "payload": {"headers": [
        {"name": "From", "value": f"Anna <{sender}>"},
        {"name": "Subject", "value": "Prepare sample import requests"},
        {"name": "X-Wrike-Metadata", "value": json.dumps(METADATA)},
    ]}}


def comment(cid="IEAEXAMPLEC00001", *, author="KUEXAMP1", when="2026-08-28T12:33:28Z",
            text="Hello"):
    row = {"id": cid, "createdDate": when, "text": text}
    if author is not None:
        row["authorId"] = author
    return row


class FakeSearch:
    """The Jira reader ``decide_wrike_action`` uses, plus the canary the create step introduced."""

    def __init__(self, *, exists=None, found=None, proven=None):
        self.calls: list[tuple] = []
        self._exists = exists or {"ok": True, "exists": True, "authenticated": True}
        self._found = found or {"ok": True, "matched": False, "authenticated": None,
                                "empty": True}
        self._proven = proven if proven is not None else {"ok": True, "proven": True}

    def issue_exists(self, key):
        self.calls.append(("issue_exists", key))
        return self._exists

    def find_issue_for_task(self, task):
        self.calls.append(("find_issue_for_task", task))
        return self._found

    def credentials_proven(self):
        self.calls.append(("credentials_proven",))
        return self._proven


class FakeWrike:
    def __init__(self, comments=None, *, ok=True, raises=None):
        self.calls: list[tuple] = []
        self._comments = comments if comments is not None else [comment()]
        self._ok = ok
        self._raises = raises

    def get_task_comments(self, task):
        self.calls.append(("get_task_comments", task))
        if self._raises is not None:
            raise self._raises
        if not self._ok:
            return {"ok": False, "task_id": task, "reason": "not_visible", "comments": []}
        return {"ok": True, "task_id": task, "comments": list(self._comments),
                "count": len(self._comments), "empty": not self._comments}


class FakeWriter:
    """Counts every call. ``posted`` is what "nothing was written" is asserted against.

    ``anchors`` is what the issue already carries; ``add_comment`` does **not** add to it by
    itself, so a test that wants the second pass to see the first pass's comment has to say so —
    the way Jira would.
    """

    def __init__(self, *, anchors=(), read_raises=None, read_result=None, write_raises=None):
        self.calls: list[str] = []
        self.posted: list[tuple[str, str]] = []
        self.anchors = list(anchors)
        self._read_raises = read_raises
        self._read_result = read_result
        self._write_raises = write_raises
        self._next_id = 0

    def existing_comments(self, issue_key):
        self.calls.append("existing_comments")
        if self._read_raises is not None:
            raise self._read_raises
        if self._read_result is not None:
            return self._read_result
        return {"ok": True, "issue_key": issue_key, "count": len(self.anchors),
                "anchors": list(self.anchors), "authenticated": True}

    def add_comment(self, issue_key, text):
        self.calls.append("add_comment")
        if self._write_raises is not None:
            raise self._write_raises
        self.posted.append((issue_key, text))
        self._next_id += 1
        return {"comment_id": f"jira-{self._next_id}", "issue_key": issue_key, "status": 201}


@pytest.fixture
def register(tmp_path):
    """A real register, in a throwaway file. Never the operator's, never data/mossy.duckdb."""
    return tmp_path / "wrike-jira-links.duckdb"


@pytest.fixture
def linked(register):
    """The task has an issue in the register — the state ``linked_and_alive`` is decided from."""
    links.remember_link(TASK, ISSUE, f"https://x.atlassian.net/browse/{ISSUE}",
                        db_path=register)
    return register


# ---------------------------------------------------------------------- the anchor format
class TestTheAnchorIsOneFormat:
    """Written once, read once. A format spelled twice is a format that drifts, and the thing
    it guards against here is permanent."""

    def test_the_anchor_carries_the_wrike_comment_id(self):
        assert copier.anchor_for("IEAEXAMPLEC00001") == "[wrike-comment:IEAEXAMPLEC00001]"

    def test_what_was_written_is_what_is_read_back(self):
        body = {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [
                {"type": "text", "text": "text\n\n" + copier.anchor_for("ABC123")}]}]}
        assert copier.anchors_in(body) == {"ABC123"}

    def test_an_anchor_nested_deeper_is_still_found(self):
        """The body shape is Jira's, not ours. The anchor must not stop working because the
        text ended up one node further down."""
        body = {"content": [{"content": [{"content": [
            {"type": "text", "text": "[wrike-comment:DEEP]"}]}]}]}
        assert copier.anchors_in(body) == {"DEEP"}

    def test_plain_text_bodies_work_too(self):
        assert copier.anchors_in("older Jira: [wrike-comment:PLAIN]") == {"PLAIN"}

    def test_a_comment_without_an_anchor_yields_nothing(self):
        assert copier.anchors_in({"type": "text", "text": "somebody typed this by hand"}) == set()

    def test_several_anchors_in_one_body_all_come_back(self):
        text = "[wrike-comment:A] and [wrike-comment:B]"
        assert copier.anchors_in(text) == {"A", "B"}

    def test_a_body_that_is_not_a_body_is_empty_not_an_exception(self):
        """Never raises: this runs inside the guard that decides whether writing is safe, and a
        guard that explodes on an odd shape is a guard that gets wrapped in a bare except."""
        assert copier.anchors_in(None) == set()
        assert copier.anchors_in(42) == set()


# ---------------------------------------------------------------------- the author
class TestTheAuthorIsNeverLost:
    """Measured 11 September 2026: GET /contacts returns 1 of 1 on this Collaborator licence, so
    three of the four authors of the real comments do not resolve. The id goes in raw — dropping
    the author would lose the one thing that says two people wrote two comments."""

    def test_the_raw_wrike_id_is_the_author(self):
        assert "Wrike comment by KUEXAMP1 on" in copier.comment_body(comment(author="KUEXAMP1"))

    def test_the_id_is_taken_from_an_embedded_author_object_too(self):
        """2 of the 29 carry ``author: {id, name}`` instead of ``authorId`` — the ones written by
        the contact this licence can see."""
        row = {"id": "X", "createdDate": "2026-09-10T14:39:15Z", "text": "hi",
               "author": {"id": "KUEXAMP2", "name": "Olaf"}}
        assert "Wrike comment by KUEXAMP2 on" in copier.comment_body(row)

    def test_the_embedded_name_is_not_used(self):
        """Deliberate: printing a name for the visible contact and an id for everybody else
        would put two formats on one issue, and read as a bug rather than as a known limit."""
        row = {"id": "X", "createdDate": "2026-09-10T14:39:15Z", "text": "hi",
               "author": {"id": "KUEXAMP2", "name": "Olaf"}}
        assert "Olaf" not in copier.comment_body(row)

    def test_an_author_that_is_missing_entirely_still_has_a_slot(self):
        row = {"id": "X", "createdDate": "2026-09-10T14:39:15Z", "text": "hi"}
        body = copier.comment_body(row)
        assert copier.UNKNOWN_AUTHOR in body
        assert body.startswith("Wrike comment by ")

    def test_the_names_survive_inside_the_text_where_they_already_are(self):
        """Readability does not depend on resolving ids: Wrike mentions carry the name, and
        the create step's converter leaves it as plain text."""
        row = comment(text='<a class="stream-user-id avatar" rel="KUEXAMP1">'
                           "&#64;Anna Beispiel-Müller</a> Done !")
        assert "@Anna Beispiel-Müller Done !" in copier.comment_body(row)

    def test_the_licence_limit_is_stated_where_a_caller_would_look(self):
        assert "Collaborator" in copier.AUTHOR_IS_AN_ID_BY_LICENCE
        assert "1 contact of 1" in copier.AUTHOR_IS_AN_ID_BY_LICENCE

    def test_one_visible_contact_is_not_one_contact(self):
        """The trap this flow keeps meeting: an empty-or-thin 200 is not a fact about the
        world. Written down so nobody reads an absent author as a deleted one."""
        assert "only one is visible" in copier.AUTHOR_IS_AN_ID_BY_LICENCE


class TestTheDateIsWrikesOwn:
    def test_the_measured_shape_becomes_a_readable_stamp(self):
        assert copier._when({"createdDate": "2026-08-28T12:33:28Z"}) == "2026-08-28 12:33 UTC"

    def test_a_date_in_another_shape_is_passed_through_untouched(self):
        """Not parsed into a guess. A wrong timestamp on a comment nobody can delete is worse
        than an ugly one."""
        assert copier._when({"createdDate": "28.08.2026"}) == "28.08.2026"

    def test_no_date_still_leaves_the_sentence_readable(self):
        assert copier._when({}) == copier.UNKNOWN_DATE


# ---------------------------------------------------------------------- the body
class TestTheBodyHasThreeParts:
    def test_header_then_text_then_anchor(self):
        body = copier.comment_body(comment(cid="C1", author="KUEXAMP1",
                                           when="2026-08-28T12:33:28Z", text="Hello"))
        assert body == ("Wrike comment by KUEXAMP1 on 2026-08-28 12:33 UTC\n\n"
                        "Hello\n\n"
                        "[wrike-comment:C1]")

    def test_the_anchor_is_last(self):
        body = copier.comment_body(comment(text="a<br />b"))
        assert body.rstrip().endswith("]")
        assert body.rstrip().splitlines()[-1].startswith("[wrike-comment:")

    def test_an_empty_comment_still_gets_a_header_and_an_anchor(self):
        body = copier.comment_body(comment(cid="C2", text=""))
        assert body.startswith("Wrike comment by ")
        assert body.endswith("[wrike-comment:C2]")

    def test_the_conversion_is_vi203s_own(self):
        """Imported, not copied. The one already measured against the six descriptions and
        these 29 comments — a second copy would drift from the one with the measurements."""
        raw = "<ul><li>one</li><li>two</li></ul>"
        creator = _load("create_jira_for_task")
        assert creator._plain(raw) in copier.comment_body(comment(text=raw))


class TestTheRealTwentyNine:
    """The corpus as captured from the API — not invented markup, but not byte-verbatim: non-breaking
    spaces became plain spaces and rows were trimmed when it was captured (measured 14 Sep 2026)."""

    def test_every_real_comment_round_trips_through_its_anchor(self):
        for row in REAL_COMMENTS:
            body = copier.comment_body(row)
            assert copier.anchors_in(body) == {row["id"]}

    def test_no_html_tag_survives_into_any_of_them(self):
        import re
        for row in REAL_COMMENTS:
            body = copier.comment_body(row)
            assert not re.search(r"</?[a-z][a-z0-9]*[ />]", body), row["id"]
            assert "&#" not in body, row["id"]

    def test_every_real_comment_names_an_author(self):
        for row in REAL_COMMENTS:
            assert copier.UNKNOWN_AUTHOR not in copier.comment_body(row), row["id"]

    def test_the_two_with_an_embedded_author_are_still_ids(self):
        embedded = [r for r in REAL_COMMENTS if isinstance(r.get("author"), dict)]
        assert len(embedded) == 2
        for row in embedded:
            assert "Wrike comment by KUEXAMP2 on" in copier.comment_body(row)


class TestOldestFirst:
    """The Wrike API answers newest first — measured on the 29, 2026-09-10 down to 2026-06-12 —
    and an issue that reads backwards is an issue nobody can reorder afterwards."""

    def test_the_api_corpus_really_does_arrive_newest_first(self):
        dates = [r["createdDate"] for r in REAL_COMMENTS]
        assert dates == sorted(dates, reverse=True)

    def test_the_copy_order_is_chronological(self):
        ordered = copier._in_order(list(REAL_COMMENTS))
        assert [r["createdDate"] for r in ordered] == sorted(r["createdDate"]
                                                             for r in REAL_COMMENTS)

    def test_it_sorts_rather_than_reversing(self):
        """Reversing would produce backwards output the day the API's order changed, and
        silently. Given an already-chronological list it must stay chronological."""
        rows = [comment(cid="a", when="2026-01-01T00:00:00Z"),
                comment(cid="b", when="2026-02-01T00:00:00Z")]
        assert [r["id"] for r in copier._in_order(rows)] == ["a", "b"]

    def test_a_comment_without_a_date_keeps_its_place_instead_of_being_dropped(self):
        rows = [comment(cid="b", when="2026-02-01T00:00:00Z"),
                {"id": "nodate", "text": "x"},
                comment(cid="a", when="2026-01-01T00:00:00Z")]
        assert [r["id"] for r in copier._in_order(rows)] == ["a", "b", "nodate"]


# ---------------------------------------------------------------------- which verdicts copy
class TestOnlyLinkedAndAliveCopies:
    def test_a_confirmed_issue_is_the_one_case_that_copies(self, linked):
        result = plan(message(), search=FakeSearch(), wrike=FakeWrike(), db_path=linked)
        assert result["verdict"] == decide_module.LINKED_AND_ALIVE
        assert result["action"] == copier.ACTION_COPY
        assert result["issue_key"] == ISSUE

    def test_with_no_link_the_issue_has_to_be_created_first(self, register):
        result = plan(message(), search=FakeSearch(), wrike=FakeWrike(), db_path=register)
        assert result["verdict"] == decide_module.NO_LINK_FOUND
        assert result["action"] == copier.ACTION_CREATE_FIRST

    def test_found_by_label_only_defers_to_vi203(self, register):
        """The issue exists but the local row is missing. One module owns that row, and it is
        the one whose subtask is repairing it."""
        search = FakeSearch(found={"ok": True, "matched": True, "authenticated": True,
                                   "issue_key": ISSUE, "label_confirmed": True})
        result = plan(message(), search=search, wrike=FakeWrike(), db_path=register)
        assert result["verdict"] == decide_module.FOUND_BY_LABEL_ONLY
        assert result["action"] == copier.ACTION_REPAIR_FIRST

    def test_a_message_that_is_not_a_notification_does_nothing(self, register):
        result = plan({"id": "x", "payload": {"headers": []}}, search=FakeSearch(),
                      wrike=FakeWrike(), db_path=register)
        assert result["action"] == copier.ACTION_NOTHING

    def test_a_missing_client_stops_rather_than_copying(self, register):
        result = plan(message(), search=None, wrike=None, db_path=register)
        assert result["verdict"] == decide_module.NOT_CONFIGURED
        assert result["action"] == copier.ACTION_STOP

    def test_an_unreadable_jira_stops(self, linked):
        search = FakeSearch(exists={"ok": False, "exists": None})
        search._exists = None  # the shape decide_wrike_action calls "not a result"
        result = plan(message(), search=search, wrike=FakeWrike(), db_path=linked)
        assert result["verdict"] == decide_module.JIRA_ERROR
        assert result["action"] == copier.ACTION_STOP


class TestWrikeSilenceIsNotNothingNew:
    def test_a_wrike_failure_stops_the_pass_instead_of_copying_nothing_quietly(self, linked):
        result = plan(message(), search=FakeSearch(),
                      wrike=FakeWrike(raises=RuntimeError("boom")), db_path=linked)
        assert result["action"] == copier.ACTION_STOP
        assert result["reason"] == "comments_unreadable"

    def test_an_empty_comment_list_is_flagged_as_uncertain(self, linked):
        result = plan(message(), search=FakeSearch(), wrike=FakeWrike([]), db_path=linked)
        assert result["action"] == copier.ACTION_COPY
        assert result["would_copy"] == []
        assert result["comments_certain"] is False
        assert "Collaborator" in result["comments_means"]

    def test_a_comment_with_no_id_is_reported_and_never_copied(self, linked):
        """The register keeps it on purpose; this module refuses it. It cannot be anchored and
        cannot be recorded, so copying it would repeat it on every pass, for ever."""
        rows = [comment(cid="C1"), {"createdDate": "2026-08-28T12:33:28Z", "text": "no id"}]
        result = plan(message(), search=FakeSearch(), wrike=FakeWrike(rows), db_path=linked)
        assert [c["comment_id"] for c in result["would_copy"]] == ["C1"]
        assert result["skipped_without_id"] == 1


class TestTheRegisterIsSubtracted:
    def test_a_comment_already_recorded_is_not_offered_again(self, linked):
        links.remember_comment(TASK, "C1", db_path=linked)
        rows = [comment(cid="C1"), comment(cid="C2")]
        result = plan(message(), search=FakeSearch(), wrike=FakeWrike(rows), db_path=linked)
        assert [c["comment_id"] for c in result["would_copy"]] == ["C2"]


# ---------------------------------------------------------------------- the gate
class TestWithoutApplyNothingIsWritten:
    """The gate, and the class that must never be relaxed. Same rule as the create step and as the
    Freshdesk reply gate (the Freshdesk reply gate): the default describes and stops.

    The writer counts every call, so this asserts that no write **and no read-back** happened —
    not merely that no comment came out the far end. A module that reached Jira by default would
    still be reaching Jira by default.
    """

    def test_the_default_writes_nothing(self, linked):
        writer = FakeWriter()
        result = run(message(), search=FakeSearch(), wrike=FakeWrike([comment(cid="C1")]),
                     writer=writer, db_path=linked)
        assert writer.calls == []
        assert writer.posted == []
        assert result["applied"] is False
        assert result["posted"] == 0
        assert result["detail"] == "Nothing was written. Pass apply=True to copy these."

    def test_apply_false_spelled_out_writes_nothing_either(self, linked):
        writer = FakeWriter()
        run(message(), apply=False, search=FakeSearch(), wrike=FakeWrike([comment(cid="C1")]),
            writer=writer, db_path=linked)
        assert writer.calls == []

    def test_the_register_gains_nothing_without_apply(self, linked):
        run(message(), search=FakeSearch(), wrike=FakeWrike([comment(cid="C1")]),
            writer=FakeWriter(), db_path=linked)
        assert links.copied_comment_ids(TASK, db_path=linked) == set()

    def test_plan_has_no_apply_parameter_at_all(self):
        """Not "defaults to False": absent. There is no argument anybody can pass to ``plan``
        that makes it write."""
        import inspect
        assert "apply" not in inspect.signature(plan).parameters

    def test_it_still_says_what_it_would_have_copied(self, linked):
        """Refusing to write is not refusing to answer."""
        result = run(message(), search=FakeSearch(), wrike=FakeWrike([comment(cid="C1")]),
                     writer=FakeWriter(), db_path=linked)
        assert [c["comment_id"] for c in result["would_copy"]] == ["C1"]

    def test_with_apply_the_same_call_does_write(self, linked):
        """The other half: without this, "wrote nothing" could be a module that never writes."""
        writer = FakeWriter()
        result = run(message(), apply=True, search=FakeSearch(),
                     wrike=FakeWrike([comment(cid="C1")]), writer=writer, db_path=linked)
        assert result["posted"] == 1
        assert len(writer.posted) == 1

    def test_no_writer_is_reported_and_nothing_is_invented(self, linked):
        result = run(message(), apply=True, search=FakeSearch(),
                     wrike=FakeWrike([comment(cid="C1")]), writer=None, db_path=linked)
        assert result["reason"] == "no_writer"
        assert result["posted"] == 0


class TestTheCredentialsAreProvenBeforeWriting:
    """The canary the create step introduced. An unauthenticated client is served empty searches and
    404s, and a copy made on a bad token cannot be taken back."""

    def test_an_unproven_token_copies_nothing(self, linked):
        writer = FakeWriter()
        search = FakeSearch(proven={"ok": True, "proven": None,
                                    "reason": "empty_project_or_unauthenticated"})
        result = run(message(), apply=True, search=search,
                     wrike=FakeWrike([comment(cid="C1")]), writer=writer, db_path=linked)
        assert result["reason"] == "authentication_not_proven"
        assert writer.calls == []

    def test_the_canary_runs_before_the_read_back(self, linked):
        search = FakeSearch(proven={"ok": True, "proven": None})
        writer = FakeWriter()
        run(message(), apply=True, search=search, wrike=FakeWrike([comment(cid="C1")]),
            writer=writer, db_path=linked)
        assert ("credentials_proven",) in search.calls
        assert "existing_comments" not in writer.calls


# ---------------------------------------------------------------------- the anchor read-back
class TestTheAnchorIsReadBeforeAnythingIsWritten:
    """The guard specific to this subtask.

    A duplicate issue is flagged by the label search; a duplicate **comment** is flagged by
    nothing at all, and this Jira account cannot delete it. So Jira's own anchors — not the
    local register, which is one losable file — decide what has already been copied, and they
    are read before the first POST rather than after it.
    """

    def test_the_read_back_happens_before_the_first_write(self, linked):
        writer = FakeWriter()
        run(message(), apply=True, search=FakeSearch(), wrike=FakeWrike([comment(cid="C1")]),
            writer=writer, db_path=linked)
        assert writer.calls[0] == "existing_comments"
        assert writer.calls.index("existing_comments") < writer.calls.index("add_comment")

    def test_a_comment_already_anchored_in_jira_is_not_copied_again(self, linked):
        """The register knows nothing — this is the case where it was lost, or never written.
        Jira says the comment is already there, and Jira is the record."""
        writer = FakeWriter(anchors=["C1"])
        result = run(message(), apply=True, search=FakeSearch(),
                     wrike=FakeWrike([comment(cid="C1")]), writer=writer, db_path=linked)
        assert writer.posted == []
        assert result["posted"] == 0
        assert result["already_in_jira"] == ["C1"]

    def test_finding_it_in_jira_repairs_the_register(self, linked):
        writer = FakeWriter(anchors=["C1"])
        run(message(), apply=True, search=FakeSearch(), wrike=FakeWrike([comment(cid="C1")]),
            writer=writer, db_path=linked)
        assert links.copied_comment_ids(TASK, db_path=linked) == {"C1"}

    def test_the_new_one_is_copied_while_the_anchored_one_is_not(self, linked):
        writer = FakeWriter(anchors=["C1"])
        rows = [comment(cid="C1", when="2026-01-01T00:00:00Z"),
                comment(cid="C2", when="2026-02-01T00:00:00Z")]
        result = run(message(), apply=True, search=FakeSearch(), wrike=FakeWrike(rows),
                     writer=writer, db_path=linked)
        assert len(writer.posted) == 1
        assert "[wrike-comment:C2]" in writer.posted[0][1]
        assert result["posted"] == 1

    def test_an_unreadable_read_back_stops_the_pass(self, linked):
        """Unknown is not none. Writing here would be copying blind onto an issue nobody can
        clean, which is the whole reason this read exists."""
        writer = FakeWriter(read_raises=copier.JiraCommentError("Jira refused", status=404))
        result = run(message(), apply=True, search=FakeSearch(),
                     wrike=FakeWrike([comment(cid="C1")]), writer=writer, db_path=linked)
        assert writer.posted == []
        assert result["reason"] == "existing_comments_unreadable"
        assert result["applied"] is False

    def test_a_partial_read_back_stops_the_pass_too(self, linked):
        """More comments than this read saw: the anchor list is incomplete, and an incomplete
        anchor list is exactly what would let a duplicate through."""
        writer = FakeWriter(read_result={"ok": False, "reason": "more_than_one_page",
                                         "anchors": [], "total": 140})
        result = run(message(), apply=True, search=FakeSearch(),
                     wrike=FakeWrike([comment(cid="C1")]), writer=writer, db_path=linked)
        assert writer.posted == []
        assert result["reason"] == "existing_comments_unreadable"

    def test_the_reason_is_written_down_where_the_caller_reads_it(self, linked):
        writer = FakeWriter(read_raises=RuntimeError("boom"))
        result = run(message(), apply=True, search=FakeSearch(),
                     wrike=FakeWrike([comment(cid="C1")]), writer=writer, db_path=linked)
        assert result["detail"] == copier.ANCHORS_MUST_BE_READ_FIRST
        assert "is not an empty one" in copier.ANCHORS_MUST_BE_READ_FIRST

    def test_every_posted_body_carries_its_anchor(self, linked):
        rows = [comment(cid="C1", when="2026-01-01T00:00:00Z"),
                comment(cid="C2", when="2026-02-01T00:00:00Z")]
        writer = FakeWriter()
        run(message(), apply=True, search=FakeSearch(), wrike=FakeWrike(rows), writer=writer,
            db_path=linked)
        for _, body in writer.posted:
            assert copier.anchors_in(body), body

    def test_the_writes_go_out_oldest_first(self, linked):
        rows = [comment(cid="new", when="2026-02-01T00:00:00Z"),
                comment(cid="old", when="2026-01-01T00:00:00Z")]
        writer = FakeWriter()
        run(message(), apply=True, search=FakeSearch(), wrike=FakeWrike(rows), writer=writer,
            db_path=linked)
        assert [copier.anchors_in(b).pop() for _, b in writer.posted] == ["old", "new"]


class TestACrashHalfWayThroughDoesNotDuplicate:
    """The failure this ordering is chosen for: POST first, record second. A pass that dies in
    between leaves the comment in Jira with its anchor and no row in the register — and the next
    pass must copy nothing and write the row it is missing."""

    def test_the_second_pass_copies_nothing_and_heals_the_row(self, linked, monkeypatch):
        first = FakeWriter()
        # The crash: the POST lands, the register write never happens. A context, not
        # ``monkeypatch.undo()``: undo reverts every patch in the test, the conftest's lock
        # directory included, and the second pass then locked in the operator's real directory
        # — measured, the per-task lock.
        with monkeypatch.context() as crashed:
            crashed.setattr(links, "remember_comment",
                            lambda *a, **k: {"ok": False, "reason": "crashed"})
            run(message(), apply=True, search=FakeSearch(),
                wrike=FakeWrike([comment(cid="C1")]), writer=first, db_path=linked)
        assert len(first.posted) == 1
        assert links.copied_comment_ids(TASK, db_path=linked) == set()

        # The next pass. Jira carries the anchor the dead pass wrote.
        second = FakeWriter(anchors=["C1"])
        result = run(message(), apply=True, search=FakeSearch(),
                     wrike=FakeWrike([comment(cid="C1")]), writer=second, db_path=linked)
        assert second.posted == []
        assert result["healed"] == 1
        assert links.copied_comment_ids(TASK, db_path=linked) == {"C1"}

    def test_a_refused_write_stops_the_loop_instead_of_hammering(self, linked):
        rows = [comment(cid="C1", when="2026-01-01T00:00:00Z"),
                comment(cid="C2", when="2026-02-01T00:00:00Z")]
        writer = FakeWriter(write_raises=copier.JiraCommentError("refused", status=400))
        result = run(message(), apply=True, search=FakeSearch(), wrike=FakeWrike(rows),
                     writer=writer, db_path=linked)
        assert writer.calls.count("add_comment") == 1
        assert result["reason"] == "copy_failed"
        assert result["failed_comment_id"] == "C1"

    def test_a_refused_write_is_never_recorded_as_copied(self, linked):
        """Recording a write that did not happen loses the comment for good — the register would
        subtract it on every later pass."""
        writer = FakeWriter(write_raises=RuntimeError("boom"))
        run(message(), apply=True, search=FakeSearch(), wrike=FakeWrike([comment(cid="C1")]),
            writer=writer, db_path=linked)
        assert links.copied_comment_ids(TASK, db_path=linked) == set()

    def test_a_successful_copy_is_recorded(self, linked):
        writer = FakeWriter()
        run(message(), apply=True, search=FakeSearch(), wrike=FakeWrike([comment(cid="C1")]),
            writer=writer, db_path=linked)
        assert links.copied_comment_ids(TASK, db_path=linked) == {"C1"}

    def test_running_twice_copies_once(self, linked):
        """The ordinary repeat, guarded by the register alone — no anchor needed, because
        nothing was lost."""
        first, second = FakeWriter(), FakeWriter()
        run(message(), apply=True, search=FakeSearch(), wrike=FakeWrike([comment(cid="C1")]),
            writer=first, db_path=linked)
        result = run(message(), apply=True, search=FakeSearch(),
                     wrike=FakeWrike([comment(cid="C1")]), writer=second, db_path=linked)
        assert len(first.posted) == 1
        assert second.posted == []
        assert result["posted"] == 0


# ---------------------------------------------------------------------- the real client
class TestTheRealCommentClient:
    """``JiraCommentWriter`` itself, with no double in the way.

    Every test above injects ``FakeWriter`` and asserts that the anchors reached *it*. That is
    the shape of the mutation that survived forty-three green tests in the create step — the rule that a double never replaces the real client again —
    so the real class is pinned separately: the URLs it calls, the verbs it uses, the body it
    sends, and its refusal to turn a failure into an empty answer.
    """

    @pytest.fixture()
    def transport(self, monkeypatch):
        sent: dict = {"requests": []}
        replies: dict = {"GET": {"comments": [], "total": 0, "maxResults": 100},
                         "POST": {"id": "10101"}}
        status = {"GET": 200, "POST": 201}

        class Response:
            def __init__(self, payload, code):
                self._b = json.dumps(payload).encode()
                self._c = code

            def getcode(self):
                return self._c

            def read(self):
                return self._b

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake(request, timeout=None):
            method = request.get_method()
            sent["requests"].append({
                "url": request.full_url, "method": method,
                "headers": dict(request.headers),
                "body": json.loads(request.data.decode()) if request.data else None,
            })
            sent[method.lower()] = sent["requests"][-1]
            if status[method] >= 400:
                # What the real urlopen does with a 4xx or 5xx: it RAISES. A fake that returned a
                # 400 as an ordinary response left the client's except-HTTPError branch run by
                # no test — measured, swallowing that branch kept the suite green.
                raise urllib.error.HTTPError(request.full_url, status[method], "refused", {},
                                             io.BytesIO(json.dumps(replies[method]).encode()))
            return Response(replies[method], status[method])

        monkeypatch.setattr(copier.urllib.request, "urlopen", fake)
        sent["replies"] = replies
        sent["status"] = status
        return sent

    @pytest.fixture()
    def client(self):
        return copier.JiraCommentWriter("example.atlassian.net", "a@b.test", "tok")

    def test_the_write_is_one_post_to_the_comment_endpoint(self, client, transport):
        client.add_comment(ISSUE, "text")
        assert transport["post"]["method"] == "POST"
        assert transport["post"]["url"] == (
            f"https://example.atlassian.net/rest/api/3/issue/{ISSUE}/comment")

    def test_the_anchor_really_travels_in_the_post_body(self, client, transport):
        """Not "reached the double": reached the request. This is the assertion the create step's
        mutation would have needed."""
        client.add_comment(ISSUE, "line\n\n[wrike-comment:C1]")
        assert copier.anchors_in(transport["post"]["body"]["body"]) == {"C1"}

    def test_the_body_is_the_adf_shape_vi203_measured(self, client, transport):
        client.add_comment(ISSUE, "one\n\ntwo")
        body = transport["post"]["body"]["body"]
        assert body["type"] == "doc" and body["version"] == 1
        assert [p["content"][0]["text"] for p in body["content"]] == ["one", "two"]

    def test_the_read_is_one_get_to_the_same_endpoint(self, client, transport):
        client.existing_comments(ISSUE)
        assert transport["get"]["method"] == "GET"
        assert transport["get"]["url"].startswith(
            f"https://example.atlassian.net/rest/api/3/issue/{ISSUE}/comment?")
        assert transport["get"]["body"] is None

    def test_the_anchors_come_back_from_real_adf_bodies(self, client, transport):
        transport["replies"]["GET"] = {"total": 1, "comments": [
            {"id": "1", "body": {"type": "doc", "content": [{"type": "paragraph", "content": [
                {"type": "text", "text": "header\n\n[wrike-comment:REAL]"}]}]}}]}
        assert client.existing_comments(ISSUE)["anchors"] == ["REAL"]

    def test_both_verbs_carry_the_basic_credentials(self, client, transport):
        client.existing_comments(ISSUE)
        client.add_comment(ISSUE, "x")
        for request in transport["requests"]:
            assert request["headers"]["Authorization"].startswith("Basic ")

    def test_a_refused_read_raises_rather_than_looking_empty(self, client, monkeypatch):
        """The whole guard rests on this. A 404 here — which an unauthenticated client is also
        served — must never arrive at the caller as "no comments"."""
        def refused(request, timeout=None):
            raise copier.urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)

        monkeypatch.setattr(copier.urllib.request, "urlopen", refused)
        with pytest.raises(copier.JiraCommentError) as exc:
            client.existing_comments(ISSUE)
        assert exc.value.status == 404

    def test_a_refused_write_raises_rather_than_looking_written(self, client, transport):
        transport["status"]["POST"] = 400
        transport["replies"]["POST"] = {"errorMessages": ["nope"]}
        with pytest.raises(copier.JiraCommentError) as refused:
            client.add_comment(ISSUE, "x")
        assert refused.value.status == 400

    @pytest.mark.parametrize("payload", [{}, [], "<html>proxy page</html>", None,
                                         {"comments": None}, {"total": 0}])
    def test_a_200_that_is_not_a_page_of_comments_is_not_no_comments(self, client, transport,
                                                                     payload):
        """Read as no comments, the anchors would be empty and every comment would be posted
        again onto an issue nobody can clean. The docstring always promised a raise."""
        transport["replies"]["GET"] = payload
        with pytest.raises(copier.JiraCommentError):
            client.existing_comments(ISSUE)

    def test_a_real_page_with_no_comments_is_still_an_answer(self, client, transport):
        """The inversion: a proper empty page is not an error."""
        transport["replies"]["GET"] = {"comments": [], "total": 0, "maxResults": 100}
        assert client.existing_comments(ISSUE)["anchors"] == []

    def test_a_200_with_no_id_is_not_a_written_comment(self, client, transport):
        transport["replies"]["POST"] = {"self": "https://x/1"}
        with pytest.raises(copier.JiraCommentError):
            client.add_comment(ISSUE, "x")

    def test_more_comments_than_this_read_saw_is_not_ok(self, client, transport):
        """The stated limit: 100 asked for, no paging. It refuses out loud instead of handing
        back an anchor list that is quietly short."""
        transport["replies"]["GET"] = {"total": 140, "comments": [{"id": "1", "body": "x"}]}
        answer = client.existing_comments(ISSUE)
        assert answer["ok"] is False
        assert answer["reason"] == "more_than_one_page"
        assert answer["total"] == 140

    def test_the_limit_is_written_down_beside_the_code_that_has_it(self):
        import inspect
        source = inspect.getsource(copier.JiraCommentWriter.existing_comments)
        assert "startAt" in source
        assert "not been measured here" in source

    def test_an_empty_issue_key_never_becomes_a_request(self, client, transport):
        with pytest.raises(copier.JiraCommentError):
            client.existing_comments("")
        assert transport["requests"] == []

    def test_from_env_needs_all_three(self, monkeypatch):
        for name in ("JIRA_SITE", "JIRA_USER_EMAIL", "JIRA_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        assert copier.JiraCommentWriter.from_env() is None
        monkeypatch.setenv("JIRA_SITE", "https://example.atlassian.net/")
        monkeypatch.setenv("JIRA_USER_EMAIL", "a@b.test")
        assert copier.JiraCommentWriter.from_env() is None
        monkeypatch.setenv("JIRA_API_KEY", "tok")
        assert copier.JiraCommentWriter.from_env().site == "example.atlassian.net"

    def test_the_class_has_two_verbs_and_no_others(self):
        """A writer that grew a delete or an update would be a writer nobody reviewed."""
        public = {n for n in vars(copier.JiraCommentWriter) if not n.startswith("_")}
        assert public == {"existing_comments", "add_comment", "from_env"}


class TestTheReadOnlyModuleStaysReadOnly:
    def test_jira_label_search_gained_no_write(self):
        """the label search is read-only and this subtask did not change that: the GET that guards the
        write lives with the write, not in the search."""
        search_module = _load("jira_label_search")
        import inspect
        source = inspect.getsource(search_module)
        assert '"POST"' not in source and "'POST'" not in source
        assert "method" not in inspect.signature(search_module.JiraLabelSearch._get).parameters
