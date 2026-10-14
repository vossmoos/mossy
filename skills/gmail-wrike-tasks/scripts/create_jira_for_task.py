# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python. Without one it falls back to sys.executable.
"""Create the Jira issue for a Wrike task. **The first thing in this flow that writes.**

``decide_wrike_action`` describes what already exists and deliberately recommends
nothing; this module is where the decision to create lives, and where the only write in the
chain happens.

★ IT WRITES NOTHING WITHOUT ``--apply``. The default is to describe the issue it would create
and stop. That is the rule a customer-facing reply gate follows, and it
is here for the same reason: a write that goes out because nobody thought about it is a write
nobody can take back. The flag is passed by hand, one task at a time.

★★ NOTHING CREATED HERE CAN BE DELETED. This Jira account has no delete permission: a surplus
issue is renamed, never removed. So a duplicate is not noise that gets cleaned up later — it is
noise that stays, in the project, forever. That is what raises the cost of creating on a bad
token from "a wasted call" to "permanent litter", and it is why the conditions below are as
strict as they are. A permanent test issue exists because of the measurement that proved labels work
on create; it is not going anywhere either.

WHAT IT CREATES ON, AND WHAT IT REFUSES

Of the eleven verdicts, exactly one leads to a new issue:

    no_link_found        create — and only with authentication proven, see below
    found_by_label_only  repair the local row. Jira already has the issue; no write there
    not_a_notification   nothing to do
    linked_and_alive     nothing to create; commenting is the comment copy
    not_configured, link_unconfirmable, link_disagrees, label_on_wrong_issue,
    ambiguous_match, jira_error, register_unreadable       stop, and say why

``link_unconfirmable`` is the one worth naming twice: the table has a row and Jira answered 404,
which an unauthenticated client is also served. Creating there would open a duplicate for a task
that already has an issue — permanently, per the paragraph above.

THE CANARY, AND THE UNCERTAINTY THAT SURVIVES IT

``no_link_found`` is empty for three reasons at once: the task may have no issue; the credentials
may not authenticate; or no issue carries a label yet. The first two are settled by asking a
question that must return rows — ``credentials_proven`` — and refusing to create unless it does.

The third is not settled, and saying so is part of the job: **an issue created before labels
existed is invisible to the search.** If the local register was lost, that task will get a second
issue, and nobody can delete the first. Nothing here can prevent it; it shrinks to nothing as
this module labels what it creates.

★★★ THE WRITE PATH HAS RUN ONCE FOR REAL — THE CREATE, AND NOTHING MORE. Declared, because the
distance between "tested" and "exercised" is exactly where this flow keeps finding defects.

What **is** verified:

* The ``POST`` itself, separately, with curl: 201, a test issue created, the label came back on
  the issue, and the label search found it. That is what proved labels work inside the create,
  and it is the first time the label search returned a row rather than an empty page.
* ``JiraIssueCreator.create_issue`` has ten tests against a fake transport that pin the exact
  body Jira would receive — the project, the summary, the ADF description, and the labels. They
  exist because a mutation moving ``labels`` out of that body left forty-three tests green:
  every one injected a double and asserted the labels reached *it*, which is the trap of a double standing in for the real client.

* **The create path with ``--apply``, once, by hand**: ``process_wrike_notification.py --apply``
  on one real e-mail created one real issue.

What is **not** verified: every other write. The copy onto an issue that exists, the repair branch
and the duty with writing on have never run for real. There is nowhere to rehearse them — Jira
does not allow deleting, so every attempt leaves a permanent issue in the client's real project,
and Wrike is the client's, so no throwaway task may be created there either. So their first real run, like the create's, is against work that matters, by a
person, one task at a time, which is the whole reason ``--apply`` exists and is passed by hand.

**A permanent test issue stays in the project as the witness of that measurement.** It cannot be deleted, and it
is the evidence that the POST shape in this module is the one Jira accepted.

THE ORDER, AND WHAT A CRASH LEAVES BEHIND

Measured against the live Jira before this was written: ``POST /issue`` accepts ``labels`` in the
same body — 201, on the test issue — the issue comes back carrying the label, and the label search finds it.
So this is **one write, not two**:

    1. POST /issue, with the label inside the same body
    2. GET  /issue/{key}      — read-back, confirmation
    3. remember_link(...)     — the local row

**The label inside the create is not an optimisation; it is what makes a half-finished run
recoverable.** Crash after step 1 and the issue exists *with its label*, so the next pass finds
it by search, reports ``found_by_label_only``, repairs the row and creates nothing. Were the
label a second call, a crash between creating and labelling would leave an issue that neither
the table nor the search knows — invisible for ever, one more on every pass, none of them
deletable.

THE SUMMARY IS EVALUATED BY A MODEL (the Wrike → Jira flow); THE ISSUE LINKS TO THE TASK, IT DOES NOT COPY IT

This used to say "No model" and title the issue with the Wrike title as-is. That was a decision of
ours against the ticket's own words — *"read the ticket and all comments and create task in Jira
project with some summary evaluated (the same as for Freshdesk+Email)"* — and it is revoked. The
create now reads the task **and all its comments** (it read none before: a second defect against
the same sentence), and ``analyze_wrike_task`` evaluates ``summary`` and ``problem`` from all of
it.

The description is assembled here, not by the model: the model's ``problem``, then the link back
to the Wrike task. **The original Wrike description is not copied into the issue** — removed on 14
September 2026, on purpose. It carried the end customer's personal data — name, street, mobile,
e-mail — out of the client's Wrike into an issue on another organisation's Jira,
where it cannot be deleted. And it added nothing the link does not: the copy was there so
that the model could not be the only source, and the link gives the same guarantee — whoever needs
the detail opens the task, where the data lives. So **an issue with no link is not created**
(``no_wrike_link``): without it the issue would hold only what the model wrote.

Declared, not changed here: the task still reaches Jira through the model's ``summary`` and
``problem``, which are evaluated from the description and the comments and can repeat what they
say, and through the text of every comment ``copy_wrike_comments`` copies.

**If the evaluation fails, nothing is created** — ``analysis_failed``, and the e-mail stays
unhandled for the next pass. There is no fallback to the Wrike title: an issue that cannot be
deleted may not come out in two shapes depending on whether the model answered. The same holds for
the comments: if they cannot be read, ``comments_unreadable``, because a summary evaluated without
them would be that second shape.

**A dry run calls the model too.** The proposal is built before ``apply`` is looked at, so a
hand-run check shows the summary before writing is ever enabled — which is what it is for. The
duty that will run this must not inherit it silently: in dry mode it labels nothing, sees the same
e-mails again, and would call the model per e-mail and per pass, indefinitely.

Freshdesk's analyser is neither imported nor touched: skills do not import each other, and its
prompt is about a different kind of work.

LOGGING: NONE IN THE SCRIPTS, AND THAT IS A DECISION

Nothing in this skill's scripts logs, and that is not an oversight. **Here the envelope IS the record**:
every run returns a dict whose ``reason`` says what happened and why. That is this flow's
convention — the Freshdesk scripts print the same kind of envelope and log nothing either — and a
failure that does not reach the envelope is the defect, whatever a log line might have said.

A log line would not reach anybody anyway. Mossy configures no handlers anywhere in the
repository — no ``basicConfig``, no ``addHandler`` — so Python falls back to its last-resort
handler: WARNING and above reach stderr, and **an INFO is dropped**. That was measured by running
it, not read from the documentation.

The alert to the operator — "the Jira token has expired, nothing is being created" — belongs to
the process that reads Gmail and runs this module. That process is the duty,
``duties/wrike_tasks.py``, and it has never been started. It turns each ``reason`` into a log line:
a WARNING when a person should look, which reaches stderr, and an INFO otherwise, which by the
paragraph above is dropped. Nothing sends a message anybody receives. Log lines added here too
would be more text nobody reads, and would look as though the problem had been handled.

Self-contained: no ``import mossy``, no ``python-dotenv``, its own urllib client for the one
write. ``jira_label_search`` stays read-only — it has no method parameter and never gained one.
"""

