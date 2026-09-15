"""Wrike exposed as a Pydantic AI capability — READ ONLY.

Two operations, and nothing else: one task by its API id, and that task's comments.

Read-only structurally: ``_get`` is the only transport and it has **no ``method`` parameter**, so
adding a write means adding a function, not passing an argument.

★ "READ ONLY" DESCRIBES THIS CAPABILITY, NOT WHAT THE AGENT CAN DO WITH WRIKE.

Anyone reading it as a guarantee is wrong. ``shell_capability`` — ``run_command``, bash with no
restriction — is registered on every channel that takes the shared capabilities (CLI, Slack,
AG-UI, MCP and the queue worker). If ``WRIKE_TOKEN`` is in Mossy's environment, the agent can
``curl -X POST`` to Wrike regardless of this capability: a skill does not bound an agent that has
another way to reach the same service.

★ PERSONAL DATA — DECLARED, NOT FIXED.

These reads put task descriptions and comments into the model's context, and they can carry an end
customer's personal data: name, street, mobile, e-mail. The agent can then write that text into
Jira with ``create_issue`` (or ``add_comment``), which the Jira capability exposes. Nothing in this
module stops that; only the agent's own judgement does.

Measured against the Wrike API, so nobody re-derives it: the API takes the task's API id
(``MAAAAAEXAMPLE``), not the permalink number (a 400); ``description`` comes by default; comments
come from the ``/tasks/{id}/comments`` sub-resource; and a Collaborator licence serves a restricted
read as 200 with an empty list — the same bytes as "nothing there" (``EMPTY_IS_NOT_ABSENCE``).

Environment:

    WRIKE_TOKEN   Wrike permanent access token.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from pydantic_ai.capabilities.toolset import Toolset
from pydantic_ai.toolsets import FunctionToolset

API_BASE = "https://www.wrike.com/api/v4"

#: The two paths, and the only two.
TASK_PATH = "/tasks/{task_id}"
TASK_COMMENTS_PATH = "/tasks/{task_id}/comments"

#: Said once, and attached to every empty result.
EMPTY_IS_NOT_ABSENCE = (
    "Wrike answered 200 with an empty list. On a Collaborator licence a restricted read looks "
    "exactly like this, so an empty answer is not evidence that nothing is there."
)

#: Statuses that mean the caller was refused, which is never the same as an empty answer.
UNAUTHORIZED = 401
FORBIDDEN = 403


class WrikeError(RuntimeError):
    """A Wrike request failed. Carries the status and body so a caller can tell 401 from 404."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body

    @property
    def refused(self) -> bool:
        """True for 401/403 — the read was refused, as opposed to failing or finding nothing."""
        return self.status in (UNAUTHORIZED, FORBIDDEN)

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} — HTTP {self.status}" if self.status else base


