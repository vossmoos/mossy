# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python. Without one it falls back to sys.executable.
"""Copy a Wrike task's new comments onto the Jira issue it already has.

Point 2 of the Wrike → Jira flow: *"If there is a new comment to the ticket which was already created
in our Jira - it should be left as a comment to the existing Jira ticket."* The create step creates the
issue; this is the other write in the chain, and the one that runs again and again on the same
task for as long as people keep talking in Wrike.

★ IT WRITES NOTHING WITHOUT ``apply=True``. Same gate as the create step and the same reason: the default describes what it would copy and stops. ``plan()`` has no
``apply`` parameter at all, so there is no way to ask it to write.

★★ THE DUPLICATE HERE IS WORSE THAN THE CREATE'S. Creating twice leaves two issues; copying twice
leaves the same paragraph N times on one issue, once per pass, for ever — this account has no
delete permission (``NOTHING_CAN_BE_DELETED``), so nothing gets tidied up afterwards. And unlike
a duplicate issue, a duplicate comment is invisible to the label search: nothing will ever flag
it. That is what the whole read-back below is for.

TWO RECORDS, AND JIRA IS THE ONE THAT DECIDES

The local register (``wrike_jira_links.copied_comment_ids``) is fast, free and **losable** — one
DuckDB file under ``~/.local/share`` that nobody backs up. If it were the only guard, losing it
would re-copy every comment of every task onto issues that cannot be cleaned.

So each copied comment carries an anchor, ``[wrike-comment:<wrike comment id>]``, at the end of
its text, and **the anchors already in Jira are read before anything is written**. The register
becomes an optimisation; the issue itself is the record. It is the same relationship as the label
in the label search: the local row says where to look, the remote data says what is true.

    1. GET  /issue/{key}/comment   — the anchors already there
    2. subtract them, and the register's ids, from what Wrike returned
    3. POST /issue/{key}/comment   — one call per remaining comment, oldest first
    4. remember_comment(...)       — the local row, after the write it describes

**Crash between 3 and 4 and nothing is lost.** The comment is in Jira with its anchor, the
register never heard about it, and the next pass reads the anchor back, copies nothing and
records the row it is missing (``healed``). That is the failure this ordering is chosen for: the
opposite order — record first, then post — would drop a comment for good if the POST failed.

★★★ AN UNREADABLE READ-BACK STOPS THE PASS. If step 1 fails, the anchors are unknown, and
"unknown" is not "none". Writing then would be copying blind onto an issue nobody can clean.
This is the ``empty ≠ absent`` rule of ``jira_label_search`` applied to the one place where
getting it wrong is permanent, and it is why ``existing_comments`` raises instead of returning
an empty list.

THE AUTHOR IS AN ID, AND THAT IS A LICENCE LIMIT, NOT A DESIGN CHOICE

Each copied comment is headed:

    Wrike comment by KUEXAMP1 on 2026-08-28 12:33 UTC

The id, not the name. **Measured on 11 September 2026: ``GET /contacts`` returns 1 contact of
1** — this token's own — because it is a Collaborator licence: it sees tasks and comments, not
the people directory. The three other authors of the 29 real comments (``KUEXAMP1``,
``KUEXAMP4``, ``KUEXAMP3``) do not resolve to anything. So there is no name to print. Run this
with an account that can see the directory and resolving them is one more call and a place in
the format that is already there.

★★★★ AND THE TRAP, WHICH IS THIS REPOSITORY'S USUAL ONE: **200 with one contact does not mean
"there is one contact", it means "I can see one".** Anyone treating that list as complete would
read an absent author as deleted rather than as invisible — and would then be tempted to drop
the author from the header. The author is never dropped: an id nobody can resolve still says two
different people wrote two different comments, and that is most of what attribution is for.

Readability is not lost to this. The names are **inside** the comment text already, because
Wrike mentions carry them — ``<a class="stream-user-id" rel="KUEXAMP1">@Anna Beispiel-Müller</a>``
becomes ``@Anna Beispiel-Müller`` (the create step's converter, pinned by its own tests). The header id is
metadata; the content keeps its names.

The payload itself carries an embedded ``author`` object for the contacts this licence can see —
2 of the 29, both this token's own — and the other 27 carry a bare ``authorId``. That name is
deliberately **not** used: it would print a name for some comments and an id for others on the
same issue, which reads as two different formats rather than one format with a known limit.

THE COMMENT TEXT GOES AS IT IS — NOTED, NOT FIXED, AND NOT OURS TO DECIDE

Each comment is posted with its text as Wrike has it, and a comment can carry the end customer's
personal data: name, street, mobile, e-mail. That data then sits in an issue on
another organisation's Jira, where it cannot be deleted. It is the
reason the original task description was taken out of the issue on 14 September 2026
(``create_jira_for_task``). The same remedy does not fit here: a comment cannot be summarised —
it is what somebody said, and a summary would put words in their mouth — and the copy was
asked for explicitly, as it is. Whether personal data may travel this way is the client's
decision, not this module's.

WHAT IT ACTS ON

    linked_and_alive     copy — the issue is confirmed in Jira and the table agrees
    found_by_label_only  the issue exists, but the local row is missing. the create step repairs it;
                         this pass copies nothing, so that one module owns that row
    no_link_found        nothing to comment on. the create step creates first
    not_a_notification, linked_and_alive-without-comments   nothing to do
    everything else      stop, and say why

The conversion from Wrike's HTML is the create step's ``_plain`` **unchanged** — imported, not copied —
and it was measured against these same 29 comments as well as the six task descriptions. The ADF
shape is the create step's ``_adf``, measured in the Jira interface on a permanent test issue.

It logs nothing, by the decision recorded in ``create_jira_for_task``: the envelope is
the record, Mossy configures no handlers, and the alert to the operator belongs to the process
that reads Gmail.

Self-contained: no ``import mossy``, no ``python-dotenv``, its own urllib client for the one
write and the read that guards it. ``jira_label_search`` stays read-only and untouched, and so
do ``jira.py``, ``create_jira_issue.py``, ``analyze_context.py`` and ``handle_duplicate.py``.
"""

