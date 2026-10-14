# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python. Without one it falls back to sys.executable.
"""Fetch unprocessed Freshdesk mention notifications from Gmail (flow step 1).

Self-contained like its siblings: no ``import mossy``, no ``python-dotenv``, its own urllib
client. Even where the core has a Gmail capability, this duplicates part of it on purpose —
the skill script runtime has no mossy on its path, so a flow step that leaned on the capability
could not be run by ``run_skill_script`` at all. The same reason ``analyze_context.py`` carries
its own Freshdesk client while ``mossy/capabilities/freshdesk.py`` exists.

**Two subcommands, and the split is the point.**

``list`` finds candidates and downloads them. It **never labels anything**: the processed label
means "this e-mail produced its Jira issue", and putting it on at fetch time would make it a
lie for every run that fails afterwards — the message would be skipped forever with nothing to
show for it. Listing is a read.

``mark-processed`` puts the processed label on, and is called at the end of the flow, only once
the issue exists and has been confirmed. Same rule as the read-back gates in
``handle_duplicate.py`` and ``create_jira_issue.py``: a marker that can outrun the work it
describes is worse than none.

``mark-skipped`` puts a **different** label on, for e-mails the parser refused — measured, the
mailbox also carries "New Thread created" (type 29) and "Note Added" notifications with no
``X-FD-*`` headers, which will never produce an issue. Without it they come back every pass
forever. The two labels are not interchangeable: ``mossy-processed`` claims a Jira issue exists,
``mossy-skipped`` says none does and none will.

Dedupe is the labels themselves, enforced server-side in the query (``-label:<name>`` for both)
rather than by filtering afterwards: an e-mail already dealt with is never even returned.

Neither label is created from code. They belong to the operator; creating one would hide a
misconfiguration behind a label nobody chose.

Environment:

    GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN   OAuth2, all three required
    GMAIL_USER_ID                     mailbox, default "me"
    GMAIL_ALLOWED_SENDERS             comma-separated senders the listing is restricted to
    FRESHDESK_BUGS_PROCESSED_LABEL    label for e-mails that produced an issue, default
                                      "mossy-processed"
    FRESHDESK_BUGS_SKIPPED_LABEL      label for e-mails that were not processable, default
                                      "mossy-skipped"

CLI (named flags only):

    fetch_mention_emails.py list --out-dir /tmp/mentions [--max-results 10]
    fetch_mention_emails.py mark-processed --message-id 1a05c08e5a30851b
    fetch_mention_emails.py mark-skipped --message-id 1a05bd4cfcc1ddb0 \
        --reason unknown_notification_type
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_API_BASE = "https://gmail.googleapis.com/gmail/v1"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_EXPIRY_MARGIN_SECONDS = 60

DEFAULT_PROCESSED_LABEL = "mossy-processed"
DEFAULT_SKIPPED_LABEL = "mossy-skipped"


class FetchError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _processed_label() -> str:
    """"A Jira issue exists for this e-mail." Never set for anything else."""
    return (os.environ.get("FRESHDESK_BUGS_PROCESSED_LABEL") or "").strip() \
        or DEFAULT_PROCESSED_LABEL


def _skipped_label() -> str:
    """"This e-mail was not processable." No issue was filed, and none will be.

    A separate label from the processed one on purpose. Marking an unparseable notification as
    processed would claim a Jira issue that does not exist, and nobody reading the mailbox could
    tell the two apart afterwards. Both stop the message coming back; only one means work got
    done.
    """
    return (os.environ.get("FRESHDESK_BUGS_SKIPPED_LABEL") or "").strip() \
        or DEFAULT_SKIPPED_LABEL


def _allowed_senders() -> list[str]:
    raw = os.environ.get("GMAIL_ALLOWED_SENDERS") or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def sender_filter(senders: list[str]) -> str:
    if not senders:
        return ""
    if len(senders) == 1:
        return f"from:{senders[0]}"
    return "{" + " ".join(f"from:{s}" for s in senders) + "}"


def build_query(senders: list[str], processed_label: str, skipped_label: str) -> str:
    """The Gmail search: the configured senders, minus everything already dealt with.

    **Both** labels are excluded. Processed means an issue was filed; skipped means the e-mail
    was never processable — measured, the mailbox carries "New Thread created" (type 29) and
    "Note Added" notifications with no ``X-FD-*`` headers at all, and without the second
    exclusion those would come back on every pass forever.

    The exclusions are part of the query, not a filter applied to the results: a message that
    has been dealt with is never returned, so it cannot be picked up by a caller that forgets
    to check.
    """
    parts = [part for part in (
        sender_filter(senders),
        f"-label:{processed_label}",
        f"-label:{skipped_label}",
    ) if part]
    return " ".join(parts)


class GmailFetchClient:
    """The slice of Gmail this step needs: list, download raw, add one label."""

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
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise FetchError(f"The Gmail refresh token was not accepted: {detail[:200]}",
                             status=exc.code) from exc
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise FetchError("Google returned no access token.")
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

    def list_ids(self, query: str, max_results: int) -> list[str]:
        status, body = self.call("GET", f"/users/{self.user_id}/messages",
                                 query={"q": query, "maxResults": str(max_results)})
        if status >= 400:
            raise FetchError(f"Gmail refused the listing: {body}", status=status)
        messages = body.get("messages") or [] if isinstance(body, dict) else []
        return [m["id"] for m in messages if isinstance(m, dict) and m.get("id")]

    def raw_message(self, message_id: str) -> bytes:
        status, body = self.call("GET", f"/users/{self.user_id}/messages/{message_id}",
                                 query={"format": "raw"})
        if status >= 400:
            raise FetchError(f"Gmail refused message {message_id}: {body}", status=status)
        raw = body.get("raw") if isinstance(body, dict) else None
        if not isinstance(raw, str):
            raise FetchError(f"Gmail returned no raw payload for {message_id}.")
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))

    def label_id(self, name: str) -> str | None:
        status, body = self.call("GET", f"/users/{self.user_id}/labels")
        if status >= 400:
            raise FetchError(f"Gmail refused the label listing: {body}", status=status)
        for label in (body.get("labels") or [] if isinstance(body, dict) else []):
            if isinstance(label, dict) and label.get("name") == name:
                return label.get("id")
        return None

    def add_label(self, message_id: str, label_id: str) -> list[str]:
        status, body = self.call(
            "POST", f"/users/{self.user_id}/messages/{message_id}/modify",
            body={"addLabelIds": [label_id]})
        if status >= 400:
            raise FetchError(f"Gmail refused to label {message_id}: {body}", status=status)
        return list(body.get("labelIds") or []) if isinstance(body, dict) else []

    def label_ids_of(self, message_id: str) -> list[str]:
        status, body = self.call("GET", f"/users/{self.user_id}/messages/{message_id}",
                                 query={"format": "metadata"})
        if status >= 400:
            raise FetchError(f"Gmail refused message {message_id}: {body}", status=status)
        return list(body.get("labelIds") or []) if isinstance(body, dict) else []


def list_mentions(out_dir: str, max_results: int = 10,
                  client: GmailFetchClient | None = None) -> dict[str, Any]:
    """Download every unprocessed notification to ``out_dir``. Labels nothing.

    Refuses to run without ``GMAIL_ALLOWED_SENDERS``. An empty allowlist used to mean "no
    restriction", so the query became ``-label:… -label:…`` and matched the **whole mailbox** —
    every unlabelled personal e-mail downloaded and handed to the analyser, which files Jira
    issues this account cannot delete. The safe reading of "no senders configured" is "read
    nothing", not "read everything".
    """
    senders = _allowed_senders()
    if not senders:
        # Checked before the client exists: this must cost zero Gmail calls.
        return {
            "ok": False,
            "reason": "no_allowed_senders",
            "detail": (
                "GMAIL_ALLOWED_SENDERS is empty or unset, so there is no sender to restrict the "
                "search to and the mailbox was not queried. Set it to the Freshdesk notification "
                "address (comma-separated for several) and run again."
            ),
        }
    client = client or GmailFetchClient.from_env()
    if client is None:
        return {"ok": False, "reason": "gmail_not_configured"}
    label = _processed_label()
    skipped = _skipped_label()
    query = build_query(senders, label, skipped)
    ids = client.list_ids(query, max_results)
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    emails = []
    for message_id in ids:
        path = target / f"{message_id}.eml"
        path.write_bytes(client.raw_message(message_id))
        emails.append({"message_id": message_id, "eml_path": str(path)})
    return {
        "ok": True,
        "query_sent": query,
        "processed_label": label,
        "skipped_label": skipped,
        "count": len(emails),
        "emails": emails,
        "detail": (
            "These are notifications nothing has been done with yet: the query excludes "
            f"anything already carrying {label!r} (an issue was filed) or {skipped!r} (it was "
            "not processable). Nothing was labelled here — mark a message processed only after "
            "its Jira issue exists and has been confirmed, and skipped only when the parser "
            "refused it."
        ),
    }


def _apply_label(message_id: str, label_name: str, client: GmailFetchClient | None,
                 extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Add one label and confirm it by read-back before reporting success.

    The write does not count until an independent read shows it, the same gate the Jira steps
    use. A marker that reports success it cannot demonstrate is worse than no marker: the
    e-mail would be treated as handled while nothing shows for it.
    """
    client = client or GmailFetchClient.from_env()
    if client is None:
        return {"ok": False, "reason": "gmail_not_configured"}
    label_id = client.label_id(label_name)
    if label_id is None:
        # Deliberately not created from code: the labels are the operator's, and inventing one
        # would hide a misconfiguration behind a label nobody chose.
        return {"ok": False, "reason": "label_not_found", "label": label_name}
    client.add_label(message_id, label_id)
    confirmed = label_id in client.label_ids_of(message_id)
    return {
        "ok": bool(confirmed),
        "message_id": message_id,
        "label": label_name,
        "label_id": label_id,
        "confirmed": confirmed,
        **(extra or {}),
        **({} if confirmed else {"reason": "label_not_confirmed"}),
    }


