# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python. Without one it falls back to sys.executable.
"""Decide whether a Gmail message is a Wrike task notification, and pull the task id out.

This is the trigger of the flow. The brief for the Wrike → Jira flow says it in as many words: *"the trigger is in Email, we
do not poll Wrike."* So the mailbox is what starts work, and this module is the thing that reads
one message and answers a single question — **has something happened on a task, and which
task?**

**It deliberately does not say WHAT happened, and that is the decision of this ticket.** The headers
were measured twice. When this was written, ``notificationType`` was ``"Regular"`` and
``entityType`` ``"Task"`` on every notification, and ``watchReason`` ``"Assignee"`` on every one.
On 14 September 2026, over the 12 notifications left unprocessed in the real mailbox,
``notificationType`` and ``entityType`` were still the same everywhere, but **``watchReason`` was
not**: ``"Assignee"`` on 8 and ``"Mentioned"`` on 4. So the header does tell one thing apart — why
the recipient was notified.

It is still not what the flow uses, because of what it answers. ``watchReason`` says why the e-mail
was sent to me — I am an assignee, I was mentioned — not whether the task is mine. Whether a task
concerns me is read from the task itself, its ``responsibleIds`` in the Wrike API: the source of
truth, and independent of how or in which language the notification was worded. Beyond that one
field, an assignment, a comment and attachments being added still look alike by header and by
subject.

The one signal that does separate them is the **first line of the body** — "asignó la tarea a…",
"@Mia Muster Würdest du…", "Image-….png (novedad)". It is in the language of the recipient's
Wrike account, so a parser built on it breaks when somebody changes their locale, and **it breaks
silently**: every notification would still parse, and every one would come back as an unknown
kind, or worse, as the wrong one. So this module reads none of it.

What the e-mail means here is exactly "something happened on task X". Deciding what to do about
it is the API's job — it is the source of truth, it is already written (``wrike_api``), and asking it
costs one read.

**Nothing here touches Gmail.** No OAuth, no network, no environment beyond one allowlist. It is
handed a message that somebody else downloaded and it returns a verdict, which is what makes the
whole of it testable. Fetching and labelling are the next subtask.

Read-only in the strongest sense: this module has no side effects at all. It does not reply, it
does not delete, it does not send, and it does not label.

Configuration:

    WRIKE_TASKS_ALLOWED_SENDERS   comma-separated; this parser falls back to "wrike@wrike.com",
                                  but the listing (fetch_wrike_notifications) has no default and
                                  refuses to query the mailbox without it

**This flow has its own sender allowlist, its own labels, and does not share the Freshdesk
flow's.** Two senders arrive from Wrike and only one of them is work: ``wrike@wrike.com`` carries
task notifications, while ``noreply@wrike.com`` carries workspace invitations and password
resets — measured, four of the seven Wrike messages in the mailbox. And the marker labels are
``mossy-wrike-processed`` / ``mossy-wrike-skipped`` rather than the Freshdesk flow's
``mossy-processed`` / ``mossy-skipped``, because the Freshdesk ones mean "a Jira issue exists for
this e-mail", which for a Wrike notification would be *almost* true — and "almost true" is how
two flows start reading one marker in different ways. Like the Freshdesk labels, they are the
operator's: created by hand in Gmail, never by the agent, and a missing one is reported as
``label_not_found`` rather than invented.

NOTED AND NOT FIXED, because it is not this ticket's to change: ``GMAIL_ALLOWED_SENDERS`` is a
single shared variable, and ``gmail-freshdesk-bugs`` builds its query as ``from:`` over it. If
somebody adds ``wrike@wrike.com`` there, **that flow will start seeing Wrike notifications** and
marking them with its own labels. Nothing here does that, and the Freshdesk flow is untouched —
it works, and nobody asked for it to change.

What the caller gets, and why each field is in it:

* ``is_wrike_task_notification`` — the question this module exists to answer.
* ``task_api_id`` — from the header, and only from the header. See below.
* ``message_id`` / ``thread_id`` — Gmail's own ids, so the next step can label the exact message
  and not process it twice. They are not in the MIME; they come from the API envelope.
* ``metadata`` — the whole parsed header, so a later ticket that does want ``watchReason`` or
  ``lastChangeTime`` does not have to re-parse anything.

**The task id comes from the header and can never come from the body.** ``taskApiId`` lives in
``X-Wrike-Metadata`` and nowhere else: measured, the string ``MAAAAA`` appears zero times in the
body of all three notifications. What the body does carry is ``open.htm?id=1234567890`` — the
permalink number, which is a **different identifier**, and which the Wrike API refuses with
``400 Invalid Task ID``, measured. A parser that fell back to the body would therefore pick up a
number that looks like an id, is not one, and fails at the next step rather than here.
"""

from __future__ import annotations

import base64
import binascii
import email
import email.policy
import email.utils
import json
import os
from typing import Any

#: The header that carries everything. Wrike spells it exactly this way; lookups are
#: case-insensitive because Gmail's API does not promise the case it stores.
METADATA_HEADER = "X-Wrike-Metadata"

#: The field inside that header holding the id the Wrike API accepts (``MAAAAAEXmain``).
TASK_API_ID_FIELD = "taskApiId"

#: The other identifier in the same header — the number a permalink shows. Kept for reference
#: and **never** used as a task id: the API answers 400 to it, measured.
PERMALINK_ID_FIELD = "taskId"

#: Task notifications come from here. ``noreply@wrike.com`` is the same company and not the same
#: thing: invitations and password resets, which are not work.
DEFAULT_ALLOWED_SENDERS = ("wrike@wrike.com",)

