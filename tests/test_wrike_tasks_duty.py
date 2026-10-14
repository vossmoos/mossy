"""The wrike-tasks duty, offline. Nothing here reaches Gmail, Wrike, Jira or OpenAI.

The network is blocked for the whole file — the ways out, not ``socket.socket`` as a class, which
asyncio needs for its self-pipe (the lesson written in ``test_freshdesk_bugs_duty.py``).

``evaluate()`` runs the skill's scripts as subprocesses in production. Here the runner is replaced
by one that calls the **real** ``main()`` of each script in this process, with the clients each
``main()`` builds from the environment replaced by doubles. So what is exercised is the duty, the
command line it builds, and the scripts' own wiring — not a fake of them. The register is a real
DuckDB path under ``tmp_path``; the model is the conftest's fake.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import duties.wrike_tasks as duty_module  # noqa: E402

_SCRIPTS = _ROOT / "skills" / "gmail-wrike-tasks" / "scripts"


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fetch = _load("fetch_wrike_notifications")
proc = _load("process_wrike_notification")
wrike_api = _load("wrike_api")
search_mod = _load("jira_label_search")
creator_mod = _load("create_jira_for_task")
copier = _load("copy_wrike_comments")
links = _load("wrike_jira_links")

pytestmark = pytest.mark.usefixtures("fake_wrike_model")

TASK = "MAAAAAEXmain"
ME = "KUEXAMP2"
COMMENTS = [{"id": "C1", "createdDate": "2026-01-01T10:00:00Z", "authorId": "KUEXAMP1",
             "text": "uno"}]
PROCESSED = "LBL_mossy-wrike-processed"


def notification(message_id):
    return {"id": message_id, "threadId": f"t-{message_id}", "payload": {"headers": [
        {"name": "From", "value": "Anna <wrike@wrike.com>"},
        {"name": "Subject", "value": "Translate measurement requests"},
        {"name": "X-Wrike-Metadata", "value": json.dumps(
            {"taskApiId": TASK, "taskId": 1234567890, "notificationType": "Regular",
             "entityType": "Task"})},
    ]}}


# ------------------------------------------------------------------------------ doubles
class Gmail:
    def __init__(self, ids=("gmail-1",)):
        self.ids = list(ids)
        self.reads = 0
        self.writes: list[tuple[str, str]] = []

    def list_ids(self, query, max_results):
        self.reads += 1
        return self.ids[:max_results]

    def metadata_message(self, message_id):
        self.reads += 1
        return notification(message_id)

    def label_id(self, name):
        return f"LBL_{name}"

    def add_label(self, message_id, label_id):
        self.writes.append((message_id, label_id))
        return [label_id]

    def label_ids_of(self, message_id):
        return [label for m, label in self.writes if m == message_id]


class Refused(RuntimeError):
    """What Jira's writer raises on a refused create: the status travels."""

    def __init__(self, status):
        super().__init__(f"Jira answered {status}")
        self.status = status
        self.body = None


class Jira:
    """Reader, creator and comment writer, with memory."""

    def __init__(self):
        self.issues: dict[str, list[str]] = {}
        self.comments: dict[str, list[str]] = {}
        self.attempts = 0
        self.creates = 0
        self.posts = 0
        self.refuse_with = None
        self.network_down = False

    def find_issue_for_task(self, task):
        keys = [k for k, labels in self.issues.items() if f"wrike-{task}" in labels]
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
        self.attempts += 1
        if self.network_down:
            raise ConnectionError("the request never reached Jira")
        if self.refuse_with is not None:
            raise Refused(self.refuse_with)
        self.creates += 1
        key = f"PROJ-{900 + self.creates}"
        self.issues[key] = list(labels)
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
    def __init__(self):
        self.me = {"ok": True, "me_id": ME}

    def get_me(self):
        return self.me

    def get_task(self, task):
        return {"ok": True, "task_id": task, "task": {
            "title": "Translate measurement requests", "description": "<p>hi</p>",
            "responsibleIds": [ME], "permalink": "https://www.wrike.com/open.htm?id=1234567890"}}

    def get_task_comments(self, task):
        return {"ok": True, "task_id": task, "comments": list(COMMENTS), "count": len(COMMENTS)}