from __future__ import annotations

import base64
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


ACTION_COPY = "copy_comments"
ACTION_CREATE_FIRST = "create_issue_first"
ACTION_REPAIR_FIRST = "repair_link_first"
ACTION_NOTHING = "nothing_to_do"
ACTION_STOP = "stop"

#: What marks a Jira comment as a copy of a particular Wrike comment. One place, because the
#: writer that puts it in and the reader that looks for it have to agree, and a format spelled
#: twice is a format that drifts — the same reason ``TASK_LABEL_PREFIX`` is one constant.
ANCHOR_PREFIX = "wrike-comment:"

#: Deliberately greedy about what an id may contain and strict about the brackets: the id comes
#: from Wrike and this module never invents one. ``\S`` so a line break cannot be swallowed.
ANCHOR_PATTERN = re.compile(r"\[" + re.escape(ANCHOR_PREFIX) + r"([^\]\s]+)\]")

#: Printed where an author id is missing from the payload altogether. Never an empty slot: a
#: header that silently loses its author reads as though nobody wrote the comment.
UNKNOWN_AUTHOR = "an author Wrike did not name"
UNKNOWN_DATE = "an unknown date"

#: Said wherever the id could be mistaken for a decision somebody made about formatting.
AUTHOR_IS_AN_ID_BY_LICENCE = (
    "The author is a raw Wrike id because this token is a Collaborator licence: measured on 11 "
    "September 2026, GET /contacts returns 1 contact of 1 — its own — so the other authors "
    "resolve to nothing. With an account that sees the people directory, resolving them is one "
    "more call. And one contact coming back does not mean there is one contact; it means only "
    "one is visible."
)

#: Said wherever a caller could read a failed read-back as an empty one.
ANCHORS_MUST_BE_READ_FIRST = (
    "The anchors already in Jira are read before anything is written. An unreadable read-back "
    "is not an empty one: without it a comment already copied cannot be told from a new one, "
    "and this account cannot delete the duplicate that would follow."
)

#: Said when Wrike returns no comments at all.
EMPTY_IS_NOT_ABSENCE = (
    "Wrike returned no comments. On a Collaborator licence a restricted read looks exactly like "
    "this, so this is not evidence that there is nothing to copy."
)

_DECIDE = None


def _decide_module():
    global _DECIDE
    if _DECIDE is None:
        _DECIDE = _sibling("decide_wrike_action")
    return _DECIDE