#: Reasons a message is not a task notification. Each is a different thing that happened, and
#: they are told apart because a caller logging them should be able to see which.
NOT_A_SENDER = "not_a_task_sender"
NO_METADATA_HEADER = "no_wrike_metadata_header"
METADATA_UNREADABLE = "wrike_metadata_unreadable"
NO_TASK_API_ID = "no_task_api_id"


def allowed_senders() -> list[str]:
    """``WRIKE_TASKS_ALLOWED_SENDERS``, or the measured default.

    Its own variable, not ``GMAIL_ALLOWED_SENDERS``: that one is shared with the Freshdesk flow,
    which uses it to build its search. Widening it here would widen that.
    """
    raw = (os.environ.get("WRIKE_TASKS_ALLOWED_SENDERS") or "").strip()
    if not raw:
        return list(DEFAULT_ALLOWED_SENDERS)
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _headers_from_payload(message: dict[str, Any]) -> dict[str, str]:
    """Gmail's ``payload.headers`` list, folded into a case-insensitive mapping."""
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return {}
    headers: dict[str, str] = {}
    for entry in payload.get("headers") or []:
        if isinstance(entry, dict) and entry.get("name"):
            headers[str(entry["name"]).lower()] = str(entry.get("value") or "")
    return headers


def _headers_from_raw(message: dict[str, Any]) -> dict[str, str]:
    """The same mapping out of a ``format=RAW`` message, which is base64url MIME.

    Supported because it is what ``get_message(messageFormat="RAW")`` returns and what an
    ``.eml`` on disk is; the caller should not have to convert one into the other to ask this
    module a question.
    """
    raw = message.get("raw")
    if not isinstance(raw, (str, bytes)):
        return {}
    if isinstance(raw, str):
        raw = raw.encode("ascii", errors="ignore")
    try:
        decoded = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
    except (binascii.Error, ValueError):
        return {}
    parsed = email.message_from_bytes(decoded, policy=email.policy.default)
    return {str(k).lower(): str(v) for k, v in parsed.items()}


def headers_of(message: dict[str, Any]) -> dict[str, str]:
    """Every header of a Gmail message, whichever format it was fetched in."""
    return _headers_from_payload(message) or _headers_from_raw(message)


def sender_address(headers: dict[str, str]) -> str:
    """The bare address out of ``From``, which arrives RFC-2047 encoded with a display name."""
    _name, address = email.utils.parseaddr(headers.get("from", ""))
    return address.strip().lower()


def parse(message: dict[str, Any], *, senders: list[str] | None = None) -> dict[str, Any]:
    """One Gmail message in, a verdict out. Never raises for a message it does not understand.

    A malformed notification is an ordinary thing to find in a mailbox — the mailbox is not ours
    and nobody promised us its contents — so every failure comes back as a ``reason`` a caller
    can log and skip on, never as an exception that stops a pass over ten messages at the third.
    """
    result: dict[str, Any] = {
        "ok": False,
        "is_wrike_task_notification": False,
        "message_id": str(message.get("id") or "") or None,
        "thread_id": str(message.get("threadId") or "") or None,
    }
    headers = headers_of(message)
    address = sender_address(headers)
    result["sender"] = address or None

    allowed = [s.lower() for s in (senders if senders is not None else allowed_senders())]
    if address not in allowed:
        # noreply@wrike.com lands here: same company, not this flow's work.
        result["reason"] = NOT_A_SENDER
        return result

    raw_metadata = headers.get(METADATA_HEADER.lower())
    if raw_metadata is None or not raw_metadata.strip():
        # A Wrike message that is not a task notification at all — nothing to be alarmed by,
        # and told apart from a header that is present and broken.
        result["reason"] = NO_METADATA_HEADER
        return result

    try:
        metadata = json.loads(raw_metadata)
    except (json.JSONDecodeError, TypeError):
        # Present and unreadable is a different fact from absent: this one means Wrike changed
        # something, or the message was mangled, and somebody should look.
        result["reason"] = METADATA_UNREADABLE
        result["raw_metadata"] = raw_metadata[:500]
        return result
    if not isinstance(metadata, dict):
        result["reason"] = METADATA_UNREADABLE
        result["raw_metadata"] = raw_metadata[:500]
        return result

    result["metadata"] = metadata
    task_api_id = str(metadata.get(TASK_API_ID_FIELD) or "").strip()
    if not task_api_id:
        # The header is there and readable and carries no task. Never rescued from the body:
        # the body's only identifier is the permalink number, which the API refuses.
        result["reason"] = NO_TASK_API_ID
        return result

    result["ok"] = True
    result["is_wrike_task_notification"] = True
    result["task_api_id"] = task_api_id
    # Carried for reference only. It is NOT a task id: GET /tasks/<this> is a 400, measured.
    permalink_id = metadata.get(PERMALINK_ID_FIELD)
    if permalink_id is not None:
        result["permalink_id"] = str(permalink_id)
    result["rfc822_message_id"] = headers.get("message-id") or None
    result["subject"] = headers.get("subject") or None
    return result


__all__ = [
    "DEFAULT_ALLOWED_SENDERS",
    "METADATA_HEADER",
    "METADATA_UNREADABLE",
    "NOT_A_SENDER",
    "NO_METADATA_HEADER",
    "NO_TASK_API_ID",
    "PERMALINK_ID_FIELD",
    "TASK_API_ID_FIELD",
    "allowed_senders",
    "headers_of",
    "parse",
    "sender_address",
]
