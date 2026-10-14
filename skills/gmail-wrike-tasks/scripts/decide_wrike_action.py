# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python. Without one it falls back to sys.executable.
"""Given a Wrike notification, describe what is already there. **It decides nothing to write.**

This joins the four pieces: ``parse_wrike_notification`` pulls the task id out of the
e-mail, ``wrike_jira_links`` remembers which Jira issue a task has, ``jira_label_search`` finds
one again by label or confirms a key, and ``wrike_api`` brings the task's comments.

**It describes; it does not act.** Creating an issue is the create step and commenting on one is the comment copy.
Nothing here writes to Jira, to Wrike or to the database — every call it makes is a read. The
separation is not tidiness: a module that says "here is what I see" and a module that decides to
write are answerable for different mistakes, and the verdict below marked ``no_link_found`` is
uncertain for **three different reasons at once**, which is not a thing to hand to a caller as a
recommendation.

THE ORDER: the table first, then Jira.

The rule this rests on, written where the table lives: *the table says which key to look at,
Jira says whether that key still exists.* An index is read before going to the shelf. It is also
local and free where the search is a network call per notification. The label search is the
recovery path, for when the file was lost or the link was made on another machine, not the first
question — and it finds only issues that carry the label: the ones this flow created, not an
issue opened by hand.

★ AUTHENTICATION IS NEVER ASSUMED, AND ``authenticated`` IS NEVER False.

Measured on this Jira: an unauthenticated client is served **200 with an empty issue list** by
the search, and **404** by ``GET /issue/{key}``. Neither failure looks like a failure. So only a
**200 carrying content** proves the credentials work — a confirmed key, or a search that returned
rows — and everything else leaves ``authenticated`` at ``None``. False would be a claim as wrong
as True.

That is why ``link_unconfirmable`` exists as a verdict of its own and recommends nothing. The
table has a row, Jira answered 404, and "the issue was deleted" and "this token is no good" are
the same bytes. Reading that as "the row is dead, open a new one" would open a duplicate issue
**per task, on every pass**, for as long as the token stayed broken — measured the hard way, an
hour of believing a project was empty.

THE VERDICTS. Eleven, and none of them collapses into another:

    not_a_notification    the e-mail is not a Wrike task notification
    not_configured        a client is missing; NOT the same as "no link"
    no_link_found         table empty and search empty — uncertain three ways
    linked_and_alive      table row, key confirmed 200. Authentication proven
    link_unconfirmable    table row, key 404. Ambiguous; nothing is recommended
    found_by_label_only   Jira has it, the table does not. Authentication proven
    link_disagrees        table says one key, the label search says another
    label_on_wrong_issue  the filter returned an issue that does not carry the label
    ambiguous_match       more than one issue, or more than one page, for one label
    jira_error            a failed query — never an empty result
    register_unreadable   the local register could not be read — never "no link" (the per-task lock)

``no_link_found`` is uncertain for three reasons that are worth keeping apart: the task may
genuinely have no issue; the credentials may be bad; or the task's issue carries no label, because
it was opened by hand or before this flow labelled what it creates. All three produce the same
empty page.

Self-contained: no ``import mossy``, no ``python-dotenv``. It loads its siblings by path, the way
the tests do, because a skill's scripts are not on the import path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent


def _sibling(name: str):
    """Load a module from this skill's own scripts directory, by path.

    Not an ordinary import: the scripts directory is not on ``sys.path`` when the skill runtime
    runs one of them. Cached in ``sys.modules`` so two calls return one module object.
    """
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- the verdicts
NOT_A_NOTIFICATION = "not_a_notification"
NOT_CONFIGURED = "not_configured"
NO_LINK_FOUND = "no_link_found"
LINKED_AND_ALIVE = "linked_and_alive"
LINK_UNCONFIRMABLE = "link_unconfirmable"
FOUND_BY_LABEL_ONLY = "found_by_label_only"
LINK_DISAGREES = "link_disagrees"
LABEL_ON_WRONG_ISSUE = "label_on_wrong_issue"
AMBIGUOUS_MATCH = "ambiguous_match"
JIRA_ERROR = "jira_error"
REGISTER_UNREADABLE = "register_unreadable"

VERDICTS = (
    NOT_A_NOTIFICATION, NOT_CONFIGURED, NO_LINK_FOUND, LINKED_AND_ALIVE, LINK_UNCONFIRMABLE,
    FOUND_BY_LABEL_ONLY, LINK_DISAGREES, LABEL_ON_WRONG_ISSUE, AMBIGUOUS_MATCH, JIRA_ERROR,
    REGISTER_UNREADABLE,
)

#: Why an unreadable register is its own verdict rather than one of the other ten (the per-task lock).
REGISTER_IS_NOT_JIRA = (
    "The local register could not be read. This is not 'no link recorded' — a row may well be "
    "sitting in the table — and it is not jira_error either, because Jira was never asked. "
    "DuckDB holds the file exclusively, so an overlapping pass on this machine produces exactly "
    "this. Nothing is recommended: acting on it would create a duplicate issue or copy a "
    "comment a second time, and neither can be deleted."
)

#: The three things an empty page could mean, kept apart because they need different answers.
NO_LINK_IS_UNCERTAIN = (
    "Nothing was found, and that is uncertain three ways: the task may have no issue; the "
    "credentials may not authenticate (an unauthenticated client is served an empty 200); or the "
    "task's issue carries no Wrike label, because it was opened by hand or before this flow "
    "labelled what it creates."
)

#: Why a 404 settles nothing.
UNCONFIRMABLE_MEANS = (
    "The table has a row and Jira answered 404 for that key. An unauthenticated client is served "
    "404 too, so the issue may be gone or the token may be bad. Nothing is recommended: reading "
    "this as 'open a new issue' would create a duplicate for every task on every pass."
)

#: Why an empty comment list is not "nothing new".
COMMENTS_NOT_CERTAIN = (
    "Wrike returned no comments. On a Collaborator licence a restricted read looks exactly like "
    "this, so this is not evidence that there is nothing to copy."
)


def _verdict(name: str, task_api_id: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"verdict": name, "task_api_id": task_api_id, "authenticated": None, **extra}


def decide(
    message: dict[str, Any],
    *,
    search: Any = None,
    wrike: Any = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """One Gmail message in, a description of what already exists out.

    ``search`` and ``wrike`` are injected so this is testable without a network and without
    credentials; passing ``None`` for either is reported as ``not_configured`` rather than
    guessed around.
    """
    parser = _sibling("parse_wrike_notification")
    links = _sibling("wrike_jira_links")

    seen = parser.parse(message)
    if not seen.get("ok"):
        return _verdict(NOT_A_NOTIFICATION, seen.get("task_api_id"),
                        reason=seen.get("reason"), notification=seen)

    task = seen["task_api_id"]
    base = {"message_id": seen.get("message_id"), "thread_id": seen.get("thread_id")}

    missing = [n for n, c in (("jira", search), ("wrike", wrike)) if c is None]
    if missing:
        return _verdict(NOT_CONFIGURED, task, missing=missing, **base,
                        detail="A missing client is not the same as a missing link.")

    # ---- the table first. Local, free, and the only source that knows anything today.
    # An unreadable register is its own verdict: it is not "no row", and reading it as one is
    # what this whole module exists to refuse, one level down (the per-task lock).
    try:
        row = links.linked_issue(task, db_path=db_path)
    except links.LinkStoreError as exc:
        return _verdict(REGISTER_UNREADABLE, task, stage="linked_issue",
                        error=f"{type(exc).__name__}: {exc}", detail=REGISTER_IS_NOT_JIRA,
                        **base)

    if row is not None:
        try:
            confirmed = search.issue_exists(row["jira_key"])
        except Exception as exc:  # noqa: BLE001 - a failed query is never an empty result
            return _verdict(JIRA_ERROR, task, stage="issue_exists",
                            error=f"{type(exc).__name__}: {exc}", table_key=row["jira_key"],
                            **base)
        if _unreadable(confirmed):
            return _verdict(JIRA_ERROR, task, stage="issue_exists",
                            error="the client returned something that is not a result",
                            table_key=row["jira_key"], **base)
        if confirmed.get("exists") is True:
            # The key is alive. The label search still runs, and this is the one place it costs
            # a call that the happy path could have skipped: it is what detects two issues for
            # one task. A disagreement found here is cheap; found later it is a comment on the
            # wrong issue.
            try:
                alongside = search.find_issue_for_task(task)
            except Exception as exc:  # noqa: BLE001
                return _verdict(JIRA_ERROR, task, stage="find_issue_for_task",
                                error=f"{type(exc).__name__}: {exc}",
                                table_key=row["jira_key"], **base)
            if _unreadable(alongside):
                return _verdict(JIRA_ERROR, task, stage="find_issue_for_task",
                                error="the client returned something that is not a result",
                                table_key=row["jira_key"], **base)
            anomaly = _anomaly(alongside)
            if anomaly:
                return _verdict(anomaly, task, authenticated=True, table_key=row["jira_key"],
                                search=alongside, **base)
            if alongside.get("matched") and alongside.get("issue_key") != row["jira_key"]:
                return _verdict(LINK_DISAGREES, task, authenticated=True,
                                table_key=row["jira_key"],
                                label_key=alongside.get("issue_key"),
                                detail="The table and the label point at different issues. One "
                                       "task has one issue; somebody has to say which.",
                                **base)
            return _with_comments(
                _verdict(LINKED_AND_ALIVE, task, authenticated=True,
                         issue_key=row["jira_key"], issue_url=row.get("jira_url"),
                         source="table", **base),
                task, wrike, links, db_path,
            )
        # 404. Ambiguous on its own — but a search that returns rows would prove the
        # credentials, and then the 404 would mean the key really is gone.
        try:
            found = search.find_issue_for_task(task)
        except Exception as exc:  # noqa: BLE001
            return _verdict(JIRA_ERROR, task, stage="find_issue_for_task",
                            error=f"{type(exc).__name__}: {exc}", table_key=row["jira_key"],
                            **base)
        if _unreadable(found):
            return _verdict(JIRA_ERROR, task, stage="find_issue_for_task",
                            error="the client returned something that is not a result",
                            table_key=row["jira_key"], **base)
        if found.get("matched"):
            return _with_comments(
                _verdict(FOUND_BY_LABEL_ONLY, task, authenticated=True,
                         issue_key=found["issue_key"], issue_url=found.get("issue_url"),
                         source="label", stale_table_key=row["jira_key"],
                         detail="The key in the table is dead — the search proved the "
                                "credentials, so its 404 was real — and Jira has another issue "
                                "under this task's label.",
                         **base),
                task, wrike, links, db_path,
            )
        return _verdict(LINK_UNCONFIRMABLE, task, table_key=row["jira_key"],
                        detail=UNCONFIRMABLE_MEANS, **base)

    # ---- no row. The label search is the recovery path.
    try:
        found = search.find_issue_for_task(task)
    except Exception as exc:  # noqa: BLE001
        return _verdict(JIRA_ERROR, task, stage="find_issue_for_task",
                        error=f"{type(exc).__name__}: {exc}", **base)

    if _unreadable(found):
        return _verdict(JIRA_ERROR, task, stage="find_issue_for_task",
                        error="the client returned something that is not a result", **base)
    anomaly = _anomaly(found)
    if anomaly:
        return _verdict(anomaly, task, authenticated=found.get("authenticated"),
                        search=found, **base)
    if found.get("matched"):
        return _with_comments(
            _verdict(FOUND_BY_LABEL_ONLY, task, authenticated=True,
                     issue_key=found["issue_key"], issue_url=found.get("issue_url"),
                     source="label",
                     detail="Jira has it and the table does not: the register can be repaired.",
                     **base),
            task, wrike, links, db_path,
        )
    return _verdict(NO_LINK_FOUND, task, detail=NO_LINK_IS_UNCERTAIN, search=found, **base)


def _unreadable(answer: Any) -> bool:
    """An answer this module cannot read is an error, never "nothing found".

    A dependency that returns something unexpected must not be flattened into an empty result:
    that is the same collapse this whole module exists to avoid, one level down.
    """
    return not isinstance(answer, dict)


def _anomaly(found: dict[str, Any]) -> str | None:
    """The search's own anomalies, promoted to verdicts of their own rather than flattened."""
    if found.get("label_confirmed") is False:
        return LABEL_ON_WRONG_ISSUE
    anomalies = found.get("anomalies") or []
    if "more_than_one_issue" in anomalies or "more_than_one_page" in anomalies:
        return AMBIGUOUS_MATCH
    return None


