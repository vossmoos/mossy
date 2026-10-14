"""The entry point of the Wrike flow (the Wrike → Jira flow, piece 2): parse, decide, gate, write, label.

Nothing here touches the network: ``urlopen`` explodes, the credentials are removed, and every
client is a double. The Jira double has **memory** — an issue created carries its label and is
found by the next search, a comment posted carries its anchor — like the one in
``test_wrike_flow_idempotence``. The register is a real DuckDB file under ``tmp_path`` and the
locks live in the conftest's throwaway directory.

What is pinned, and the mutation each part was written against (run on a green baseline of 702
before these tests existed, all seven green then):

* without ``apply`` nothing is written to Jira, the register or Gmail (P6);
* a task with no issue and my id not in ``responsibleIds`` gets no issue (P1);
* a task that already has a live issue gets its comments copied even when I am not in
  ``responsibleIds`` — the gate governs creating, not copying (P3);
* who I am comes from ``?me=true`` and is never guessed (P2, P4, P5 — pinned in
  ``test_wrike_api``, and exercised here through the envelope);
* the writers' own lock covers deciding and writing, and this script takes no outer lock that
  would lock the process out of its own write (P7).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

# Creating evaluates the summary with a model (the Wrike → Jira flow, piece 5).
pytestmark = pytest.mark.usefixtures("fake_wrike_model")


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


proc = _load("process_wrike_notification")
fetch = _load("fetch_wrike_notifications")
links = _load("wrike_jira_links")
locking = _load("task_lock")
copier = _load("copy_wrike_comments")
creator_mod = _load("create_jira_for_task")
wrike_api = _load("wrike_api")
search_mod = _load("jira_label_search")
_load("parse_wrike_notification")
_load("decide_wrike_action")

TASK = "MAAAAAEXmain"
ME = "KUEXAMP2"
OTHER = "KUEXAMP5"
COMMENTS = [
    {"id": "C1", "createdDate": "2026-01-01T10:00:00Z", "authorId": "KUEXAMP1", "text": "uno"},
    {"id": "C2", "createdDate": "2026-01-02T10:00:00Z", "authorId": "KUEXAMP4", "text": "dos"},
]


@pytest.fixture(autouse=True)
def _offline(monkeypatch, tmp_path):
    def _no_network(*args, **kwargs):
        raise AssertionError("a test reached for the network")

    monkeypatch.setattr(fetch.urllib.request, "urlopen", _no_network)
    for name in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "WRIKE_TOKEN",
                 "JIRA_SITE", "JIRA_USER_EMAIL", "JIRA_API_KEY", "JIRA_SPACE_NAME",
                 "WRIKE_TASKS_PROCESSED_LABEL", "WRIKE_TASKS_SKIPPED_LABEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WRIKE_TASKS_ALLOWED_SENDERS", "wrike@wrike.com")
    # The register main() would open by default is the operator's; never in a test.
    monkeypatch.setenv("WRIKE_JIRA_LINKS_DB", str(tmp_path / "default-register.duckdb"))


def message(mid="gmail-1", sender="wrike@wrike.com"):
    return {"id": mid, "threadId": f"t-{mid}", "payload": {"headers": [
        {"name": "From", "value": f"Wrike <{sender}>"},
        {"name": "Subject", "value": "Prepare sample import requests"},
        {"name": "X-Wrike-Metadata", "value": json.dumps(
            {"taskApiId": TASK, "taskId": 1234567890, "notificationType": "Regular",
             "entityType": "Task"})},
    ]}}


class Jira:
    """One fake Jira in all three roles, with memory."""

    def __init__(self):
        self.issues: dict[str, dict] = {}
        self.comments: dict[str, list[str]] = {}
        self.creates = 0
        self.posts = 0

    def find_issue_for_task(self, task):
        label = f"wrike-{task}"
        keys = [k for k, v in self.issues.items() if label in v["labels"]]
        if not keys:
            return {"ok": True, "matched": False, "authenticated": None, "empty": True}
        return {"ok": True, "matched": True, "authenticated": True, "issue_key": keys[0],
                "issue_url": f"https://x/browse/{keys[0]}", "issue_keys": keys,
                "label_confirmed": True, "confirmed_keys": keys}

    def issue_exists(self, key):
        if key in self.issues:
            return {"ok": True, "exists": True, "authenticated": True}
        return {"ok": True, "exists": None, "authenticated": None}

    def credentials_proven(self):
        return {"ok": True, "proven": True}

    def create_issue(self, summary, description, labels, issue_type="Task"):
        self.creates += 1
        key = f"PROJ-{900 + self.creates}"
        self.issues[key] = {"labels": list(labels)}
        self.comments[key] = []
        return {"issue_key": key, "issue_url": f"https://x/browse/{key}", "status": 201}

    def existing_comments(self, key):
        anchors = set()
        for body in self.comments.get(key, []):
            anchors |= copier.anchors_in(body)
        return {"ok": True, "issue_key": key, "count": len(self.comments.get(key, [])),
                "anchors": sorted(anchors), "authenticated": True}

    def add_comment(self, key, text):
        self.posts += 1
        self.comments.setdefault(key, []).append(text)
        return {"comment_id": f"jira-{self.posts}", "issue_key": key, "status": 201}


class Wrike:
    def __init__(self, responsible=(ME,), me=None, me_raises=None, task_raises=None,
                 comments=COMMENTS):
        self.responsible = responsible
        self.me = me if me is not None else {"ok": True, "me_id": ME}
        self.me_raises = me_raises
        self.task_raises = task_raises
        self.rows = list(comments)
        self.me_calls = 0
        self.task_calls = 0

    def get_me(self):
        self.me_calls += 1
        if self.me_raises is not None:
            raise self.me_raises
        return self.me

    def get_task(self, task):
        self.task_calls += 1
        if self.task_raises is not None:
            raise self.task_raises
        body = {"title": "Prepare sample import requests", "description": "<p>hi</p>",
                "permalink": "https://www.wrike.com/open.htm?id=1234567890"}
        if self.responsible is not None:
            body["responsibleIds"] = list(self.responsible)
        return {"ok": True, "task_id": task, "task": body}

    def get_task_comments(self, task):
        return {"ok": True, "task_id": task, "comments": list(self.rows),
                "count": len(self.rows), "empty": not self.rows}


class Gmail:
    def __init__(self, msg=None, label_exists=True, fetch_error=None):
        self.msg = msg if msg is not None else message()
        self.label_exists = label_exists
        self.fetch_error = fetch_error
        self.writes: list[tuple[str, str]] = []
        self.fetched: list[str] = []

    def metadata_message(self, mid):
        self.fetched.append(mid)
        if self.fetch_error is not None:
            raise self.fetch_error
        return self.msg

    def label_id(self, name):
        return f"LBL_{name}" if self.label_exists else None

    def add_label(self, mid, lid):
        self.writes.append((mid, lid))
        return [lid]

    def label_ids_of(self, mid):
        return ["INBOX"] + [lid for m, lid in self.writes if m == mid]


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "register.duckdb"


def run(db, *, apply, wrike=None, jira=None, gmail=None, msg=None, message_id=None):
    jira = jira if jira is not None else Jira()
    return proc.process(msg if msg is not None or message_id else message(),
                        message_id=message_id, apply=apply, gmail=gmail,
                        wrike=wrike if wrike is not None else Wrike(), search=jira,
                        creator=jira, writer=jira, db_path=db)


def seed_live_issue(jira, db, *, in_register=True):
    jira.issues["PROJ-800"] = {"labels": [f"wrike-{TASK}"]}
    jira.comments["PROJ-800"] = []
    if in_register:
        links.remember_link(TASK, "PROJ-800", "https://x/browse/PROJ-800", db_path=db)


LABEL_PROCESSED = "LBL_mossy-wrike-processed"
LABEL_SKIPPED = "LBL_mossy-wrike-skipped"


# ------------------------------------------------------------------------ without --apply
class TestAnEmptyCommentListIsLabelledButSaysSo:
    """Stopping on an empty list was tried on 14 September 2026 and reverted the same day: two of
    the six live tasks measured really have no comments, and every later notification for them
    would have been held for good. The 29 comments of MAAAAAEXmain were read with this same licence.
    So an empty list is labelled — and the envelope says, at the top, that it was empty and that
    empty is not proof of absence."""

    def test_an_empty_list_is_labelled_and_the_envelope_says_it_was_empty(self, db):
        jira, gmail = Jira(), Gmail()
        seed_live_issue(jira, db)
        out = run(db, apply=True, wrike=Wrike(comments=[]), jira=jira, gmail=gmail)
        assert out["ok"] is True and out["action"] == "copied"
        assert gmail.writes == [("gmail-1", LABEL_PROCESSED)]
        assert jira.posts == 0
        assert out["comments_certain"] is False
        assert out["comments_means"] == copier.EMPTY_IS_NOT_ABSENCE

    def test_with_comments_it_is_copied_and_labelled(self, db):
        """The inversion: comments present, and no trail claiming the list was empty."""
        jira, gmail = Jira(), Gmail()
        seed_live_issue(jira, db)
        out = run(db, apply=True, jira=jira, gmail=gmail)
        assert out["ok"] is True and out["action"] == "copied"
        assert gmail.writes == [("gmail-1", LABEL_PROCESSED)]
        assert "comments_certain" not in out


class TestWithoutApplyNothingIsWritten:
    def test_the_create_branch_describes_and_writes_nowhere(self, db):
        jira, gmail = Jira(), Gmail()
        out = run(db, apply=False, jira=jira, gmail=gmail)
        assert out["ok"] is True and out["action"] == "describe"
        assert out["create"].get("would_create"), "it describes the issue it would create"
        assert jira.creates == 0 and jira.posts == 0
        assert links.linked_issue(TASK, db_path=db) is None, "nothing in the register"
        assert gmail.writes == [], "nothing labelled"

    def test_the_copy_branch_describes_and_writes_nowhere(self, db):
        jira, gmail = Jira(), Gmail()
        seed_live_issue(jira, db)
        out = run(db, apply=False, jira=jira, gmail=gmail)
        assert out["ok"] is True and out["stage"] == "copy"
        assert jira.posts == 0 and jira.creates == 0
        assert links.copied_comment_ids(TASK, db_path=db) == set()
        assert gmail.writes == []

    def test_a_skip_is_not_labelled_either(self, db):
        gmail = Gmail()
        out = run(db, apply=False, wrike=Wrike(responsible=(OTHER,)), gmail=gmail)
        assert out["reason"] == proc.NOT_RESPONSIBLE and gmail.writes == []

    def test_main_does_not_even_build_the_writers(self, db, tmp_path, monkeypatch, capsys):
        built = []
        monkeypatch.setattr(creator_mod.JiraIssueCreator, "from_env",
                            classmethod(lambda cls: built.append("creator")))
        monkeypatch.setattr(copier.JiraCommentWriter, "from_env",
                            classmethod(lambda cls: built.append("writer")))
        jira = Jira()
        monkeypatch.setattr(wrike_api.WrikeClient, "from_env", classmethod(lambda cls: Wrike()))
        monkeypatch.setattr(search_mod.JiraLabelSearch, "from_env", classmethod(lambda cls: jira))
        path = tmp_path / "message.json"
        path.write_text(json.dumps(message()), encoding="utf-8")
        assert proc.main(["--message-json", str(path)]) == 0
        assert built == [], "without --apply the Jira writers are never constructed"
        assert json.loads(capsys.readouterr().out)["action"] == "describe"


# -------------------------------------------------------------------- the gate: creating
class TestTheGateGovernsCreating:
    def test_no_issue_and_not_responsible_creates_nothing_and_is_skipped(self, db):
        jira, gmail = Jira(), Gmail()
        out = run(db, apply=True, wrike=Wrike(responsible=(OTHER,)), jira=jira, gmail=gmail)
        assert out["ok"] is True and out["reason"] == proc.NOT_RESPONSIBLE
        assert jira.creates == 0
        assert links.linked_issue(TASK, db_path=db) is None
        assert gmail.writes == [("gmail-1", LABEL_SKIPPED)]

    def test_no_issue_and_responsible_creates_and_is_processed(self, db):
        jira, gmail = Jira(), Gmail()
        out = run(db, apply=True, jira=jira, gmail=gmail)
        assert out["ok"] is True and out["action"] == "created"
        assert jira.creates == 1
        assert links.linked_issue(TASK, db_path=db)["jira_key"] == "PROJ-901"
        assert gmail.writes == [("gmail-1", LABEL_PROCESSED)]

    def test_no_copy_straight_after_a_create(self, db):
        """The pass stops after a create: the comments it read are recorded as copied and reach
        the issue only through the model's summary, never as their own text."""
        jira = Jira()
        run(db, apply=True, jira=jira, gmail=Gmail())
        assert jira.creates == 1 and jira.posts == 0

    def test_the_next_notification_posts_none_of_the_comments_the_create_summarised(self, db):
        """Measured 14 September 2026 before the fix, on the 29-comment task: 0 recorded after
        the create, 29 posted as loose comments by the next notification."""
        jira = Jira()
        run(db, apply=True, jira=jira, gmail=Gmail())
        out = run(db, apply=True, jira=jira, gmail=Gmail(), msg=message("gmail-2"))
        assert out["stage"] == "copy"
        assert jira.creates == 1 and jira.posts == 0
        assert links.copied_comment_ids(TASK, db_path=db) == {c["id"] for c in COMMENTS}

    def test_summarised_comments_that_were_not_recorded_stop_unlabelled(self, db, monkeypatch):
        monkeypatch.setattr(links, "remember_comment",
                            lambda *a, **k: {"ok": False, "reason": "database_error"})
        jira, gmail = Jira(), Gmail()
        out = run(db, apply=True, jira=jira, gmail=gmail)
        assert out["reason"] == proc.WRITE_STOPPED
        assert out["write_reason"] == "summarised_comments_not_recorded"
        assert jira.creates == 1 and out["create"]["created"]["issue_key"] == "PROJ-901"
        assert gmail.writes == [], "a failure of ours is never labelled"

    def test_unknown_responsibility_is_not_no(self, db):
        jira, gmail = Jira(), Gmail()
        out = run(db, apply=True, wrike=Wrike(responsible=None), jira=jira, gmail=gmail)
        assert out["ok"] is False and out["reason"] == proc.RESPONSIBILITY_UNKNOWN
        assert jira.creates == 0 and gmail.writes == [], "a failure of ours is never labelled"

    def test_an_unreadable_task_stops_unlabelled(self, db):
        jira, gmail = Jira(), Gmail()
        wrike = Wrike(task_raises=wrike_api.WrikeError("forbidden", status=403))
        out = run(db, apply=True, wrike=wrike, jira=jira, gmail=gmail)
        assert out["reason"] == proc.WRIKE_UNREADABLE
        assert jira.creates == 0 and gmail.writes == []