from __future__ import annotations

import base64
import html as _html
import importlib.util
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent


def _sibling(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ACTION_CREATE = "create_issue"
ACTION_REPAIR_LINK = "repair_link"
ACTION_NOTHING = "nothing_to_do"
ACTION_STOP = "stop"

#: Said wherever a caller could be tempted to retry into existence.
NOTHING_CAN_BE_DELETED = (
    "This Jira account has no delete permission. Anything created here is permanent — a surplus "
    "issue is renamed, never removed — so a duplicate is not noise that gets cleaned up, it is "
    "noise that stays."
)

#: The uncertainty the canary does not remove.
PRE_LABEL_ISSUES_ARE_INVISIBLE = (
    "An issue created before this module started labelling is invisible to the label search. If "
    "the local register was lost, such a task gets a second issue and the first cannot be "
    "deleted. This shrinks to nothing as labelled issues replace unlabelled ones."
)

_DECIDE = None


def _decide_module():
    global _DECIDE
    if _DECIDE is None:
        _DECIDE = _sibling("decide_wrike_action")
    return _DECIDE


class JiraWriteError(RuntimeError):
    """A refused or unreachable write. Carries Jira's status and body, and names the status in
    its message — the way ``JiraSearchError``, ``JiraCommentError`` and ``WrikeError`` already
    did. A 401 on ``POST /issue`` used to reach the envelope as "Jira refused the issue."
    and nothing else: measured against the real client."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} — HTTP {self.status}" if self.status else base


class JiraIssueCreator:
    """One write: ``POST /issue``. Nothing else, and no other verb in the class."""

    def __init__(self, site: str, email: str, api_token: str, space_name: str,
                 *, timeout_seconds: int = 30) -> None:
        self.site = _normalize_site(site)
        self.email = email
        self.api_token = api_token
        self.space_name = space_name
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> "JiraIssueCreator | None":
        site = _normalize_site(os.environ.get("JIRA_SITE") or "")
        email = (os.environ.get("JIRA_USER_EMAIL") or "").strip()
        token = (os.environ.get("JIRA_API_KEY") or "").strip()
        space = (os.environ.get("JIRA_SPACE_NAME") or "").strip()
        if not site or not email or not token or not space:
            return None
        return cls(site, email, token, space)

    def create_issue(self, summary: str, description: str, labels: list[str],
                     issue_type: str = "Task") -> dict[str, Any]:
        """POST the issue **with its labels in the same body**. Measured: 201, and the label
        comes back on the issue.

        Returns the created key and url. Raises ``JiraWriteError`` on anything but 200/201 —
        a refused create must never look like a successful one.
        """
        fields: dict[str, Any] = {
            "project": {"key": self.space_name},
            "summary": summary,
            "issuetype": {"name": issue_type},
            "labels": list(labels),
        }
        if description:
            fields["description"] = _adf(description)
        status, body = self._post("/issue", {"fields": fields})
        if status not in (200, 201) or not isinstance(body, dict) or not body.get("key"):
            raise JiraWriteError("Jira refused the issue.", status=status, body=body)
        key = str(body["key"])
        return {"issue_key": key, "issue_url": f"https://{self.site}/browse/{key}",
                "status": status}

    def _post(self, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
        url = f"https://{self.site}/rest/api/3{path}"
        token = base64.b64encode(f"{self.email}:{self.api_token}".encode()).decode("ascii")
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Basic {token}", "Content-Type": "application/json",
                     "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return response.getcode() or 200, _decode(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _decode(exc.read() if exc.fp else b"")
        except urllib.error.URLError as exc:
            raise JiraWriteError(f"Jira is unreachable: {exc.reason}") from exc


def plan(message: dict[str, Any], *, search: Any = None, wrike: Any = None,
         db_path: Path | str | None = None) -> dict[str, Any]:
    """What would happen, without doing any of it. Reads only."""
    decide_module = _decide_module()
    verdict = decide_module.decide(message, search=search, wrike=wrike, db_path=db_path)
    name = verdict["verdict"]

    if name == decide_module.NO_LINK_FOUND:
        action = ACTION_CREATE
    elif name == decide_module.FOUND_BY_LABEL_ONLY:
        action = ACTION_REPAIR_LINK
    elif name in (decide_module.NOT_A_NOTIFICATION, decide_module.LINKED_AND_ALIVE):
        action = ACTION_NOTHING
    else:
        action = ACTION_STOP

    return {"action": action, "verdict": name, "task_api_id": verdict.get("task_api_id"),
            "decision": verdict, "applied": False,
            "note": NOTHING_CAN_BE_DELETED if action == ACTION_CREATE else None}


def run(message: dict[str, Any], *, apply: bool = False, search: Any = None, wrike: Any = None,
        creator: Any = None, db_path: Path | str | None = None,
        lock_dir: Path | str | None = None) -> dict[str, Any]:
    """Describe, and write only when ``apply`` is true.

    ``apply`` defaults to False and is the only thing that lets a write happen. Without it not
    one write call is made — not to Jira, not to the register.

    **One pass at a time over one task** (the per-task lock). When ``apply`` is set, the whole of this —
    the deciding as well as the writing — happens inside a per-task lock, because the race is
    the gap between the two: two passes that both search and find nothing both create, and
    neither issue can be deleted. Measured at two issues for one task before the lock existed.
    The lock covers this machine only; see ``task_lock.ONE_MACHINE_ONLY``.

    Without ``apply`` no lock is taken: describing writes nothing and may overlap freely.
    """
    if not apply:
        return _run_unlocked(message, apply=False, search=search, wrike=wrike, creator=creator,
                             db_path=db_path)
    locking = _sibling("task_lock")
    task_hint = (_sibling("parse_wrike_notification").parse(message) or {}).get("task_api_id")
    return locking.locked_run(
        task_hint,
        lambda: _run_unlocked(message, apply=True, search=search, wrike=wrike, creator=creator,
                              db_path=db_path),
        lock_dir=lock_dir, busy_base={"action": ACTION_STOP, "created": None},
    )


def _run_unlocked(message: dict[str, Any], *, apply: bool = False, search: Any = None,
                  wrike: Any = None, creator: Any = None,
                  db_path: Path | str | None = None) -> dict[str, Any]:
    """The pass itself. Called by ``run`` inside the lock; never call it directly to write."""
    links = _sibling("wrike_jira_links")
    result = plan(message, search=search, wrike=wrike, db_path=db_path)
    action, decision = result["action"], result["decision"]
    task = result["task_api_id"]

    if action == ACTION_CREATE:
        result["would_create"] = _proposed_issue(task, message, wrike)
        result["uncertainty"] = PRE_LABEL_ISSUES_ARE_INVISIBLE

    if not apply:
        result["detail"] = "Nothing was written. Pass --apply to create this."
        return result

    if action == ACTION_REPAIR_LINK:
        stored = links.remember_link(task, decision["issue_key"], decision.get("issue_url"),
                                     db_path=db_path)
        result["applied"] = bool(stored.get("ok"))
        result["repaired"] = stored
        return result

    if action != ACTION_CREATE:
        return result

    if creator is None:
        result["reason"] = "no_creator"
        result["detail"] = "Jira write credentials are not configured; nothing was created."
        return result

    # The canary. Refuse to create unless the credentials have been proven by a question that
    # must return rows — because a create on a bad token is permanent litter.
    try:
        proof = search.credentials_proven()
    except Exception as exc:  # noqa: BLE001 - a check that failed is not a check that passed
        # This used to escape as an exception: measured with the network dropped here.
        result["reason"] = "credentials_check_failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["detail"] = (
            "Nothing was created: the credential check itself failed, so the credentials are "
            "unknown — and unknown is not proven. " + NOTHING_CAN_BE_DELETED
        )
        return result
    if not proof.get("proven"):
        result["reason"] = "authentication_not_proven"
        result["credentials"] = proof
        result["detail"] = (
            "Nothing was created: the credentials could not be proven, and an unauthenticated "
            "client is served an empty search. " + NOTHING_CAN_BE_DELETED
        )
        return result

    proposed = result["would_create"]
    if proposed.get("source_error"):
        # Checked before no_summary on purpose. A Wrike 401, 403 or dropped network
        # leaves the title empty too, and "the task carried no title" would send whoever reads
        # it to Wrike instead of to the credentials. The title is unknown, not missing.
        result["reason"] = "wrike_unreadable"
        result["error"] = str(proposed["source_error"])
        result["detail"] = (
            "Wrike did not answer the read of this task, so its title is unknown — not missing. "
            "Check the Wrike token and this account's licence: a 401 is a token Wrike refused, a "
            "403 a read the licence does not allow, and a 200 with no task is a task this "
            "licence is not shown. Nothing was created. " + NOTHING_CAN_BE_DELETED
        )
        return result
    if proposed.get("comments_error"):
        # the Wrike → Jira flow asks for the summary to be evaluated from the task AND all its comments. Without
        # them it would be evaluated from less, and the issue would come out in a second shape.
        result["reason"] = "comments_unreadable"
        result["error"] = str(proposed["comments_error"])
        result["detail"] = (
            "The task was read but its comments could not be, so the summary cannot be "
            "evaluated from the whole task. Nothing was created. " + NOTHING_CAN_BE_DELETED
        )
        return result
    if proposed.get("summary_missing"):
        # Never a placeholder. An issue with no title would be permanent litter that somebody
        # has to find by eye and cannot delete, and the Wrike task not having a title is a fact
        # worth reporting rather than papering over.
        result["reason"] = "no_summary"
        result["detail"] = (
            "The Wrike task carried no title, so there is nothing to name the issue. Nothing was "
            "created. " + NOTHING_CAN_BE_DELETED
        )
        return result
    analysis = proposed.get("analysis") or {}
    if not analysis.get("ok") or not proposed["summary"].strip():
        # Decision 3: stop. Not no_summary — the task HAS a title; what failed is the model, and
        # saying otherwise would send whoever reads it to the wrong place (the lesson of naming what failed).
        result["reason"] = "analysis_failed"
        result["analysis"] = analysis
        result["detail"] = (
            f"The summary could not be evaluated ({analysis.get('reason') or 'not attempted'}), "
            "so nothing was created. There is no fallback to the Wrike title: an issue that "
            "cannot be deleted may not come out in two shapes depending on whether the model "
            "answered. The next pass tries again. " + NOTHING_CAN_BE_DELETED
        )
        return result
    if not proposed.get("permalink"):
        # The link is what replaced the original description. Without it the issue would hold
        # only what the model wrote, with no way back to the source.
        result["reason"] = "no_wrike_link"
        result["detail"] = (
            "The issue would carry no link back to the Wrike task, and the link is what stands "
            "in for the original description: without it the issue holds only what the model "
            "wrote. Nothing was created. " + NOTHING_CAN_BE_DELETED
        )
        return result
    try:
        created = creator.create_issue(proposed["summary"], proposed["description"],
                                       proposed["labels"])
    except Exception as exc:  # noqa: BLE001 - a refused create is never a created issue
        result["reason"] = "create_failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        # What Jira said, kept. None when the request never reached Jira: a status is
        # never invented for a network failure.
        result["status"] = getattr(exc, "status", None)
        result["body"] = getattr(exc, "body", None)
        return result

    result["created"] = created
    # Read-back before the row is written, the same gate the Freshdesk flow uses. A row pointing
    # at an issue that was not really created would send every future pass into the void.
    try:
        confirmed = search.issue_exists(created["issue_key"])
    except Exception as exc:  # noqa: BLE001 - the write happened; the answer must say so
        # ★ The one failure in this flow that comes AFTER a write. The issue exists in
        # Jira and cannot be deleted; this used to give the caller an exception and no
        # ``created`` — the report of the only write this pass made, lost.
        result["reason"] = "read_back_failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["detail"] = (
            f"The issue {created['issue_key']} was created, and the read-back that confirms it "
            "failed. It exists in Jira and cannot be deleted. No local row was written. The "
            "label was sent inside the create, so the next pass finds it by search rather than "
            "creating a second one — provided the label landed, which nothing re-read."
        )
        return result
    result["confirmed"] = confirmed
    if confirmed.get("exists") is not True:
        result["reason"] = "not_confirmed_on_read_back"
        result["detail"] = (
            "The issue was not confirmed, so no local row was written. The label was sent inside "
            "the create, so the next pass finds it by search rather than creating a second one — "
            "provided the label landed: the read-back asks for the key, not the label."
        )
        return result

    stored = links.remember_link(task, created["issue_key"], created["issue_url"],
                                 db_path=db_path)
    result["stored"] = stored
    result["applied"] = True

    # The comments the summary was evaluated from are recorded as copied. They are NOT in the
    # description — that is the model's problem and the link — they reach the issue only through
    # what the model wrote. Recording them is the decision that they are not posted afterwards as
    # loose comments: without it the next notification posted every one of them, measured 14
    # September 2026 on the 29-comment task, 0 recorded after the create and 29 posted by the next
    # pass. Unlike the link, a missing row here is rescued by nothing — no comment was posted, so
    # none carries an anchor — so a failure is reported, not only kept.
    ids = proposed.get("summarised_comment_ids") or []
    unrecorded = [cid for cid in ids
                  if not links.remember_comment(task, cid, db_path=db_path).get("ok")]
    result["summarised_comments"] = {"count": len(ids), "recorded": len(ids) - len(unrecorded),
                                     "unrecorded": unrecorded}
    if unrecorded:
        result["reason"] = "summarised_comments_not_recorded"
        result["detail"] = (
            f"The issue {created['issue_key']} was created, and {len(unrecorded)} of the "
            f"{len(ids)} comments its summary was evaluated from could not be recorded as copied. "
            "The next pass will post those as comments; nothing in Jira rescues them."
        )
    return result


def _proposed_issue(task_api_id: str, message: dict[str, Any], wrike: Any) -> dict[str, Any]:
    """The issue to create: the summary evaluated by a model, and a link back to the task (the Wrike → Jira flow).

    Reads the task **and all its comments** — *"read the ticket and all comments"* — and asks
    ``analyze_wrike_task`` for ``summary`` and ``problem``. The description is assembled here: the
    model's ``problem``, then the link back. The original description is read and given to the
    model, and never copied into the issue — see the module docstring.

    What it records instead of guessing: ``source_error`` when the task could not be read,
    ``comments_error`` when its comments could not be, ``summary_missing`` when the task has no
    title, and ``analysis`` — the model's envelope, successful or not. The caller decides; this
    never falls back to the Wrike title.

    The permalink comes from the notification's own header rather than from the verdict: the
    verdict only carries the parsed notification when it rejected it, so reading it from there
    silently produced an issue with no link back to Wrike — measured, and the reason this
    parses the message again. Parsing is pure and costs nothing.
    """
    search_mod = _sibling("jira_label_search")
    parser = _sibling("parse_wrike_notification")
    proposed: dict[str, Any] = {
        "summary": "", "description": "", "labels": [search_mod.task_label(task_api_id)],
        "wrike_title": "",
    }
    permalink = None
    seen = parser.parse(message)
    permalink_id = seen.get("permalink_id")
    if permalink_id:
        permalink = f"https://www.wrike.com/open.htm?id={permalink_id}"

    title, original = "", ""
    if wrike is not None:
        try:
            answer = wrike.get_task(task_api_id)
        except Exception as exc:  # noqa: BLE001
            proposed["source_error"] = f"{type(exc).__name__}: {exc}"
            answer = None
        if isinstance(answer, dict) and answer.get("ok"):
            task = answer.get("task") or {}
            title = str(task.get("title") or "").strip()
            original = _plain(str(task.get("description") or ""))
            if not permalink and task.get("permalink"):
                permalink = str(task["permalink"])
        elif isinstance(answer, dict):
            proposed["source_error"] = answer.get("reason") or "wrike_refused"
    proposed["wrike_title"] = title

    problem = ""
    if title and "source_error" not in proposed:
        # the Wrike → Jira flow: "all comments". Read only when there is a task to summarise; a failed read
        # is recorded, never replaced by an empty list — a summary evaluated without the
        # comments would be a second shape of the same undeletable issue.
        comments = None
        try:
            listed = wrike.get_task_comments(task_api_id)
        except Exception as exc:  # noqa: BLE001
            proposed["comments_error"] = f"{type(exc).__name__}: {exc}"
            listed = None
        if isinstance(listed, dict) and listed.get("ok"):
            comments = [c for c in (listed.get("comments") or []) if isinstance(c, dict)]
        elif "comments_error" not in proposed:
            proposed["comments_error"] = (
                (listed.get("reason") if isinstance(listed, dict) else None) or "wrike_refused")
        if comments is not None:
            # Every comment read goes to the model (``comments_used``), so every id read is in
            # the summary — kept so a confirmed create can record them as copied.
            proposed["summarised_comment_ids"] = [
                str(c.get("id") or "").strip() for c in comments if str(c.get("id") or "").strip()]
            analysis = _sibling("analyze_wrike_task").analyze(title, original, comments)
            proposed["analysis"] = analysis
            if analysis.get("ok"):
                proposed["summary"] = analysis["summary"]
                problem = analysis["problem"]

    # The model's problem and the link back, and never the original description: it carried the
    # end customer's personal data into another organisation's Jira, where it cannot be deleted,
    # and the link already gives what the copy was for. See the module docstring.
    parts = [problem] if problem else []
    if permalink:
        parts.append(f"Wrike: {permalink}")
        proposed["permalink"] = permalink
    proposed["description"] = "\n\n".join(parts)
    if not title:
        # Never invented. An empty title is reported and stops the create rather than becoming
        # a placeholder somebody has to find later — and cannot delete.
        proposed["summary_missing"] = True
    return proposed


#: Tags that end a line. Dropping a tag without putting a break in its place runs the next
#: element onto the end of the previous one — measured on the six real task descriptions, where
#: it produced "TaskCreate a concept for data extraction and storageBuild the identification
#: prompt…", a checklist of nine items rendered as one unreadable sentence. Wrike's own
#: ``briefDescription`` has the same defect ("BackgroundWe receive standard PDF documents…"),
#: which is a good reason not to copy it.
_BLOCK_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "tr", "ul", "ol", "blockquote",
               "table", "section", "article")

#: Checkbox state, kept. A checklist item whose ``checked="checked"`` was dropped would read as
#: outstanding work that is already done, or — worse, and this is the direction that matters —
#: as done work that is not. Six of the nine items on the main task are ticked; losing that
#: turns a progress report into a fiction, permanently, in an issue nobody can delete.
_CHECKED = "[x] "
_UNCHECKED = "[ ] "


def _plain(html_text: str) -> str:
    """Wrike descriptions are HTML. Structure becomes line breaks; nothing is rewritten.

    Measured against the six real task descriptions in
    ``tests/fixtures/wrike_task_descriptions.json`` — not against invented markup. Between them
    they carry headings, checklists with ticked and unticked boxes, ordered lists, links whose
    text is sometimes the URL and sometimes a title, HTML entities (``&#34;``, ``&#61;``,
    ``&#64;``, ``&amp;``), inline ``<span>`` styling, HTML comments used as separators, an empty
    ``<h2></h2>`` and an empty checklist item.

    What it does, and why each part is there:

    * **Block tags become breaks.** The defect this replaces stripped every tag and left the
      pieces glued together.
    * **List items become ``- `` lines**, so a checklist reads as a checklist, and
      **``<ol>`` items keep their numbers** — an ordered list carries an order, and dashes
      throw it away.
    * **Checkboxes become ``[x]``/``[ ]``**, because their state is information.
    * **A link keeps its text, and its URL when the text is not already the URL.** Losing an
      address in an issue that cannot be deleted is worse than a slightly longer line.
    * **Comments go first.** Measured: the final tag sweep already removes a plain ``<!-- -->``,
      so for the six this line changes nothing. It is here for a comment containing ``>``, which
      the sweep would cut in the middle and leave ``-->`` visible in the issue. None of the six
      has one; it is a reachable case, not a measured one, and it is pinned as such.

    **Glued words are not this converter's — measured three times; do not measure it a fourth.**
    Two run-together words were seen in one of the six descriptions.
    The six old descriptions cleared the converter twice; then the task changed
    (it gained a JSON block), so it was measured again on 14 September 2026 against the live HTML:
    the raw HTML carries both with their spaces, and this function returns both with their
    spaces. The spaces are in the original and
    survive. What looked glued was a terminal wrapping long lines, not the data.
    """
    text = re.sub(r"<!--.*?-->", "", html_text, flags=re.DOTALL)

    # Checkbox state before the tags are gone: the attribute is the information.
    text = re.sub(r"<input\b[^>]*\bchecked\b[^>]*>", _CHECKED, text, flags=re.IGNORECASE)
    text = re.sub(r"<input\b[^>]*type=[\"\']?checkbox[\"\']?[^>]*>", _UNCHECKED, text,
                  flags=re.IGNORECASE)

    # A link keeps its text; the URL follows when the text does not already carry it.
    def _link(match: "re.Match[str]") -> str:
        href = _html.unescape(match.group(1)).strip()
        label = re.sub(r"<[^>]+>", "", match.group(2)).strip()
        if not label:
            return href
        if href and href not in label:
            # Parentheses, not angle brackets: the tag sweep below would eat ``<https://…>``
            # as if it were markup and the address would vanish — measured.
            return f"{label} ({href})"
        return label

    text = re.sub(r"<a\b[^>]*href=[\"\']([^\"\']*)[\"\'][^>]*>(.*?)</a>", _link, text,
                  flags=re.IGNORECASE | re.DOTALL)

    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)

    # Ordered lists keep their numbers. "Solution concept" on the main task is an <ol> of three
    # steps, and rendering it with dashes threw away the one thing an ordered list carries that
    # a bulleted one does not. Done before the generic <li> rule below, so only these are
    # numbered. Nested lists are not handled: none of the six has one, and guessing at a shape
    # nobody has seen would be inventing.
    def _numbered(block: "re.Match[str]") -> str:
        counter = [0]

        def _item(_m: "re.Match[str]") -> str:
            counter[0] += 1
            return f"\n{counter[0]}. "

        return "\n" + re.sub(r"<li\b[^>]*>", _item, block.group(1), flags=re.IGNORECASE) + "\n"

    text = re.sub(r"<ol\b[^>]*>(.*?)</ol>", _numbered, text,
                  flags=re.IGNORECASE | re.DOTALL)

    text = re.sub(r"<li\b[^>]*>", "\n- ", text, flags=re.IGNORECASE)
    for tag in _BLOCK_TAGS:
        text = re.sub(rf"</?{tag}\b[^>]*>", "\n", text, flags=re.IGNORECASE)

    text = re.sub(r"<[^>]+>", "", text)
    text = _html.unescape(text)

    lines = [re.sub(r"[ \t]+", " ", line).rstrip() for line in text.split("\n")]
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _adf(text: str) -> dict[str, Any]:
    """The smallest Atlassian Document Format that carries paragraphs.

    **Newlines inside a text node are rendered as line breaks, and that is measured, not
    assumed.** The ADF documentation describes a ``hardBreak`` node for a break within a
    paragraph, which reads as though a bare ``\n`` would be collapsed — and a checklist of nine
    items lives inside one such node here, so if that reading were right the description would
    come out as one run-on line and the whole HTML conversion above would be undone at the last
    step.

    It is not right. Measured in the Jira interface on 11 September 2026, on a permanent test issue: two
    comments were posted, one with ``\n`` inside a single text node and one with ``hardBreak``
    nodes between three text nodes. **They render identically — three lines each.** So the
    simple shape stays.

    This is written down because the next person to read the ADF specification will reach the
    same wrong conclusion, and "fixing" it would be a change with no observable effect made on
    the strength of a document over an observation.

    The same single-node shape is used by ``mossy/capabilities/jira.py`` and the Freshdesk
    flow's ``handle_duplicate.py``. By this measurement it is not a defect there either, and
    neither file is touched.
    """
    paragraphs = [p for p in text.split("\n\n") if p.strip()] or [text]
    return {
        "type": "doc", "version": 1,
        "content": [{"type": "paragraph",
                     "content": [{"type": "text", "text": p.strip()}]} for p in paragraphs],
    }


def _normalize_site(site: str) -> str:
    value = (site or "").strip().rstrip("/")
    for prefix in ("https://", "http://"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
    return value


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", errors="replace")


__all__ = [
    "ACTION_CREATE",
    "ACTION_NOTHING",
    "ACTION_REPAIR_LINK",
    "ACTION_STOP",
    "NOTHING_CAN_BE_DELETED",
    "PRE_LABEL_ISSUES_ARE_INVISIBLE",
    "JiraIssueCreator",
    "JiraWriteError",
    "plan",
    "run",
]
