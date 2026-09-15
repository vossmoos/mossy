"""Creating the Jira issue for a Wrike task — the first thing in this flow that writes.

the create step. Loaded by file path. **Nothing here touches the network**: the Jira reader, the Jira
writer and the Wrike client are injected, and the writer double counts every call so "wrote
nothing" is an assertion rather than a hope.

``TestWithoutApplyNothingIsWritten`` is the gate, and it is the class that must never be relaxed.
The default is to describe and stop. That is the rule a customer-facing reply gate follows,
and it is here for the same reason — plus one that reply gate did not have: **this Jira
account cannot delete.** A surplus issue is renamed, never removed, so a write that goes out
because nobody thought about it stays in the project for good.

``TestOnlyNoLinkFoundCreates`` holds the other half: seven verdicts stop, one repairs a local
row, one does nothing, and exactly one creates — and even that one needs the credentials proven
first, because creating on a bad token would put a permanent duplicate in the project on every
pass.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

# Every issue this file creates has its summary evaluated by a model (the Wrike → Jira flow), faked here.
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
links = _load("wrike_jira_links")
decide_module = _load("decide_wrike_action")
run = creator_module.run

TASK = "MAAAAAEXmain"
LABEL = "wrike-MAAAAAEXmain"
METADATA = {"taskApiId": TASK, "taskId": 1234567890, "notificationType": "Regular",
            "entityType": "Task"}


def message(*, sender="wrike@wrike.com"):
    return {"id": "gmail-1", "threadId": "thread-1", "payload": {"headers": [
        {"name": "From", "value": f"Anna <{sender}>"},
        {"name": "Subject", "value": "Prepare sample import requests"},
        {"name": "X-Wrike-Metadata", "value": json.dumps(METADATA)},
    ]}}


class FakeSearch:
    def __init__(self, *, found=None, exists=None, proven=None):
        self.calls: list[tuple] = []
        self._found = found or {"ok": True, "matched": False, "authenticated": None,
                                "empty": True}
        self._exists = exists or {"ok": True, "exists": True, "authenticated": True}
        self._proven = proven if proven is not None else {"ok": True, "proven": True}

    def find_issue_for_task(self, task):
        self.calls.append(("find_issue_for_task", task))
        return self._found

    def issue_exists(self, key):
        self.calls.append(("issue_exists", key))
        return self._exists

    def credentials_proven(self):
        self.calls.append(("credentials_proven",))
        return self._proven


class FakeWrike:
    def __init__(self, title="Prepare sample import requests",
                 description="<p>Background</p><p>Goal</p>", ok=True):
        self._title = title
        self._description = description
        self._ok = ok

    def get_task(self, task):
        if not self._ok:
            return {"ok": False, "reason": "not_visible"}
        return {"ok": True, "task": {"id": task, "title": self._title,
                                     "description": self._description}}

    def get_task_comments(self, task):
        return {"ok": True, "comments": [{"id": "c1"}], "empty": False, "count": 1}


class FakeCreator:
    """Counts every write. An empty ``calls`` list is the strongest assertion in this file."""

    def __init__(self, raises=None):
        self.calls: list[dict] = []
        self.raises = raises

    def create_issue(self, summary, description, labels, issue_type="Task"):
        self.calls.append({"summary": summary, "description": description,
                           "labels": list(labels), "issue_type": issue_type})
        if self.raises is not None:
            raise self.raises
        return {"issue_key": "PROJ-99", "issue_url": "https://x/browse/PROJ-99", "status": 201}


MATCHED = {"ok": True, "matched": True, "authenticated": True, "issue_key": "PROJ-2",
           "issue_url": "https://x/browse/PROJ-2", "label_confirmed": True}
NOT_FOUND = {"ok": True, "exists": None, "authenticated": None}
UNPROVEN = {"ok": True, "proven": None, "reason": "empty_project_or_unauthenticated"}


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "links.duckdb"


@pytest.fixture(autouse=True)
def _never_the_real_register(tmp_path, monkeypatch):
    monkeypatch.setenv("WRIKE_JIRA_LINKS_DB", str(tmp_path / "fallback.duckdb"))


class TestWithoutApplyNothingIsWritten:
    """★ The gate. Default is describe-and-stop, and nothing may relax it."""

    def test_no_issue_is_created(self, db):
        writer = FakeCreator()
        result = run(message(), search=FakeSearch(), wrike=FakeWrike(), creator=writer,
                     db_path=db)
        assert writer.calls == []
        assert result["applied"] is False

    def test_no_local_row_is_written(self, db):
        run(message(), search=FakeSearch(), wrike=FakeWrike(), creator=FakeCreator(),
            db_path=db)
        assert links.linked_issue(TASK, db_path=db) is None

    def test_it_still_describes_what_it_would_create(self, db):
        result = run(message(), search=FakeSearch(), wrike=FakeWrike(), creator=FakeCreator(),
                     db_path=db)
        assert result["action"] == creator_module.ACTION_CREATE
        assert result["would_create"]["summary"] == ("Model summary of: Prepare sample import "
                                                     "requests")
        assert result["would_create"]["wrike_title"] == "Prepare sample import requests"
        assert result["would_create"]["labels"] == [LABEL]

    def test_it_says_how_to_write(self, db):
        result = run(message(), search=FakeSearch(), wrike=FakeWrike(), creator=FakeCreator(),
                     db_path=db)
        assert "--apply" in result["detail"]

    def test_the_repair_path_writes_nothing_either(self, db):
        run(message(), search=FakeSearch(found=MATCHED), wrike=FakeWrike(),
            creator=FakeCreator(), db_path=db)
        assert links.linked_issue(TASK, db_path=db) is None

    def test_apply_defaults_to_false(self):
        import inspect

        assert inspect.signature(run).parameters["apply"].default is False

    def test_plan_never_writes_even_when_asked_to_apply(self, db):
        """``plan`` has no apply at all: describing is a separate function from doing."""
        import inspect

        assert "apply" not in inspect.signature(creator_module.plan).parameters


class TestOnlyNoLinkFoundCreates:
    def _run(self, db, search, *, existing=None, apply=True):
        writer = FakeCreator()
        if existing:
            links.remember_link(TASK, existing, db_path=db)
        result = run(message(), apply=apply, search=search, wrike=FakeWrike(),
                     creator=writer, db_path=db)
        return result, writer

    def test_no_link_found_creates(self, db):
        result, writer = self._run(db, FakeSearch())
        assert result["verdict"] == decide_module.NO_LINK_FOUND
        assert len(writer.calls) == 1
        assert result["applied"] is True

    def test_not_a_notification_creates_nothing(self, db):
        writer = FakeCreator()
        result = run(message(sender="noreply@wrike.com"), apply=True, search=FakeSearch(),
                     wrike=FakeWrike(), creator=writer, db_path=db)
        assert result["action"] == creator_module.ACTION_NOTHING
        assert writer.calls == []

    def test_linked_and_alive_creates_nothing(self, db):
        result, writer = self._run(db, FakeSearch(), existing="PROJ-1")
        assert result["verdict"] == decide_module.LINKED_AND_ALIVE
        assert result["action"] == creator_module.ACTION_NOTHING
        assert writer.calls == []

    @pytest.mark.parametrize("name,search,existing", [
        ("link_unconfirmable", FakeSearch(exists=NOT_FOUND), "PROJ-1"),
        ("link_disagrees", FakeSearch(found={**MATCHED, "issue_key": "PROJ-9"}), "PROJ-1"),
        ("label_on_wrong_issue",
         FakeSearch(found={"ok": True, "matched": True, "label_confirmed": False}), None),
        ("ambiguous_match",
         FakeSearch(found={**MATCHED, "anomalies": ["more_than_one_issue"]}), None),
    ])
    def test_the_stopping_verdicts_stop(self, db, name, search, existing):
        result, writer = self._run(db, search, existing=existing)
        assert result["action"] == creator_module.ACTION_STOP, name
        assert writer.calls == [], name

    def test_not_configured_stops(self, db):
        writer = FakeCreator()
        result = run(message(), apply=True, search=None, wrike=FakeWrike(), creator=writer,
                     db_path=db)
        assert result["action"] == creator_module.ACTION_STOP
        assert writer.calls == []

    def test_a_jira_error_stops(self, db):
        class Broken(FakeSearch):
            def find_issue_for_task(self, task):
                raise RuntimeError("unreachable")

        result, writer = self._run(db, Broken())
        assert result["action"] == creator_module.ACTION_STOP
        assert writer.calls == []


class TestTheCanaryGatesCreation:
    """Creating on unproven credentials would be permanent litter, not a wasted call."""

    def test_it_is_asked_before_the_create(self, db):
        search = FakeSearch()
        run(message(), apply=True, search=search, wrike=FakeWrike(), creator=FakeCreator(),
            db_path=db)
        names = [c[0] for c in search.calls]
        assert names.index("credentials_proven") < names.index("issue_exists")

    def test_unproven_credentials_create_nothing(self, db):
        writer = FakeCreator()
        result = run(message(), apply=True, search=FakeSearch(proven=UNPROVEN),
                     wrike=FakeWrike(), creator=writer, db_path=db)
        assert writer.calls == []
        assert result["reason"] == "authentication_not_proven"
        assert links.linked_issue(TASK, db_path=db) is None

    def test_the_refusal_says_nothing_can_be_deleted(self, db):
        result = run(message(), apply=True, search=FakeSearch(proven=UNPROVEN),
                     wrike=FakeWrike(), creator=FakeCreator(), db_path=db)
        assert creator_module.NOTHING_CAN_BE_DELETED in result["detail"]

    def test_the_surviving_uncertainty_is_declared(self, db):
        """The canary settles two of the three meanings of an empty page, not the third."""
        result = run(message(), search=FakeSearch(), wrike=FakeWrike(), creator=FakeCreator(),
                     db_path=db)
        assert result["uncertainty"] == creator_module.PRE_LABEL_ISSUES_ARE_INVISIBLE
        assert "cannot be deleted" in result["uncertainty"]


class TestTheLabelGoesInsideTheCreate:
    """Measured: POST /issue accepts labels, 201, and the search finds the issue by it."""

    def test_one_write_call_carries_the_label(self, db):
        writer = FakeCreator()
        run(message(), apply=True, search=FakeSearch(), wrike=FakeWrike(), creator=writer,
            db_path=db)
        assert len(writer.calls) == 1
        assert writer.calls[0]["labels"] == [LABEL]

    def test_there_is_no_second_write_for_the_label(self, db):
        """The mutation this exists for: moving the label out to its own call. A crash between
        the two would leave an issue neither the table nor the search can find — for ever, and
        undeletable."""
        writer = FakeCreator()
        run(message(), apply=True, search=FakeSearch(), wrike=FakeWrike(), creator=writer,
            db_path=db)
        assert len(writer.calls) == 1
        assert all(c["labels"] for c in writer.calls)

    def test_the_creator_class_has_one_write_method(self):
        public = {n for n in dir(creator_module.JiraIssueCreator) if not n.startswith("_")}
        assert public == {"from_env", "create_issue"}

    def test_the_label_is_the_one_the_search_looks_for(self):
        search_mod = _load("jira_label_search")
        assert search_mod.task_label(TASK) == LABEL


class TestTheRealWriteClient:
    """``JiraIssueCreator`` itself, with no double in the way.

    Written because a mutation survived: moving ``labels`` out of the POST body left all
    forty-three tests green. Every one of them injected a fake creator and asserted the labels
    reached *it* — nothing checked that the real class puts them in the request. That is the rule that a double never replaces the real client
    exactly: the tests proved the scaffolding.
    """

    @pytest.fixture()
    def transport(self, monkeypatch):
        sent = {}

        class Response:
            def __init__(self, payload, status=201):
                self._b = json.dumps(payload).encode()
                self._s = status

            def getcode(self):
                return self._s

            def read(self):
                return self._b

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake(request, timeout=None):
            sent["url"] = request.full_url
            sent["method"] = request.get_method()
            sent["body"] = json.loads(request.data.decode())
            sent["headers"] = dict(request.headers)
            return Response({"key": "PROJ-99", "id": "1"})

        monkeypatch.setattr(creator_module.urllib.request, "urlopen", fake)
        return sent

    @pytest.fixture()
    def client(self):
        return creator_module.JiraIssueCreator("example.atlassian.net", "a@b.test", "tok", "PROJ")

    def test_the_labels_are_in_the_post_body(self, client, transport):
        """The measurement this rests on: POST /issue accepts labels, 201, on a test issue."""
        client.create_issue("A title", "Some text", [LABEL])
        assert transport["body"]["fields"]["labels"] == [LABEL]

    def test_it_is_one_post_to_the_issue_endpoint(self, client, transport):
        client.create_issue("A title", "Some text", [LABEL])
        assert transport["method"] == "POST"
        assert transport["url"].endswith("/rest/api/3/issue")

    def test_the_project_and_summary_travel(self, client, transport):
        client.create_issue("A title", "Some text", [LABEL])
        fields = transport["body"]["fields"]
        assert fields["project"] == {"key": "PROJ"}
        assert fields["summary"] == "A title"

    def test_the_description_is_adf(self, client, transport):
        client.create_issue("A title", "One\n\nTwo", [LABEL])
        doc = transport["body"]["fields"]["description"]
        assert doc["type"] == "doc" and doc["version"] == 1
        assert [c["content"][0]["text"] for c in doc["content"]] == ["One", "Two"]

    def test_an_empty_description_is_left_out(self, client, transport):
        client.create_issue("A title", "", [LABEL])
        assert "description" not in transport["body"]["fields"]

    def test_it_returns_the_key_and_a_browse_url(self, client, transport):
        created = client.create_issue("A title", "x", [LABEL])
        assert created["issue_key"] == "PROJ-99"
        assert created["issue_url"] == "https://example.atlassian.net/browse/PROJ-99"

    def test_a_refusal_raises_rather_than_looking_created(self, client, monkeypatch):
        import urllib.error

        def refused(request, timeout=None):
            raise urllib.error.HTTPError("u", 400, "bad", {}, None)

        monkeypatch.setattr(creator_module.urllib.request, "urlopen", refused)
        with pytest.raises(creator_module.JiraWriteError):
            client.create_issue("A title", "x", [LABEL])

    def test_a_200_with_no_key_is_not_a_creation(self, client, monkeypatch):
        class Response:
            def getcode(self):
                return 201

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(creator_module.urllib.request, "urlopen",
                            lambda request, timeout=None: Response())
        with pytest.raises(creator_module.JiraWriteError):
            client.create_issue("A title", "x", [LABEL])

    def test_from_env_needs_all_four(self, monkeypatch):
        for name in ("JIRA_SITE", "JIRA_USER_EMAIL", "JIRA_API_KEY", "JIRA_SPACE_NAME"):
            monkeypatch.delenv(name, raising=False)
        assert creator_module.JiraIssueCreator.from_env() is None

    def test_the_orchestrator_hands_the_label_to_the_real_signature(self):
        """The contract between the two, held against the real class — no double involved."""
        import inspect

        params = list(inspect.signature(
            creator_module.JiraIssueCreator.create_issue).parameters)
        assert params[:4] == ["self", "summary", "description", "labels"]


class TestTheOrderAndWhatACrashLeaves:
    def test_a_failed_create_writes_no_row(self, db):
        writer = FakeCreator(raises=RuntimeError("Jira refused"))
        result = run(message(), apply=True, search=FakeSearch(), wrike=FakeWrike(),
                     creator=writer, db_path=db)
        assert result["reason"] == "create_failed"
        assert links.linked_issue(TASK, db_path=db) is None

    def test_a_failed_read_back_writes_no_row(self, db):
        """And says why that is survivable: the label is inside the issue."""
        result = run(message(), apply=True, search=FakeSearch(exists=NOT_FOUND),
                     wrike=FakeWrike(), creator=FakeCreator(), db_path=db)
        assert result["reason"] == "not_confirmed_on_read_back"
        assert links.linked_issue(TASK, db_path=db) is None
        assert "next pass finds it by search" in result["detail"]

    def test_the_read_back_happens_before_the_row(self, db):
        search = FakeSearch()
        run(message(), apply=True, search=search, wrike=FakeWrike(), creator=FakeCreator(),
            db_path=db)
        assert ("issue_exists", "PROJ-99") in search.calls
        assert links.linked_issue(TASK, db_path=db)["jira_key"] == "PROJ-99"

    def test_a_confirmed_create_writes_the_row_with_the_url(self, db):
        run(message(), apply=True, search=FakeSearch(), wrike=FakeWrike(),
            creator=FakeCreator(), db_path=db)
        row = links.linked_issue(TASK, db_path=db)
        assert row["jira_key"] == "PROJ-99"
        assert row["jira_url"] == "https://x/browse/PROJ-99"


class TestTheSummarisedCommentsAreRecordedAsCopied:
    """the Wrike → Jira flow. The comments a create summarised are in its description already. Recorded as
    copied, or the next notification posts every one of them as a loose comment — measured on
    14 September 2026 before this was so: 0 of 29 recorded after the create, 29 posted next."""

    def test_a_confirmed_create_records_every_comment_it_summarised(self, db):
        result = run(message(), apply=True, search=FakeSearch(), wrike=FakeWrike(),
                     creator=FakeCreator(), db_path=db)
        assert links.copied_comment_ids(TASK, db_path=db) == {"c1"}
        assert result["summarised_comments"] == {"count": 1, "recorded": 1, "unrecorded": []}
        assert result.get("reason") is None

    def test_without_apply_nothing_is_recorded(self, db):
        run(message(), search=FakeSearch(), wrike=FakeWrike(), creator=FakeCreator(), db_path=db)
        assert links.copied_comment_ids(TASK, db_path=db) == set()

    def test_an_unconfirmed_create_records_nothing(self, db):
        run(message(), apply=True, search=FakeSearch(exists=NOT_FOUND), wrike=FakeWrike(),
            creator=FakeCreator(), db_path=db)
        assert links.copied_comment_ids(TASK, db_path=db) == set()

    def test_a_row_that_fails_is_reported_and_the_create_is_kept(self, db, monkeypatch):
        """Unlike the link, nothing in Jira rescues these rows: a description carries no anchors."""
        monkeypatch.setattr(links, "remember_comment",
                            lambda *a, **k: {"ok": False, "reason": "database_error"})
        result = run(message(), apply=True, search=FakeSearch(), wrike=FakeWrike(),
                     creator=FakeCreator(), db_path=db)
        assert result["reason"] == "summarised_comments_not_recorded"
        assert result["applied"] is True
        assert result["created"]["issue_key"] == "PROJ-99"
        assert result["summarised_comments"]["unrecorded"] == ["c1"]


class TestTheRepairPath:
    def test_found_by_label_only_repairs_and_does_not_create(self, db):
        writer = FakeCreator()
        result = run(message(), apply=True, search=FakeSearch(found=MATCHED),
                     wrike=FakeWrike(), creator=writer, db_path=db)
        assert result["action"] == creator_module.ACTION_REPAIR_LINK
        assert writer.calls == []
        assert links.linked_issue(TASK, db_path=db)["jira_key"] == "PROJ-2"

    def test_it_is_the_key_jira_returned(self, db):
        run(message(), apply=True, search=FakeSearch(found={**MATCHED, "issue_key": "PROJ-77",
                                                            "issue_url": "u/77"}),
            wrike=FakeWrike(), creator=FakeCreator(), db_path=db)
        assert links.linked_issue(TASK, db_path=db)["jira_key"] == "PROJ-77"


class TestWhatTheIssueCarries:
    def test_the_summary_is_the_models_not_the_wrike_title(self, db):
        """Changed with the Wrike → Jira flow's summary: this asserted the Wrike title used as-is, which was a
        decision against the ticket's own words: "with some summary evaluated"."""
        writer = FakeCreator()
        run(message(), apply=True, search=FakeSearch(), wrike=FakeWrike(title="Do the thing"),
            creator=writer, db_path=db)
        assert writer.calls[0]["summary"] == "Model summary of: Do the thing"

    def test_the_description_carries_no_html(self, db):
        writer = FakeCreator()
        run(message(), apply=True, search=FakeSearch(), wrike=FakeWrike(), creator=writer,
            db_path=db)
        assert "<p>" not in writer.calls[0]["description"]

    def test_it_carries_a_link_back_to_the_wrike_task(self, db):
        writer = FakeCreator()
        run(message(), apply=True, search=FakeSearch(), wrike=FakeWrike(), creator=writer,
            db_path=db)
        assert "open.htm?id=1234567890" in writer.calls[0]["description"]

    def test_the_original_description_reaches_the_model_and_never_jira(self, db, fake_wrike_model):
        """14 September 2026. The original carried the end customer's personal data — name,
        street, mobile, e-mail — out of the client's Wrike into an issue on another organisation's
        Jira that cannot be deleted. The description is the model's problem and the link; the
        detail stays in the task, where the link leads."""
        writer = FakeCreator()
        run(message(), apply=True, search=FakeSearch(),
            wrike=FakeWrike(description="<p>Background</p><p>Goal</p>"), creator=writer,
            db_path=db)
        description = writer.calls[0]["description"]
        assert "Background" not in description and "Goal" not in description
        assert description == ("Model problem for: Prepare sample import requests\n\n"
                               "Wrike: https://www.wrike.com/open.htm?id=1234567890")
        assert "Background" in fake_wrike_model.calls[-1][1], "the model still reads it"

    def test_an_issue_with_no_link_back_is_not_created(self, db):
        """The link stands in for the original. Without it the issue would hold only what the
        model wrote, with no way back to the source."""
        without_permalink = message()
        metadata = {k: v for k, v in METADATA.items() if k != "taskId"}
        without_permalink["payload"]["headers"][2]["value"] = json.dumps(metadata)
        writer = FakeCreator()
        result = run(without_permalink, apply=True, search=FakeSearch(), wrike=FakeWrike(),
                     creator=writer, db_path=db)
        assert result["reason"] == "no_wrike_link"
        assert writer.calls == []
        assert links.linked_issue(TASK, db_path=db) is None

    def test_why_the_original_is_not_copied_is_written_in_the_code(self):
        doc = " ".join(creator_module.__doc__.split())
        assert "personal data" in doc and "another organisation's Jira" in doc
        assert "no_wrike_link" in doc

    def test_the_summary_is_evaluated_and_freshdesks_analyser_is_not_used(self):
        """Replaces test_no_model_is_called, which pinned the decision the Wrike → Jira flow revoked. The
        summary now comes from this skill's own analyze_wrike_task; Freshdesk's
        analyze_context.py stays untouched and unimported: skills do not import each other,
        and its prompt is about a different kind of work."""
        source = (_SCRIPTS / "create_jira_for_task.py").read_text(encoding="utf-8")
        body = source.split('"""', 2)[2]
        assert "analyze_wrike_task" in body
        assert "analyze_context" not in body
        assert "No model:" not in creator_module.__doc__

    def test_a_task_with_no_title_is_refused_rather_than_named_something(self, db):
        """An untitled issue would be permanent litter somebody has to find by eye."""
        writer = FakeCreator()
        result = run(message(), apply=True, search=FakeSearch(), wrike=FakeWrike(title="   "),
                     creator=writer, db_path=db)
        assert result["reason"] == "no_summary"
        assert writer.calls == []
        assert links.linked_issue(TASK, db_path=db) is None

    def test_a_wrike_that_cannot_be_read_does_not_become_a_placeholder(self, db):
        """the failure-envelope rule changed the reason this asserts. It used to be ``no_summary`` — "the Wrike
        task carried no title" — which is false for a read Wrike refused, and sends whoever reads
        it to Wrike instead of to the credentials. The intent is unchanged: nothing is created
        under a placeholder title. See ``tests/test_wrike_flow_errors.py``."""
        writer = FakeCreator()
        result = run(message(), apply=True, search=FakeSearch(), wrike=FakeWrike(ok=False),
                     creator=writer, db_path=db)
        assert result["reason"] == "wrike_unreadable"
        assert writer.calls == []