# ------------------------------------------------------------------- the gate: NOT copying
class TestTheGateDoesNotGovernCopying:
    """An issue that exists keeps receiving comments: a mute issue is worse than one kept up to
    date, and the decision that the task concerns me was taken when the issue was created."""

    def test_a_live_issue_gets_its_comments_though_i_am_no_longer_responsible(self, db):
        jira, gmail = Jira(), Gmail()
        seed_live_issue(jira, db)
        out = run(db, apply=True, wrike=Wrike(responsible=(OTHER,)), jira=jira, gmail=gmail)
        assert out["ok"] is True and out["action"] == "copied"
        assert jira.posts == len(COMMENTS)
        assert gmail.writes == [("gmail-1", LABEL_PROCESSED)]

    def test_the_copy_branch_does_not_ask_who_i_am(self, db):
        """So an expired Wrike token or a 403 on /contacts cannot mute a live issue."""
        jira = Jira()
        seed_live_issue(jira, db)
        wrike = Wrike(me_raises=wrike_api.WrikeError("refused", status=401))
        out = run(db, apply=True, wrike=wrike, jira=jira, gmail=Gmail())
        assert out["action"] == "copied" and jira.posts == len(COMMENTS)
        assert wrike.me_calls == 0

    def test_a_label_only_issue_is_repaired_and_copied_without_the_gate(self, db):
        jira = Jira()
        seed_live_issue(jira, db, in_register=False)
        out = run(db, apply=True, wrike=Wrike(responsible=(OTHER,)), jira=jira, gmail=Gmail())
        assert out["action"] == "copied"
        assert links.linked_issue(TASK, db_path=db)["jira_key"] == "PROJ-800"
        assert jira.creates == 0 and jira.posts == len(COMMENTS)


