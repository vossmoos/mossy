# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python. Without one it falls back to sys.executable.
"""Parse a Freshdesk thread notification e-mail into the ids and the text it carries.

This is step 2 of the flow, and it exists because the link in the e-mail is not enough. Measured
against three real notifications from ticket 101:

* The link only ever carries ``?thread=N`` — an index, not an id. It cannot be handed to the API.
* The ids that matter are in headers: ``X-FD-Ticket-Id`` and ``X-FD-Note-Id``.
* **The message text is often not in the API at all.** Of three thread messages, one came back
  from ``get_ticket_conversations`` and two did not: only the message that happened to *be* the
  thread's anchoring note is a conversation. So the e-mail is the source of the text, not a
  convenience — and it carried 2005 characters over three paragraphs without truncating.

**``X-FD-Note-Id`` is the note the thread hangs from, never the message we were mentioned in.**
Two different notifications from the same thread carry the same value while their texts differ.
It is returned as ``thread_anchor_note_id`` for exactly that reason: passing it to
``analyze_context.py --conversation-id`` would analyse the anchor note and file a Jira issue
about the wrong text, which is the failure that step 3 of ``SKILL.md`` warns about.

Two notification kinds arrive in the same mailbox and both matter:

    X-FD-Type: 31   "Mentioned in private in …"  — we were tagged
    X-FD-Type: 30   "New reply in …"             — a reply on a thread we follow

Their bodies read differently ("tagged you in a thread" vs "There is a new reply on your
thread") but the message itself sits in the same place, so one extraction covers both.

Self-contained (like the other scripts here): no ``import mossy``, no ``python-dotenv``, no
third-party HTML parser — ``email`` and ``re`` from the standard library are enough. Nothing is
read from the environment and nothing is written anywhere: this script only reads one file.

**Only mentions are processed by default.** A reply belongs to a thread that already produced
its own mention notification, so acting on both files the same work twice — measured: the reply
on thread 2 produced a ``problem`` almost identical to the mention that opened it, because both
draw their context from the same ticket thread. A reply is reported as
``notification_type_skipped``, carrying what it was, so the drop is visible in a log. Pass
``--require-type any`` to process them.

CLI (named flags only):

    parse_mention_email.py --eml-path /path/to/notification.eml
    parse_mention_email.py --eml-path /path/to/notification.eml --require-type any
"""

from __future__ import annotations

import argparse
import email
import email.policy
import html as html_module
import json
import re
import sys
from pathlib import Path
from typing import Any

#: The headers this parser refuses to guess. A missing one is reported by name.
REQUIRED_HEADERS = ("X-FD-Ticket-Id", "X-FD-Note-Id", "X-FD-Type")

#: ``X-FD-Type`` values seen on this account, and what they mean. An unknown value is an error
#: rather than a default: a third kind of notification would otherwise be silently treated as a
#: mention and turned into a Jira issue nobody asked for.
NOTIFICATION_TYPES = {"31": "mention", "30": "reply"}

#: What ``--require-type`` accepts. ``mention`` is the default because a reply belongs to a
#: thread that already produced its own mention: processing both files the same work twice.
#: Measured — the reply on thread 2 produced a ``problem`` almost identical to the mention that
#: opened it, because both lean on the same ticket thread for context. ``any`` exists so that
#: turning replies back on is a flag, not a code change.
REQUIRE_TYPES = ("mention", "any")

#: The label Freshdesk puts immediately before the message. Present in both kinds, measured.
_TEXT_START = re.compile(r">\s*Thread message\s*</p>", re.I)

#: The footer rule that closes the message block, ahead of the "View thread" button.
_TEXT_END = re.compile(r"<hr\b", re.I)

#: ``…/a/tickets/101?thread=2&follow=email`` — the index, from the link.
_THREAD_IN_URL = re.compile(r"[?&]thread=(\d+)")

#: ``<uuid.account.ticket.thread.note@thread-notification.freshdesk.com>`` — the same index,
#: from the Message-ID. Both are read and compared: they are independent copies of one fact,
#: and a disagreement means the e-mail is not shaped the way this parser assumes.
_MESSAGE_ID_CORE = re.compile(r"<?([^<>@]+)@")

#: Any link into the Freshdesk agent view of a ticket.
_TICKET_URL = re.compile(r'href=["\'](https?://[^"\']*/a/tickets/\d+[^"\']*)["\']', re.I)


def _fail(reason: str, **extra: Any) -> dict[str, Any]:
    """One shape for every refusal, so a caller never has to guess whether it worked."""
    return {"ok": False, "reason": reason, **extra}


def _html_body(message: email.message.EmailMessage) -> str | None:
    """The HTML part. These notifications are single-part text/html — no text/plain exists."""
    if not message.is_multipart():
        return message.get_content() if message.get_content_type() == "text/html" else None
    for part in message.walk():
        if part.get_content_type() == "text/html":
            return part.get_content()
    return None


def _visible_text(fragment: str) -> str:
    """Tags out, entities decoded, whitespace collapsed.

    Tags become a space rather than nothing: the message is built from nested ``<div>``s, and
    dropping them outright would glue the end of one paragraph to the start of the next.
    """
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", fragment)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html_module.unescape(text).split())


