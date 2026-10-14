"""The one call a duty makes to enqueue company checks. Nothing else is public.

A duty should not have to know this skill's scripts, its JSON shapes or its environment
variables. It calls one function with the ticket ids it already has, and gets back the requests
to enqueue — or nothing at all, which is the common case once contacts are linked. No duty in
this repository calls it yet; the usage below is how one would.

**Where the pre-check runs.** Inside this call, which means inside the caller's ``evaluate()``.
It reads Freshdesk to answer "does this contact already have a company", so it cannot run in a
duty's ``check()``: the sentinel calls that once a second, and a network call there would be one
call per second, per duty, forever.

**It never raises.** A duty that asks for company checks is doing something secondary; if this
fails — Freshdesk down, misconfiguration, a bad ticket id — the caller's own work must go on.
Failures come back as ``notes`` and an empty ``requests`` list, so the duty can log them and
continue rather than catching exceptions it did not cause.

Usage from a duty (the import is by file path, because ``skills/`` is not a package and the
folder name contains a hyphen):

```python
import importlib.util
spec = importlib.util.spec_from_file_location(
    "company_linking_hook",
    Path(__file__).resolve().parents[1] / "skills" / "company-contact-linking" / "queue_hook.py",
)
company_linking = importlib.util.module_from_spec(spec)
spec.loader.exec_module(company_linking)

# ...later, inside evaluate():
plan = company_linking.plan_company_checks([t["id"] for t in tickets])
requests += [EnqueueRequest(kind="goal", **r) for r in plan["requests"]]
```

Standard library only, and nothing imported from outside this folder.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

_SCRIPT = Path(__file__).resolve().parent / "scripts" / "plan_company_check.py"
_TIMEOUT_SECONDS = 120
# Duty-originated work sits behind anything a person is waiting for. 3 matches Mossy's
# BACKGROUND priority; it is passed as a plain int so this file stays free of Mossy imports.
BACKGROUND_PRIORITY = 3


def plan_company_checks(
    ticket_ids: Iterable[int],
    *,
    already_queued: Iterable[str] = (),
    python_executable: str | None = None,
    timeout_seconds: int = _TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Which of these tickets need a company-check task.

    Returns ``{"requests": [...], "skipped": [...], "notes": [...]}``. Each request is a plain
    dict — ``goal``, ``dedupe_key``, ``priority``, ``context`` — so the caller builds whatever
    object its runtime expects and this skill stays portable.

    ``already_queued`` is a set of dedupe keys the caller has already used in this run; matching
    requests are dropped, so a duty that calls this twice in one pass queues nothing twice.
    """
    ids = [int(t) for t in ticket_ids if str(t).strip()]
    if not ids:
        return {"requests": [], "skipped": [], "notes": []}

    command = [
        python_executable or sys.executable,
        str(_SCRIPT),
        "--ticket-ids",
        ",".join(str(i) for i in ids),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(_SCRIPT.parent),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return _failure(f"the company pre-check timed out after {timeout_seconds}s")
    except OSError as exc:  # the interpreter or the script is not where we thought
        return _failure(f"the company pre-check could not be started: {exc}")

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        detail = (completed.stderr or completed.stdout or "").strip()[:300]
        return _failure(f"the company pre-check returned no usable JSON: {detail}")

    if not isinstance(payload, dict) or not payload.get("ok"):
        return _failure(str((payload or {}).get("error") or "the company pre-check failed"))

    seen = set(already_queued)
    requests: list[dict[str, Any]] = []
    duplicates = 0
    for planned in payload.get("planned") or []:
        key = planned.get("dedupe_key")
        if key and key in seen:
            duplicates += 1
            continue
        if key:
            seen.add(key)
        requests.append(
            {
                "goal": planned["goal"],
                "dedupe_key": key,
                "priority": BACKGROUND_PRIORITY,
                "context": {
                    "skill": "company-contact-linking",
                    "ticket_id": planned.get("ticket_id"),
                    "contact_id": planned.get("contact_id"),
                    "subject": planned.get("subject"),
                },
            }
        )

    notes = list(payload.get("notes") or [])
    if duplicates:
        notes.append(f"{duplicates} company check(s) were already queued in this run.")
    return {"requests": requests, "skipped": payload.get("skipped") or [], "notes": notes}


def company_check_requests(
    ticket_ids: Iterable[int],
    request_factory: Callable[..., Any] | None = None,
    **kwargs: Any,
) -> list[Any]:
    """The one-liner: the requests to enqueue, and nothing else.

    With a ``request_factory`` (a duty passes its own ``EnqueueRequest``) the dicts are built
    into that type; without one the dicts come back as they are. Notes and skips are dropped —
    use :func:`plan_company_checks` when the caller wants to log them.
    """
    plan = plan_company_checks(ticket_ids, **kwargs)
    if request_factory is None:
        return plan["requests"]
    return [request_factory(kind="goal", **request) for request in plan["requests"]]


def _failure(message: str) -> dict[str, Any]:
    # An empty request list plus a note: the caller logs it and carries on with its own work.
    return {"requests": [], "skipped": [], "notes": [message]}


__all__ = ["BACKGROUND_PRIORITY", "company_check_requests", "plan_company_checks"]
