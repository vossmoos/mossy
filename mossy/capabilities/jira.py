"""Jira Cloud integration exposed as a Pydantic AI capability (toolset).

Raw Jira REST API operations for the configured project. Business rules live in skills.

Env contract:

    JIRA_SITE          e.g. "vossmoos.atlassian.net" (https:// prefix optional)
    JIRA_SPACE_NAME    project key, e.g. "VM"
    JIRA_USER_EMAIL    Atlassian account email for Basic auth
    JIRA_API_KEY       Atlassian API token

If any are missing, `jira_capability()` returns None so Mossy still boots without Jira.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from pydantic_ai.capabilities.toolset import Toolset
from pydantic_ai.toolsets import FunctionToolset


class JiraError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.status is not None:
            parts.append(f"HTTP {self.status}")
        if self.body is not None:
            if isinstance(self.body, dict):
                messages = self.body.get("errorMessages")
                if isinstance(messages, list) and messages:
                    parts.append("; ".join(str(m) for m in messages))
                elif self.body.get("errors"):
                    parts.append(json.dumps(self.body["errors"], ensure_ascii=False)[:500])
                else:
                    parts.append(json.dumps(self.body, ensure_ascii=False)[:500])
            else:
                parts.append(str(self.body)[:500])
        return " — ".join(parts)


def _adf_text(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def _normalize_site(site: str) -> str:
    value = site.strip().rstrip("/")
    for prefix in ("https://", "http://"):
        if value.lower().startswith(prefix):
            value = value[len(prefix) :]
    return value


@dataclass(frozen=True)
class JiraClient:
    """Minimal Jira Cloud REST API v3 client."""

    site: str
    space_name: str
    email: str
    api_token: str
    timeout_seconds: int = 120

    @classmethod
    def from_env(cls) -> JiraClient | None:
        site = _normalize_site(os.environ.get("JIRA_SITE") or "")
        space_name = (os.environ.get("JIRA_SPACE_NAME") or "").strip()
        email = (os.environ.get("JIRA_USER_EMAIL") or "").strip()
        api_token = (os.environ.get("JIRA_API_KEY") or "").strip()
        if not site or not space_name or not email or not api_token:
            return None
        return cls(site=site, space_name=space_name, email=email, api_token=api_token)

    def issue_key(self, task_id: str) -> str:
        raw = task_id.strip().upper()
        space = self.space_name.upper()
        if re.fullmatch(r"\d+", raw):
            return f"{space}-{raw}"
        if "-" in raw:
            prefix, _, suffix = raw.partition("-")
            if prefix != space:
                raise ValueError(
                    f"Task id {task_id!r} is not in configured project {self.space_name!r}."
                )
            if not suffix.isdigit():
                raise ValueError(f"Invalid task id {task_id!r}; expected a numeric suffix.")
            return raw
        raise ValueError(
            f"Invalid task id {task_id!r}; use a number or {self.space_name}-<number>."
        )

    def get_issue(self, task_id: str) -> dict[str, Any]:
        key = self.issue_key(task_id)
        status, data = self.request_json(
            "GET",
            f"/issue/{urllib.parse.quote(key, safe='')}",
            query={
                "fields": "summary,description,status,assignee,reporter,created,updated,comment",
            },
        )
        if status != 200 or not isinstance(data, dict):
            raise JiraError(f"Jira issue {key} fetch failed.", status=status, body=data)
        fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
        comments = fields.get("comment") if isinstance(fields.get("comment"), dict) else {}
        comment_list = comments.get("comments") if isinstance(comments.get("comments"), list) else []
        return {
            "key": data.get("key", key),
            "id": data.get("id"),
            "summary": fields.get("summary"),
            "status": _user_field(fields.get("status"), name_key="name"),
            "assignee": _user_field(fields.get("assignee")),
            "reporter": _user_field(fields.get("reporter")),
            "created": fields.get("created"),
            "updated": fields.get("updated"),
            "description": fields.get("description"),
            "comment_count": comments.get("total", len(comment_list)),
        }

    def add_comment(self, task_id: str, comment: str) -> dict[str, Any]:
        key = self.issue_key(task_id)
        text = comment.strip()
        if not text:
            raise ValueError("comment must not be empty.")
        status, data = self.request_json(
            "POST",
            f"/issue/{urllib.parse.quote(key, safe='')}/comment",
            body={"body": _adf_text(text)},
        )
        if status not in (200, 201) or not isinstance(data, dict):
            raise JiraError(f"Jira comment on {key} failed.", status=status, body=data)
        return {
            "issue_key": key,
            "comment_id": data.get("id"),
            "created": data.get("created"),
        }

    def list_transitions(self, task_id: str) -> list[dict[str, Any]]:
        key = self.issue_key(task_id)
        status, data = self.request_json(
            "GET",
            f"/issue/{urllib.parse.quote(key, safe='')}/transitions",
        )
        if status != 200 or not isinstance(data, dict):
            raise JiraError(f"Jira transitions for {key} failed.", status=status, body=data)
        transitions = data.get("transitions")
        if not isinstance(transitions, list):
            return []
        out: list[dict[str, Any]] = []
        for item in transitions:
            if not isinstance(item, dict):
                continue
            to_status = item.get("to") if isinstance(item.get("to"), dict) else {}
            out.append(
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "to_status": to_status.get("name"),
                }
            )
        return out

    def transition_issue(self, task_id: str, transition: str) -> dict[str, Any]:
        key = self.issue_key(task_id)
        needle = transition.strip()
        if not needle:
            raise ValueError("transition must not be empty.")
        available = self.list_transitions(task_id)
        match = _match_transition(available, needle)
        if match is None:
            names = ", ".join(
                f"{t.get('name')} (-> {t.get('to_status')})" for t in available if t.get("name")
            )
            raise JiraError(
                f"No transition matching {transition!r} on {key}. Available: {names or 'none'}."
            )
        status, data = self.request_json(
            "POST",
            f"/issue/{urllib.parse.quote(key, safe='')}/transitions",
            body={"transition": {"id": str(match["id"])}},
        )
        if status != 204 and not (status == 200 and data is None):
            raise JiraError(f"Jira transition on {key} failed.", status=status, body=data)
        refreshed = self.get_issue(task_id)
        return {
            "issue_key": key,
            "transition": match.get("name"),
            "status": refreshed.get("status"),
        }

    def assign_issue(self, task_id: str, assignee: str) -> dict[str, Any]:
        key = self.issue_key(task_id)
        account_id = self._resolve_assignee(task_id, assignee)
        status, data = self.request_json(
            "PUT",
            f"/issue/{urllib.parse.quote(key, safe='')}",
            body={"fields": {"assignee": {"accountId": account_id}}},
        )
        if status != 204 and not (status == 200 and data is None):
            raise JiraError(f"Jira assign on {key} failed.", status=status, body=data)
        refreshed = self.get_issue(task_id)
        return {
            "issue_key": key,
            "assignee": refreshed.get("assignee"),
        }

    def log_work(
        self,
        task_id: str,
        time_spent: str,
        *,
        comment: str | None = None,
    ) -> dict[str, Any]:
        key = self.issue_key(task_id)
        spent = time_spent.strip()
        if not spent:
            raise ValueError("time_spent must not be empty.")
        body: dict[str, Any] = {"timeSpent": spent}
        if comment and comment.strip():
            body["comment"] = _adf_text(comment.strip())
        status, data = self.request_json(
            "POST",
            f"/issue/{urllib.parse.quote(key, safe='')}/worklog",
            body=body,
        )
        if status not in (200, 201) or not isinstance(data, dict):
            raise JiraError(f"Jira worklog on {key} failed.", status=status, body=data)
        return {
            "issue_key": key,
            "worklog_id": data.get("id"),
            "time_spent": data.get("timeSpent"),
            "time_spent_seconds": data.get("timeSpentSeconds"),
            "created": data.get("created"),
        }

    def _resolve_assignee(self, task_id: str, assignee: str) -> str:
        value = assignee.strip()
        if not value:
            raise ValueError("assignee must not be empty.")
        if re.fullmatch(r"\d+:\S+", value):
            return value
        key = self.issue_key(task_id)
        status, data = self.request_json(
            "GET",
            "/user/assignable/search",
            query={"project": self.space_name, "query": value, "maxResults": "20"},
        )
        if status != 200 or not isinstance(data, list):
            raise JiraError("Jira assignable user search failed.", status=status, body=data)
        if not data:
            raise JiraError(f"No assignable user matching {assignee!r} on project {self.space_name}.")
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
                return account_id
        if len(data) == 1 and isinstance(data[0], dict):
            account_id = data[0].get("accountId")
            if isinstance(account_id, str):
                return account_id
        options = ", ".join(
            str(u.get("displayName"))
            for u in data
            if isinstance(u, dict) and u.get("displayName")
        )
        raise JiraError(
            f"Assignee {assignee!r} is ambiguous on {key}. Matches: {options or 'none'}."
        )

    def request_json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: Any = None,
    ) -> tuple[int, Any]:
        url = self._url(path, query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": self._auth_header(),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        def do_call() -> tuple[int, bytes]:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                    return resp.getcode() or 200, resp.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read() if exc.fp else b""

        status, raw = do_call()
        if status == 429:
            time.sleep(60)
            status, raw = do_call()
        if not raw:
            return status, None
        try:
            return status, json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            return status, {"_raw": raw.decode("utf-8", errors="replace")}

    def _auth_header(self) -> str:
        token = base64.b64encode(f"{self.email}:{self.api_token}".encode()).decode("ascii")
        return f"Basic {token}"

    def _url(self, path: str, query: dict[str, str] | None = None) -> str:
        clean_path = path if path.startswith("/") else f"/{path}"
        base = f"https://{self.site.rstrip('/')}/rest/api/3{clean_path}"
        return base + ("?" + urllib.parse.urlencode(query) if query else "")


def _user_field(value: Any, *, name_key: str = "displayName") -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "account_id": value.get("accountId"),
        "display_name": value.get(name_key),
        "email": value.get("emailAddress"),
    }


def _match_transition(
    transitions: list[dict[str, Any]],
    needle: str,
) -> dict[str, Any] | None:
    if needle.isdigit():
        for item in transitions:
            if str(item.get("id")) == needle:
                return item
    lowered = needle.casefold()
    for item in transitions:
        name = str(item.get("name") or "").casefold()
        to_status = str(item.get("to_status") or "").casefold()
        if lowered in (name, to_status):
            return item
    for item in transitions:
        name = str(item.get("name") or "").casefold()
        to_status = str(item.get("to_status") or "").casefold()
        if lowered in name or lowered in to_status:
            return item
    return None


def jira_capability() -> Toolset | None:
    """Expose Jira operations for the `jira` skill."""
    client = JiraClient.from_env()
    if client is None:
        return None

    async def get_issue(task_id: str) -> dict[str, Any]:
        """Read one issue in the configured Jira project by numeric id or full key."""
        return client.get_issue(task_id)

    async def add_comment(task_id: str, comment: str) -> dict[str, Any]:
        """Add a plain-text comment to an issue."""
        return client.add_comment(task_id, comment)

    async def list_transitions(task_id: str) -> list[dict[str, Any]]:
        """List workflow transitions currently available for an issue."""
        return client.list_transitions(task_id)

    async def transition_issue(task_id: str, transition: str) -> dict[str, Any]:
        """Move an issue through the workflow by transition or target status name."""
        return client.transition_issue(task_id, transition)

    async def assign_issue(task_id: str, assignee: str) -> dict[str, Any]:
        """Assign an issue by display name, email, or Atlassian account id."""
        return client.assign_issue(task_id, assignee)

    async def log_work(task_id: str, time_spent: str, comment: str | None = None) -> dict[str, Any]:
        """Log time on an issue. time_spent uses Jira format, e.g. 30m, 1h, 1h 30m."""
        return client.log_work(task_id, time_spent, comment=comment)

    return Toolset(
        FunctionToolset(
            [
                get_issue,
                add_comment,
                list_transitions,
                transition_issue,
                assign_issue,
                log_work,
            ],
            id="jira",
            instructions=(
                "These tools implement the `jira` skill: raw Jira Cloud API access for the "
                f"configured project ({client.space_name} on {client.site}). Use get_issue and "
                "list_transitions to gather context. Use add_comment, transition_issue, "
                "assign_issue, and log_work only when a task explicitly calls for them. "
                "task_id may be a bare number (190) or a full key (VM-190)."
            ),
        )
    )