class TestTheDescriptionIsReadable:
    """HTML to text, measured against the six real Wrike descriptions — not invented markup.

    The defect this replaces stripped every tag and left the pieces glued together:

        "LotoMito nupe nuto tora lora peto rami ratoSito kara toto vuto…"

    Nine checklist items rendered as one unreadable sentence, in an issue that cannot be
    deleted. Wrike's own ``briefDescription`` field has exactly the same defect, which is a good
    reason not to have copied it.

    The fixture is the API's own output for the six tasks in this project. Between them they
    carry headings, ticked and unticked checkboxes, ordered lists, links whose text is sometimes
    the URL and sometimes a title, entities (``&#34;`` ``&#61;`` ``&#64;`` ``&amp;``), inline
    ``<span>`` styling, HTML comments used as separators, an empty ``<h2></h2>`` and an empty
    checklist item.
    """

    FIXTURE = Path(__file__).resolve().parent / "fixtures" / "wrike_task_descriptions.json"
    MAIN = "MAAAAAEXmain"
    GERMAN = "MAAAAAEXgerm"
    PLAIN = "MAAAAAEXtext"
    EMPTY_ITEM = "MAAAAAEXlist"

    @pytest.fixture()
    def corpus(self):
        return json.loads(self.FIXTURE.read_text(encoding="utf-8"))

    def rendered(self, corpus, key):
        return creator_module._plain(corpus[key]["description"])

    def test_the_fixture_is_the_six_real_tasks(self, corpus):
        assert len(corpus) == 6
        assert all(v["description"] for v in corpus.values())

    def test_no_heading_runs_into_what_follows(self, corpus):
        """The exact string the defect produced, on the exact task that produced it."""
        out = self.rendered(corpus, self.MAIN)
        assert "LotoMito nupe nuto" not in out
        assert "ToloVulo zelo" not in out
        assert "VuraZera kasi" not in out

    def test_every_heading_is_on_its_own_line(self, corpus):
        lines = self.rendered(corpus, self.MAIN).splitlines()
        for heading in ("Tolo", "Vura", "Loto", "Peze nuto", "Mikalo"):
            assert heading in lines, heading

    def test_the_checklist_is_one_item_per_line(self, corpus):
        lines = [l for l in self.rendered(corpus, self.MAIN).splitlines()
                 if l.startswith("- [")]
        assert len(lines) == 9

    def test_a_list_item_never_runs_into_the_next(self, corpus):
        out = self.rendered(corpus, self.MAIN)
        assert "ratoSito" not in out
        assert "petoZeto kavu" not in out

    def test_ordered_list_items_keep_their_numbers(self, corpus):
        """"Peze nuto" is an ``<ol>`` of three steps. Rendering it with dashes threw
        away the one thing an ordered list carries that a bulleted one does not."""
        lines = self.rendered(corpus, self.MAIN).splitlines()
        assert "1. Vulo raze kara SIZE ralo vulo toze vuze tora zeze ralo nulo" in lines
        assert "2. Vulo kaze kara sipe lovu kara VUVU MIRA kakalo" in lines
        assert "3. Vulo lokalo lora lovu VUVU Kato" in lines

    def test_ordered_list_items_are_separate_lines_too(self, corpus):
        assert "nuloVulo kaze" not in self.rendered(corpus, self.MAIN)

    def test_the_numbering_restarts_in_each_ordered_list(self, corpus):
        """The second task also has an ``<ol>``; its first item must be 1, not 4."""
        lines = self.rendered(corpus, self.EMPTY_ITEM).splitlines()
        numbered = [l for l in lines if l[:3] in ("1. ", "2. ", "3. ")]
        assert numbered[0].startswith("1. "), numbered

    def test_unordered_items_are_not_numbered(self, corpus):
        """The checklist stays dashes. If everything were numbered, the distinction the
        ordered list makes would be gone again, the other way round."""
        out = self.rendered(corpus, self.MAIN)
        assert out.count("- [x] ") == 6
        assert "1. [x]" not in out

    def test_the_german_task_reads_as_a_checklist(self, corpus):
        lines = [l for l in self.rendered(corpus, self.GERMAN).splitlines()
                 if l.startswith("- [")]
        assert len(lines) == 6
        assert lines[0] == "- [ ] Miralo nuralo peralo Raralo siralü"

    def test_a_description_with_no_markup_survives_unharmed(self, corpus):
        out = self.rendered(corpus, self.PLAIN)
        assert out.startswith("Nusimi pesimi rasimü lonulo")
        assert "<" not in out and ">" not in out


