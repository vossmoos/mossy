"""What already exists for a Wrike task, described — and never a decision to write anything.

the decision step. Loaded by file path. **Nothing here touches the network**: the Jira and Wrike clients are
injected, and the doubles below record what was asked so the order of consultation is a fact a
test can hold rather than a comment.

Two classes carry the weight.

``TestTheTenVerdicts`` drives every branch and asserts each one produces its **own** verdict. The
point is not coverage: it is that "no link", "not authenticated", "not configured" and "the key
404s" are four different situations, and a module that flattened any two of them would send the
caller down the wrong road while looking like it worked.

``TestTheUnconfirmableCaseRecommendsNothing`` is the one that matters most. The table has a row,
Jira answered 404, and an unauthenticated client is served 404 too. Reading that as "the row is
dead, open a new issue" would create a duplicate issue **per task, on every pass**, for as long as
the token stayed broken. The verdict stops there and recommends nothing, and these tests are what
stop somebody making it helpful.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


decide_module = _load("decide_wrike_action")
links = _load("wrike_jira_links")
decide = decide_module.decide

TASK = "MAAAAAEXmain"
METADATA = {"taskApiId": TASK, "taskId": 1234567890, "accountApiId": "IEAAUIYL",
            "notificationType": "Regular", "entityType": "Task", "watchReason": "Assignee"}


def message(*, sender="wrike@wrike.com", metadata=METADATA):
    headers = [{"name": "From", "value": f"=?UTF-8?Q?Anna?= <{sender}>"},
               {"name": "Subject", "value": "Prepare sample import requests"}]
    if metadata is not None:
        headers.append({"name": "X-Wrike-Metadata", "value": json.dumps(metadata)})
    return {"id": "gmail-1", "threadId": "thread-1", "payload": {"headers": headers}}


class FakeSearch:
    """Records every call, so the ORDER of consultation is testable and not just documented."""

    def __init__(self, *, exists=None, found=None, raises_on=None):
        self.calls: list[tuple[str, str]] = []
        self._exists = exists
        self._found = found
        self._raises_on = raises_on

    def issue_exists(self, key):
        self.calls.append(("issue_exists", key))
        if self._raises_on == "issue_exists":
            raise RuntimeError("jira is unreachable")
        return self._exists

    def find_issue_for_task(self, task):
        self.calls.append(("find_issue_for_task", task))
        if self._raises_on == "find_issue_for_task":
            raise RuntimeError("jira is unreachable")
        return self._found


class FakeWrike:
    def __init__(self, answer=None, raises=None):
        self.answer = answer
        self.raises = raises
        self.calls: list[str] = []

    def get_task_comments(self, task):
        self.calls.append(task)
        if self.raises is not None:
            raise self.raises
        return self.answer


EXISTS = {"ok": True, "exists": True, "authenticated": True, "issue_key": "PROJ-1"}
NOT_FOUND = {"ok": True, "exists": None, "authenticated": None,
             "reason": "not_found_or_unauthenticated"}
SEARCH_EMPTY = {"ok": True, "matched": False, "authenticated": None, "empty": True}


def SEARCH_MATCH(key="PROJ-2", **extra):
    return {"ok": True, "matched": True, "authenticated": True, "issue_key": key,
            "issue_url": f"https://x/browse/{key}", "label_confirmed": True, **extra}


COMMENTS = {"ok": True, "comments": [{"id": "c1"}, {"id": "c2"}], "empty": False, "count": 2}
NO_COMMENTS = {"ok": True, "comments": [], "empty": True, "count": 0}


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "links.duckdb"


@pytest.fixture(autouse=True)
def _never_the_real_register(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIKE_JIRA_LINKS_DB", str(tmp_path / "fallback.duckdb"))


#: Sentinel for the enumeration below: this case gets a register that cannot be opened.
UNREADABLE = object()


class TestTheElevenVerdicts:
    """Each situation gets its own verdict. None of them collapses into another."""

    def test_1_not_a_notification(self, db):
        result = decide(message(sender="noreply@wrike.com"),
                        search=FakeSearch(), wrike=FakeWrike(), db_path=db)
        assert result["verdict"] == decide_module.NOT_A_NOTIFICATION

    def test_2_not_configured_is_not_no_link(self, db):
        """A missing client says nothing about whether a link exists."""
        result = decide(message(), search=None, wrike=FakeWrike(), db_path=db)
        assert result["verdict"] == decide_module.NOT_CONFIGURED
        assert result["verdict"] != decide_module.NO_LINK_FOUND
        assert result["missing"] == ["jira"]

    def test_3_no_link_found(self, db):
        result = decide(message(), search=FakeSearch(found=SEARCH_EMPTY),
                        wrike=FakeWrike(COMMENTS), db_path=db)
        assert result["verdict"] == decide_module.NO_LINK_FOUND

    def test_4_linked_and_alive(self, db):
        links.remember_link(TASK, "PROJ-1", "https://x/browse/PROJ-1", db_path=db)
        result = decide(message(),
                        search=FakeSearch(exists=EXISTS, found=SEARCH_EMPTY),
                        wrike=FakeWrike(COMMENTS), db_path=db)
        assert result["verdict"] == decide_module.LINKED_AND_ALIVE
        assert result["issue_key"] == "PROJ-1"
        assert result["source"] == "table"

    def test_5_link_unconfirmable(self, db):
        links.remember_link(TASK, "PROJ-1", db_path=db)
        result = decide(message(),
                        search=FakeSearch(exists=NOT_FOUND, found=SEARCH_EMPTY),
                        wrike=FakeWrike(COMMENTS), db_path=db)
        assert result["verdict"] == decide_module.LINK_UNCONFIRMABLE
        assert result["table_key"] == "PROJ-1"

    def test_6_found_by_label_only(self, db):
        result = decide(message(), search=FakeSearch(found=SEARCH_MATCH()),
                        wrike=FakeWrike(COMMENTS), db_path=db)
        assert result["verdict"] == decide_module.FOUND_BY_LABEL_ONLY
        assert result["issue_key"] == "PROJ-2"
        assert result["source"] == "label"

    def test_7_link_disagrees(self, db):
        """Table says one key, the label says another. One task has one issue."""
        links.remember_link(TASK, "PROJ-1", db_path=db)
        result = decide(message(),
                        search=FakeSearch(exists=EXISTS, found=SEARCH_MATCH("PROJ-9")),
                        wrike=FakeWrike(COMMENTS), db_path=db)
        assert result["verdict"] == decide_module.LINK_DISAGREES
        assert result["table_key"] == "PROJ-1"
        assert result["label_key"] == "PROJ-9"

    def test_8_label_on_wrong_issue(self, db):
        result = decide(message(), search=FakeSearch(found={
            **SEARCH_EMPTY, "matched": True, "label_confirmed": False,
            "anomalies": ["label_not_on_issue"]}),
            wrike=FakeWrike(COMMENTS), db_path=db)
        assert result["verdict"] == decide_module.LABEL_ON_WRONG_ISSUE

    def test_9_ambiguous_match(self, db):
        result = decide(message(),
                        search=FakeSearch(found=SEARCH_MATCH(anomalies=["more_than_one_issue"])),
                        wrike=FakeWrike(COMMENTS), db_path=db)
        assert result["verdict"] == decide_module.AMBIGUOUS_MATCH

    def test_9b_more_than_one_page_is_also_ambiguous(self, db):
        result = decide(message(),
                        search=FakeSearch(found=SEARCH_MATCH(anomalies=["more_than_one_page"])),
                        wrike=FakeWrike(COMMENTS), db_path=db)
        assert result["verdict"] == decide_module.AMBIGUOUS_MATCH

    def test_10_jira_error_is_never_an_empty_result(self, db):
        result = decide(message(),
                        search=FakeSearch(found=SEARCH_EMPTY, raises_on="find_issue_for_task"),
                        wrike=FakeWrike(COMMENTS), db_path=db)
        assert result["verdict"] == decide_module.JIRA_ERROR
        assert result["verdict"] != decide_module.NO_LINK_FOUND

    def test_the_eleven_verdicts_are_eleven_different_strings(self):
        assert len(set(decide_module.VERDICTS)) == 11

    def test_11_register_unreadable_is_not_no_link(self, tmp_path):
        """the per-task lock. The register could not be read, so there may well be a row. Reading that as
        'no link' opens a second Jira issue for a task that already has one."""
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("x")
        result = decide(message(), search=FakeSearch(found=SEARCH_EMPTY),
                        wrike=FakeWrike(COMMENTS), db_path=blocker / "inside.duckdb")
        assert result["verdict"] == decide_module.REGISTER_UNREADABLE
        assert result["verdict"] != decide_module.NO_LINK_FOUND
        assert result["verdict"] != decide_module.JIRA_ERROR
        assert "duplicate issue" in result["detail"]

    def test_an_unreadable_register_never_reaches_jira(self, tmp_path):
        """Jira was not asked, so the verdict must not blame it — and no call may go out."""
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("x")
        search = FakeSearch(found=SEARCH_EMPTY)
        decide(message(), search=search, wrike=FakeWrike(COMMENTS),
               db_path=blocker / "inside.duckdb")
        assert search.calls == []

    def test_an_unreadable_register_mid_flow_is_not_nothing_copied(self, tmp_path, monkeypatch):
        """The issue is confirmed and stays confirmed; what is unknown is which comments were
        already copied. Reported, never flattened into 'all of them are new'."""
        db = tmp_path / "l.duckdb"
        links.remember_link(TASK, "PROJ-1", db_path=db)

        def unreadable(*a, **k):
            raise links.LinkStoreError("held by another process")

        monkeypatch.setattr(links, "comments_not_yet_copied", unreadable)
        result = decide(message(), search=FakeSearch(exists=EXISTS, found=SEARCH_EMPTY),
                        wrike=FakeWrike(COMMENTS), db_path=db)
        assert result["verdict"] == decide_module.LINKED_AND_ALIVE
        assert result["comments_certain"] is False
        assert "LinkStoreError" in result["comments_error"]
        assert "comments_to_copy" not in result

    def test_every_verdict_the_module_can_return_is_declared(self, db):
        """A verdict invented at a call site and missing from VERDICTS would be one nobody can
        enumerate."""
        seen = set()
        cases = [
            (message(sender="noreply@wrike.com"), FakeSearch(), FakeWrike(), None),
            (message(), None, FakeWrike(), None),
            (message(), FakeSearch(found=SEARCH_EMPTY), FakeWrike(COMMENTS), None),
            (message(), FakeSearch(exists=EXISTS, found=SEARCH_EMPTY), FakeWrike(COMMENTS), "PROJ-1"),
            (message(), FakeSearch(exists=NOT_FOUND, found=SEARCH_EMPTY), FakeWrike(COMMENTS), "PROJ-1"),
            (message(), FakeSearch(found=SEARCH_MATCH()), FakeWrike(COMMENTS), None),
            (message(), FakeSearch(exists=EXISTS, found=SEARCH_MATCH("PROJ-9")), FakeWrike(COMMENTS), "PROJ-1"),
            (message(), FakeSearch(found={**SEARCH_EMPTY, "matched": True,
                                          "label_confirmed": False}), FakeWrike(COMMENTS), None),
            (message(), FakeSearch(found=SEARCH_MATCH(anomalies=["more_than_one_issue"])),
             FakeWrike(COMMENTS), None),
            (message(), FakeSearch(raises_on="find_issue_for_task"), FakeWrike(COMMENTS), None),
            # the per-task lock: the register itself cannot be read. Marked by the sentinel below.
            (message(), FakeSearch(found=SEARCH_EMPTY), FakeWrike(COMMENTS), UNREADABLE),
        ]
        import tempfile

        for msg, s, w, existing in cases:
            root = Path(tempfile.mkdtemp())
            if existing is UNREADABLE:
                blocker = root / "not-a-directory"
                blocker.write_text("x")
                path = blocker / "inside.duckdb"
            else:
                path = root / "l.duckdb"
                if existing:
                    links.remember_link(TASK, existing, db_path=path)
            seen.add(decide(msg, search=s, wrike=w, db_path=path)["verdict"])
        assert seen == set(decide_module.VERDICTS), sorted(seen)


class TestAuthenticatedIsNeverFalse:
    """True only when a 200 carried content. None whenever it is not known."""

    def test_a_confirmed_key_proves_it(self, db):
        links.remember_link(TASK, "PROJ-1", db_path=db)
        result = decide(message(), search=FakeSearch(exists=EXISTS, found=SEARCH_EMPTY),
                        wrike=FakeWrike(COMMENTS), db_path=db)
        assert result["authenticated"] is True

    def test_rows_from_the_search_prove_it(self, db):
        result = decide(message(), search=FakeSearch(found=SEARCH_MATCH()),
                        wrike=FakeWrike(COMMENTS), db_path=db)
        assert result["authenticated"] is True

    def test_an_empty_search_leaves_it_unknown(self, db):
        result = decide(message(), search=FakeSearch(found=SEARCH_EMPTY),
                        wrike=FakeWrike(COMMENTS), db_path=db)
        assert result["authenticated"] is None

    def test_a_404_leaves_it_unknown(self, db):
        links.remember_link(TASK, "PROJ-1", db_path=db)
        result = decide(message(), search=FakeSearch(exists=NOT_FOUND, found=SEARCH_EMPTY),
                        wrike=FakeWrike(COMMENTS), db_path=db)
        assert result["authenticated"] is None

    def test_it_is_never_false_on_any_path(self, db, tmp_path):
        """Swept across every case, because False would be a claim as wrong as True."""
        import tempfile

        for s, w, existing in [
            (FakeSearch(found=SEARCH_EMPTY), FakeWrike(COMMENTS), None),
            (FakeSearch(exists=EXISTS, found=SEARCH_EMPTY), FakeWrike(COMMENTS), "PROJ-1"),
            (FakeSearch(exists=NOT_FOUND, found=SEARCH_EMPTY), FakeWrike(COMMENTS), "PROJ-1"),
            (FakeSearch(found=SEARCH_MATCH()), FakeWrike(COMMENTS), None),
            (FakeSearch(raises_on="find_issue_for_task"), FakeWrike(COMMENTS), None),
        ]:
            path = Path(tempfile.mkdtemp()) / "l.duckdb"
            if existing:
                links.remember_link(TASK, existing, db_path=path)
            result = decide(message(), search=s, wrike=w, db_path=path)
            assert result["authenticated"] in (True, None), result


class TestTheUnconfirmableCaseRecommendsNothing:
    """★ A 404 is "deleted" or "bad token", and the two are the same bytes."""

    @pytest.fixture()
    def result(self, db):
        links.remember_link(TASK, "PROJ-1", db_path=db)
        return decide(message(), search=FakeSearch(exists=NOT_FOUND, found=SEARCH_EMPTY),
                      wrike=FakeWrike(COMMENTS), db_path=db)

    def test_it_recommends_nothing(self, result):
        """No key to act on, no comments to copy, no recommendation of any name."""
        assert "issue_key" not in result
        assert "comments_to_copy" not in result
        for banned in ("create", "recommendation", "action", "next_step"):
            assert banned not in result, banned

    def test_it_is_not_reported_as_no_link(self, result):
        """The failure this class exists for: collapsing it into "nothing found" is what would
        open a duplicate issue per task on every pass."""
        assert result["verdict"] != decide_module.NO_LINK_FOUND
        assert result["verdict"] == decide_module.LINK_UNCONFIRMABLE

    def test_it_says_why_it_is_stopping(self, result):
        assert result["detail"] == decide_module.UNCONFIRMABLE_MEANS
        assert "duplicate" in result["detail"]
        assert "404" in result["detail"]

    def test_it_keeps_the_key_it_could_not_confirm(self, result):
        assert result["table_key"] == "PROJ-1"

    def test_a_search_that_proves_the_credentials_resolves_it(self, db):
        """When rows come back, the credentials are good, so the 404 was real — and Jira has an
        issue under the label. That is a different verdict, not this one."""
        links.remember_link(TASK, "PROJ-1", db_path=db)
        result = decide(message(), search=FakeSearch(exists=NOT_FOUND, found=SEARCH_MATCH("PROJ-7")),
                        wrike=FakeWrike(COMMENTS), db_path=db)
        assert result["verdict"] == decide_module.FOUND_BY_LABEL_ONLY
        assert result["stale_table_key"] == "PROJ-1"
        assert result["issue_key"] == "PROJ-7"


class TestTheTableIsConsultedFirst:
    def test_with_no_row_only_the_search_runs(self, db):
        s = FakeSearch(found=SEARCH_EMPTY)
        decide(message(), search=s, wrike=FakeWrike(COMMENTS), db_path=db)
        assert s.calls == [("find_issue_for_task", TASK)]

    def test_with_a_row_the_key_is_confirmed_before_the_label_is_searched(self, db):
        """The order, counted. Asking Jira before the table would spend a network call per
        notification on a search that finds only the issues this flow labelled."""
        links.remember_link(TASK, "PROJ-1", db_path=db)
        s = FakeSearch(exists=EXISTS, found=SEARCH_EMPTY)
        decide(message(), search=s, wrike=FakeWrike(COMMENTS), db_path=db)
        assert s.calls[0] == ("issue_exists", "PROJ-1")

    def test_the_key_confirmed_is_the_one_the_table_holds(self, db):
        links.remember_link(TASK, "PROJ-42", db_path=db)
        s = FakeSearch(exists=EXISTS, found=SEARCH_EMPTY)
        decide(message(), search=s, wrike=FakeWrike(COMMENTS), db_path=db)
        assert ("issue_exists", "PROJ-42") in s.calls

    def test_an_unreadable_answer_is_an_error_not_an_empty_result(self, db):
        """A dependency returning something this module cannot read must not become "nothing
        found" — that is the same collapse, one level down."""
        result = decide(message(), search=FakeSearch(found=None),
                        wrike=FakeWrike(COMMENTS), db_path=db)
        assert result["verdict"] == decide_module.JIRA_ERROR
        assert result["verdict"] != decide_module.NO_LINK_FOUND

    def test_a_non_notification_consults_nothing(self, db):
        s, w = FakeSearch(), FakeWrike(COMMENTS)
        decide(message(sender="noreply@wrike.com"), search=s, wrike=w, db_path=db)
        assert s.calls == [] and w.calls == []