def _with_comments(verdict, task, wrike, links, db_path) -> dict[str, Any]:
    """Attach which comments are still to copy. A Wrike failure does not undo a confirmed issue.

    Knowing the issue is already worth something: the verdict stays ``linked_and_alive`` and
    carries ``comments_error``, rather than becoming a failure that throws the confirmation away.
    """
    try:
        answer = wrike.get_task_comments(task)
    except Exception as exc:  # noqa: BLE001
        verdict["comments_error"] = f"{type(exc).__name__}: {exc}"
        verdict["comments_certain"] = False
        return verdict
    if not answer.get("ok"):
        verdict["comments_error"] = answer.get("reason") or "wrike_refused"
        verdict["comments_certain"] = False
        return verdict

    rows = answer.get("comments") or []
    try:
        verdict["comments_to_copy"] = links.comments_not_yet_copied(task, rows, db_path=db_path)
    except links.LinkStoreError as exc:
        # The issue is confirmed and that stays; what is unknown is which comments were already
        # copied. Reported as an error rather than as "all of them are new", which would be a
        # full re-copy of the task (the per-task lock).
        verdict["comments_error"] = f"{type(exc).__name__}: {exc}"
        verdict["comments_certain"] = False
        verdict["comments_seen"] = len(rows)
        return verdict
    verdict["comments_seen"] = len(rows)
    # An empty answer from Wrike is not "nothing new": a Collaborator licence hides what it does
    # not allow behind the same empty list.
    verdict["comments_certain"] = bool(rows)
    if not rows:
        verdict["comments_means"] = COMMENTS_NOT_CERTAIN
    return verdict


__all__ = [
    "AMBIGUOUS_MATCH",
    "COMMENTS_NOT_CERTAIN",
    "FOUND_BY_LABEL_ONLY",
    "JIRA_ERROR",
    "LABEL_ON_WRONG_ISSUE",
    "LINKED_AND_ALIVE",
    "LINK_DISAGREES",
    "LINK_UNCONFIRMABLE",
    "NOT_A_NOTIFICATION",
    "NOT_CONFIGURED",
    "NO_LINK_FOUND",
    "NO_LINK_IS_UNCERTAIN",
    "REGISTER_IS_NOT_JIRA",
    "REGISTER_UNREADABLE",
    "UNCONFIRMABLE_MEANS",
    "VERDICTS",
    "decide",
]