class TestTheCheckboxStateIsNotLost:
    """★ ``checked="checked"`` is information. Losing it turns a progress report into fiction.

    Six of the nine items on the main task are ticked. If the attribute were dropped, the issue
    would say nine things are outstanding when six are done — or, in the direction that matters
    more, would let somebody read done work into work that is not. Permanently, in a project
    with no delete.
    """

    FIXTURE = TestTheDescriptionIsReadable.FIXTURE
    MAIN = "MAAAAAEXmain"
    GERMAN = "MAAAAAEXgerm"

    @pytest.fixture()
    def corpus(self):
        return json.loads(self.FIXTURE.read_text(encoding="utf-8"))

    def test_the_six_ticked_items_are_marked_ticked(self, corpus):
        out = creator_module._plain(corpus[self.MAIN]["description"])
        assert out.count("- [x] ") == 6

    def test_the_three_unticked_items_are_marked_unticked(self, corpus):
        out = creator_module._plain(corpus[self.MAIN]["description"])
        assert out.count("- [ ] ") == 3

    def test_the_two_states_are_different_strings(self, corpus):
        """If ticked and unticked rendered the same, the count above would still pass."""
        assert creator_module._CHECKED != creator_module._UNCHECKED

    def test_a_ticked_item_carries_its_own_text(self, corpus):
        out = creator_module._plain(corpus[self.MAIN]["description"])
        assert "- [x] Mito nupe nuto tora lora peto rami rato" in out
        assert "- [ ] Mize nuze" in out

    def test_an_all_unticked_task_reports_none_done(self, corpus):
        """The other direction: a task where nothing is ticked must not show anything ticked."""
        out = creator_module._plain(corpus[self.GERMAN]["description"])
        assert "[x]" not in out
        assert out.count("- [ ] ") == 6

    def test_the_raw_attribute_never_survives_as_text(self, corpus):
        for task in corpus.values():
            out = creator_module._plain(task["description"])
            assert "checked" not in out
            assert "checkbox" not in out