class JiraCommentError(RuntimeError):
    """A comment read or write failed. Carries the status so 404 can be told from 400."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} — HTTP {self.status}" if self.status else base


class JiraCommentWriter:
    """The comments of one issue: read them, then add one. Two verbs, and no others.

    The GET is here rather than in ``jira_label_search`` on purpose. It is not a search — it
    exists only to protect the POST that follows it, and a guard that lives apart from the thing
    it guards is a guard somebody forgets to call. ``jira_label_search`` keeps its four read-only
    methods and gains nothing.
    """

    def __init__(self, site: str, email: str, api_token: str, *,
                 timeout_seconds: int = 30) -> None:
        self.site = _normalize_site(site)
        self.email = email
        self.api_token = api_token
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> "JiraCommentWriter | None":
        """``None`` when anything is missing. Never raises, never makes a request."""
        site = _normalize_site(os.environ.get("JIRA_SITE") or "")
        email = (os.environ.get("JIRA_USER_EMAIL") or "").strip()
        token = (os.environ.get("JIRA_API_KEY") or "").strip()
        if not site or not email or not token:
            return None
        return cls(site, email, token)

    def existing_comments(self, issue_key: str) -> dict[str, Any]:
        """Every comment on the issue, and the Wrike anchors found in them.

        **Raises on any failure rather than returning an empty list.** An unreadable page and an
        issue with no comments are the same shape to a caller that flattens them, and the cost
        of confusing the two is a duplicate comment that cannot be deleted. A 404 here is the
        same ambiguity as everywhere else in this flow — gone, or unauthenticated — so it is an
        error too, not "no comments".
        """
        key = str(issue_key).strip()
        if not key:
            raise JiraCommentError("An issue key is required; this module never guesses one.")
        payload = self._get(f"/issue/{urllib.parse.quote(key)}/comment",
                            {"maxResults": "100", "orderBy": "created"})
        if not isinstance(payload, dict) or not isinstance(payload.get("comments"), list):
            # ★ A 200 that is not a page of comments — HTML from a proxy, ``{}``, an empty body —
            # is not "no comments". Taken as that, the anchors would be empty and every comment
            # would be posted again onto an issue nobody can clean.
            raise JiraCommentError(
                f"Jira answered the comment read for {key} with something that is not a page of "
                "comments, so the anchors on the issue are unknown.", body=payload)
        comments = [c for c in payload["comments"] if isinstance(c, dict)]
        anchors: set[str] = set()
        for comment in comments:
            anchors.update(anchors_in(comment.get("body")))
        result: dict[str, Any] = {
            "ok": True, "issue_key": key, "count": len(comments), "anchors": sorted(anchors),
            "authenticated": True,
        }
        total = payload.get("total") if isinstance(payload, dict) else None
        if isinstance(total, int) and total > len(comments):
            # ★ A LIMIT, STATED RATHER THAN HIDDEN. This asks for 100 and does not page. An
            # issue holding more than that reports the shortfall and **copying stops for that
            # issue until somebody pages it** — it does not truncate, because a truncated anchor
            # list is precisely what would let a duplicate through, and truncation is silent
            # while a refusal is not. The fix is ``startAt`` paging, which this endpoint is
            # documented to accept and which has **not been measured here**, so it is not
            # written on trust. The busiest of the real Wrike tasks carries 29 comments.
            result["ok"] = False
            result["reason"] = "more_than_one_page"
            result["total"] = total
            result["detail"] = (
                f"Jira reports {total} comments and this read saw {len(comments)}. The anchors "
                "are incomplete, so nothing may be copied on this pass."
            )
        return result

    def add_comment(self, issue_key: str, text: str) -> dict[str, Any]:
        """POST one comment. Raises unless Jira answers 200/201 — a refused write is never a
        successful one, and a caller that recorded it would lose the comment for good."""
        key = str(issue_key).strip()
        creator = _sibling("create_jira_for_task")
        status, body = self._post(f"/issue/{urllib.parse.quote(key)}/comment",
                                  {"body": creator._adf(text)})
        if status not in (200, 201) or not isinstance(body, dict) or not body.get("id"):
            raise JiraCommentError("Jira refused the comment.", status=status, body=body)
        return {"comment_id": str(body["id"]), "issue_key": key, "status": status}

    # ------------------------------------------------------------------ transport
    def _headers(self) -> dict[str, str]:
        token = base64.b64encode(f"{self.email}:{self.api_token}".encode()).decode("ascii")
        return {"Authorization": f"Basic {token}", "Accept": "application/json"}

    def _get(self, path: str, query: dict[str, str]) -> Any:
        url = (f"https://{self.site}/rest/api/3{path}"
               + ("?" + urllib.parse.urlencode(query) if query else ""))
        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return _decode(response.read())
        except urllib.error.HTTPError as exc:
            body = _decode(exc.read() if exc.fp else b"")
            raise JiraCommentError(f"Jira refused the comment read: {url}",
                                   status=exc.code, body=body) from exc
        except urllib.error.URLError as exc:
            raise JiraCommentError(f"Jira is unreachable: {exc.reason}") from exc

    def _post(self, path: str, payload: dict[str, Any]) -> tuple[int, Any]:
        url = f"https://{self.site}/rest/api/3{path}"
        headers = {**self._headers(), "Content-Type": "application/json"}
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return response.getcode() or 200, _decode(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _decode(exc.read() if exc.fp else b"")
        except urllib.error.URLError as exc:
            raise JiraCommentError(f"Jira is unreachable: {exc.reason}") from exc


# ---------------------------------------------------------------------- the anchor
def anchor_for(comment_id: str) -> str:
    """``IEAEXAMPLEC00001`` -> ``[wrike-comment:IEAEXAMPLEC00001]``. The one place it is
    spelled."""
    return f"[{ANCHOR_PREFIX}{str(comment_id).strip()}]"


def anchors_in(body: Any) -> set[str]:
    """Every Wrike comment id anchored anywhere in a Jira comment body.

    Takes ADF, plain text or anything nested: it walks the structure and matches on the text it
    finds. Written to survive the body shape changing, because the anchor is the only thing
    standing between a retried pass and a permanent duplicate, and it must not stop working
    because Jira wrapped the text in one more node.
    """
    return set(ANCHOR_PATTERN.findall(_text_of(body)))


def _text_of(node: Any) -> str:
    """All the text in an ADF body, concatenated the way ADF renders inline nodes: end to end."""
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        pieces = [str(node["text"])] if isinstance(node.get("text"), str) else []
        pieces.extend(_text_of(child) for child in node.get("content") or [])
        return "".join(pieces)
    if isinstance(node, list):
        return "".join(_text_of(child) for child in node)
    return ""


# ---------------------------------------------------------------------- the body
def _author_id(comment: dict[str, Any]) -> str:
    """The raw Wrike id of whoever wrote it. Never empty — see ``AUTHOR_IS_AN_ID_BY_LICENCE``.

    Two shapes, both measured on the 29 real comments: 27 carry a bare ``authorId``, and 2 carry
    an embedded ``author`` object — the ones written by the contact this licence can see. The id
    is taken from either; the embedded ``name`` is not used, so one issue never mixes names and
    ids.
    """
    direct = str(comment.get("authorId") or "").strip()
    if direct:
        return direct
    author = comment.get("author")
    if isinstance(author, dict):
        embedded = str(author.get("id") or "").strip()
        if embedded:
            return embedded
    return UNKNOWN_AUTHOR


def _when(comment: dict[str, Any]) -> str:
    """``2026-09-10T14:39:15Z`` -> ``2026-09-10 14:39 UTC``. Anything else is passed through raw.

    Not reformatted by parsing into a datetime and printing it back: a date that does not match
    the measured shape is shown as Wrike sent it, because a wrong timestamp on a comment nobody
    can delete is worse than an ugly one.
    """
    raw = str(comment.get("createdDate") or "").strip()
    if not raw:
        return UNKNOWN_DATE
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}):\d{2}(?:\.\d+)?Z", raw)
    return f"{match.group(1)} {match.group(2)} UTC" if match else raw


def comment_body(comment: dict[str, Any]) -> str:
    """What one Wrike comment looks like as a Jira comment: header, text, anchor.

    The header names the author and the date because the copy loses both otherwise — a Jira
    comment is attributed to whoever's token posted it, which would make every copied comment
    look like it was written by this integration.

    The anchor goes **last**, so it sits out of the way of the text and so the read-back finds
    it wherever the body is truncated for display. The text itself is the create step's ``_plain``,
    unchanged: mentions keep their names, links keep their URLs, checkboxes keep their state.
    """
    creator = _sibling("create_jira_for_task")
    text = creator._plain(str(comment.get("text") or ""))
    header = f"Wrike comment by {_author_id(comment)} on {_when(comment)}"
    parts = [header]
    if text:
        parts.append(text)
    parts.append(anchor_for(comment.get("id") or ""))
    return "\n\n".join(parts)


def _in_order(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Oldest first, so the issue reads in the order the conversation happened.

    Measured: the Wrike API returns a task's comments **newest first** (the 29 real ones run from
    2026-09-10 down to 2026-06-12). This sorts rather than reversing that, because reversing
    would silently produce backwards output the day the API's order changed, and an ISO-8601 Z
    timestamp sorts correctly as a string. A comment with no date keeps its place relative to the
    others — the sort is stable — instead of being dropped or guessed at.
    """
    dated = [c for c in comments if str(c.get("createdDate") or "").strip()]
    undated = [c for c in comments if not str(c.get("createdDate") or "").strip()]
    return sorted(dated, key=lambda c: str(c["createdDate"])) + undated


