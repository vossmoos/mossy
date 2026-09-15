"""What the two Wrike writers report when something fails. the failure-envelope rule.

Loaded by file path. Nothing here touches the network: where the real client matters — the
status Jira sent, the refusal Wrike sent — ``urlopen`` is replaced for that one test; everywhere
else the clients are doubles. The register is a real DuckDB file under ``tmp_path`` and the
locks live in the conftest's throwaway directory.

Each class answers a failure measured in the failure-envelope rule against the real clients:

* three calls with no envelope around them — the canary in both writers and the read-back after
  a create — so a network drop there came out as an exception instead of an answer. The
  read-back one is the one that matters: **the issue had been created**, and the caller got an
  exception with no ``created`` in it;
* a Wrike 401/403 while creating reported ``no_summary`` — "the task carried no title" — which
  sends whoever reads it to look at Wrike instead of at the credentials;
* a refused ``POST /issue`` lost the HTTP status and Jira's body: "Jira refused the issue."

Written first, and red against the code as it stood.
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

# These tests create issues, and every create evaluates its summary with a model (the Wrike → Jira flow).
pytestmark = pytest.mark.usefixtures("fake_wrike_model")


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


creator_module = _load("create_jira_for_task")
copier = _load("copy_wrike_comments")
links = _load("wrike_jira_links")
wrike_api = _load("wrike_api")
search_module = _load("jira_label_search")

TASK = "MAAAAAEXmain"
KEY = "PROJ-900"


def message():
    return {"id": "gmail-1", "threadId": "t-1", "payload": {"headers": [
        {"name": "From", "value": "Anna <wrike@wrike.com>"},
        {"name": "Subject", "value": "Prepare sample import requests"},
        {"name": "X-Wrike-Metadata", "value": json.dumps(
            {"taskApiId": TASK, "taskId": 1234567890, "notificationType": "Regular",
             "entityType": "Task"})},
    ]}}


UNREACHABLE = search_module.JiraSearchError(
    "Jira is unreachable: [Errno 51] Network is unreachable")


class FakeSearch:
    def __init__(self, *, linked=False, canary_raises=None, read_back_raises=None):
        self.calls: list[str] = []
        self._linked = linked
        self._canary_raises = canary_raises
        self._read_back_raises = read_back_raises

    def find_issue_for_task(self, task):
        self.calls.append("find_issue_for_task")
        return {"ok": True, "matched": False, "authenticated": None, "empty": True}

    def issue_exists(self, key):
        self.calls.append("issue_exists")
        # The first call for a linked task is decide's; a read-back only happens after a create.
        if self._read_back_raises is not None and not self._linked:
            raise self._read_back_raises
        return {"ok": True, "exists": True, "authenticated": True}

    def credentials_proven(self):
        self.calls.append("credentials_proven")
        if self._canary_raises is not None:
            raise self._canary_raises
        return {"ok": True, "proven": True}


class FakeWrike:
    def __init__(self, *, title="Prepare sample import requests"):
        self._title = title

    def get_task(self, task):
        return {"ok": True, "task_id": task, "task": {
            "title": self._title, "description": "<p>x</p>",
            "permalink": "https://www.wrike.com/open.htm?id=1234567890"}}

    def get_task_comments(self, task):
        return {"ok": True, "task_id": task, "count": 1, "comments": [
            {"id": "C1", "createdDate": "2026-01-01T10:00:00Z", "authorId": "K", "text": "x"}]}


class FakeCreator:
    def __init__(self):
        self.calls = 0

    def create_issue(self, summary, description, labels):
        self.calls += 1
        return {"issue_key": KEY, "issue_url": f"https://x/browse/{KEY}", "status": 201}


class FakeWriter:
    def __init__(self):
        self.calls: list[str] = []

    def existing_comments(self, key):
        self.calls.append("existing_comments")
        return {"ok": True, "issue_key": key, "count": 0, "anchors": []}

    def add_comment(self, key, text):
        self.calls.append("add_comment")
        return {"comment_id": "1", "issue_key": key, "status": 201}


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "wrike-jira-links.duckdb"


@pytest.fixture()
def linked(db):
    links.remember_link(TASK, KEY, f"https://x/browse/{KEY}", db_path=db)
    return db


def create(db, **kw):
    kw.setdefault("search", FakeSearch())
    kw.setdefault("wrike", FakeWrike())
    kw.setdefault("creator", FakeCreator())
    return creator_module.run(message(), apply=True, db_path=db, **kw)


def _http_error(url, code, body):
    return urllib.error.HTTPError(url, code, "refused", {}, io.BytesIO(json.dumps(body).encode()))


# ---------------------------------------------------------------------- the three bare calls
class TestTheCallsThatUsedToEscapeNowAnswer:
    """A failure is an answer. Before the failure-envelope rule these three came out as exceptions: measured with
    the network dropped at each of them, against the real clients."""

    def test_the_canary_failing_while_creating_is_an_envelope(self, db):
        writer = FakeCreator()
        result = create(db, search=FakeSearch(canary_raises=UNREACHABLE), creator=writer)
        assert result["reason"] == "credentials_check_failed"
        assert "unreachable" in result["error"]
        assert writer.calls == 0

    def test_a_failed_canary_is_not_a_proven_one(self, db):
        """Unknown is not proven: the refusal to create stands when the check itself fails."""
        result = create(db, search=FakeSearch(canary_raises=RuntimeError("boom")))
        assert result["applied"] is False
        assert "created" not in result

    def test_the_canary_failing_while_copying_is_an_envelope(self, linked):
        writer = FakeWriter()
        result = copier.run(message(), apply=True, db_path=linked, writer=writer,
                            search=FakeSearch(linked=True, canary_raises=UNREACHABLE),
                            wrike=FakeWrike())
        assert result["reason"] == "credentials_check_failed"
        assert "unreachable" in result["error"]
        assert writer.calls == []

    def test_the_read_back_failing_after_a_create_is_an_envelope(self, db):
        result = create(db, search=FakeSearch(read_back_raises=UNREACHABLE))
        assert result["reason"] == "read_back_failed"
        assert "unreachable" in result["error"]


class TestTheWriteThatHappenedIsReported:
    """The read-back that fails after a create is the one that matters: the issue EXISTS. The
    caller must be told so, with its key — not handed an exception that loses the one write this
    pass made, in a Jira where nothing can be deleted."""

    def test_the_envelope_carries_created_and_the_key(self, db):
        result = create(db, search=FakeSearch(read_back_raises=UNREACHABLE))
        assert result["created"]["issue_key"] == KEY
        assert result["created"]["issue_url"] == f"https://x/browse/{KEY}"

    def test_it_says_in_words_that_the_issue_was_created(self, db):
        result = create(db, search=FakeSearch(read_back_raises=UNREACHABLE))
        assert "was created" in result["detail"]
        assert KEY in result["detail"]

    def test_no_row_is_written_for_an_unconfirmed_issue(self, db):
        """Same rule as not_confirmed_on_read_back: the label inside the issue is what finds it
        again, so the register waits for a confirmation."""
        create(db, search=FakeSearch(read_back_raises=UNREACHABLE))
        assert links.linked_issue(TASK, db_path=db) is None

    def test_the_next_pass_is_pointed_at_the_label_not_at_creating_again(self, db):
        result = create(db, search=FakeSearch(read_back_raises=UNREACHABLE))
        assert "label" in result["detail"]


# ---------------------------------------------------------------------- the text that lied
class TestAWrikeRefusalIsNotAMissingTitle:
    """``no_summary`` says "the Wrike task carried no title". For a 401 that is false, and it
    sends whoever reads it to Wrike instead of to the credentials. The code is fixed, not the
    text: the text stays true for the case it describes."""

    @pytest.fixture()
    def wrike_refuses(self, monkeypatch):
        def install(code=None, down=False):
            def fake(request, timeout=None):
                if down:
                    raise urllib.error.URLError("[Errno 51] Network is unreachable")
                raise _http_error(request.full_url, code, {"error": "not_authorized"})

            monkeypatch.setattr(wrike_api.urllib.request, "urlopen", fake)
            return wrike_api.WrikeClient("tok")
        return install

    @pytest.mark.parametrize("code", [401, 403])
    def test_a_refused_read_does_not_say_no_summary(self, db, wrike_refuses, code):
        result = create(db, wrike=wrike_refuses(code))
        assert result["reason"] != "no_summary"
        assert result["reason"] == "wrike_unreadable"

    @pytest.mark.parametrize("code", [401, 403])
    def test_the_status_wrike_sent_is_in_the_envelope(self, db, wrike_refuses, code):
        result = create(db, wrike=wrike_refuses(code))
        assert str(code) in result["error"]

    def test_an_unreachable_wrike_is_not_a_missing_title_either(self, db, wrike_refuses):
        result = create(db, wrike=wrike_refuses(down=True))
        assert result["reason"] == "wrike_unreadable"
        assert "unreachable" in result["error"]

    def test_it_points_at_the_credentials(self, db, wrike_refuses):
        result = create(db, wrike=wrike_refuses(401))
        assert "carried no title" not in result["detail"]
        assert "token" in result["detail"].lower()

    def test_nothing_is_created_on_a_refused_read(self, db, wrike_refuses):
        writer = FakeCreator()
        create(db, wrike=wrike_refuses(401), creator=writer)
        assert writer.calls == 0

    def test_a_task_that_really_has_no_title_still_says_no_summary(self, db):
        """The text is true here, so it stays."""
        result = create(db, wrike=FakeWrike(title=""))
        assert result["reason"] == "no_summary"


# ---------------------------------------------------------------------- the lost status
class TestARefusedCreateKeepsWhatJiraSaid:
    """``JiraWriteError`` now carries its status in its message and its body in the envelope,
    the way ``JiraSearchError``, ``JiraCommentError`` and ``WrikeError`` already did."""

    @pytest.fixture()
    def jira_refuses(self, monkeypatch):
        body = {"errorMessages": ["You do not have permission to create issues in this project."]}

        def fake(request, timeout=None):
            raise _http_error(request.full_url, 401, body)

        monkeypatch.setattr(creator_module.urllib.request, "urlopen", fake)
        return body

    @pytest.fixture()
    def real_creator(self):
        return creator_module.JiraIssueCreator("x.atlassian.net", "a@b.test", "tok", "PROJ")

    def test_the_error_string_names_the_status(self):
        err = creator_module.JiraWriteError("Jira refused the issue.", status=401, body={})
        assert str(err) == "Jira refused the issue. — HTTP 401"

    def test_without_a_status_the_message_is_unchanged(self):
        assert str(creator_module.JiraWriteError("Jira is unreachable")) == "Jira is unreachable"

    def test_a_401_on_post_issue_keeps_its_status(self, db, jira_refuses, real_creator):
        result = create(db, creator=real_creator)
        assert result["reason"] == "create_failed"
        assert result["status"] == 401
        assert "HTTP 401" in result["error"]

    def test_a_401_on_post_issue_keeps_jiras_body(self, db, jira_refuses, real_creator):
        result = create(db, creator=real_creator)
        assert result["body"] == jira_refuses

    def test_a_create_that_never_reached_jira_has_no_status_to_invent(self, db, monkeypatch,
                                                                       real_creator):
        def down(request, timeout=None):
            raise urllib.error.URLError("[Errno 51] Network is unreachable")

        monkeypatch.setattr(creator_module.urllib.request, "urlopen", down)
        result = create(db, creator=real_creator)
        assert result["reason"] == "create_failed"
        assert result["status"] is None


# ---------------------------------------------------------------------- logging, as a decision
class TestNoLoggingIsWrittenDownAsADecision:
    """Nothing here logs, on purpose, and the reason is in the module so nobody reads the
    absence as something forgotten."""

    def test_the_envelope_is_the_record(self):
        assert "the envelope IS the record" in creator_module.__doc__

    def test_the_measurement_about_mossys_logging_is_there(self):
        """The exact claims, not loose words: "measured" alone appears all over this docstring,
        so it would stay green with the measurement deleted."""
        doc = " ".join(creator_module.__doc__.split())
        assert "Mossy configures no handlers" in doc
        assert "an INFO is dropped" in doc
        assert "measured by running it, not read from the documentation" in doc

    def test_the_operator_alert_is_placed_where_it_belongs(self):
        doc = " ".join(creator_module.__doc__.split())
        assert "the process that reads Gmail" in doc
        assert "No such process ships in this repository" in doc
        assert "does not exist yet" not in doc

    def test_the_other_writer_points_at_the_same_decision(self):
        assert "create_jira_for_task" in copier.__doc__
        assert "logs nothing" in copier.__doc__

    def test_neither_writer_imports_logging(self):
        for module in (creator_module, copier):
            source = Path(module.__file__).read_text(encoding="utf-8")
            assert "import logging" not in source