class TestWhatElseTheSixCarry:
    FIXTURE = TestTheDescriptionIsReadable.FIXTURE

    @pytest.fixture()
    def corpus(self):
        return json.loads(self.FIXTURE.read_text(encoding="utf-8"))

    def test_no_tag_survives_in_any_of_the_six(self, corpus):
        import re

        for key, task in corpus.items():
            out = creator_module._plain(task["description"])
            assert not re.search(r"</?(h2|li|ul|ol|br|span|input|label|a)\b", out), key

    def test_entities_are_decoded_everywhere(self, corpus):
        for key, task in corpus.items():
            out = creator_module._plain(task["description"])
            for entity in ("&#34;", "&#61;", "&#64;", "&amp;", "&nbsp;"):
                assert entity not in out, f"{key}: {entity}"

    def test_a_link_keeps_its_text_and_its_address(self, corpus):
        out = creator_module._plain(corpus["MAAAAAEXmain"]["description"])
        assert "[#399516] ZEKALO: Pekalo 440894 : KALOLO " \
               "(https://example.com/link-1)" in out

    def test_a_link_whose_text_is_the_url_is_not_doubled(self, corpus):
        out = creator_module._plain(corpus["MAAAAAEXalfa"]["description"])
        assert out.count("https://example.com/link-4") == 1

    def test_an_address_is_never_eaten_by_the_tag_sweep(self, corpus):
        """Angle brackets around a URL would look like markup to the final strip — measured,
        and the reason the address is in parentheses."""
        out = creator_module._plain(corpus["MAAAAAEXbeta"]["description"])
        assert "https://example.com/link-5" in out

    def test_a_run_of_breaks_keeps_its_lines_apart(self, corpus):
        """``<br />`` is what separates the lines of the JSON example in the German task.

        Measured: without it that description drops from 49 lines to 18 and the example becomes
        "…kanulo lonulo vumilo:Razelo: "sizelo": "Tozelo", "vuzelo"…" — the
        same run-on defect as the headings, in the block a reader most needs to read.
        """
        out = creator_module._plain(corpus["MAAAAAEXgerm"]["description"])
        lines = out.splitlines()
        # The leading space is the source's own indentation and is kept: in a JSON example it
        # is content, so only trailing whitespace is trimmed.
        assert ' "sizelo": "Tozelo",' in lines
        assert ' "vuzelo": "Zezelo 21 kakami",' in lines
        assert "vumilo:Razelo:" not in out
        assert len(lines) > 40

    def test_a_comment_containing_an_angle_bracket_leaves_nothing_behind(self):
        """Declared not-from-the-corpus, and pinned anyway.

        None of the six has a comment with ``>`` in it. The final tag sweep would cut such a
        comment in the middle and leave ``-->`` visible in an issue nobody can delete, so the
        explicit removal stays — and this is the only thing that exercises it.
        """
        assert creator_module._plain("a<!-- x > y -->b") == "ab"
        assert "-->" not in creator_module._plain("a<!-- x > y -->b")

    def test_an_empty_checklist_item_reads_as_an_empty_item(self, corpus):
        """It is faithful: that Wrike task really does have one. An earlier version filtered a
        bare ``-`` that the six never produce — measured — so the filter was removed rather than
        kept as a line nothing exercises."""
        out = creator_module._plain(corpus["MAAAAAEXlist"]["description"])
        assert "- [ ]" in out.splitlines()

    def test_html_comments_do_not_survive_as_text(self, corpus):
        out = creator_module._plain(corpus["MAAAAAEXgerm"]["description"])
        assert "<!--" not in out and "-->" not in out

    def test_an_empty_heading_leaves_no_blank_run(self, corpus):
        out = creator_module._plain(corpus["MAAAAAEXgerm"]["description"])
        assert "\n\n\n" not in out
        assert out.startswith("Pemilo")

    def test_the_template_separator_lines_survive(self, corpus):
        """Rows of dashes are content in the German task, not list markers."""
        out = creator_module._plain(corpus["MAAAAAEXgerm"]["description"])
        assert "-------------------------------------" in out

    def test_no_description_comes_out_empty(self, corpus):
        for key, task in corpus.items():
            assert creator_module._plain(task["description"]).strip(), key

    def test_nothing_is_rewritten(self, corpus):
        """Every word of the source still present. The conversion adds structure, never edits."""
        out = creator_module._plain(corpus["MAAAAAEXtext"]["description"])
        assert "Tolonu Vulonu" in out and "Pevu-Lovumi" in out


