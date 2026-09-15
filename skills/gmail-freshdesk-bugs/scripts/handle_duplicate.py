# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python. Without one it falls back to sys.executable.
"""Duplicate flow over an existing Jira issue.

When a new Freshdesk report turns out to be a duplicate of a known Jira issue, this comments the
new context onto that issue and moves it to a target status — but only after **confirming the
comment by read-back**. The rule: an unconfirmed write does not count, so
the workflow is never advanced on the strength of a POST that returned 2xx.

Self-contained (like ``dedupe_cache.py``): no ``import mossy``, no ``python-dotenv``; its own
urllib Jira client. Config from the environment:

    JIRA_SITE, JIRA_SPACE_NAME, JIRA_USER_EMAIL, JIRA_API_KEY
    DUPLICATE_TARGET_STATUS   default "To Do"; a project whose statuses have other names sets
                              its own.

CLI (named flags only): handle_duplicate.py --jira-key PROJ-123 --ticket-id 100 \
    --conversation-id 5 --freshdesk-url https://… --problem-text "…"
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

_DEFAULT_TARGET_STATUS = "To Do"


def _target_status() -> str:
    return (os.environ.get("DUPLICATE_TARGET_STATUS") or _DEFAULT_TARGET_STATUS).strip()


class JiraError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def _adf_text(text: str) -> dict[str, Any]:
    """Plain text as a minimal Atlassian Document Format doc (same shape the capability uses)."""
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def _normalize_site(site: str) -> str:
    value = site.strip().rstrip("/")
    for prefix in ("https://", "http://"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
    return value


@dataclass
class DuplicateJiraClient:
    """Minimal Jira Cloud REST v3 client — the reads and writes this flow needs, urllib only."""

    site: str
    space_name: str
    email: str
    api_token: str
    timeout_seconds: int = 120

    @classmethod
    def from_env(cls) -> "DuplicateJiraClient | None":
        site = _normalize_site(os.environ.get("JIRA_SITE") or "")
        space = (os.environ.get("JIRA_SPACE_NAME") or "").strip()
        email = (os.environ.get("JIRA_USER_EMAIL") or "").strip()
        token = (os.environ.get("JIRA_API_KEY") or "").strip()
        if not site or not space or not email or not token:
            return None
        return cls(site=site, space_name=space, email=email, api_token=token)

    def issue_key(self, task_id: str) -> str:
        raw = str(task_id).strip().upper()
        space = self.space_name.upper()
        if re.fullmatch(r"\d+", raw):
            return f"{space}-{raw}"
        if "-" in raw:
            return raw
        raise ValueError(f"Invalid issue id {task_id!r}; use a number or {self.space_name}-<number>.")

    def get_status(self, key: str) -> str | None:
        status, data = self.request_json("GET", f"/issue/{key}", query={"fields": "status"})
        if status != 200 or not isinstance(data, dict):
            raise JiraError(f"Jira status read for {key} failed.", status=status, body=data)
        fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
        st = fields.get("status") if isinstance(fields.get("status"), dict) else {}
        return st.get("name")

    def add_comment(self, key: str, text: str) -> str | None:
        status, data = self.request_json(
            "POST", f"/issue/{key}/comment", body={"body": _adf_text(text)}
        )
        if status not in (200, 201) or not isinstance(data, dict):
            raise JiraError(f"Jira comment on {key} failed.", status=status, body=data)
        cid = data.get("id")
        return str(cid) if cid is not None else None

    def comment_ids(self, key: str) -> list[str]:
        status, data = self.request_json("GET", f"/issue/{key}/comment")
        if status != 200 or not isinstance(data, dict):
            return []
        comments = data.get("comments")
        if not isinstance(comments, list):
            return []
        return [str(c.get("id")) for c in comments if isinstance(c, dict) and c.get("id") is not None]

    def list_transitions(self, key: str) -> list[dict[str, Any]]:
        status, data = self.request_json("GET", f"/issue/{key}/transitions")
        if status != 200 or not isinstance(data, dict):
            raise JiraError(f"Jira transitions for {key} failed.", status=status, body=data)
        out: list[dict[str, Any]] = []
        for item in data.get("transitions") or []:
            if not isinstance(item, dict):
                continue
            to = item.get("to") if isinstance(item.get("to"), dict) else {}
            out.append({"id": item.get("id"), "name": item.get("name"), "to_status": to.get("name")})
        return out

    def transition(self, key: str, transition_id: str) -> None:
        status, data = self.request_json(
            "POST", f"/issue/{key}/transitions", body={"transition": {"id": str(transition_id)}}
        )
        if status != 204 and not (status == 200 and data is None):
            raise JiraError(f"Jira transition on {key} failed.", status=status, body=data)

    # ------------------------------------------------------------------ transport
    def request_json(self, method, path, *, query=None, body=None) -> tuple[int, Any]:
        url = self._url(path, query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": self._auth_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read()
                code = resp.getcode() or 200
        except urllib.error.HTTPError as exc:
            raw = exc.read() if exc.fp else b""
            code = exc.code
        if not raw:
            return code, None
        try:
            return code, json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return code, {"_raw": raw.decode("utf-8", errors="replace")}

    def _auth_header(self) -> str:
        token = base64.b64encode(f"{self.email}:{self.api_token}".encode()).decode("ascii")
        return f"Basic {token}"

    def _url(self, path: str, query: dict[str, str] | None = None) -> str:
        clean = path if path.startswith("/") else f"/{path}"
        base = f"https://{self.site.rstrip('/')}/rest/api/3{clean}"
        return base + ("?" + urllib.parse.urlencode(query) if query else "")


def _match_target(transitions: list[dict[str, Any]], target: str) -> dict[str, Any] | None:
    """A transition landing on the target status (matched on to_status, then on name)."""
    needle = target.casefold()
    for t in transitions:
        if str(t.get("to_status") or "").casefold() == needle:
            return t
    for t in transitions:
        if str(t.get("name") or "").casefold() == needle:
            return t
    return None


def _duplicate_comment(ticket_id: int, thread_anchor_note_id: int | None, freshdesk_url: str,
                       problem_text: str) -> str:
    return (
        "Weiterer Report zu diesem Issue (moeglicher Doppelfall).\n"
        f"Freshdesk-Ticket: {ticket_id}"
        + (f" — Thread anchor note: {thread_anchor_note_id}"
           if thread_anchor_note_id is not None else "")
        + "\n"
        f"Link: {freshdesk_url}\n\n"
        f"Problem:\n{problem_text}"
    )


def handle_duplicate(
    jira_key: str,
    ticket_id: int,
    thread_anchor_note_id: int | None,
    freshdesk_url: str,
    problem_text: str,
    *,
    client: DuplicateJiraClient | None = None,
) -> dict[str, Any]:
    """Comment the new context onto an existing issue, confirm it, then move it to the target status.

    The read-back gates the transition: if the comment is not confirmed on the issue, nothing is
    transitioned and ``ok`` is False. If the comment lands but the transition fails (or the issue
    is already at target), the context is not lost — ``ok`` stays True and the caller is told.
    """
    client = client or DuplicateJiraClient.from_env()
    if client is None:
        return {
            "ok": False, "jira_key": jira_key, "comment_id": None, "transitioned": False,
            "status_before": None, "status_after": None,
            "warnings": ["Jira is not configured (JIRA_SITE/SPACE_NAME/USER_EMAIL/API_KEY)."],
        }

    target = _target_status()
    key = client.issue_key(jira_key)
    warnings: list[str] = []

    status_before = client.get_status(key)

    comment_text = _duplicate_comment(ticket_id, thread_anchor_note_id, freshdesk_url,
                                      problem_text)
    comment_id = client.add_comment(key, comment_text)

    # READ-BACK GATE: the comment must be on the issue before we touch the workflow. A POST that
    # returned an id but does not show up on read-back does NOT count.
    confirmed = comment_id is not None and str(comment_id) in client.comment_ids(key)
    if not confirmed:
        return {
            "ok": False, "jira_key": key, "comment_id": comment_id, "transitioned": False,
            "status_before": status_before, "status_after": status_before,
            "warnings": ["comment not confirmed on read-back; issue not transitioned"],
        }

    # Already at target — a no-op, not an error.
    if status_before and status_before.casefold() == target.casefold():
        return {
            "ok": True, "jira_key": key, "comment_id": comment_id, "transitioned": False,
            "status_before": status_before, "status_after": status_before,
            "warnings": [f"already in target status {target!r}; no transition"],
        }

    match = _match_target(client.list_transitions(key), target)
    if match is None:
        warnings.append(
            f"no transition to target status {target!r} available; comment saved, fix status by hand"
        )
        return {
            "ok": True, "jira_key": key, "comment_id": comment_id, "transitioned": False,
            "status_before": status_before, "status_after": status_before, "warnings": warnings,
        }

    try:
        client.transition(key, match["id"])
    except JiraError as exc:
        # The comment already landed; the state can be fixed by hand. Context is not lost.
        warnings.append(
            f"transition to {target!r} failed ({exc}); comment saved, fix status by hand"
        )
        return {
            "ok": True, "jira_key": key, "comment_id": comment_id, "transitioned": False,
            "status_before": status_before, "status_after": status_before, "warnings": warnings,
        }

    status_after = client.get_status(key)
    if not status_after or status_after.casefold() != target.casefold():
        warnings.append(
            f"transition to {target!r} not confirmed on read-back; comment saved, fix status by hand"
        )
        return {
            "ok": True, "jira_key": key, "comment_id": comment_id, "transitioned": False,
            "status_before": status_before, "status_after": status_after, "warnings": warnings,
        }

    return {
        "ok": True, "jira_key": key, "comment_id": comment_id, "transitioned": True,
        "status_before": status_before, "status_after": status_after, "warnings": warnings,
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Duplicate flow over an existing Jira issue.")
    p.add_argument("--jira-key", required=True)
    p.add_argument("--ticket-id", type=int, required=True)
    # Gone, as in analyze_context.py: the id that used to arrive here identifies the thread,
    # not the message, and a comment naming it "Konversation" claimed otherwise — permanently,
    # since this Jira cannot delete.
    p.add_argument("--thread-anchor-note-id", type=int, default=None,
                   help="Optional. The note the thread hangs from, for the Freshdesk reference.")
    p.add_argument("--freshdesk-url", required=True)
    p.add_argument("--problem-text", required=True)
    return p


def main() -> None:  # pragma: no cover - exercised end to end, not in the offline suite
    args = _build_parser().parse_args()
    out = handle_duplicate(
        args.jira_key, args.ticket_id, args.thread_anchor_note_id, args.freshdesk_url,
        args.problem_text
    )
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