# ------------------------------------------------------------------------ who am I
class TestWhoIAmIsNeverGuessed:
    @pytest.mark.parametrize("me", [
        {"ok": False, "reason": "me_unresolved", "rows": 0},
        {"ok": False, "reason": "me_unresolved", "rows": 2},
        {"ok": False, "reason": "me_unresolved", "rows": 1, "me_flag": None},
    ])
    def test_an_unresolved_me_stops_before_creating(self, db, me):
        jira, gmail = Jira(), Gmail()
        out = run(db, apply=True, wrike=Wrike(me=me), jira=jira, gmail=gmail)
        assert out["ok"] is False and out["reason"] == "me_unresolved"
        assert jira.creates == 0 and gmail.writes == []

    def test_a_refused_contacts_read_stops_before_creating(self, db):
        jira = Jira()
        wrike = Wrike(me_raises=wrike_api.WrikeError("refused", status=401))
        out = run(db, apply=True, wrike=wrike, jira=jira, gmail=Gmail())
        assert out["reason"] == proc.WRIKE_UNREADABLE and jira.creates == 0


# --------------------------------------------------------------------------- the lock
class TestTheLock:
    def test_a_task_held_by_another_pass_writes_nothing(self, db):
        jira, gmail = Jira(), Gmail()
        with locking.task_lock(TASK) as state:
            assert state == locking.HELD
            out = run(db, apply=True, jira=jira, gmail=gmail)
        assert out["ok"] is False and out["write_reason"] == locking.TASK_IS_BUSY
        assert jira.creates == 0 and gmail.writes == []

    def test_the_write_happens_inside_the_writers_lock_and_not_against_itself(self, db):
        """Measured before this was written: an outer lock around ``run(apply=True)`` makes the
        process lock itself out and report ``task_is_busy`` — it looks like it works and writes
        nothing. So the create must land, and must say the lock was held for it."""
        jira = Jira()
        out = run(db, apply=True, jira=jira, gmail=Gmail())
        assert out["action"] == "created" and jira.creates == 1
        assert out["create"].get("lock") == locking.HELD

    def test_the_script_never_calls_run_unlocked(self):
        source = (_SCRIPTS / "process_wrike_notification.py").read_text(encoding="utf-8")
        body = source.split('"""', 2)[2]
        assert "_run_unlocked(" not in body and "task_lock(" not in body


