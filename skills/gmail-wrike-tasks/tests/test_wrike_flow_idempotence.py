"""the create step and the comment copy together, over one register: retries, overlaps, and an unreadable store.

the per-task lock. Until this file the two writers were only ever tested apart — the create step against its fake
creator, the comment copy against its fake writer — and the two rescues they depend on, the Jira label and
the comment anchor, had never been exercised in the same flow. A drift between the modules would
have been seen by nobody.

The Jira here is a fake **with memory**: an issue created carries its label and is found by the
next search, a comment posted carries its anchor and is read back by the next pass — the way the
real one behaves, measured in the create step (on a test issue) and the comment copy. The register is a real DuckDB file
under ``tmp_path``, and the locks live in the conftest's throwaway directory.

Nothing here touches the network.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
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


creator = _load("create_jira_for_task")
copier = _load("copy_wrike_comments")
links = _load("wrike_jira_links")
decide_module = _load("decide_wrike_action")
locking = _load("task_lock")
# Loaded here, not lazily inside a thread: two threads racing on the first import of a sibling
# get a half-initialised module — measured while writing this file.
_load("parse_wrike_notification")
_load("jira_label_search")

TASK = "MAAAAAEXmain"
COMMENTS = [
    {"id": "C1", "createdDate": "2026-01-01T10:00:00Z", "authorId": "KUEXAMP1", "text": "uno"},
    {"id": "C2", "createdDate": "2026-01-02T10:00:00Z", "authorId": "KUEXAMP4", "text": "dos"},
]


def message(mid="gmail-1"):
    return {"id": mid, "threadId": f"t-{mid}", "payload": {"headers": [
        {"name": "From", "value": "Anna <wrike@wrike.com>"},
        {"name": "Subject", "value": "Prepare sample import requests"},
        {"name": "X-Wrike-Metadata", "value": json.dumps(
            {"taskApiId": TASK, "taskId": 1234567890, "notificationType": "Regular",
             "entityType": "Task"})},
    ]}}


class Jira:
    """One fake Jira playing all three roles, with memory — so a rescue is a real rescue."""

    def __init__(self):
        self.issues: dict[str, dict] = {}
        self.comments: dict[str, list[str]] = {}
        self.creates = 0
        self.posts = 0
        # Hooks a test can set to hold a pass at the exact point of the race.
        self.on_search = None
        self.on_read_back = None

    # --- the reader decide_wrike_action and the canary use
    def find_issue_for_task(self, task):
        label = f"wrike-{task}"
        keys = [k for k, v in self.issues.items() if label in v["labels"]]
        answer = ({"ok": True, "matched": False, "authenticated": None, "empty": True}
                  if not keys else
                  {"ok": True, "matched": True, "authenticated": True, "issue_key": keys[0],
                   "issue_url": f"https://x/browse/{keys[0]}", "issue_keys": keys,
                   "label_confirmed": True, "confirmed_keys": keys,
                   **({"anomalies": ["more_than_one_issue"]} if len(keys) > 1 else {})})
        if self.on_search:
            self.on_search()
        return answer

    def issue_exists(self, key):
        if key in self.issues:
            return {"ok": True, "exists": True, "authenticated": True}
        return {"ok": True, "exists": None, "authenticated": None}

    def credentials_proven(self):
        return {"ok": True, "proven": True}

    # --- the create step's writer
    def create_issue(self, summary, description, labels, issue_type="Task"):
        self.creates += 1
        key = f"PROJ-{900 + self.creates}"
        self.issues[key] = {"labels": list(labels)}
        self.comments[key] = []
        return {"issue_key": key, "issue_url": f"https://x/browse/{key}", "status": 201}

    # --- the comment copy's writer
    def existing_comments(self, key):
        bodies = self.comments.get(key, [])
        anchors = set()
        for body in bodies:
            anchors |= copier.anchors_in(body)
        answer = {"ok": True, "issue_key": key, "count": len(bodies),
                  "anchors": sorted(anchors), "authenticated": True}
        if self.on_read_back:
            self.on_read_back()
        return answer

    def add_comment(self, key, text):
        self.posts += 1
        self.comments.setdefault(key, []).append(text)
        return {"comment_id": f"jira-{self.posts}", "issue_key": key, "status": 201}

    def anchors_on(self, key):
        return [sorted(copier.anchors_in(b))[0] for b in self.comments.get(key, [])]


class Wrike:
    def __init__(self, comments=COMMENTS):
        self.rows = list(comments)

    def get_task(self, task):
        return {"ok": True, "task_id": task, "task": {
            "title": "Prepare sample import requests", "description": "<p>hi</p>",
            "permalink": "https://www.wrike.com/open.htm?id=1234567890"}}

    def get_task_comments(self, task):
        return {"ok": True, "task_id": task, "comments": list(self.rows),
                "count": len(self.rows)}


class _BeforeItsComments:
    """The task as it was read before any comment was written on it."""

    def __init__(self, wrike):
        self._wrike = wrike

    def get_task(self, task):
        return self._wrike.get_task(task)

    def get_task_comments(self, task):
        return {"ok": True, "task_id": task, "comments": [], "count": 0}


def create(jira, wrike, db, mid="gmail-1", **kw):
    """the create step over the task read before its comments existed. A create records the comments it
    summarised as copied (the Wrike → Jira flow), and every test in this file is about the comments that come
    AFTER the create — their copy, their lock, their rescue — so the create summarises none."""
    return creator.run(message(mid), apply=True, search=jira, wrike=_BeforeItsComments(wrike),
                       creator=jira, db_path=db, **kw)


def copy(jira, wrike, db, mid="gmail-1", **kw):
    return copier.run(message(mid), apply=True, search=jira, wrike=wrike, writer=jira,
                      db_path=db, **kw)


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "wrike-jira-links.duckdb"


def _crashed(*_a, **_k):
    return {"ok": False, "reason": "crashed"}


# ---------------------------------------------------------------------- both rescues, one flow
class TestBothRescuesInOneFlow:
    """The two half-way crashes measured in the per-task lock, one after the other, over one register.

    Pass 1 dies after ``POST /issue`` and before the row: the label must rescue the issue.
    Pass 2 dies after ``POST /comment`` and before the row: the anchor must rescue the comments.
    Pass 3 must find everything done and write nothing.
    """

    def test_three_passes_two_crashes_one_issue_two_comments(self, db, monkeypatch):
        jira, wrike = Jira(), Wrike()

        # ---- pass 1: create lands, the row does not
        with monkeypatch.context() as m:
            m.setattr(links, "remember_link", _crashed)
            first = create(jira, wrike, db)
        assert jira.creates == 1
        assert links.linked_issue(TASK, db_path=db) is None
        copied = copy(jira, wrike, db)
        assert copied["verdict"] == decide_module.FOUND_BY_LABEL_ONLY
        assert copied["action"] == copier.ACTION_REPAIR_FIRST
        assert jira.posts == 0
        key = first["created"]["issue_key"]

        # ---- pass 2: the label rescues the row; the comments land, their rows do not
        repaired = create(jira, wrike, db)
        assert repaired["verdict"] == decide_module.FOUND_BY_LABEL_ONLY
        assert jira.creates == 1
        assert links.linked_issue(TASK, db_path=db)["jira_key"] == key
        with monkeypatch.context() as m:
            m.setattr(links, "remember_comment", _crashed)
            copy(jira, wrike, db)
        assert jira.posts == 2
        assert links.copied_comment_ids(TASK, db_path=db) == set()

        # ---- pass 3: the anchors rescue the rows; nothing is written in Jira
        nothing = create(jira, wrike, db)
        healed = copy(jira, wrike, db)
        assert nothing["verdict"] == decide_module.LINKED_AND_ALIVE
        assert healed["posted"] == 0
        assert healed["healed"] == 2

        assert jira.creates == 1
        assert jira.posts == 2
        assert jira.anchors_on(key) == ["C1", "C2"]
        assert links.copied_comment_ids(TASK, db_path=db) == {"C1", "C2"}

    def test_the_issue_vi203_creates_is_the_one_vi204_comments_on(self, db):
        """The seam between the two modules: the key the create step records is the key the comment copy reads."""
        jira, wrike = Jira(), Wrike()
        created = create(jira, wrike, db)
        copy(jira, wrike, db)
        key = created["created"]["issue_key"]
        assert list(jira.comments) == [key]
        assert jira.anchors_on(key) == ["C1", "C2"]

    def test_a_new_wrike_comment_later_is_copied_once_and_alone(self, db):
        jira, wrike = Jira(), Wrike()
        create(jira, wrike, db)
        copy(jira, wrike, db)
        wrike.rows.append({"id": "C3", "createdDate": "2026-01-03T10:00:00Z",
                           "authorId": "KUEXAMP1", "text": "tres"})
        later = copy(jira, wrike, db, mid="gmail-2")
        assert later["posted"] == 1
        assert jira.anchors_on(list(jira.issues)[0]) == ["C1", "C2", "C3"]


# ---------------------------------------------------------------------- overlapping passes
class TestTwoOverlappingPassesOnThisMachine:
    """Measured before the lock existed: two passes that both read before either wrote made two
    issues and two copies of every comment. Here the first pass is held at the exact point of
    that race — after deciding, before writing — while the second one tries."""

    def _hold_first_pass(self, jira, hook, target):
        inside, release = threading.Event(), threading.Event()

        def hold():
            inside.set()
            assert release.wait(timeout=10)

        setattr(jira, hook, hold)
        out = {}
        thread = threading.Thread(target=lambda: out.setdefault("first", target()))
        thread.start()
        assert inside.wait(timeout=10)
        setattr(jira, hook, None)   # the second pass must not block on it
        return thread, release, out

    def test_the_second_create_is_refused_while_the_first_decides(self, db):
        jira, wrike = Jira(), Wrike()
        thread, release, out = self._hold_first_pass(
            jira, "on_search", lambda: create(jira, wrike, db, mid="gmail-A"))
        try:
            second = create(jira, wrike, db, mid="gmail-B")
        finally:
            release.set()
            thread.join(timeout=10)
        assert second["reason"] == locking.TASK_IS_BUSY
        assert second["lock"] == locking.BUSY
        assert out["first"]["applied"] is True
        assert jira.creates == 1

    def test_the_second_copy_is_refused_between_read_back_and_write(self, db):
        jira, wrike = Jira(), Wrike()
        create(jira, wrike, db)
        thread, release, out = self._hold_first_pass(
            jira, "on_read_back", lambda: copy(jira, wrike, db, mid="gmail-A"))
        try:
            second = copy(jira, wrike, db, mid="gmail-B")
        finally:
            release.set()
            thread.join(timeout=10)
        assert second["reason"] == locking.TASK_IS_BUSY
        assert second["posted"] == 0
        assert out["first"]["posted"] == 2
        assert jira.posts == 2
        assert jira.anchors_on(list(jira.issues)[0]) == ["C1", "C2"]

    def test_describing_is_never_blocked(self, db):
        """Without apply nothing is written, so nothing is locked: a dry run may overlap."""
        jira, wrike = Jira(), Wrike()
        create(jira, wrike, db)
        thread, release, _ = self._hold_first_pass(
            jira, "on_read_back", lambda: copy(jira, wrike, db, mid="gmail-A"))
        try:
            dry = copier.run(message("gmail-B"), search=jira, wrike=wrike, writer=jira,
                             db_path=db)
        finally:
            release.set()
            thread.join(timeout=10)
        assert dry.get("reason") != locking.TASK_IS_BUSY
        assert len(dry["would_copy"]) == 2

    def test_two_machines_are_not_covered_and_this_test_says_so(self, db, tmp_path):
        """THE LIMIT, PINNED. Two lock directories are two machines as far as ``flock`` knows,
        and the race comes straight back: both read no anchors, both post. This test asserts
        the duplicate on purpose. If it ever fails, the lock grew to cover more than it used to,
        and ``task_lock.ONE_MACHINE_ONLY`` must be rewritten to match — not this test deleted."""
        jira, wrike = Jira(), Wrike()
        create(jira, wrike, db)
        both_read = threading.Barrier(2)
        jira.on_read_back = lambda: both_read.wait(timeout=10)
        machines = [tmp_path / "machine-a", tmp_path / "machine-b"]
        threads = [threading.Thread(target=copy, args=(jira, wrike, db),
                                    kwargs={"mid": f"gmail-{i}", "lock_dir": d})
                   for i, d in enumerate(machines)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        # Every comment twice. Sorted, because the two threads interleave their posts in
        # whatever order the scheduler picks; the duplicate is the claim, not the order.
        assert sorted(jira.anchors_on(list(jira.issues)[0])) == ["C1", "C1", "C2", "C2"]
        assert "does not cover two machines" in locking.ONE_MACHINE_ONLY


# ---------------------------------------------------------------------- the store, end to end
class TestAnUnreadableRegisterStopsTheWholeFlow:
    """Point 1 of the per-task lock through both writers. The row is there; another process holds the
    DuckDB file; and before the per-task lock that read as "no link" — a second issue, permanent."""

    def test_neither_writer_writes_while_the_register_is_held(self, db):
        jira, wrike = Jira(), Wrike()
        create(jira, wrike, db)
        copy(jira, wrike, db)
        assert (jira.creates, jira.posts) == (1, 2)

        holder = subprocess.Popen(
            [sys.executable, "-c",
             f"import duckdb,sys;held=duckdb.connect({str(db)!r});print('ready',flush=True);"
             "sys.stdin.readline();held.close()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout.readline().strip() == "ready"
            refused_create = create(jira, wrike, db, mid="gmail-2")
            refused_copy = copy(jira, wrike, db, mid="gmail-2")
        finally:
            holder.stdin.write("\n")
            holder.stdin.flush()
            holder.wait(timeout=10)

        assert refused_create["verdict"] == decide_module.REGISTER_UNREADABLE
        assert refused_create["action"] == creator.ACTION_STOP
        assert refused_copy["verdict"] == decide_module.REGISTER_UNREADABLE
        assert refused_copy["action"] == copier.ACTION_STOP
        assert (jira.creates, jira.posts) == (1, 2)


# ---------------------------------------------------------------------- paths 2 and 5, pinned
class TestTheDedupeIsByTaskNotByEmail:
    """Path 2 of the per-task lock: different e-mails about one task. Covered before the per-task lock and measured
    during it — three notifications, one ``taskApiId``, one issue — but pinned by no test. The
    register is keyed by the task (``wrike_jira_links``: ``task_api_id`` is the primary key), so
    the second e-mail decides from the table, not from a rescue."""

    def test_three_different_emails_for_one_task_make_one_issue(self, db):
        jira, wrike = Jira(), Wrike()
        passes = [(create(jira, wrike, db, mid=m), copy(jira, wrike, db, mid=m))
                  for m in ("gmail-A", "gmail-B", "gmail-C")]
        assert jira.creates == 1
        assert jira.posts == 2
        assert [c["verdict"] for c, _ in passes] == [
            decide_module.NO_LINK_FOUND, decide_module.LINKED_AND_ALIVE,
            decide_module.LINKED_AND_ALIVE]
        # Decided from the table — the dedupe by task — and not rescued by the label search.
        assert [c["decision"].get("source") for c, _ in passes[1:]] == ["table", "table"]
        assert jira.anchors_on(list(jira.issues)[0]) == ["C1", "C2"]


class TestALostRegisterIsRescuedFromJira:
    """Path 5 of the per-task lock: the local register deleted between passes. Covered before the per-task lock and
    measured during it, pinned by no test. Nothing in Jira depends on the file: the label finds
    the issue again and the anchors find the comments again, in one pass, when the create step runs
    before the comment copy."""

    def test_deleting_the_register_duplicates_nothing(self, db):
        jira, wrike = Jira(), Wrike()
        create(jira, wrike, db)
        copy(jira, wrike, db)
        key = list(jira.issues)[0]

        for leftover in db.parent.glob(db.name + "*"):   # the file and any DuckDB .wal
            leftover.unlink()
        assert not db.exists()

        repaired = create(jira, wrike, db, mid="gmail-2")
        healed = copy(jira, wrike, db, mid="gmail-2")
        assert repaired["verdict"] == decide_module.FOUND_BY_LABEL_ONLY
        assert repaired["applied"] is True
        assert healed["verdict"] == decide_module.LINKED_AND_ALIVE
        assert healed["healed"] == 2
        assert healed["posted"] == 0

        assert (jira.creates, jira.posts) == (1, 2)
        assert jira.anchors_on(key) == ["C1", "C2"]
        assert links.linked_issue(TASK, db_path=db)["jira_key"] == key
        assert links.copied_comment_ids(TASK, db_path=db) == {"C1", "C2"}

    def test_the_pass_after_the_rescue_writes_nothing(self, db):
        jira, wrike = Jira(), Wrike()
        create(jira, wrike, db)
        copy(jira, wrike, db)
        for leftover in db.parent.glob(db.name + "*"):
            leftover.unlink()
        create(jira, wrike, db, mid="gmail-2")
        copy(jira, wrike, db, mid="gmail-2")
        after = (create(jira, wrike, db, mid="gmail-3"), copy(jira, wrike, db, mid="gmail-3"))
        assert after[0]["verdict"] == decide_module.LINKED_AND_ALIVE
        assert after[1]["posted"] == 0 and after[1]["healed"] == 0
        assert (jira.creates, jira.posts) == (1, 2)