# ---------------------------------------------------------------------- plan and run
def plan(message: dict[str, Any], *, search: Any = None, wrike: Any = None,
         db_path: Path | str | None = None) -> dict[str, Any]:
    """What would be copied, without copying any of it. Reads only.

    No ``apply`` parameter, deliberately: there is no argument anybody can pass to this function
    that makes it write.
    """
    decide_module = _decide_module()
    verdict = decide_module.decide(message, search=search, wrike=wrike, db_path=db_path)
    name = verdict["verdict"]

    if name == decide_module.LINKED_AND_ALIVE:
        action = ACTION_COPY
    elif name == decide_module.FOUND_BY_LABEL_ONLY:
        action = ACTION_REPAIR_FIRST
    elif name == decide_module.NO_LINK_FOUND:
        action = ACTION_CREATE_FIRST
    elif name == decide_module.NOT_A_NOTIFICATION:
        action = ACTION_NOTHING
    else:
        action = ACTION_STOP

    result: dict[str, Any] = {
        "action": action, "verdict": name, "task_api_id": verdict.get("task_api_id"),
        "issue_key": verdict.get("issue_key"), "issue_url": verdict.get("issue_url"),
        "decision": verdict, "applied": False, "copied": [], "posted": 0,
    }
    if action != ACTION_COPY:
        return result

    if verdict.get("comments_error"):
        # The issue is confirmed but Wrike would not say what is on the task. Not "nothing new".
        result["action"] = ACTION_STOP
        result["reason"] = "comments_unreadable"
        result["detail"] = (
            "The Jira issue is confirmed, but the task's comments could not be read from Wrike, "
            "so what is new is unknown: " + str(verdict["comments_error"])
        )
        return result

    candidates = verdict.get("comments_to_copy") or []
    with_id = [c for c in candidates if str(c.get("id") or "").strip()]
    without_id = [c for c in candidates if not str(c.get("id") or "").strip()]

    result["would_copy"] = [
        {"comment_id": str(c["id"]), "anchor": anchor_for(c["id"]), "body": comment_body(c)}
        for c in _in_order(with_id)
    ]
    result["candidates"] = len(candidates)
    result["comments_seen"] = verdict.get("comments_seen", 0)
    result["author_note"] = AUTHOR_IS_AN_ID_BY_LICENCE
    if without_id:
        # The register keeps an id-less comment on purpose (losing it silently is worse), but it
        # cannot be anchored and cannot be recorded, so copying it would repeat it on every pass
        # onto an issue nobody can clean. Reported for a person to handle, never written.
        result["skipped_without_id"] = len(without_id)
        result["skipped_detail"] = (
            "A comment with no id cannot carry an anchor and cannot be recorded, so it would be "
            "copied again on every pass. It is reported instead of written."
        )
    if not verdict.get("comments_certain", True):
        result["comments_certain"] = False
        result["comments_means"] = EMPTY_IS_NOT_ABSENCE
    return result