class TestWhichCommentsAreStillToCopy:
    def test_they_are_the_ones_not_already_recorded(self, db):
        links.remember_link(TASK, "PROJ-1", db_path=db)
        links.remember_comment(TASK, "c1", db_path=db)
        result = decide(message(), search=FakeSearch(exists=EXISTS, found=SEARCH_EMPTY),
                        wrike=FakeWrike(COMMENTS), db_path=db)
        assert [c["id"] for c in result["comments_to_copy"]] == ["c2"]

    def test_all_of_them_when_none_is_recorded(self, db):
        links.remember_link(TASK, "PROJ-1", db_path=db)
        result = decide(message(), search=FakeSearch(exists=EXISTS, found=SEARCH_EMPTY),
                        wrike=FakeWrike(COMMENTS), db_path=db)
        assert [c["id"] for c in result["comments_to_copy"]] == ["c1", "c2"]

    def test_an_empty_answer_from_wrike_is_not_nothing_new(self, db):
        """A Collaborator licence hides what it does not allow behind the same empty list."""
        links.remember_link(TASK, "PROJ-1", db_path=db)
        result = decide(message(), search=FakeSearch(exists=EXISTS, found=SEARCH_EMPTY),
                        wrike=FakeWrike(NO_COMMENTS), db_path=db)
        assert result["comments_certain"] is False
        assert result["comments_means"] == decide_module.COMMENTS_NOT_CERTAIN

    def test_a_non_empty_answer_is_certain(self, db):
        links.remember_link(TASK, "PROJ-1", db_path=db)
        result = decide(message(), search=FakeSearch(exists=EXISTS, found=SEARCH_EMPTY),
                        wrike=FakeWrike(COMMENTS), db_path=db)
        assert result["comments_certain"] is True
        assert "comments_means" not in result

    def test_a_wrike_failure_does_not_undo_a_confirmed_issue(self, db):
        """Knowing the issue is already worth something. The verdict stays, the failure is one
        more key rather than a verdict of its own."""
        links.remember_link(TASK, "PROJ-1", db_path=db)
        result = decide(message(), search=FakeSearch(exists=EXISTS, found=SEARCH_EMPTY),
                        wrike=FakeWrike(raises=OSError("network")), db_path=db)
        assert result["verdict"] == decide_module.LINKED_AND_ALIVE
        assert result["issue_key"] == "PROJ-1"
        assert result["comments_error"] == "OSError: network"
        assert result["comments_certain"] is False

    def test_a_refused_wrike_read_is_reported_too(self, db):
        links.remember_link(TASK, "PROJ-1", db_path=db)
        result = decide(message(), search=FakeSearch(exists=EXISTS, found=SEARCH_EMPTY),
                        wrike=FakeWrike({"ok": False, "reason": "not_configured"}), db_path=db)
        assert result["verdict"] == decide_module.LINKED_AND_ALIVE
        assert result["comments_error"] == "not_configured"

    def test_comments_are_not_fetched_when_there_is_no_issue(self, db):
        w = FakeWrike(COMMENTS)
        decide(message(), search=FakeSearch(found=SEARCH_EMPTY), wrike=w, db_path=db)
        assert w.calls == []

    def test_they_are_fetched_for_a_label_only_match(self, db):
        result = decide(message(), search=FakeSearch(found=SEARCH_MATCH()),
                        wrike=FakeWrike(COMMENTS), db_path=db)
        assert [c["id"] for c in result["comments_to_copy"]] == ["c1", "c2"]