class TestTheAdfShapeIsWhatWasMeasured:
    """A newline inside one text node renders as a line break. Measured, not read.

    The ADF specification describes a ``hardBreak`` node for a break within a paragraph, which
    reads as though a bare ``\n`` would be collapsed. The nine checklist items of the main task
    live inside one such node, so on that reading the description would come out as one run-on
    line and the whole HTML conversion would be undone at the last step.

    It was measured instead. In the Jira interface on 11 September 2026, on a permanent test issue: one
    comment with ``\n`` inside a single text node, one with ``hardBreak`` nodes between three
    text nodes. **Identical on screen — three lines each.**

    These tests exist so that nobody "fixes" this later on the strength of the documentation. A
    change with no observable effect, made against an observation, is a change that costs review
    time and risks breaking what works.
    """

    def test_a_paragraph_is_one_text_node_with_its_newlines_intact(self):
        doc = creator_module._adf("one\ntwo\nthree")
        assert len(doc["content"]) == 1
        node = doc["content"][0]["content"]
        assert len(node) == 1, "split into several nodes: the measurement says it need not be"
        assert node[0]["text"] == "one\ntwo\nthree"

    def test_no_hard_break_nodes_are_emitted(self):
        """Not forbidden because they are wrong — they render the same — but because adding
        them would be a change nobody can see, justified by a document over a measurement."""
        import json as _json

        doc = creator_module._adf("one\ntwo")
        assert "hardBreak" not in _json.dumps(doc)

    def test_blank_lines_still_start_a_new_paragraph(self):
        """The one split that IS made, and the only one the measurement calls for."""
        doc = creator_module._adf("first para\n\nsecond para")
        assert len(doc["content"]) == 2
        assert [c["content"][0]["text"] for c in doc["content"]] == ["first para", "second para"]

    def test_the_checklist_survives_as_one_node(self):
        """The case the worry was about: nine items, one node, newlines kept."""
        fixture = Path(__file__).resolve().parent / "fixtures" / "wrike_task_descriptions.json"
        corpus = json.loads(fixture.read_text(encoding="utf-8"))
        text = creator_module._plain(corpus["MAAAAAEXmain"]["description"])
        doc = creator_module._adf(text)
        checklist = [c for c in doc["content"] if "- [x] " in c["content"][0]["text"]]
        assert len(checklist) == 1
        assert checklist[0]["content"][0]["text"].count("\n") == 8

    def test_the_measurement_is_recorded_beside_the_code(self):
        source = (_SCRIPTS / "create_jira_for_task.py").read_text(encoding="utf-8")
        block = source.split("def _adf(")[1].split('"""')[1]
        assert "permanent test issue" in block
        assert "11 September 2026" in block
        assert "render identically" in block

    def test_the_same_shape_elsewhere_is_noted_and_left_alone(self):
        """jira.py and handle_duplicate.py do the same thing. By this measurement that is not a
        defect there either, and neither file is this ticket's to touch."""
        source = (_SCRIPTS / "create_jira_for_task.py").read_text(encoding="utf-8")
        block = source.split("def _adf(")[1].split('"""')[1]
        assert "handle_duplicate.py" in block
        assert "neither file is touched" in block