def run(message: dict[str, Any], *, apply: bool = False, search: Any = None, wrike: Any = None,
        writer: Any = None, db_path: Path | str | None = None,
        lock_dir: Path | str | None = None) -> dict[str, Any]:
    """Describe, and copy only when ``apply`` is true.

    ``apply`` defaults to False and is the only thing that lets a write happen. Without it not
    one write call is made — not to Jira, not to the register — and the read-back is not even
    performed, because there is nothing to protect.

    **One pass at a time over one task** (the per-task lock). When ``apply`` is set, the read-back and the
    writes that follow it happen inside a per-task lock. The anchor stops a *retry* from copying
    twice; it cannot stop two passes that both read the anchors before either writes one, and
    that was measured at two copies of every comment. The lock covers this machine only; see
    ``task_lock.ONE_MACHINE_ONLY``.
    """
    if not apply:
        return _run_unlocked(message, apply=False, search=search, wrike=wrike, writer=writer,
                             db_path=db_path)
    locking = _sibling("task_lock")
    task_hint = (_sibling("parse_wrike_notification").parse(message) or {}).get("task_api_id")
    return locking.locked_run(
        task_hint,
        lambda: _run_unlocked(message, apply=True, search=search, wrike=wrike, writer=writer,
                              db_path=db_path),
        lock_dir=lock_dir, busy_base={"action": ACTION_STOP, "copied": [], "posted": 0},
    )


