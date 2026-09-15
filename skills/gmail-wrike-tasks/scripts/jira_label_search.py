# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python. Without one it falls back to sys.executable.
"""Find the Jira issue that carries a Wrike task's label. Read-only, and honest about silence.

The local register (``wrike_jira_links``) is fast but losable: it lives in a file under
``~/.local/share`` that nobody backs up. The label on the issue is what makes that file an
optimisation instead of the only copy — lose the file and the links can be found again; have no
label and they cannot.

**Reading only.** Writing the label when the issue is created belongs to the create step. Nothing
here creates, updates or comments on anything: the only verb in this module is GET.

★ **AN EMPTY RESULT IS NOT AN ANSWER, AND THIS IS THE POINT OF THE MODULE.** Measured against
the real Jira: a client whose token does not authenticate is served **200 with**
``{"issues": []}`` — not a 401. The search cannot tell "this task has no issue" from "these
credentials are no good". That cost an hour of believing a project was empty when the token was
simply wrong, and it is the exact shape of failure this flow keeps finding: a wrong answer
that looks like an ordinary one.

So this module never reports "no link" on its own. An empty page comes back as ``matched: False``
with ``authenticated: None`` and ``empty_means`` spelling out what it could also be. Only a
**non-empty** result proves the credentials work — rows cannot come back unauthenticated — and
only then is ``authenticated`` True. A caller that opens a new Jira issue on an unqualified empty
answer will open one per task, every pass, for as long as the token stays broken.

The same trap sits on the other side: ``GET /issue/{key}`` answers **404** for an unauthenticated
client too, so "the row is dead, open a new issue" is only safe once the credentials are known
good. See ``wrike_jira_links.TABLE_IS_AN_INDEX``.

What else was measured, so nobody re-derives it:

* ``/rest/api/3/search`` is **gone**: 410, *"The requested API has been removed. Please migrate
  to /rest/api/3/search/jql"*. It is not deprecated-but-working; it is removed.
* ``/rest/api/3/search/jql`` accepts ``labels = "..."`` and answers 200 with
  ``{"issues": [], "isLast": true}`` when nothing matches.
* A malformed JQL answers **400** with the syntax error, so "nothing matched" and "the query was
  wrong" are distinguishable — which is the one distinction this API does make cleanly.

Measured again once the credentials worked, and these are what the module is built on:

* ``GET /issue/PROJ-193`` -> 200, so a key can be confirmed to still exist.
* ``labels`` **comes back when it is asked for**. So a match can be checked against the data
  rather than trusted because the filter said so — see ``label_confirmed`` below.
* The response carries ``nextPageToken`` with ``isLast: false``. The new endpoint pages by
  **token**, not by ``startAt``/``total`` like the removed one. This module does not page: a
  label encodes one Wrike task, so zero or one issue is the whole of what is expected, and a
  second page means something is wrong rather than something is long. Paging would gather that
  quietly and hand it back looking ordinary. ``isLast: false`` is reported as an anomaly and
  nothing is truncated in silence.

**Still true for issues this flow did not create, and not fixed by authenticating:** an issue opened
by hand, or before this flow labelled what it creates, carries no Wrike label, so a search for its
task returns empty for that reason too — a third meaning of the same empty page, on top of the two
below. The first real issue the flow created (14 September 2026) was created with its
task's label in the same body; the read-back confirmed its key, not the label. The test label
``wrike-MAAAAATESTONLY`` stays on a permanent test issue, left by the measurement that proved
labels work on create (11 September 2026; label re-read on 14 September).

Configuration, following ``mossy/capabilities/jira.py``:

    JIRA_SITE        e.g. example.atlassian.net
    JIRA_USER_EMAIL  the account the token belongs to
    JIRA_API_KEY     API token; sent as HTTP Basic <email>:<token>

Self-contained: no ``import mossy``, no ``python-dotenv``, its own urllib client. It does not
import ``mossy/capabilities/jira.py`` and does not touch the Freshdesk flow's
``create_jira_issue.py`` — that flow works and nobody asked for it to change.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

#: The endpoint that exists. ``/rest/api/3/search`` is removed — 410, measured — so this is not
#: a preference between two working paths.
SEARCH_PATH = "/search/jql"
REMOVED_SEARCH_PATH = "/search"

#: How a Wrike task is marked on its Jira issue. One place, because the search that reads it and
#: the creation that will write it (the create step) have to agree, and a format spelled twice is a format
#: that drifts. Jira labels may not contain spaces; a Wrike API id never does.
TASK_LABEL_PREFIX = "wrike-"

#: Said once and attached to every empty answer.
EMPTY_IS_NOT_ABSENCE = (
    "Jira answered 200 with no issues. An unauthenticated client is served exactly this — 200 "
    "with an empty list, not a 401 — so an empty page is not evidence that the task has no "
    "issue. Confirm the credentials before treating it as one."
)


class JiraSearchError(RuntimeError):
    """A search failed. Carries the status and body so 400 can be told from 404 and from 410."""

    def __init__(self, message: str, *, status: int | None = None, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body

    @property
    def bad_query(self) -> bool:
        """True for 400 — the JQL was malformed. Never the same as "nothing matched"."""
        return self.status == 400

    def __str__(self) -> str:
        base = super().__str__()
        return f"{base} — HTTP {self.status}" if self.status else base


def task_label(task_api_id: str) -> str:
    """``MAAAAAEXmain`` -> ``wrike-MAAAAAEXmain``. The one place the format is spelled."""
    return f"{TASK_LABEL_PREFIX}{str(task_api_id).strip()}"


class JiraLabelSearch:
    """One read: which issue carries this task's label. No writes, no other verb."""

    def __init__(self, site: str, email: str, api_token: str, *, space_name: str = "",
                 timeout_seconds: int = 30) -> None:
        self.site = _normalize_site(site)
        self.email = email
        self.api_token = api_token
        self.space_name = (space_name or "").strip()
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_env(cls) -> "JiraLabelSearch | None":
        """``None`` when any of the three is missing. Never raises, never makes a request.

        The convention of ``JiraClient.from_env`` and ``GmailClient.from_env``: an unconfigured
        deployment is an ordinary state of the world, not a failure.
        """
        site = _normalize_site(os.environ.get("JIRA_SITE") or "")
        email = (os.environ.get("JIRA_USER_EMAIL") or "").strip()
        token = (os.environ.get("JIRA_API_KEY") or "").strip()
        if not site or not email or not token:
            return None
        return cls(site, email, token,
                   space_name=(os.environ.get("JIRA_SPACE_NAME") or "").strip())

    # ------------------------------------------------------------------ transport (GET only)
    def _get(self, path: str, query: dict[str, str]) -> Any:
        """One GET. **No method parameter**, and no other verb appears in this module."""
        url = (
            f"https://{self.site}/rest/api/3{path}"
            + ("?" + urllib.parse.urlencode(query) if query else "")
        )
        token = base64.b64encode(f"{self.email}:{self.api_token}".encode()).decode("ascii")
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                return _decode(response.read())
        except urllib.error.HTTPError as exc:
            body = _decode(exc.read() if exc.fp else b"")
            if exc.code == 400:
                raise JiraSearchError(
                    "Jira refused the JQL (400). This is a malformed query, not an empty result.",
                    status=400, body=body,
                ) from exc
            if exc.code == 410:
                raise JiraSearchError(
                    "Jira answered 410: this search endpoint has been removed. "
                    f"Use {SEARCH_PATH}, not {REMOVED_SEARCH_PATH}.",
                    status=410, body=body,
                ) from exc
            raise JiraSearchError(f"Jira search failed: {url}", status=exc.code, body=body) from exc
        except urllib.error.URLError as exc:
            raise JiraSearchError(f"Jira is unreachable: {exc.reason}") from exc

    # ------------------------------------------------------------------ the one operation
    def find_issue_for_task(self, task_api_id: str, *, limit: int = 5) -> dict[str, Any]:
        """The issue carrying this task's label, if the search can see one.

        Returns ``matched: True`` with the key when rows come back, and ``matched: False`` with
        ``authenticated: None`` when the page is empty — never a bare "there is no issue".

        ``authenticated`` is True only when rows came back, because that is the only thing that
        proves it: an unauthenticated client is served an empty 200. It is never False, because
        nothing this module can see distinguishes a bad token from an empty project.
        """
        label = task_label(task_api_id)
        result: dict[str, Any] = {
            "ok": True, "task_api_id": str(task_api_id).strip(), "label": label,
            "matched": False, "authenticated": None,
        }
        if not str(task_api_id).strip():
            return {**result, "ok": False, "reason": "no_task_api_id",
                    "detail": "A Wrike task API id is required; this module never guesses one."}

        payload = self._get(SEARCH_PATH, {
            "jql": f'labels = "{label}"',
            "fields": "key,labels",
            "maxResults": str(int(limit)),
        })
        issues = payload.get("issues") if isinstance(payload, dict) else None
        rows = [i for i in issues if isinstance(i, dict)] if isinstance(issues, list) else []

        if not rows:
            result["empty"] = True
            result["empty_means"] = EMPTY_IS_NOT_ABSENCE
            return result

        # Rows came back, so the credentials work. This is the only place that can say so.
        result["authenticated"] = True
        result["issue_keys"] = [str(i.get("key")) for i in rows if i.get("key")]

        # The filter said these carry the label; the data says whether they do. ``labels`` comes
        # back when asked for — measured — so this is checkable, and a check that can be made is
        # a check that should be. A row without the label means the query and the issue disagree,
        # which is not something to act on quietly.
        confirmed, unconfirmed, uncheckable = [], [], []
        for row in rows:
            key = str(row.get("key") or "")
            labels = (row.get("fields") or {}).get("labels")
            if not isinstance(labels, list):
                uncheckable.append(key)
            elif label in labels:
                confirmed.append(key)
            else:
                unconfirmed.append(key)

        if uncheckable:
            # The response did not carry labels at all. Not False — nothing was checked.
            result["label_confirmed"] = None
            result["unchecked_keys"] = uncheckable
        elif unconfirmed:
            result["label_confirmed"] = False
            result["issues_without_the_label"] = unconfirmed
        else:
            result["label_confirmed"] = True
        result["confirmed_keys"] = confirmed

        # A row that does not carry the label is not this task's issue, whatever the filter
        # returned. When nothing could be checked, the filter is all there is.
        usable = confirmed if (confirmed or unconfirmed) else result["issue_keys"]
        result["matched"] = bool(usable)
        result["issue_key"] = usable[0] if usable else None
        result["issue_url"] = (
            f"https://{self.site}/browse/{result['issue_key']}" if result["issue_key"] else None
        )

        reasons = []
        if unconfirmed:
            reasons.append("label_not_on_issue")
        if len(usable) > 1:
            # One task, one issue. More than one means somebody labelled by hand, or the create step
            # created a second: the caller has to decide, and it must not be hidden.
            reasons.append("more_than_one_issue")
        if isinstance(payload, dict) and payload.get("isLast") is False:
            # The new endpoint pages by token. One label should never fill a page, so a second
            # page is an anomaly, not a long list — and this module does not follow it.
            reasons.append("more_than_one_page")
            result["next_page_token"] = payload.get("nextPageToken")
        if reasons:
            result["reason"] = reasons[0] if len(reasons) == 1 else ",".join(reasons)
            result["anomalies"] = reasons
        return result


    def issue_exists(self, issue_key: str) -> dict[str, Any]:
        """Is this Jira key still there? ``GET /issue/{key}``, measured at 200 for an existing issue.

        **A 404 does not mean the issue is gone.** An unauthenticated client is served 404 here
        too — measured — exactly as it is served an empty 200 by the search. So this reports
        ``exists: None`` for a 404, never False: "deleted" and "these credentials are no good"
        arrive as the same status, and a caller that read the first into the second would open a
        duplicate issue for every task, on every pass, for as long as the token stayed broken.

        A **200 settles both questions at once**: the issue is there, and the credentials work.
        That is the only positive proof of authentication this module can obtain, which is why
        ``authenticated`` is True only here and when the search returns rows.
        """
        key = str(issue_key).strip()
        result: dict[str, Any] = {"ok": True, "issue_key": key, "exists": None,
                                  "authenticated": None}
        if not key:
            return {**result, "ok": False, "reason": "no_issue_key"}
        try:
            payload = self._get(f"/issue/{urllib.parse.quote(key)}", {"fields": "key"})
        except JiraSearchError as exc:
            if exc.status == 404:
                result["reason"] = "not_found_or_unauthenticated"
                result["detail"] = (
                    "Jira answered 404. An unauthenticated client is served 404 here too, so "
                    "this is not evidence that the issue was deleted."
                )
                return result
            raise
        result["exists"] = True
        result["authenticated"] = True
        if isinstance(payload, dict) and payload.get("key"):
            result["issue_key"] = str(payload["key"])
        return result


    def credentials_proven(self) -> dict[str, Any]:
        """Ask a question that MUST return rows if the credentials work.

        The canary. An unauthenticated client is served an empty 200 by every search, so an empty
        label search proves nothing on its own — and a caller about to create a Jira issue needs
        better than nothing, because creating on a bad token opens a duplicate per task on every
        pass and **nothing here can delete it afterwards**.

        So this asks for the newest issue in the configured project. Rows coming back settle it:
        the credentials work. An empty answer does not settle the opposite — the project could
        genuinely be empty — so ``proven`` is True or None, never False, for the same reason
        ``authenticated`` is.
        """
        result: dict[str, Any] = {"ok": True, "proven": None, "project": self.space_name}
        if not self.space_name:
            return {**result, "ok": False, "reason": "no_project",
                    "detail": "JIRA_SPACE_NAME is not set, so there is no question to ask."}
        payload = self._get(SEARCH_PATH, {
            "jql": f"project = {self.space_name} ORDER BY created DESC",
            "fields": "key",
            "maxResults": "1",
        })
        issues = payload.get("issues") if isinstance(payload, dict) else None
        if isinstance(issues, list) and issues:
            result["proven"] = True
            return result
        result["reason"] = "empty_project_or_unauthenticated"
        result["detail"] = (
            "The newest-issue query came back empty. The project may genuinely have no issues, "
            "or the credentials may not authenticate — an unauthenticated client is served an "
            "empty 200. This does not prove the credentials are bad; it fails to prove they are "
            "good, which is enough to refuse to create."
        )
        return result


def _normalize_site(site: str) -> str:
    value = (site or "").strip().rstrip("/")
    for prefix in ("https://", "http://"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
    return value


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", errors="replace")


__all__ = [
    "EMPTY_IS_NOT_ABSENCE",
    "REMOVED_SEARCH_PATH",
    "SEARCH_PATH",
    "TASK_LABEL_PREFIX",
    "JiraLabelSearch",
    "JiraSearchError",
    "task_label",
]