class TestAMentionIsLeftAsPlainText:
    """A Wrike mention is an ``<a>`` with no ``href``, and it must stay plain text.

    ``<a class="stream-user-id avatar" rel="KUEXAMP1">@Anna Beispiel-Müller</a>``. The link rule
    requires an ``href``, so these fall through to the generic tag strip and come out as
    ``@Anna Beispiel-Müller`` — the right answer by a route nobody chose, which is exactly why it
    needs pinning: generalising the link rule to any ``<a>`` would start appending ``(KUEXAMP1)``
    to every mention.

    And they stay plain on purpose. A Wrike ``@name`` is not a Jira user; turning it into a real
    Jira mention would need a contact mapping that does not exist, and a wrong mention notifies
    the wrong person in a comment nobody can delete. Plain text notifies no one and claims
    nothing.
    """

    MENTION = ('<a class="stream-user-id avatar" rel="KUEXAMP1">&#64;Anna Beispiel-Müller</a>'
               " Done !")

    def test_the_name_survives(self):
        assert creator_module._plain(self.MENTION) == "@Anna Beispiel-Müller Done !"

    def test_the_wrike_contact_id_does_not_leak_into_the_text(self):
        assert "KUEXAMP1" not in creator_module._plain(self.MENTION)

    def test_the_rel_attribute_is_not_treated_as_a_link_target(self):
        """The failure a generalised link rule would cause."""
        out = creator_module._plain(self.MENTION)
        assert "(" not in out and ")" not in out

    def test_a_highlighted_mention_behaves_the_same(self):
        """Wrike adds classes to some mentions: ``highlighted``, and one has it twice."""
        doubled = ('<a class="stream-user-id avatar highlighted highlighted" rel="KUEXAMP4">'
                   "&#64;Olaf Muster</a> don&#39;t forget")
        assert creator_module._plain(doubled) == "@Olaf Muster don't forget"

    def test_a_real_link_still_keeps_its_address(self):
        """The other half of the pair: an ``<a>`` WITH href must not lose its URL."""
        linked = '<a href="https://example.com/link-4"> the file </a>'
        assert "https://example.com/link-4" in creator_module._plain(linked)