def mark_processed(message_id: str,
                   client: GmailFetchClient | None = None) -> dict[str, Any]:
    """Mark an e-mail as having produced a Jira issue. Only after the issue is confirmed."""
    return _apply_label(message_id, _processed_label(), client,
                        {"marked": "processed"})


def mark_skipped(message_id: str, reason: str,
                 client: GmailFetchClient | None = None) -> dict[str, Any]:
    """Mark an e-mail as not processable. **No issue was filed and none will be.**

    Uses the skipped label, never the processed one. The two are not interchangeable: one
    claims work was done, the other says it never could be.
    """
    return _apply_label(message_id, _skipped_label(), client,
                        {"marked": "skipped", "skip_reason": reason})


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch unprocessed Freshdesk mention e-mails.")
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list", help="Download unprocessed notifications. Labels nothing.")
    listing.add_argument("--out-dir", required=True)
    listing.add_argument("--max-results", type=int, default=10)
    marking = sub.add_parser("mark-processed", help="Label one message as done, after the work.")
    marking.add_argument("--message-id", required=True)
    skipping = sub.add_parser(
        "mark-skipped", help="Label one message as not processable. No issue was filed.")
    skipping.add_argument("--message-id", required=True)
    skipping.add_argument("--reason", required=True,
                          help="Why it was skipped, e.g. the parser's reason.")
    return parser


def main() -> None:  # pragma: no cover - exercised end to end, not in the offline suite
    args = _build_parser().parse_args()
    if args.command == "list":
        out = list_mentions(args.out_dir, max_results=args.max_results)
    elif args.command == "mark-skipped":
        out = mark_skipped(args.message_id, args.reason)
    else:
        out = mark_processed(args.message_id)
    print(json.dumps(out, ensure_ascii=False))
    sys.exit(0 if out.get("ok") else 1)


if __name__ == "__main__":
    main()