class TestItWritesNothing:
    def test_the_register_is_untouched(self, db):
        """Every path, and the file must hold exactly what the test put there."""
        links.remember_link(TASK, "PROJ-1", db_path=db)
        before = links.linked_issue(TASK, db_path=db)["jira_key"]
        decide(message(), search=FakeSearch(exists=EXISTS, found=SEARCH_MATCH("PROJ-9")),
               wrike=FakeWrike(COMMENTS), db_path=db)
        assert links.linked_issue(TASK, db_path=db)["jira_key"] == before
        assert links.copied_comment_ids(TASK, db_path=db) == set()

    def test_a_label_only_match_does_not_repair_the_table(self, db):
        """It reports that the register could be repaired; repairing is somebody else's job."""
        decide(message(), search=FakeSearch(found=SEARCH_MATCH()),
               wrike=FakeWrike(COMMENTS), db_path=db)
        assert links.linked_issue(TASK, db_path=db) is None

    def test_it_calls_no_writing_function(self):
        source = (_SCRIPTS / "decide_wrike_action.py").read_text(encoding="utf-8")
        body = source.split('"""', 2)[2]
        for forbidden in ("remember_link", "remember_comment", "add_comment", "create_"):
            assert forbidden not in body, forbidden

    def test_it_imports_nothing_that_could_reach_out(self):
        import re

        source = (_SCRIPTS / "decide_wrike_action.py").read_text(encoding="utf-8")
        imported = set(re.findall(r"^\s*import\s+([a-z_.0-9]+)", source, re.MULTILINE))
        assert imported == {"importlib.util", "sys"}

    def test_it_is_self_contained(self):
        import re

        source = (_SCRIPTS / "decide_wrike_action.py").read_text(encoding="utf-8")
        assert not re.search(r"^\s*(?:import|from)\s+(mossy|dotenv)\b", source, re.MULTILINE)
