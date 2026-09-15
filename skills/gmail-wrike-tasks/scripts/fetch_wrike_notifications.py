# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python. Without one it falls back to sys.executable.
"""Fetch unprocessed Wrike task notifications from Gmail, and mark one done (the Wrike → Jira flow, piece 1).

The trigger of the flow. The brief for the Wrike → Jira flow, literally: *"the agent should monitor Gmail inbox"* and *"the
trigger is in Email, we do not poll Wrike."* This module is what reads the mailbox. What a message
means is ``parse_wrike_notification``'s job; what to do about it belongs to the rest of the flow.

PORTED FROM ``gmail-freshdesk-bugs/scripts/fetch_mention_emails.py``, NOT SHARED

Skills do not import each other — each is transferred on its own — and the skill script
runtime has no ``mossy`` on its path, so ``mossy/capabilities/gmail.py`` cannot be reached from
here either. So this file carries its own OAuth client, as the Freshdesk reader does, over the
**same three credentials and the same mailbox**: ``GMAIL_CLIENT_ID``, ``GMAIL_CLIENT_SECRET``,
``GMAIL_REFRESH_TOKEN``. No new client in Google Cloud. Nothing of the Freshdesk flow is touched.

Kept as it was: the OAuth refresh, the listing, the exclusions inside the query, the split between
a listing that only reads and marking that writes, labels that are never created, the read-back.
Changed: the sender list and the labels are this flow's own, the message is fetched as
``format=metadata`` instead of ``raw``, nothing is written to disk, and every failure — above all
a refused refresh token — comes back as its own reason instead of an exception or an empty list.

**Three subcommands, and the split is the point.**

``list`` finds the notifications nothing has been done with yet and returns their headers. It
**never labels anything**: the processed label means "this task is up to date in Jira", and
putting it on at fetch time would make that a lie for every pass that fails afterwards — the
e-mail would be skipped forever with nothing to show for it. Listing is a read.

``mark-processed`` and ``mark-skipped`` put a label on, and are called at the end of a pass. Both
are writes to the operator's mailbox; the first real one is run by hand. The two labels are not
interchangeable: processed claims the work is done, skipped says the e-mail was never work.

EXCLUSION IS IN THE QUERY, NOT AFTER IT

Dedupe is the labels themselves, sent to Gmail as ``-label:<processed> -label:<skipped>``. An
e-mail already dealt with is never returned, so a caller that forgets to check cannot pick it up.
Filtering the results afterwards would download what was already handled and depend on every
caller remembering to drop it.

LABELS ARE NEVER CREATED

``mossy-wrike-processed`` and ``mossy-wrike-skipped`` belong to the operator and are created by
hand in Gmail. A missing one is ``label_not_found`` and the pass stops; creating it from code would
hide a misconfiguration behind a label nobody chose. A label only counts once a fresh read of the
message shows it (``label_not_confirmed`` otherwise).

AN EMPTY SENDER LIST IS REFUSED — AND THAT DIFFERS FROM THE PARSER ON PURPOSE

``parse_wrike_notification.allowed_senders`` falls back to ``wrike@wrike.com`` when the variable is
empty. **This module does not, and the two must not be "unified".** The parser judges a message
that has already been downloaded: a default there cannot widen what is read from the mailbox. This
module decides what is read. A sender list is a gate, and a convenient default opens it without
anybody deciding to — here an empty ``WRIKE_TASKS_ALLOWED_SENDERS`` would otherwise mean either a
query nobody chose or, as it once did in the Freshdesk reader, one with no ``from:`` at all, which
matches the whole mailbox. So an empty list reads nothing and costs zero Gmail calls.

FORMAT=METADATA: THE HEADERS, AND NOT THE BODY

The parser reads ``From``, ``Message-ID``, ``Subject`` and ``X-Wrike-Metadata`` — the task id lives
in that header and nowhere else (see the notification parser) — and it accepts ``payload.headers`` as well as ``raw``.
So the body is not downloaded: less traffic, and less of the client's data passing through here.

★ MEASURED. **``format=metadata`` returns ``X-Wrike-Metadata`` inside ``payload.headers``, and no
body.** Measured by hand on 14 September 2026 against the real mailbox, on one real notification:
status 200, 25 headers, ``X-Wrike-Metadata`` among them, and the payload
carried no ``body`` and no ``parts`` — the body is not downloaded. ``parse_wrike_notification.parse``
on that message returned ``ok`` with its ``task_api_id``. Earlier that day the read
could not run: the Gmail credentials in ``.env`` pointed at another mailbox. One message is one
message: if Wrike ever drops the header, the parser cannot read the task id and this module is
built on the wrong format — that changes the decision, not a line of code.

★ A DEAD TOKEN MUST NOT LOOK LIKE AN EMPTY MAILBOX. While the Google Cloud app behind these
credentials stays in **Testing**, its refresh token **expires every 7 days**; moving the app out of
Testing is not this flow's to do. A reader that let that failure through as an empty listing
would return no e-mails and no visible error — indistinguishable from "there is nothing to
process", and the flow would sit idle, apparently healthy, until somebody noticed. That is the
collapse this flow has been avoiding since the label search, where an empty page meant three different
things. So a refused refresh is ``gmail_token_refused``, with Google's status and error code (never
the token), in its own reason — the way a Wrike 401 answers ``wrike_unreadable`` rather
than a sentence about something else. ``ok: True`` with ``count: 0`` is only ever returned after the
token was accepted and Gmail answered the listing.

Environment:

    GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN   OAuth2, all three required
    GMAIL_USER_ID                   mailbox, default "me"
    WRIKE_TASKS_ALLOWED_SENDERS     comma-separated; REQUIRED here, no default (see above)
    WRIKE_TASKS_PROCESSED_LABEL     default "mossy-wrike-processed"
    WRIKE_TASKS_SKIPPED_LABEL       default "mossy-wrike-skipped"

CLI (named flags only):

    fetch_wrike_notifications.py list [--max-results 10]
    fetch_wrike_notifications.py mark-processed --message-id 0a1b2c3d4e5f6072
    fetch_wrike_notifications.py mark-skipped --message-id 0a1b2c3d4e5f6072 \\
        --reason no_wrike_metadata_header
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_API_BASE = "https://gmail.googleapis.com/gmail/v1"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_EXPIRY_MARGIN_SECONDS = 60

DEFAULT_PROCESSED_LABEL = "mossy-wrike-processed"
DEFAULT_SKIPPED_LABEL = "mossy-wrike-skipped"

#: One reason per way of failing, so no failure can be read as another — least of all as an empty
#: mailbox.
NO_ALLOWED_SENDERS = "no_allowed_senders"
GMAIL_NOT_CONFIGURED = "gmail_not_configured"
GMAIL_TOKEN_REFUSED = "gmail_token_refused"
GMAIL_UNREACHABLE = "gmail_unreachable"
GMAIL_LISTING_REFUSED = "gmail_listing_refused"
GMAIL_MESSAGE_REFUSED = "gmail_message_refused"
GMAIL_LABEL_REFUSED = "gmail_label_refused"
LABEL_NOT_FOUND = "label_not_found"
LABEL_NOT_CONFIRMED = "label_not_confirmed"

#: Said wherever a dead token could be mistaken for a quiet mailbox.
TOKEN_REFUSED_IS_NOT_EMPTY = (
    "Google refused the Gmail refresh token, so the mailbox was NOT read. This is not an empty "
    "mailbox and must not be treated as 'nothing to process'. While the Google Cloud app is in "
    "Testing, a refresh token expires after 7 days: renew it and run again."
)


class GmailFetchError(RuntimeError):
    """A Gmail call failed. ``kind`` says which way, so a caller never has to guess.

    ``token``    Google refused the refresh token; its ``error`` code is in ``google_error``
    ``network``  the request never reached Google, so ``status`` is None — never invented
    ``http``     Gmail answered with an error status
    """

    def __init__(self, message: str, *, kind: str, status: int | None = None,
                 google_error: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.google_error = google_error


def allowed_senders() -> list[str]:
    """``WRIKE_TASKS_ALLOWED_SENDERS``, with **no default**.

    Deliberately not ``parse_wrike_notification.allowed_senders``, which has one: see "AN EMPTY
    SENDER LIST IS REFUSED" in the module docstring before changing either.
    """
    raw = os.environ.get("WRIKE_TASKS_ALLOWED_SENDERS") or ""
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _processed_label() -> str:
    """"This task is up to date in Jira." Never set for anything else."""
    return (os.environ.get("WRIKE_TASKS_PROCESSED_LABEL") or "").strip() \
        or DEFAULT_PROCESSED_LABEL


def _skipped_label() -> str:
    """"This e-mail was never work for this flow." A different label from processed, on purpose:
    both stop the message coming back, only one claims work was done."""
    return (os.environ.get("WRIKE_TASKS_SKIPPED_LABEL") or "").strip() \
        or DEFAULT_SKIPPED_LABEL


def sender_filter(senders: list[str]) -> str:
    if not senders:
        return ""
    if len(senders) == 1:
        return f"from:{senders[0]}"
    return "{" + " ".join(f"from:{s}" for s in senders) + "}"


def build_query(senders: list[str], processed_label: str, skipped_label: str) -> str:
    """The Gmail search: the configured senders, minus everything already dealt with.

    Both labels are excluded **in the query**, so a message already handled is never returned.
    ``build_query`` itself stays permissive about an empty sender list; the refusal is one level
    up, in ``list_notifications``, before any client exists.
    """
    parts = [part for part in (
        sender_filter(senders),
        f"-label:{processed_label}",
        f"-label:{skipped_label}",
    ) if part]
    return " ".join(parts)


class GmailFetchClient:
    """The slice of Gmail this step needs: list, read headers, add one label, read it back."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str,
                 user_id: str = "me", timeout_seconds: int = 120) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.user_id = user_id
        self.timeout_seconds = timeout_seconds
        self._access_token: str | None = None
        self._expires_at = 0.0

    @classmethod
    def from_env(cls) -> "GmailFetchClient | None":
        client_id = (os.environ.get("GMAIL_CLIENT_ID") or "").strip()
        client_secret = (os.environ.get("GMAIL_CLIENT_SECRET") or "").strip()
        refresh_token = (os.environ.get("GMAIL_REFRESH_TOKEN") or "").strip()
        if not client_id or not client_secret or not refresh_token:
            return None
        return cls(client_id, client_secret, refresh_token,
                   (os.environ.get("GMAIL_USER_ID") or "").strip() or "me")

    def access_token(self) -> str:
        """A short-lived access token from the refresh token. **A refusal raises, kind token.**"""
        if self._access_token and time.time() < self._expires_at - _EXPIRY_MARGIN_SECONDS:
            return self._access_token
        data = urllib.parse.urlencode({
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token",
        }).encode("utf-8")
        req = urllib.request.Request(
            _TOKEN_URL, data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read() if exc.fp else b""
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {}
            google_error = body.get("error") if isinstance(body, dict) else None
            raise GmailFetchError(
                f"Google refused the Gmail refresh token (HTTP {exc.code}"
                + (f", {google_error}" if google_error else "") + ").",
                kind="token", status=exc.code, google_error=google_error) from exc
        except urllib.error.URLError as exc:
            raise GmailFetchError(f"Google's token endpoint is unreachable: {exc.reason}.",
                                  kind="network") from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise GmailFetchError("Google answered the refresh without an access token.",
                                  kind="token", status=200)
        expires_in = payload.get("expires_in")
        try:
            lifetime = float(expires_in) if expires_in is not None else 3600.0
        except (TypeError, ValueError):
            lifetime = 3600.0
        self._access_token = token
        self._expires_at = time.time() + lifetime
        return token

    def call(self, method: str, path: str, *, query: dict[str, str] | None = None,
             body: Any = None) -> tuple[int, Any]:
        url = f"{_API_BASE}{path}" + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.access_token()}",
            "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read()
                return resp.getcode() or 200, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read() if exc.fp else b""
            try:
                return exc.code, json.loads(raw) if raw else None
            except json.JSONDecodeError:
                return exc.code, {"_raw": raw.decode("utf-8", errors="replace")}
        except urllib.error.URLError as exc:
            raise GmailFetchError(f"Gmail is unreachable: {exc.reason}.", kind="network") from exc

    def list_ids(self, query: str, max_results: int) -> list[str]:
        status, body = self.call("GET", f"/users/{self.user_id}/messages",
                                 query={"q": query, "maxResults": str(max_results)})
        if status >= 400:
            raise GmailFetchError(f"Gmail refused the listing: {body}", kind="http", status=status)
        messages = body.get("messages") or [] if isinstance(body, dict) else []
        return [m["id"] for m in messages if isinstance(m, dict) and m.get("id")]

    def metadata_message(self, message_id: str) -> dict[str, Any]:
        """One message as ``format=metadata``: ids, labels and headers, no body."""
        status, body = self.call("GET", f"/users/{self.user_id}/messages/{message_id}",
                                 query={"format": "metadata"})
        if status >= 400 or not isinstance(body, dict):
            raise GmailFetchError(f"Gmail refused message {message_id}: {body}",
                                  kind="http", status=status)
        return body

    def label_id(self, name: str) -> str | None:
        status, body = self.call("GET", f"/users/{self.user_id}/labels")
        if status >= 400:
            raise GmailFetchError(f"Gmail refused the label listing: {body}",
                                  kind="http", status=status)
        for label in (body.get("labels") or [] if isinstance(body, dict) else []):
            if isinstance(label, dict) and label.get("name") == name:
                return label.get("id")
        return None

    def add_label(self, message_id: str, label_id: str) -> list[str]:
        status, body = self.call(
            "POST", f"/users/{self.user_id}/messages/{message_id}/modify",
            body={"addLabelIds": [label_id]})
        if status >= 400:
            raise GmailFetchError(f"Gmail refused to label {message_id}: {body}",
                                  kind="http", status=status)
        return list(body.get("labelIds") or []) if isinstance(body, dict) else []

    def label_ids_of(self, message_id: str) -> list[str]:
        status, body = self.call("GET", f"/users/{self.user_id}/messages/{message_id}",
                                 query={"format": "metadata"})
        if status >= 400:
            raise GmailFetchError(f"Gmail refused message {message_id}: {body}",
                                  kind="http", status=status)
        return list(body.get("labelIds") or []) if isinstance(body, dict) else []


