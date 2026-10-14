# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python. Without one it falls back to sys.executable.
"""Read a Wrike task, its comments, and who the token is. Three reads, and nothing else.

The brief for the Wrike → Jira flow says it in as many words: *"the trigger is in Email, we do not poll Wrike. We use Wrike
API to get task or comment details."* So this is not a client for Wrike — it is the reads that a
caller who **already knows which task** needs in order to fill in the detail an email notification
does not carry.

**The third read is not about a task (the Wrike → Jira flow, piece 2).** ``get_me`` asks Wrike which contact the
token belongs to, so the flow can check a task's ``responsibleIds`` before creating an issue for
it. It is still a read, and still one GET.

**There is no polling here and there is no duty that watches Wrike.** Nothing in this module
lists tasks, searches, or asks what changed. If a future caller wants that, it is a different
decision than this one and it belongs in a different ticket.

**This module never writes to Wrike, and that is structural rather than promised.** The
transport has no ``method`` parameter: ``_get`` issues GET and there is no other verb in the
file. Adding a write means adding a function, not passing an argument, which is the difference
between a rule somebody can forget and a rule somebody has to break on purpose.

A skill script, deliberately — and no longer the only Wrike client. The skill script runtime has no
``mossy`` on the path — ``tests/test_skill_is_self_contained.py`` enforces it — so a capability
could not be imported by the scripts that need Wrike, and they carry this client. Since the Wrike → Jira flow
piece 6 the core has its own read-only Wrike capability, ``mossy/capabilities/wrike.py``, ported
from this module rather than importing it: a skill can be removed, and the core must not depend on
one. ``tests/test_wrike_capability.py`` holds the two clients to the same answers.

Configuration, from the environment, following ``mossy/capabilities/jira.py``:

    WRIKE_TOKEN    Wrike permanent access token, sent as ``Authorization: bearer <token>``

**The repository's .env overrides the environment.** ``mossy/env.py`` calls
``load_dotenv(override=True)``, so a ``WRIKE_TOKEN`` in the repo's ``.env`` beats one exported in
the shell. If the two differ, the file wins — measured, and the cause of an afternoon lost
elsewhere in this codebase.

What was measured against the live API before this was written, so nobody re-derives it:

* ``GET /tasks/{apiId}`` -> 200, and **description comes by default**. Asking for it with
  ``fields=["description"]`` is a **400**: the parameter is refused precisely because the field
  is already there. Do not add it.
* ``GET /tasks/{apiId}/comments`` -> 200 with the task's comments. The sub-resource works, so
  there is no need to fetch every comment in the account and filter.
* ``GET /comments?taskId={apiId}`` -> **400, "Parameter 'taskId' is not allowed"**. The flat
  endpoint refuses the filter rather than ignoring it, which is the good failure: nobody can
  believe they are filtering when they are not.
* ``GET /tasks/1234567890`` (the number from a permalink) -> **400, "Invalid Task ID"**.
  **The API does not accept the permalink number**, and there is no conversion in this module.
  A Wrike MCP connector will convert one, but that is the connector and not this API. It is not
  needed: the notification e-mail carries ``taskApiId`` (``MAAAAAEXmain``) in its
  ``X-Wrike-Metadata`` header, which is the id this module wants. Anyone tempted to pass the
  number from ``open.htm?id=`` should read this paragraph first.

**An empty answer is not proof that there is nothing.** The account this was measured on holds a
Collaborator licence, and a licence-restricted read is served as ``200`` with an empty ``data``
list — the same shape as a task that genuinely has no comments. The two cannot be told apart
from here, so the results say so rather than letting a caller read absence into silence. See
``EMPTY_IS_NOT_ABSENCE``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API_BASE = "https://www.wrike.com/api/v4"

#: The two paths this module knows, and the only two. ``TASK_COMMENTS_PATH`` is the **sub-resource**
#: and not the flat ``/comments``: measured, the flat one refuses ``?taskId=`` with a 400, so
#: reaching for it would mean fetching every comment in the account and filtering here — more
#: traffic, and a filter this module would have to be trusted to get right.
TASK_PATH = "/tasks/{task_id}"
TASK_COMMENTS_PATH = "/tasks/{task_id}/comments"

#: Who the token belongs to. ``?me=true`` makes Wrike COMMIT to one contact, the token's own,
#: instead of handing back a directory to pick from — see ``get_me``.
CONTACTS_ME_PATH = "/contacts?me=true"

#: The one reason ``get_me`` fails with when Wrike does not name exactly one "me".
ME_UNRESOLVED = "me_unresolved"

#: Said once, and attached to every empty result. A Collaborator licence hides what it does not
#: allow behind a 200 with no rows, so "no comments came back" and "you are not allowed to see
#: the comments" are the same bytes. A caller that treats the first as the second will report a
#: task as having no discussion when it has twenty-three.
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
        """True for 401/403 — the read was refused, as opposed to failing or finding nothing.

        Named so a caller does not have to write ``exc.status in (401, 403)`` and get it wrong.
        """
        return self.status in (UNAUTHORIZED, FORBIDDEN)

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} — HTTP {self.status}" if self.status else base


class WrikeClient:
    """Three reads against Wrike. No writes, no listing, no polling."""

    def __init__(self, token: str, *, timeout_seconds: int = 30, api_base: str = API_BASE) -> None:
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.api_base = api_base.rstrip("/")

    @classmethod
    def from_env(cls) -> "WrikeClient | None":
        """``WRIKE_TOKEN`` or ``None``. Never raises, and never makes a request.

        ``None`` rather than an exception, following ``JiraClient.from_env`` and
        ``GmailClient.from_env``: a deployment without Wrike configured is an ordinary state of
        the world, not a failure, and the caller decides what to do about it. A constructor that
        raised would make an unconfigured deployment crash where it should simply do less.
        """
        token = (os.environ.get("WRIKE_TOKEN") or "").strip()
        if not token:
            return None
        return cls(token)

    # ---------------------------------------------------------------- transport (GET only)
    def _get(self, path: str) -> Any:
        """One GET. **There is no method parameter**, and no other verb exists in this module.

        Raises ``WrikeError`` for anything that is not a 200, so a refusal can never arrive at
        the caller wearing the shape of an empty result.
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

    # ---------------------------------------------------------------- the three reads
    def get_task(self, task_id: str) -> dict[str, Any]:
        """One task by its **API id** (``MAAAAAEXmain``), the id the notification e-mail carries.

        Not the number from a permalink: ``GET /tasks/1234567890`` is a 400, measured. Nothing
        here converts one into the other, and this module does not pretend it can.

        ``description`` is in the answer already; it is not requested, because asking for it is
        a 400.

        Returns ``ok: True`` with the task, or ``ok: False`` with ``reason``. A 200 carrying no
        task is ``not_visible`` and not "does not exist" — the two are the same bytes here.
        """
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
        """The comments of one task, through the **sub-resource**.

        ``/tasks/{id}/comments``, never ``/comments``: the flat endpoint refuses ``?taskId=``
        with a 400, so using it would mean pulling every comment in the account and filtering
        here.

        An empty list comes back as ``ok: True`` with ``empty: True`` and ``empty_means``
        spelling out that this is not evidence of absence. It is not an error — a task really
        can have no comments — and it is not proof either.
        """
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

    def get_me(self) -> dict[str, Any]:
        """The contact this token belongs to — ``GET /contacts?me=true`` — and nothing guessed.

        Measured on 14 September 2026: plain ``/contacts`` and ``/contacts?me=true`` both answered
        200 with one contact, ``KUEXAMP2``, carrying ``me: True``. Plain ``/contacts`` is still
        not used: on this Collaborator licence it happens to return one row, but on a fuller
        licence it returns the whole directory, and "which of these is me" would be a guess.
        ``?me=true`` is the API committing to an answer.

        The row is checked, not trusted. Exactly one row, carrying ``me: True`` and an id. Zero
        rows, two rows, or one row without the flag — the parameter silently ignored some day —
        is ``me_unresolved``, and the caller stops: who the token is is never guessed.

        A refused or failed request raises ``WrikeError``, as the other two reads do.
        """
        rows = self._rows(self._get(CONTACTS_ME_PATH))
        if len(rows) != 1:
            return {"ok": False, "reason": ME_UNRESOLVED, "rows": len(rows),
                    "detail": f"Wrike returned {len(rows)} contacts for ?me=true; exactly one "
                              "was expected. Who the token is is not guessed."}
        row = rows[0]
        if row.get("me") is not True:
            return {"ok": False, "reason": ME_UNRESOLVED, "rows": 1, "me_flag": row.get("me"),
                    "detail": "The one contact Wrike returned does not carry me: True, so it is "
                              "not accepted as the token's own."}
        me_id = str(row.get("id") or "").strip()
        if not me_id:
            return {"ok": False, "reason": ME_UNRESOLVED, "rows": 1,
                    "detail": "The contact marked me: True carries no id."}
        return {"ok": True, "me_id": me_id}


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", errors="replace")


__all__ = [
    "API_BASE",
    "CONTACTS_ME_PATH",
    "EMPTY_IS_NOT_ABSENCE",
    "FORBIDDEN",
    "ME_UNRESOLVED",
    "TASK_COMMENTS_PATH",
    "TASK_PATH",
    "UNAUTHORIZED",
    "WrikeClient",
    "WrikeError",
]