def extract_mention_text(body_html: str) -> str | None:
    """The message between the "Thread message" label and the footer rule.

    Returns ``None`` when either anchor is missing — **never an empty string**. If Freshdesk
    changes this template the caller has to hear about it: an empty mention would otherwise
    become a Jira issue with no description, which reads like a real one.
    """
    start = _TEXT_START.search(body_html)
    if start is None:
        return None
    rest = body_html[start.end():]
    end = _TEXT_END.search(rest)
    fragment = rest[:end.start()] if end else rest
    text = _visible_text(fragment)
    return text or None


def _thread_from_message_id(message_id: str) -> str | None:
    """The 4th dot-separated segment of the Message-ID local part, or ``None``.

    ``db33c87a-…-23427e735683.1000000.101.1.900000000001`` → ``"1"``. The uuid holds dashes,
    not dots, so the segments line up as uuid, account, ticket, thread, note.
    """
    core = _MESSAGE_ID_CORE.search(message_id or "")
    if core is None:
        return None
    segments = core.group(1).split(".")
    if len(segments) < 5 or not segments[3].isdigit():
        return None
    return segments[3]


def parse(eml_path: str | Path, *, require_type: str = "mention") -> dict[str, Any]:
    """Read one notification e-mail and return its ids and text, or a named refusal.

    ``require_type`` decides which notifications are worth processing. The default keeps only
    mentions; a reply is reported as skipped rather than parsed, so the caller can log what was
    dropped and why instead of silently seeing nothing.
    """
    path = Path(eml_path)
    if not path.is_file():
        return _fail("file_not_found", eml_path=str(path))
    try:
        message = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    except Exception as exc:  # noqa: BLE001 — an unreadable file is one reported condition
        return _fail("eml_unreadable", detail=str(exc)[:200])

    for header in REQUIRED_HEADERS:
        if not (message.get(header) or "").strip():
            return _fail(f"missing_header:{header}")

    raw_type = (message.get("X-FD-Type") or "").strip()
    if raw_type not in NOTIFICATION_TYPES:
        return _fail("unknown_notification_type", x_fd_type=raw_type,
                     known=sorted(NOTIFICATION_TYPES))

    notification_type = NOTIFICATION_TYPES[raw_type]
    if require_type not in REQUIRE_TYPES:
        return _fail("unknown_require_type", require_type=require_type, known=list(REQUIRE_TYPES))
    if require_type != "any" and notification_type != require_type:
        # Not an error: a deliberate skip, reported with what it was so the drop is visible in a
        # log rather than looking like a parse that found nothing.
        return _fail("notification_type_skipped",
                     notification_type=notification_type,
                     required=require_type,
                     ticket_id=int((message.get("X-FD-Ticket-Id") or "").strip()))

    body_html = _html_body(message)
    if body_html is None:
        return _fail("html_body_not_found")

    url_match = _TICKET_URL.search(body_html)
    if url_match is None:
        return _fail("freshdesk_url_not_found")
    freshdesk_url = html_module.unescape(url_match.group(1))

    thread_in_url = _THREAD_IN_URL.search(freshdesk_url)
    if thread_in_url is None:
        return _fail("thread_id_not_in_url", freshdesk_url=freshdesk_url)
    thread_from_url = thread_in_url.group(1)

    thread_from_mid = _thread_from_message_id(message.get("Message-ID") or "")
    if thread_from_mid is None:
        return _fail("message_id_unparseable", message_id=message.get("Message-ID"))

    # Two independent copies of the same index. They agree in every measured e-mail; if they
    # ever disagree the assumption behind this parser is wrong, and guessing which one to
    # believe would be worse than stopping.
    if thread_from_url != thread_from_mid:
        return _fail("thread_id_mismatch",
                     thread_id_from_url=thread_from_url,
                     thread_id_from_message_id=thread_from_mid)

    mention_text = extract_mention_text(body_html)
    if mention_text is None:
        return _fail("mention_text_not_found")

    return {
        "ok": True,
        "ticket_id": int((message.get("X-FD-Ticket-Id") or "").strip()),
        "thread_id": int(thread_from_url),
        # NOT a conversation id. The note the thread hangs from, which repeats across every
        # notification of that thread while the text changes. Do not pass it to
        # analyze_context.py --conversation-id.
        "thread_anchor_note_id": int((message.get("X-FD-Note-Id") or "").strip()),
        "notification_type": notification_type,
        "mention_text": mention_text,
        "freshdesk_url": freshdesk_url,
        "subject": (message.get("Subject") or "").strip(),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Parse a Freshdesk thread notification e-mail into ids and text.")
    parser.add_argument("--eml-path", required=True,
                        help="Path to the .eml file. Named flag; no positionals.")
    parser.add_argument("--require-type", choices=list(REQUIRE_TYPES), default="mention",
                        help="Which notifications to process. Default 'mention': a reply "
                             "belongs to a thread that already produced its own mention.")
    return parser


def main() -> None:  # pragma: no cover - exercised end to end, not in the offline suite
    args = _build_parser().parse_args()
    result = parse(args.eml_path, require_type=args.require_type)
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