class TestTheDeletionLimitationIsWrittenDown:
    def test_the_module_says_it(self):
        header = (_SCRIPTS / "create_jira_for_task.py").read_text(encoding="utf-8").split('"""')[1]
        assert "no delete permission" in header
        assert "renamed, never removed" in header

    def test_it_is_attached_to_the_refusal_that_matters(self, db):
        result = run(message(), apply=True, search=FakeSearch(proven=UNPROVEN),
                     wrike=FakeWrike(), creator=FakeCreator(), db_path=db)
        assert creator_module.NOTHING_CAN_BE_DELETED in result["detail"]

    def test_it_is_attached_to_the_plan_that_would_create(self, db):
        result = run(message(), search=FakeSearch(), wrike=FakeWrike(), creator=FakeCreator(),
                     db_path=db)
        assert result["note"] == creator_module.NOTHING_CAN_BE_DELETED

    def test_a_plan_that_would_not_create_carries_no_such_note(self, db):
        result = run(message(), search=FakeSearch(found=MATCHED), wrike=FakeWrike(),
                     creator=FakeCreator(), db_path=db)
        assert result["note"] is None


class TestTheUnexercisedWritePathIsDeclared:
    """The write path has run once for real — the create, once — and the module says what has not.

    This is a prose check and it is worth saying what that means: it holds that the paragraph is
    there and names the three things that matter, not that the paragraph is true. What makes it
    true is that nobody has run --apply, and the reason nobody can is in the paragraph itself —
    Jira does not allow deleting and Wrike is the client's, so there is no throwaway anywhere.

    The distance between "tested" and "exercised" is where this flow keeps finding
    defects, so the distance is written down rather than left for somebody to assume closed.
    """

    @pytest.fixture()
    def header(self):
        return (_SCRIPTS / "create_jira_for_task.py").read_text(encoding="utf-8").split('"""')[1]

    def test_it_says_the_create_has_run_once_and_the_rest_has_not(self, header):
        flat = " ".join(header.split())
        assert "HAS RUN ONCE FOR REAL" in flat and "created one real issue" in flat
        assert "have never run for real" in flat
        assert "HAS NEVER BEEN RUN" not in flat

    def test_it_says_what_was_verified_instead(self, header):
        """The POST separately with curl, and the ten tests on the real client."""
        assert "curl" in header and "201" in header
        assert "ten tests" in header

    def test_it_names_the_witness_that_cannot_be_deleted(self, header):
        assert "permanent test issue" in header

    def test_it_says_why_there_is_nowhere_to_run_it(self, header):
        assert "does not allow deleting" in header
        assert "Wrike is the client's" in header

    def test_the_ten_tests_it_claims_really_exist(self):
        """Not a claim about a number nobody counted: the class is here and this is its size."""
        names = [n for n in dir(TestTheRealWriteClient) if n.startswith("test_")]
        assert len(names) == 10, names


