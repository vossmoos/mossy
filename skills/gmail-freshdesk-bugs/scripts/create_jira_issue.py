# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python. Without one it falls back to sys.executable.
"""Create a Jira issue for a new (non-duplicate) bug and remember it in the cache (flow step 6).

Closes the branch that dedupe_cache.find_similar returns no match for: open a fresh Jira issue,
confirm it exists by read-back, and only then store it in the semantic-duplicate cache.

The read-back before caching is not optional. If the cache stored a key for an issue that was not
actually created, every future duplicate would be pointed at a phantom issue forever — and this
Jira does not allow deleting issues, so the bad reference could never be cleaned up.

Self-contained (like ``handle_duplicate.py``): no ``import mossy``, no ``python-dotenv``; its own
urllib Jira client. It reuses ``dedupe_cache.py`` by loading it from the sibling path (the tests
do the same) rather than duplicating it. Config from the environment:

    JIRA_SITE, JIRA_SPACE_NAME, JIRA_USER_EMAIL, JIRA_API_KEY
    JIRA_DEFAULT_ASSIGNEE   no default: unset -> created unassigned (best-effort; unresolved -> unassigned too)

CLI (named flags only): create_jira_issue.py --summary "…" --problem "…" --ticket-id 100 \
    --conversation-id 5 --freshdesk-url https://…
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DEFAULT_ASSIGNEE = ""
_DEFAULT_ISSUE_TYPE = "Task"

# Reuse the sibling dedupe cache by path — do not duplicate it.
_DEDUPE_PATH = Path(__file__).resolve().parent / "dedupe_cache.py"
_dedupe_spec = importlib.util.spec_from_file_location("dedupe_cache", _DEDUPE_PATH)
dedupe_cache = importlib.util.module_from_spec(_dedupe_spec)
_dedupe_spec.loader.exec_module(dedupe_cache)


def _default_assignee() -> str:
    return (os.environ.get("JIRA_DEFAULT_ASSIGNEE") or _DEFAULT_ASSIGNEE).strip()


class JiraError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def _adf_doc(blocks: list[str]) -> dict[str, Any]:
    """A minimal ADF document: one paragraph per non-empty block."""
    content = [
        {"type": "paragraph", "content": [{"type": "text", "text": str(b)}]}
        for b in blocks
        if str(b).strip()
    ]
    return {"type": "doc", "version": 1, "content": content}


def _normalize_site(site: str) -> str:
    value = site.strip().rstrip("/")
    for prefix in ("https://", "http://"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
    return value


@dataclass
class CreateJiraClient:
    site: str
    space_name: str
    email: str
    api_token: str
    timeout_seconds: int = 120

    @classmethod
    def from_env(cls) -> "CreateJiraClient | None":
        site = _normalize_site(os.environ.get("JIRA_SITE") or "")
        space = (os.environ.get("JIRA_SPACE_NAME") or "").strip()
        email = (os.environ.get("JIRA_USER_EMAIL") or "").strip()
        token = (os.environ.get("JIRA_API_KEY") or "").strip()
        if not site or not space or not email or not token:
            return None
        return cls(site=site, space_name=space, email=email, api_token=token)

    def match_assignee(self, query: str) -> tuple[str | None, str | None]:
        """Resolve a query to a single accountId via /user/assignable/search. Never raises.

        Same best-effort contract as the core capability's create_issue: exactly one match (or an
        exact display/email match) resolves; anything else returns (None, why)."""
        value = query.strip()
        if not value:
            return None, "assignee query was empty"
        if re.fullmatch(r"\d+:\S+", value):
            return value, None
        status, data = self.request_json(
            "GET", "/user/assignable/search",
            query={"project": self.space_name, "query": value, "maxResults": "20"},
        )
        if status != 200 or not isinstance(data, list):
            return None, f"assignable user search failed (HTTP {status})"
        if not data:
            return None, f"no assignable user matching {query!r} in project {self.space_name}"
        lowered = value.casefold()
        for user in data:
            if not isinstance(user, dict):
                continue
            account_id = user.get("accountId")
            if not isinstance(account_id, str):
                continue
            display = str(user.get("displayName") or "").casefold()
            email = str(user.get("emailAddress") or "").casefold()
            if display == lowered or email == lowered:
                return account_id, None
        if len(data) == 1 and isinstance(data[0], dict):
            account_id = data[0].get("accountId")
            if isinstance(account_id, str):
                return account_id, None
        options = ", ".join(
            str(u.get("displayName")) for u in data if isinstance(u, dict) and u.get("displayName")
        )
        return None, f"assignee {query!r} is ambiguous ({len(data)} matches: {options or 'none'})"

    def create_issue(self, summary: str, description_blocks: list[str], account_id: str | None,
                     issue_type: str = _DEFAULT_ISSUE_TYPE) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "project": {"key": self.space_name.upper()},
            "summary": summary,
            "issuetype": {"name": issue_type},
            "description": _adf_doc(description_blocks),
        }
        if account_id:
            fields["assignee"] = {"accountId": account_id}
        status, data = self.request_json("POST", "/issue", body={"fields": fields})
        if status not in (200, 201) or not isinstance(data, dict):
            raise JiraError("Jira issue creation failed.", status=status, body=data)
        return data

    def issue_exists(self, key: str) -> bool:
        """Read-back: the issue is really there. A 200 with the same key confirms it."""
        status, data = self.request_json("GET", f"/issue/{key}", query={"fields": "summary"})
        if status != 200 or not isinstance(data, dict):
            return False
        return str(data.get("key") or "") == key or data.get("id") is not None

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
                raw, code = resp.read(), (resp.getcode() or 200)
        except urllib.error.HTTPError as exc:
            raw, code = (exc.read() if exc.fp else b""), exc.code
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


def create_and_remember(
    summary: str,
    problem: str,
    ticket_id: int,
    freshdesk_url: str,
    thread_anchor_note_id: int | None = None,
    *,
    client: CreateJiraClient | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Create the Jira issue, confirm it by read-back, and only then cache it."""
    client = client or CreateJiraClient.from_env()
    if client is None:
        return {"ok": False, "jira_key": None, "jira_url": None, "assignee_set": False,
                "cached": False, "warnings": ["Jira is not configured."]}

    warnings: list[str] = []

    assignee_query = _default_assignee()
    account_id, why = client.match_assignee(assignee_query) if assignee_query else (None, None)
    assignee_set = account_id is not None
    if assignee_query and account_id is None and why:
        warnings.append(f"created without assignee — {why}")

    # The anchor note is the thread this mention hangs from, not the message itself — it is
    # written only when it is known, and named for what it is. It used to say "Conversation",
    # which claimed something the id does not identify.
    description_blocks = [
        problem,
        "Freshdesk references:",
        f"Ticket: {ticket_id}",
        *([f"Thread anchor note: {thread_anchor_note_id}"]
          if thread_anchor_note_id is not None else []),
        f"Link: {freshdesk_url}",
    ]
    try:
        created = client.create_issue(summary, description_blocks, account_id)
    except JiraError as exc:
        return {"ok": False, "jira_key": None, "jira_url": None, "assignee_set": assignee_set,
                "cached": False, "warnings": [*warnings, f"issue creation failed: {exc}"]}

    jira_key = created.get("key")
    jira_url = f"https://{client.site.rstrip('/')}/browse/{jira_key}" if jira_key else None

    # READ-BACK GATE: a key that does not resolve to a real issue must never enter the cache.
    if not jira_key or not client.issue_exists(str(jira_key)):
        warnings.append("issue not confirmed on read-back; NOT cached")
        return {"ok": False, "jira_key": jira_key, "jira_url": jira_url,
                "assignee_set": assignee_set, "cached": False, "warnings": warnings}

    remembered = dedupe_cache.remember(
        jira_key, jira_url, ticket_id, thread_anchor_note_id, summary, problem, db_path=db_path
    )
    cached = bool(remembered.get("ok"))
    if not cached:
        warnings.append("issue created and confirmed, but caching failed")

    return {"ok": True, "jira_key": jira_key, "jira_url": jira_url,
            "assignee_set": assignee_set, "cached": cached, "warnings": warnings}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Create a Jira issue for a new bug and cache it.")
    p.add_argument("--summary", required=True)
    p.add_argument("--problem", required=True)
    p.add_argument("--ticket-id", type=int, required=True)
    p.add_argument("--freshdesk-url", required=True)
    # ``--conversation-id`` is gone, as in analyze_context.py. The value that used to be passed
    # to it is the thread's anchor note, which identifies the thread and not the message; naming
    # it honestly is what stops it being written into a permanent issue as "Conversation".
    p.add_argument("--thread-anchor-note-id", type=int, default=None,
                   help="Optional. The note the thread hangs from, for the Freshdesk reference.")
    return p


def main() -> None:  # pragma: no cover - exercised end to end, not in the offline suite
    args = _build_parser().parse_args()
    out = create_and_remember(
        args.summary, args.problem, args.ticket_id, args.freshdesk_url,
        thread_anchor_note_id=args.thread_anchor_note_id
    )
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