# ----------------------------------------------------------------- labels and failures
class TestSkippedIsOnlyForWhatTheEmailIs:
    def test_not_a_wrike_notification_is_skipped_and_nothing_else_is_asked(self, db):
        jira, gmail, wrike = Jira(), Gmail(), Wrike()
        out = run(db, apply=True, msg=message(sender="noreply@wrike.com"), wrike=wrike,
                  jira=jira, gmail=gmail)
        assert out["ok"] is True and out["reason"] == proc.NOT_A_WRIKE_NOTIFICATION
        assert gmail.writes == [("gmail-1", LABEL_SKIPPED)]
        assert wrike.task_calls == 0 and wrike.me_calls == 0 and jira.creates == 0

    def test_a_dead_gmail_token_stops_and_labels_nothing(self, db):
        error = fetch.GmailFetchError("refused", kind="token", status=400,
                                      google_error="invalid_grant")
        gmail, wrike = Gmail(fetch_error=error), Wrike()
        out = proc.process(message_id="gmail-1", apply=True, gmail=gmail, wrike=wrike,
                           search=Jira(), creator=Jira(), writer=Jira(), db_path=db)
        assert out["ok"] is False and out["reason"] == fetch.GMAIL_TOKEN_REFUSED
        assert gmail.writes == [] and wrike.me_calls == 0 and wrike.task_calls == 0

    def test_a_label_that_fails_after_the_create_keeps_what_jira_did(self, db):
        jira = Jira()
        out = run(db, apply=True, jira=jira, gmail=Gmail(label_exists=False))
        assert out["ok"] is False and out["reason"] == proc.LABEL_FAILED
        assert out["outcome"] == "created"
        assert out["create"]["applied"] is True and jira.creates == 1
        assert out["label"]["reason"] == fetch.LABEL_NOT_FOUND