class InProcess:
    """Stands in for ``_run_script``: the real ``main()`` of the script, in this process."""

    def __init__(self):
        self.calls: list[tuple[str, list[str]]] = []

    async def __call__(self, script, args, timeout):
        self.calls.append((script, list(args)))
        module = {duty_module.FETCH_SCRIPT: fetch, duty_module.PROCESS_SCRIPT: proc}[script]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            module.main(list(args))
        return json.loads(out.getvalue())

    def processed(self):
        return [args for script, args in self.calls if script == duty_module.PROCESS_SCRIPT]


# ------------------------------------------------------------------------------ fixtures
@pytest.fixture(autouse=True)
def _no_outbound_calls(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("the network was reached; nothing in this file may reach it")
    monkeypatch.setattr(socket, "getaddrinfo", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    monkeypatch.setattr(socket.socket, "connect", explode)
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", explode)


@pytest.fixture(autouse=True)
def _a_fresh_memory(monkeypatch):
    """The held e-mails live at module level on purpose; each test starts with none."""
    monkeypatch.setattr(duty_module, "_HELD", {})


@pytest.fixture()
def world(monkeypatch, tmp_path):
    """Duty on, write switch absent, every client a double, the register a path not yet made."""
    for name in ("WRIKE_TASKS_WRITE_TO_JIRA", "WRIKE_TASKS_INTERVAL_MINUTES",
                 "WRIKE_TASKS_MAX_EMAILS", "WRIKE_TASKS_PROCESSED_LABEL",
                 "WRIKE_TASKS_SKIPPED_LABEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WRIKE_TASKS_ENABLED", "true")
    monkeypatch.setenv("WRIKE_TASKS_ALLOWED_SENDERS", "wrike@wrike.com")
    register = tmp_path / "register" / "wrike-jira-links.duckdb"
    monkeypatch.setenv("WRIKE_JIRA_LINKS_DB", str(register))

    w = type("World", (), {})()
    w.gmail, w.jira, w.wrike, w.runner = Gmail(), Jira(), Wrike(), InProcess()
    w.register = register
    w.built: list[str] = []

    def writer(label):
        def build(cls):
            w.built.append(label)
            return w.jira
        return classmethod(build)

    monkeypatch.setattr(fetch.GmailFetchClient, "from_env", classmethod(lambda cls: w.gmail))
    monkeypatch.setattr(wrike_api.WrikeClient, "from_env", classmethod(lambda cls: w.wrike))
    monkeypatch.setattr(search_mod.JiraLabelSearch, "from_env", classmethod(lambda cls: w.jira))
    monkeypatch.setattr(creator_mod.JiraIssueCreator, "from_env", writer("creator"))
    monkeypatch.setattr(copier.JiraCommentWriter, "from_env", writer("writer"))
    monkeypatch.setattr(duty_module, "_run_script", w.runner)
    return w


def one_pass():
    """A new instance each time, as ``mossy.duties.get_duty`` builds one per evaluate."""
    return asyncio.run(duty_module.WrikeTasks().evaluate(
        {"duty": duty_module.WrikeTasks.name, "window": "2026-09-14T10:00"}, None))


def _at(minute, second=0):
    return datetime(2026, 9, 14, 10, minute, second, tzinfo=timezone.utc)


# ------------------------------------------------------------------------------ check()
class TestCheckNeverTouchesTheNetwork:
    """It runs on every tick. Proven with the network blocked, not by reading the code."""

    def test_the_block_is_real(self):
        with pytest.raises(AssertionError):
            socket.getaddrinfo("gmail.googleapis.com", 443)

    def test_it_enqueues_on_the_interval_without_any_call(self, world):
        out = asyncio.run(duty_module.WrikeTasks().check(_at(10)))
        assert len(out) == 1
        assert out[0].kind == "evaluate_duty"
        assert out[0].dedupe_key == "wrike_tasks:2026-09-14T10:10"

    def test_it_is_silent_off_the_interval_and_late_in_the_minute(self, world):
        assert asyncio.run(duty_module.WrikeTasks().check(_at(11))) == []
        assert asyncio.run(duty_module.WrikeTasks().check(_at(10, 30))) == []

    def test_check_runs_no_script(self, world):
        asyncio.run(duty_module.WrikeTasks().check(_at(10)))
        assert world.runner.calls == [] and world.gmail.reads == 0


# ------------------------------------------------------------------------------ switch 1
class TestDisabledIsZeroOfEverything:
    @pytest.mark.parametrize("value", [None, "false", "1", "yes", ""])
    def test_check_never_fires(self, world, monkeypatch, value):
        if value is None:
            monkeypatch.delenv("WRIKE_TASKS_ENABLED", raising=False)
        else:
            monkeypatch.setenv("WRIKE_TASKS_ENABLED", value)
        assert all(asyncio.run(duty_module.WrikeTasks().check(_at(m))) == [] for m in range(60))

    def test_evaluate_does_nothing_either(self, world, monkeypatch, fake_wrike_model):
        """A pass already queued when the duty was switched off must not run."""
        monkeypatch.setenv("WRIKE_TASKS_ENABLED", "false")
        assert one_pass() == []
        assert world.runner.calls == []
        assert world.gmail.reads == 0 and world.gmail.writes == []
        assert (world.jira.attempts, world.jira.posts) == (0, 0)
        assert fake_wrike_model.calls == []
        assert not world.register.exists()


# ------------------------------------------------------------------------------ switch 2
class TestTheWriteSwitchIsTheDutys:
    @pytest.mark.parametrize("value", [None, "false", "1", "yes", ""])
    def test_it_is_off_even_with_the_duty_on(self, world, monkeypatch, value):
        if value is not None:
            monkeypatch.setenv("WRIKE_TASKS_WRITE_TO_JIRA", value)
        assert duty_module._enabled() is True
        assert duty_module._write_to_jira() is False

    def test_test_mode_writes_nothing_in_jira_the_register_or_gmail(self, world):
        one_pass()
        one_pass()
        assert world.runner.processed(), "the e-mail was run"
        assert all("--apply" not in args for args in world.runner.processed())
        assert world.built == [], "no write client was even built"
        assert (world.jira.attempts, world.jira.creates, world.jira.posts) == (0, 0, 0)
        assert not world.register.exists(), "a describe must not create the register"
        assert not world.register.parent.exists()
        assert world.gmail.writes == [], "not even a label"

    def test_with_the_switch_on_it_does_write(self, world, monkeypatch):
        """The inversion: the same pass with the switch on writes all three."""
        monkeypatch.setenv("WRIKE_TASKS_WRITE_TO_JIRA", "true")
        one_pass()
        assert all("--apply" in args for args in world.runner.processed())
        assert world.jira.creates == 1
        assert links.linked_issue(TASK, db_path=world.register)["jira_key"] == "PROJ-901"
        assert world.gmail.writes == [("gmail-1", PROCESSED)]


# ------------------------------------------------------------------------------ the model loop
class TestTheModelLoopIsCut:
    def test_test_mode_pays_the_model_once_per_email_not_per_pass(self, world, fake_wrike_model):
        """Measured before the duty: three passes, three model calls, for one e-mail."""
        for _ in range(3):
            one_pass()
        assert len(fake_wrike_model.calls) == 1
        assert len(world.runner.processed()) == 1
        assert duty_module._HELD["gmail-1"]["verdict"] == duty_module.DESCRIBED

    def test_a_model_that_keeps_failing_is_held_with_writing_on(self, world, monkeypatch,
                                                                fake_wrike_model):
        monkeypatch.setenv("WRIKE_TASKS_WRITE_TO_JIRA", "true")
        fake_wrike_model.reply = "not json"
        one_pass()
        one_pass()
        assert len(fake_wrike_model.calls) == 1
        assert world.jira.attempts == 0
        assert duty_module._HELD["gmail-1"]["reason"] == "analysis_failed"

    def test_other_emails_still_run_past_a_held_one(self, world):
        one_pass()
        world.gmail.ids.append("gmail-2")
        one_pass()
        assert [args[1] for args in world.runner.processed()] == ["gmail-1", "gmail-2"]


# ------------------------------------------------------------------------------ create_failed
def _refused(status):
    return {"ok": False, "reason": proc.WRITE_STOPPED, "write_reason": "create_failed",
            "create": {"reason": "create_failed", "status": status,
                       "would_create": {"analysis": {"ok": True}}}}


class TestARefusedCreate:
    """5xx and 429 are passing and retried; anything else is held. A passing failure held until
    a restart is a task that is never created, which is worse than one summary too many."""

    @pytest.mark.parametrize("status", [500, 502, 503, 429, None])
    def test_a_passing_refusal_is_retried(self, status):
        seen = duty_module.classify(_refused(status), write=True)
        assert seen["verdict"] == duty_module.RETRY and seen["hold"] is False

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_any_other_refusal_is_held(self, status):
        seen = duty_module.classify(_refused(status), write=True)
        assert seen["verdict"] == duty_module.HOLD and seen["hold"] is True

    def test_a_503_is_tried_again_by_the_next_pass(self, world, monkeypatch, fake_wrike_model):
        monkeypatch.setenv("WRIKE_TASKS_WRITE_TO_JIRA", "true")
        world.jira.refuse_with = 503
        one_pass()
        one_pass()
        assert world.jira.attempts == 2
        assert len(fake_wrike_model.calls) == 2, "the declared price: the summary is paid again"
        assert "gmail-1" not in duty_module._HELD

    def test_a_create_that_never_reached_jira_is_tried_again(self, world, monkeypatch):
        """No status: the network was down before Jira could decide anything."""
        monkeypatch.setenv("WRIKE_TASKS_WRITE_TO_JIRA", "true")
        world.jira.network_down = True
        one_pass()
        one_pass()
        assert world.jira.attempts == 2
        assert "gmail-1" not in duty_module._HELD

    def test_a_400_is_not(self, world, monkeypatch):
        monkeypatch.setenv("WRIKE_TASKS_WRITE_TO_JIRA", "true")
        world.jira.refuse_with = 400
        one_pass()
        one_pass()
        assert world.jira.attempts == 1
        assert duty_module._HELD["gmail-1"]["status"] == 400


# ------------------------------------------------------------------------------ wrapped reasons
class TestAReasonInsideLabelFailedStillCounts:
    """``process`` wraps every label failure as ``label_failed``, with the label's own reason under
    ``label``. ``label_not_found`` was listed as stopping the pass and could never match."""

    @staticmethod
    def _label_failed(inner):
        return {"ok": False, "reason": proc.LABEL_FAILED, "outcome": "created",
                "label": {"ok": False, "reason": inner}}

    def test_a_missing_label_stops_the_pass(self):
        seen = duty_module.classify(self._label_failed("label_not_found"), write=True)
        assert seen["verdict"] == duty_module.STOP and seen["reason"] == "label_not_found"

    def test_a_label_that_did_not_confirm_is_only_retried(self):
        seen = duty_module.classify(self._label_failed("label_not_confirmed"), write=True)
        assert seen["verdict"] == duty_module.RETRY

    def test_end_to_end_a_missing_label_ends_the_pass_after_the_first_email(self, world, monkeypatch):
        monkeypatch.setenv("WRIKE_TASKS_WRITE_TO_JIRA", "true")
        world.gmail.ids = ["gmail-1", "gmail-2", "gmail-3"]
        monkeypatch.setattr(world.gmail, "label_id", lambda name: None)
        one_pass()
        assert [args[1] for args in world.runner.processed()] == ["gmail-1"]


# ------------------------------------------------------------------------------ a person
class TestWhatAPersonMustSee:
    def test_summarised_comments_not_recorded_is_held_never_retried(self):
        """Running it again would post the summarised comments as duplicates."""
        out = {"ok": False, "reason": proc.WRITE_STOPPED,
               "write_reason": "summarised_comments_not_recorded",
               "create": {"created": {"issue_key": "PROJ-901"},
                          "would_create": {"analysis": {"ok": True}}}}
        seen = duty_module.classify(out, write=True)
        assert seen["verdict"] == duty_module.HOLD and seen["hold"] is True

    def test_a_failure_every_email_would_hit_ends_the_pass(self, world):
        world.gmail.ids = ["gmail-1", "gmail-2", "gmail-3"]
        world.wrike.me = {"ok": False, "reason": wrike_api.ME_UNRESOLVED, "rows": 0}
        one_pass()
        assert [args[1] for args in world.runner.processed()] == ["gmail-1"]