def _failure(exc: GmailFetchError, refused_reason: str, extra: dict[str, Any]) -> dict[str, Any]:
    """A failed call as an envelope. Never carries ``count`` or ``messages``: a failure has none."""
    if exc.kind == "token":
        return {"ok": False, **extra, "reason": GMAIL_TOKEN_REFUSED, "status": exc.status,
                "google_error": exc.google_error, "detail": TOKEN_REFUSED_IS_NOT_EMPTY}
    if exc.kind == "network":
        return {"ok": False, **extra, "reason": GMAIL_UNREACHABLE, "status": None,
                "detail": f"{exc} Nothing was read; this is not an empty mailbox."}
    return {"ok": False, **extra, "reason": refused_reason, "status": exc.status,
            "detail": str(exc)}


def list_notifications(max_results: int = 10,
                       client: GmailFetchClient | None = None) -> dict[str, Any]:
    """The unprocessed notifications, as ``format=metadata`` messages. **Labels nothing.**"""
    senders = allowed_senders()
    if not senders:
        # Checked before the client exists: this must cost zero Gmail calls.
        return {
            "ok": False,
            "reason": NO_ALLOWED_SENDERS,
            "detail": (
                "WRIKE_TASKS_ALLOWED_SENDERS is empty or unset, so there is no sender to restrict "
                "the search to and the mailbox was not queried. There is no default here on "
                "purpose. Set it to the Wrike notification address (wrike@wrike.com) and run again."
            ),
        }
    client = client or GmailFetchClient.from_env()
    if client is None:
        return {"ok": False, "reason": GMAIL_NOT_CONFIGURED,
                "detail": "GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET and GMAIL_REFRESH_TOKEN are all "
                          "required. Nothing was read."}
    processed = _processed_label()
    skipped = _skipped_label()
    query = build_query(senders, processed, skipped)
    base = {"query_sent": query, "processed_label": processed, "skipped_label": skipped}
    try:
        ids = client.list_ids(query, max_results)
    except GmailFetchError as exc:
        return _failure(exc, GMAIL_LISTING_REFUSED, base)
    messages: list[dict[str, Any]] = []
    for message_id in ids:
        try:
            message = client.metadata_message(message_id)
        except GmailFetchError as exc:
            return _failure(exc, GMAIL_MESSAGE_REFUSED,
                            {**base, "message_id": message_id,
                             "fetched_before_failure": len(messages)})
        messages.append(message)
    return {
        "ok": True,
        **base,
        "count": len(messages),
        "messages": messages,
        "detail": (
            "Notifications nothing has been done with yet: the query excludes anything already "
            f"carrying {processed!r} or {skipped!r}. Nothing was labelled here."
        ),
    }