class WrikeClient:
    """Two reads against Wrike. No writes, no listing, no polling."""

    def __init__(self, token: str, *, timeout_seconds: int = 30, api_base: str = API_BASE) -> None:
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.api_base = api_base.rstrip("/")

    @classmethod
    def from_env(cls) -> WrikeClient | None:
        """``WRIKE_TOKEN`` or ``None``. Never raises, and never makes a request — as
        ``JiraClient.from_env``."""
        token = (os.environ.get("WRIKE_TOKEN") or "").strip()
        if not token:
            return None
        return cls(token)

    # ---------------------------------------------------------------- transport (GET only)
    def _get(self, path: str) -> Any:
        """One GET. **There is no method parameter**, and no other verb exists in this module.

        Raises ``WrikeError`` for anything that is not a 200, so a refusal can never arrive at the
        caller wearing the shape of an empty result.
        """
        url = f"{self.api_base}{path}"
        request = urllib.request.Request(
            url, headers={"Authorization": f"bearer {self.token}"}, method="GET"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                status = response.getcode() or 200
                raw = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp else b""
            parsed = _decode(body)
            if exc.code == UNAUTHORIZED:
                raise WrikeError(
                    "Wrike refused the token (401). This is a refused read, not an empty one.",
                    status=exc.code, body=parsed,
                ) from exc
            if exc.code == FORBIDDEN:
                raise WrikeError(
                    "Wrike forbade this read (403) — licence or permissions. This is a refused "
                    "read, not an empty one.",
                    status=exc.code, body=parsed,
                ) from exc
            raise WrikeError(f"Wrike request failed: {url}", status=exc.code, body=parsed) from exc
        except urllib.error.URLError as exc:
            raise WrikeError(f"Wrike is unreachable: {exc.reason}") from exc

        if status != 200:
            raise WrikeError(f"Wrike request failed: {url}", status=status, body=_decode(raw))
        return _decode(raw)

    @staticmethod
    def _rows(payload: Any) -> list[dict[str, Any]]:
        """Wrike wraps everything as ``{"kind": ..., "data": [...]}``, single reads included."""
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return [row for row in payload["data"] if isinstance(row, dict)]
        return []

    # ---------------------------------------------------------------- the two reads
    def get_task(self, task_id: str) -> dict[str, Any]:
        """One task by its **API id**. A 200 carrying no task is ``not_visible``, not absent."""
        identifier = str(task_id).strip()
        if not identifier:
            return {"ok": False, "task_id": identifier, "reason": "no_task_id",
                    "detail": "A task API id is required; this module never searches for one."}
        rows = self._rows(self._get(TASK_PATH.format(task_id=urllib.parse.quote(identifier))))
        if not rows:
            return {"ok": False, "task_id": identifier, "reason": "not_visible",
                    "detail": EMPTY_IS_NOT_ABSENCE}
        return {"ok": True, "task_id": identifier, "task": rows[0]}

    def get_task_comments(self, task_id: str) -> dict[str, Any]:
        """The comments of one task, through the sub-resource. Empty is not proof of absence."""
        identifier = str(task_id).strip()
        if not identifier:
            return {"ok": False, "task_id": identifier, "reason": "no_task_id", "comments": [],
                    "count": 0,
                    "detail": "A task API id is required; this module never searches for one."}
        rows = self._rows(
            self._get(TASK_COMMENTS_PATH.format(task_id=urllib.parse.quote(identifier)))
        )
        result: dict[str, Any] = {
            "ok": True,
            "task_id": identifier,
            "comments": rows,
            "count": len(rows),
            "empty": not rows,
        }
        if not rows:
            result["empty_means"] = EMPTY_IS_NOT_ABSENCE
        return result


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", errors="replace")


def wrike_capability() -> Toolset | None:
    """Expose Wrike's two reads for the `wrike` skill.

    Returns None when ``WRIKE_TOKEN`` is not configured, so the capability is simply absent rather
    than failing at runtime.
    """
    client = WrikeClient.from_env()
    if client is None:
        return None

    async def get_task(task_id: str) -> dict[str, Any]:
        """Read one Wrike task by its API id (e.g. MAAAAAEXAMPLE), not the number in a permalink.
        A task that comes back empty is not visible to this account, which is not the same as not
        existing."""
        return client.get_task(task_id)

    async def get_task_comments(task_id: str) -> dict[str, Any]:
        """Read the comments of one Wrike task by its API id. An empty list is not proof that the
        task has no comments."""
        return client.get_task_comments(task_id)

    return Toolset(
        FunctionToolset(
            [get_task, get_task_comments],
            id="wrike",
            instructions=(
                "These tools implement the `wrike` skill: two reads against Wrike — one task by "
                "its API id, and that task's comments. Nothing here writes; but 'read only' "
                "describes these tools, not what an agent can do with Wrike. Descriptions and "
                "comments carry the end customer's personal data: do not copy them into Jira or "
                "anywhere else unless the task explicitly asks for it."
            ),
        )
    )


__all__ = [
    "API_BASE",
    "EMPTY_IS_NOT_ABSENCE",
    "FORBIDDEN",
    "TASK_COMMENTS_PATH",
    "TASK_PATH",
    "UNAUTHORIZED",
    "WrikeClient",
    "WrikeError",
    "wrike_capability",
]