def _run_unlocked(message: dict[str, Any], *, apply: bool = False, search: Any = None,
                  wrike: Any = None, writer: Any = None,
                  db_path: Path | str | None = None) -> dict[str, Any]:
    """The pass itself. Called by ``run`` inside the lock; never call it directly to write."""
    links = _sibling("wrike_jira_links")
    result = plan(message, search=search, wrike=wrike, db_path=db_path)
    task, issue_key = result["task_api_id"], result.get("issue_key")

    if not apply:
        result["detail"] = "Nothing was written. Pass apply=True to copy these."
        return result

    if result["action"] != ACTION_COPY:
        return result

    if writer is None:
        result["reason"] = "no_writer"
        result["detail"] = "Jira write credentials are not configured; nothing was copied."
        return result

    # The canary, the same one the create step refuses to create without: an unauthenticated client is
    # served empty searches and 404s, and neither looks like a failure.
    try:
        proof = search.credentials_proven()
    except Exception as exc:  # noqa: BLE001 - a check that failed is not a check that passed
        # This used to escape as an exception: measured with the network dropped here.
        result["reason"] = "credentials_check_failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["detail"] = (
            "Nothing was copied: the credential check itself failed, so the credentials are "
            "unknown — and unknown is not proven."
        )
        return result
    if not proof.get("proven"):
        result["reason"] = "authentication_not_proven"
        result["credentials"] = proof
        result["detail"] = (
            "Nothing was copied: the credentials could not be proven, and an unauthenticated "
            "client is served an empty 200."
        )
        return result

    # ★ The read-back, BEFORE the first write. The register says what this machine remembers
    # copying; Jira says what is actually there. Only the second one can be trusted, and a
    # failure to read it stops the pass — see ANCHORS_MUST_BE_READ_FIRST.
    try:
        already = writer.existing_comments(issue_key)
    except Exception as exc:  # noqa: BLE001 - an unreadable read-back is never an empty one
        result["reason"] = "existing_comments_unreadable"
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["detail"] = ANCHORS_MUST_BE_READ_FIRST
        return result
    if not isinstance(already, dict) or not already.get("ok"):
        result["reason"] = "existing_comments_unreadable"
        result["existing"] = already if isinstance(already, dict) else None
        result["detail"] = ANCHORS_MUST_BE_READ_FIRST
        return result

    anchors = set(already.get("anchors") or [])
    result["anchors_already_in_jira"] = len(anchors)

    copied: list[dict[str, Any]] = []
    healed: list[str] = []
    for item in result.get("would_copy") or []:
        comment_id = item["comment_id"]
        if comment_id in anchors:
            # Already on the issue and the register did not know: the pass that wrote it died
            # between the POST and the row. Record it now, copy nothing.
            links.remember_comment(task, comment_id, db_path=db_path)
            healed.append(comment_id)
            continue
        try:
            written = writer.add_comment(issue_key, item["body"])
        except Exception as exc:  # noqa: BLE001 - a refused write is never a written comment
            result["reason"] = "copy_failed"
            result["error"] = f"{type(exc).__name__}: {exc}"
            result["failed_comment_id"] = comment_id
            break
        anchors.add(comment_id)
        stored = links.remember_comment(task, comment_id, db_path=db_path)
        copied.append({"comment_id": comment_id, "jira_comment_id": written.get("comment_id"),
                       "recorded": bool(stored.get("ok"))})

    result["copied"] = copied
    result["posted"] = len(copied)
    result["already_in_jira"] = healed
    result["healed"] = len(healed)
    result["applied"] = bool(copied)
    return result


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
    "ACTION_COPY",
    "ACTION_CREATE_FIRST",
    "ACTION_NOTHING",
    "ACTION_REPAIR_FIRST",
    "ACTION_STOP",
    "ANCHORS_MUST_BE_READ_FIRST",
    "ANCHOR_PATTERN",
    "ANCHOR_PREFIX",
    "AUTHOR_IS_AN_ID_BY_LICENCE",
    "EMPTY_IS_NOT_ABSENCE",
    "UNKNOWN_AUTHOR",
    "UNKNOWN_DATE",
    "JiraCommentError",
    "JiraCommentWriter",
    "anchor_for",
    "anchors_in",
    "comment_body",
    "plan",
    "run",
]