# ---------------------------------------------------------------------------- the CLI
class TestTheCommandRuns:
    def test_an_unreadable_message_file_exits_non_zero(self, tmp_path, capsys):
        path = tmp_path / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        assert proc.main(["--message-json", str(path)]) == 1
        assert json.loads(capsys.readouterr().out)["reason"] == proc.MESSAGE_UNREADABLE

    def test_the_two_sources_cannot_be_given_together(self, tmp_path):
        with pytest.raises(SystemExit):
            proc.main(["--message-id", "x", "--message-json", str(tmp_path / "m.json")])


# ------------------------------------------------------------------ what the module declares
class TestTheDeclarationsStayWritten:
    def test_the_gate_is_about_concern_not_ownership(self):
        doc = proc.__doc__
        assert '"this concerns me"' in doc and '"this is mine"' in doc
        assert "measurement umbrella" in doc and "four" in doc

    def test_the_gate_governs_creating_and_says_why_not_copying(self):
        doc = proc.__doc__
        assert "THE GATE GOVERNS CREATING, NOT COPYING" in doc
        assert "go mute" in doc and "cheap side of the error" in doc

    def test_the_lock_trap_is_written_down(self):
        doc = proc.__doc__
        assert "task_is_busy" in doc and "never calls ``_run_unlocked``" in doc
