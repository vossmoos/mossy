# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python. Without one it falls back to sys.executable.
"""Process one Wrike notification: parse, decide, then create or copy (the Wrike → Jira flow, piece 2).

The entry point of the flow, and **one script rather than seven**. The flow is deterministic, and
every hand-off between scripts — a path, an id, a verdict carried by an agent from one call to the
next — is where an earlier version failed. So the chain runs in Python, in one process, and prints one JSON.

★ WITHOUT ``--apply`` NOTHING IS WRITTEN — not to Jira, not to the register, not to Gmail. The
default describes and stops. That is structural where it can be: ``main`` does not even build the
Jira creator or the comment writer without ``--apply``, and a label is only ever attempted with it.

THE ORDER, AND WHAT EACH FAILURE DOES

    1. the message   --message-json (a local file) or --message-id (read with piece 1's reader)
                     a Gmail failure stops, unlabelled — it is ours, not the e-mail's
    2. parse         not a Wrike task notification -> skipped, with the parser's reason
    3. decide        read-only (create.plan). A stopping verdict stops, unlabelled
    4. the gate      ONLY when the decision is to CREATE:
                       who am I   GET /contacts?me=true -> me_unresolved stops, unlabelled
                       the task   GET /tasks/{id}, responsibleIds -> unreadable stops, unlabelled
                       not in it  -> not_responsible, skipped, nothing created
    5. write         create.run, or (repair then) copy.run — each takes the per-task lock itself
    6. the label     processed once the task is up to date; label_failed keeps what Jira did

``skipped`` is only ever set for what the e-mail IS — not a notification, or about a task that
does not concern me. Never for a failure of ours: a dead token, an unreadable Wrike, a busy lock or
a refused Jira leave the e-mail unlabelled, so the next pass tries again.

THE GATE GOVERNS CREATING, NOT COPYING

``responsibleIds`` is a filter of **"this concerns me"**, not **"this is mine"**. Measured on the
six live tasks on 14 September 2026: my id is in four. In one of them — the
measurement umbrella — I am listed as a responsible although only two of its sub-tasks are mine.
Accepted, and on purpose: an extra issue in MY Jira project is cheap; opening issues for tasks
that do not concern me at all is not.

It gates **creation only.** A task that already has a live issue gets its new comments copied even
if I am no longer in its ``responsibleIds``. The decision that the task concerns me was taken when
its issue was created; if a later reassignment stopped the copy, that issue would go mute and
nobody would know why it stopped updating. Declared limitation: **a live issue keeps receiving
comments from a task that may no longer concern me.** That is the cheap side of the error — an
issue that stays up to date is better than one that silently stops. A label-only issue (the
register lost its row) is live too: it is repaired and copied without the gate.

Why the gate comes AFTER the decision, not before it: whether a live issue exists is only known
once the register and Jira have been read. Reading who I am and the task first would also let a
failure of those reads — an expired Wrike token, a 403 — block the copy onto an existing issue,
which is exactly the mute issue the gate must not cause. So the two reads happen only on the
create branch. ``create`` reads the task again for its summary: one GET more, accepted rather than
changing the create step's interface.

THE LOCK

``create.run`` and ``copy.run`` each take the per-task lock around their own deciding and
writing. **This script takes no lock of its own, and never calls ``_run_unlocked``.** Measured on
14 September 2026: holding the lock and then calling ``run(apply=True)`` in the same process makes
the inner call ``task_is_busy`` — the process locks itself out, writes nothing, and reports it as
an ordinary busy task. It would look like it works and do nothing.

The gate's reads stay outside the lock. A race over them cannot duplicate anything: the writers
decide again under the lock, so a second pass finds the first pass's issue. Declared residue: on
the repair branch, ``create.run`` decides again under the lock, and if the issue's label vanished
in the seconds between the read-only decision and the lock, it could create without the gate —
the cheap side again, an extra issue in my project.

NOT COPIED IN THE SAME PASS AS A CREATE

After an issue is created the pass stops. ``create`` records the ids of the comments the summary
was evaluated from as copied. They are not in the description — that is the model's problem and
the link — so they reach the issue only through what the model wrote, never as their own text; and
the next notification for the task copies only comments that were not in that read. Measured on 14
September 2026 before that was so, on the 29-comment task: 0 recorded after the create, 29 posted
as loose comments by the next pass. A row that fails to write is reported as
``summarised_comments_not_recorded`` and the e-mail stays unlabelled. Declared residue: a process
that dies between the create and those rows leaves them unrecorded, and nothing rescues them — no
comment was posted, so none carries an anchor — so the next pass posts them.

AN EMPTY COMMENT LIST IS LABELLED — DECLARED, NOT STOPPED ON

When Wrike returns no comments for a task whose issue exists, the pass copies nothing and labels
the e-mail processed. On a Collaborator licence an empty list could in principle be a restricted
read, and nothing here tells the two apart — so the envelope says so, at the top: ``comments_certain:
False`` and ``comments_means``. Stopping on it was tried on 14 September 2026 and reverted the same
day, because it cost more than it guarded: the comments of a task this account can see are visible
(the 29 comments of one real task were read with this same licence), while two of the six live
tasks measured have no comments at all, and every later notification for them would have been left
unlabelled and held for good.

``--message-json`` spares only the Gmail read. Wrike and Jira are still read; without them the
pass stops with the client reported missing.

CLI (named flags only):

    process_wrike_notification.py --message-id 0a1b2c3d4e5f6072            # describe
    process_wrike_notification.py --message-json /tmp/message.json         # describe, no Gmail
    process_wrike_notification.py --message-id 0a1b2c3d4e5f6072 --apply    # writes, by hand
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent

NO_MESSAGE = "no_message"
MESSAGE_UNREADABLE = "message_unreadable"
NOT_A_WRIKE_NOTIFICATION = "not_a_wrike_notification"
PLAN_STOPPED = "plan_stopped"
WRIKE_UNREADABLE = "wrike_unreadable"
RESPONSIBILITY_UNKNOWN = "responsibility_unknown"
NOT_RESPONSIBLE = "not_responsible"
WRITE_STOPPED = "write_stopped"
LABEL_FAILED = "label_failed"


def _sibling(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _gmail_failure(fetch: Any, exc: Any, message_id: str) -> dict[str, Any]:
    if exc.kind == "token":
        reason, detail = fetch.GMAIL_TOKEN_REFUSED, fetch.TOKEN_REFUSED_IS_NOT_EMPTY
    elif exc.kind == "network":
        reason, detail = fetch.GMAIL_UNREACHABLE, f"{exc} Nothing was read."
    else:
        reason, detail = fetch.GMAIL_MESSAGE_REFUSED, str(exc)
    return {"reason": reason, "status": exc.status, "message_id": message_id, "detail": detail}


def _identity(wrike: Any) -> dict[str, Any]:
    """Who the token is, from ``?me=true``. Never guessed."""
    try:
        me = wrike.get_me()
    except Exception as exc:  # noqa: BLE001 - a failed read is an answer, and it stops the create
        return {"ok": False, "reason": WRIKE_UNREADABLE, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(me, dict) or not me.get("ok") or not str(me.get("me_id") or "").strip():
        reason = me.get("reason") if isinstance(me, dict) else None
        return {"ok": False, "reason": reason or "me_unresolved", "me": me}
    return {"ok": True, "me_id": str(me["me_id"]).strip()}


def _responsibility(wrike: Any, task_api_id: str, me_id: str) -> dict[str, Any]:
    """Whether the task concerns me: my id in its ``responsibleIds``. Unknown is not "no"."""
    try:
        answer = wrike.get_task(task_api_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": WRIKE_UNREADABLE, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(answer, dict) or not answer.get("ok"):
        return {"ok": False, "reason": WRIKE_UNREADABLE,
                "task_reason": answer.get("reason") if isinstance(answer, dict) else None}
    responsible = (answer.get("task") or {}).get("responsibleIds")
    if not isinstance(responsible, list):
        return {"ok": False, "reason": RESPONSIBILITY_UNKNOWN,
                "detail": "The task carries no responsibleIds list, so whether it concerns me is "
                          "unknown — and unknown is not 'no'. Nothing was created."}
    concerns_me = me_id in responsible
    return {"ok": True, "concerns_me": concerns_me, "me_id": me_id,
            "responsible_ids": list(responsible)}


def _label(out: dict[str, Any], fetch: Any, gmail: Any, apply: bool, *,
           processed: bool = False, skipped: str | None = None) -> dict[str, Any]:
    """Put the one label this outcome calls for — only with ``apply``, and confirmed by read-back."""
    if not apply:
        out["label"] = {"attempted": False, "detail": "Without --apply nothing is labelled."}
        return out
    message_id = out.get("message_id")
    if gmail is None or not message_id:
        out["label"] = {"ok": False,
                        "reason": fetch.GMAIL_NOT_CONFIGURED if gmail is None else "no_message_id"}
    elif processed:
        out["label"] = fetch.mark_processed(message_id, client=gmail)
    else:
        out["label"] = fetch.mark_skipped(message_id, skipped or "", client=gmail)
    if not out["label"].get("ok"):
        # What Jira did stays in the envelope. The next pass decides again, finds the issue and
        # the anchors, and duplicates nothing.
        out["outcome"] = out.get("reason") or out.get("action")
        out["ok"] = False
        out["reason"] = LABEL_FAILED
    return out


def process(message: dict[str, Any] | None = None, *, message_id: str | None = None,
            apply: bool = False, gmail: Any = None, wrike: Any = None, search: Any = None,
            creator: Any = None, writer: Any = None, db_path: Path | str | None = None,
            lock_dir: Path | str | None = None) -> dict[str, Any]:
    """One notification, end to end. Writes only when ``apply`` is true."""
    fetch = _sibling("fetch_wrike_notifications")
    parser = _sibling("parse_wrike_notification")
    decide = _sibling("decide_wrike_action")
    create = _sibling("create_jira_for_task")
    copy = _sibling("copy_wrike_comments")

    out: dict[str, Any] = {"ok": False, "apply": bool(apply), "stage": "message"}

    # 1. The message.
    if message is None:
        if not message_id:
            return {**out, "reason": NO_MESSAGE,
                    "detail": "Give --message-id or --message-json."}
        if gmail is None:
            return {**out, "reason": fetch.GMAIL_NOT_CONFIGURED, "message_id": message_id}
        try:
            message = gmail.metadata_message(message_id)
        except fetch.GmailFetchError as exc:
            return {**out, **_gmail_failure(fetch, exc, message_id)}
    if not isinstance(message, dict):
        return {**out, "reason": MESSAGE_UNREADABLE}

    # 2. What the e-mail is.
    seen = parser.parse(message)
    out.update(stage="parse", message_id=seen.get("message_id") or message_id,
               task_api_id=seen.get("task_api_id"), notification=seen)
    if not seen.get("ok"):
        out.update(ok=True, action="skip", reason=NOT_A_WRIKE_NOTIFICATION,
                   skip_reason=seen.get("reason"))
        return _label(out, fetch, gmail, apply, skipped=seen.get("reason"))
    task = seen["task_api_id"]

    # 3. What already exists. Read-only.
    plan = create.plan(message, search=search, wrike=wrike, db_path=db_path)
    action, verdict = plan["action"], plan["verdict"]
    out.update(stage="decide", verdict=verdict, plan=plan)

    if action == create.ACTION_CREATE:
        # 4. The gate, on the create branch only.
        out["stage"] = "gate"
        who = _identity(wrike)
        out["identity"] = who
        if not who["ok"]:
            return {**out, "reason": who["reason"]}
        concern = _responsibility(wrike, task, who["me_id"])
        out["responsibility"] = concern
        if not concern["ok"]:
            return {**out, "reason": concern["reason"]}
        if not concern["concerns_me"]:
            out.update(ok=True, action="skip", reason=NOT_RESPONSIBLE,
                       detail="The task has no issue and my id is not in its responsibleIds, so "
                              "no issue is created.")
            return _label(out, fetch, gmail, apply, skipped=NOT_RESPONSIBLE)

        # 5. Create. The pass stops here: see NOT COPIED IN THE SAME PASS AS A CREATE.
        out["stage"] = "create"
        created = create.run(message, apply=apply, search=search, wrike=wrike,
                             creator=creator, db_path=db_path, lock_dir=lock_dir)
        out["create"] = created
        if created.get("reason"):
            return {**out, "reason": WRITE_STOPPED, "write_reason": created["reason"]}
        if not apply:
            out.update(ok=True, action="describe")
            return out
        if not created.get("applied"):
            return {**out, "reason": WRITE_STOPPED, "write_reason": "not_applied"}
        out.update(ok=True, action="created")
        return _label(out, fetch, gmail, apply, processed=True)

    if action == create.ACTION_REPAIR_LINK:
        # A label-only issue is a live issue: repaired, then copied, without the gate.
        out["stage"] = "repair"
        repaired = create.run(message, apply=apply, search=search, wrike=wrike,
                              creator=creator, db_path=db_path, lock_dir=lock_dir)
        out["repair"] = repaired
        if repaired.get("reason"):
            return {**out, "reason": WRITE_STOPPED, "write_reason": repaired["reason"]}
        if apply and not repaired.get("applied"):
            return {**out, "reason": WRITE_STOPPED, "write_reason": "repair_not_applied"}
    elif not (action == create.ACTION_NOTHING and verdict == decide.LINKED_AND_ALIVE):
        return {**out, "reason": PLAN_STOPPED,
                "detail": "The decision does not lead to a write; see plan."}

    # 5. Copy onto the live issue — no gate here, on purpose.
    out["stage"] = "copy"
    copied = copy.run(message, apply=apply, search=search, wrike=wrike, writer=writer,
                      db_path=db_path, lock_dir=lock_dir)
    out["copy"] = copied
    if copied.get("reason"):
        return {**out, "reason": WRITE_STOPPED, "write_reason": copied["reason"]}
    if not apply:
        out.update(ok=True, action="describe")
        return out
    if copied.get("action") != copy.ACTION_COPY:
        return {**out, "reason": WRITE_STOPPED, "write_reason": f"copy_{copied.get('action')}"}
    out.update(ok=True, action="copied")
    if copied.get("comments_certain") is False:
        # Wrike returned no comments. Nothing is copied and the e-mail is still labelled — see AN
        # EMPTY COMMENT LIST IS LABELLED — but the envelope says the list was empty and what that
        # does not prove, where nobody has to dig into ``copy`` to find it.
        out["comments_certain"] = False
        out["comments_means"] = copied.get("comments_means")
    return _label(out, fetch, gmail, apply, processed=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Process one Wrike notification from Gmail.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--message-id", help="Gmail message id, read with the piece-1 reader.")
    source.add_argument("--message-json", help="A local file holding one format=metadata message.")
    parser.add_argument("--apply", action="store_true",
                        help="Write: create or copy in Jira, record the link, label the e-mail.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    message = None
    if args.message_json:
        try:
            message = json.loads(Path(args.message_json).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"ok": False, "stage": "message", "reason": MESSAGE_UNREADABLE,
                              "error": f"{type(exc).__name__}: {exc}"}))
            return 1
    fetch = _sibling("fetch_wrike_notifications")
    gmail = fetch.GmailFetchClient.from_env() if (args.message_id or args.apply) else None
    wrike = _sibling("wrike_api").WrikeClient.from_env()
    search = _sibling("jira_label_search").JiraLabelSearch.from_env()
    # The two Jira writers exist only with --apply: without it there is nothing that could write.
    creator = _sibling("create_jira_for_task").JiraIssueCreator.from_env() if args.apply else None
    writer = _sibling("copy_wrike_comments").JiraCommentWriter.from_env() if args.apply else None
    out = process(message, message_id=args.message_id, apply=args.apply, gmail=gmail,
                  wrike=wrike, search=search, creator=creator, writer=writer)
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0 if out.get("ok") else 1


__all__ = [
    "LABEL_FAILED",
    "MESSAGE_UNREADABLE",
    "NOT_A_WRIKE_NOTIFICATION",
    "NOT_RESPONSIBLE",
    "NO_MESSAGE",
    "PLAN_STOPPED",
    "RESPONSIBILITY_UNKNOWN",
    "WRIKE_UNREADABLE",
    "WRITE_STOPPED",
    "main",
    "process",
]


if __name__ == "__main__":
    sys.exit(main())