def _apply_label(message_id: str, label_name: str, client: GmailFetchClient | None,
                 extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Add one existing label and confirm it by read-back before reporting success."""
    client = client or GmailFetchClient.from_env()
    if client is None:
        return {"ok": False, "reason": GMAIL_NOT_CONFIGURED}
    base = {"message_id": message_id, "label": label_name, **(extra or {})}
    try:
        label_id = client.label_id(label_name)
        if label_id is None:
            # Deliberately not created from code: the labels are the operator's, and inventing
            # one would hide a misconfiguration behind a label nobody chose.
            return {"ok": False, **base, "reason": LABEL_NOT_FOUND,
                    "detail": f"There is no Gmail label named {label_name!r}. Create it by hand; "
                              "this flow never creates one. Nothing was labelled."}
        client.add_label(message_id, label_id)
        confirmed = label_id in client.label_ids_of(message_id)
    except GmailFetchError as exc:
        return _failure(exc, GMAIL_LABEL_REFUSED, base)
    return {
        "ok": bool(confirmed),
        **base,
        "label_id": label_id,
        "confirmed": confirmed,
        **({} if confirmed else {"reason": LABEL_NOT_CONFIRMED}),
    }


def mark_processed(message_id: str, client: GmailFetchClient | None = None) -> dict[str, Any]:
    """The task is up to date in Jira. Only after that is confirmed."""
    return _apply_label(message_id, _processed_label(), client, {"marked": "processed"})


def mark_skipped(message_id: str, reason: str,
                 client: GmailFetchClient | None = None) -> dict[str, Any]:
    """The e-mail was never work for this flow. Uses the skipped label, never the processed one."""
    return _apply_label(message_id, _skipped_label(), client,
                        {"marked": "skipped", "skip_reason": reason})


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch unprocessed Wrike task notifications.")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="Return unprocessed notifications. Labels nothing.")
    listing.add_argument("--max-results", type=int, default=10)
    marking = sub.add_parser("mark-processed", help="Label one message done, after the work.")
    marking.add_argument("--message-id", required=True)
    skipping = sub.add_parser("mark-skipped", help="Label one message as never work.")
    skipping.add_argument("--message-id", required=True)
    skipping.add_argument("--reason", required=True,
                          help="Why it was skipped, e.g. the parser's reason.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "list":
        out = list_notifications(max_results=args.max_results)
    elif args.command == "mark-skipped":
        out = mark_skipped(args.message_id, args.reason)
    else:
        out = mark_processed(args.message_id)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


__all__ = [
    "DEFAULT_PROCESSED_LABEL",
    "DEFAULT_SKIPPED_LABEL",
    "GMAIL_LABEL_REFUSED",
    "GMAIL_LISTING_REFUSED",
    "GMAIL_MESSAGE_REFUSED",
    "GMAIL_NOT_CONFIGURED",
    "GMAIL_TOKEN_REFUSED",
    "GMAIL_UNREACHABLE",
    "LABEL_NOT_CONFIRMED",
    "LABEL_NOT_FOUND",
    "NO_ALLOWED_SENDERS",
    "TOKEN_REFUSED_IS_NOT_EMPTY",
    "GmailFetchClient",
    "GmailFetchError",
    "allowed_senders",
    "build_query",
    "list_notifications",
    "mark_processed",
    "mark_skipped",
    "sender_filter",
]


if __name__ == "__main__":
    sys.exit(main())