class TestTheReadModuleStaysReadOnly:
    def test_jira_label_search_gained_no_write(self):
        public = {n for n in dir(_load("jira_label_search").JiraLabelSearch)
                  if not n.startswith("_")}
        assert public == {"from_env", "find_issue_for_task", "issue_exists",
                          "credentials_proven"}

    def test_its_transport_still_has_no_method_parameter(self):
        import inspect

        search_mod = _load("jira_label_search")
        assert "method" not in inspect.signature(search_mod.JiraLabelSearch._get).parameters

    def test_this_module_is_self_contained(self):
        import re

        source = (_SCRIPTS / "create_jira_for_task.py").read_text(encoding="utf-8")
        assert not re.search(r"^\s*(?:import|from)\s+(mossy|dotenv)\b", source, re.MULTILINE)


# ---------------------------------------------------------------------- the evaluated summary
class TestTheSummaryIsEvaluated:
    """the Wrike → Jira flow point 1, literally: *"read the ticket and all comments and create task in Jira
    project with some summary evaluated (the same as for Freshdesk+Email)."*

    Before this, the create read the task and not its comments, and titled the issue with the
    Wrike title as-is — both against the ticket's words. The model is faked by the conftest;
    what is pinned here is what reaches it and what is done with its answer.
    """

    COMMENTS = json.loads((Path(__file__).resolve().parent / "fixtures"
                           / "wrike_task_comments.json").read_text(encoding="utf-8"))[:3]

    class WrikeWithComments(FakeWrike):
        def __init__(self, comments=None, raises=None, refuses=False, **kw):
            super().__init__(**kw)
            self._comments = comments
            self._raises = raises
            self._refuses = refuses
            self.comment_reads = 0

        def get_task_comments(self, task):
            self.comment_reads += 1
            if self._raises is not None:
                raise self._raises
            if self._refuses:
                return {"ok": False, "reason": "not_visible", "comments": []}
            rows = list(self._comments or [])
            return {"ok": True, "comments": rows, "count": len(rows), "empty": not rows}

    def _create(self, db, wrike, writer=None, apply=True):
        writer = writer or FakeCreator()
        result = run(message(), apply=apply, search=FakeSearch(), wrike=wrike, creator=writer,
                     db_path=db)
        return result, writer

    def test_the_comments_are_read_when_creating(self, db):
        wrike = self.WrikeWithComments(self.COMMENTS)
        self._create(db, wrike)
        assert wrike.comment_reads == 1

    def test_every_comment_reaches_the_model(self, db, fake_wrike_model):
        analysis = sys.modules["analyze_wrike_task"]
        self._create(db, self.WrikeWithComments(self.COMMENTS))
        user = fake_wrike_model.calls[-1][1]
        for comment in self.COMMENTS:
            assert analysis.comment_header(comment) in user

    def test_the_link_back_to_wrike_stays_last(self, db):
        _, writer = self._create(db, self.WrikeWithComments(self.COMMENTS))
        description = writer.calls[0]["description"]
        assert description.rstrip().endswith("open.htm?id=1234567890")

    def test_a_failed_model_stops_and_creates_nothing(self, db, fake_wrike_model):
        fake_wrike_model.raises = RuntimeError("OpenAI is down")
        result, writer = self._create(db, self.WrikeWithComments(self.COMMENTS))
        assert result["reason"] == "analysis_failed"
        assert result["analysis"]["reason"] == "model_unavailable"
        assert writer.calls == []

    def test_an_unusable_answer_stops_too(self, db, fake_wrike_model):
        fake_wrike_model.reply = json.dumps({"summary": "x" * 121, "problem": "p"})
        result, writer = self._create(db, self.WrikeWithComments(self.COMMENTS))
        assert result["reason"] == "analysis_failed"
        assert result["analysis"]["reason"] == "summary_too_long"
        assert writer.calls == []

    def test_there_is_no_fallback_to_the_wrike_title(self, db, fake_wrike_model):
        """Decision 3: one issue, one shape, whether or not the model answered."""
        fake_wrike_model.raises = RuntimeError("down")
        result, _ = self._create(db, self.WrikeWithComments(self.COMMENTS))
        assert result["would_create"]["summary"] == ""
        assert result["would_create"]["wrike_title"] == "Prepare sample import requests"

    def test_comments_that_cannot_be_read_stop_the_create(self, db, fake_wrike_model):
        """A summary evaluated without them would be the second shape of the same issue."""
        wrike = self.WrikeWithComments(raises=RuntimeError("Wrike is unreachable"))
        result, writer = self._create(db, wrike)
        assert result["reason"] == "comments_unreadable"
        assert "unreachable" in result["error"]
        assert writer.calls == []
        assert fake_wrike_model.calls == []

    def test_comments_refused_by_wrike_stop_the_create(self, db):
        result, writer = self._create(db, self.WrikeWithComments(refuses=True))
        assert result["reason"] == "comments_unreadable"
        assert writer.calls == []

    def test_zero_comments_still_creates(self, db, fake_wrike_model):
        """Stopping here would block for ever every task that really has no comments. The model
        is told what Wrike returned, not that there are none."""
        result, writer = self._create(db, self.WrikeWithComments([]))
        assert len(writer.calls) == 1
        assert "(Wrike returned no comments)" in fake_wrike_model.calls[-1][1]

    def test_no_wrike_title_stops_before_the_model_is_called(self, db, fake_wrike_model):
        result, writer = self._create(db, self.WrikeWithComments(self.COMMENTS, title="   "))
        assert result["reason"] == "no_summary"
        assert fake_wrike_model.calls == []

    def test_a_dry_run_calls_the_model_and_shows_its_answer(self, db, fake_wrike_model):
        """Wanted for hand-run checks: the summary is read before writing is ever enabled."""
        result, writer = self._create(db, self.WrikeWithComments(self.COMMENTS), apply=False)
        assert len(fake_wrike_model.calls) == 1
        assert result["would_create"]["summary"].startswith("Model summary of:")
        assert writer.calls == []

    def test_the_decision_is_written_where_the_old_one_was(self):
        doc = " ".join(creator_module.__doc__.split())
        assert "with some summary evaluated" in doc
        assert "No model:" not in doc

    def test_the_cost_of_a_dry_run_is_noted_for_the_duty(self):
        """Whatever runs this unattended must not inherit it silently: in dry mode it labels
        nothing, sees the same e-mails again, and would call the model per e-mail and per pass."""
        doc = " ".join(creator_module.__doc__.split())
        assert "per e-mail and per pass" in doc
