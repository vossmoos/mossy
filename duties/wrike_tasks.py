"""Wrike task duty: every N minutes, run the Wrike notifications through Python.

``check()`` looks at the clock and at nothing else: it runs on every tick of the duty loop, so a
network call there would be made all day. ``evaluate()`` does the work, and does it by running the
skill's own scripts — **it never enqueues a goal for an agent.** The reason: a
skill does not bound an agent that has other doors, and this flow writes to a Jira that cannot
delete. The split is not ``freshdesk_bugs``', which hands its work to a goal.

TWO SWITCHES, BOTH OFF BY DEFAULT

``WRIKE_TASKS_ENABLED`` starts the duty. ``WRIKE_TASKS_WRITE_TO_JIRA`` lets it write, and stays off
even with the first one on. Each must be the literal ``true``. **The write switch is the duty's, not
the script's**: ``process_wrike_notification.py`` writes only when it is given
``--apply``, and this duty passes it only when the switch is on. In test mode a pass describes and
writes nothing — not Jira, not the register, and not Gmail: no label, not even skipped, because
labelling is writing too.

A PASS

1. ``fetch_wrike_notifications.py list``. It reads and labels nothing. A failure is a warning and
   the pass ends: a dead Gmail token must not look like an empty mailbox.
2. For each e-mail not held (below): ``process_wrike_notification.py --message-id <id>``, plus
   ``--apply`` only with the write switch on. One subprocess per e-mail, with its own timeout, so
   an e-mail that hangs does not take the runtime with it.
3. What came back decides what follows (``classify``). "Retry" is not something the duty does: an
   e-mail left unlabelled is listed again by the next pass. A failure that every later e-mail
   would hit too — a refused token, an unresolved identity — ends the pass.

THE MODEL LOOP, AND HOW IT IS CUT

Measured on 14 September 2026: in test mode nothing is labelled, so the same e-mail comes back every
pass, and describing a create evaluates its summary — three passes, three model calls. With the
write switch on, a model that keeps failing (``analysis_failed``) did the same. A cap per pass
bounds that spending; it does not stop it. So e-mails are **held**: one already run in test mode,
or one whose pass paid for the model and created nothing, is not run again in this process. One
model call per e-mail per process start, not per pass.

The memory lives at module level, not on the duty: ``mossy.duties.get_duty`` builds a new instance
for every ``evaluate`` (read in the core), so an attribute would be empty on every pass. It is not
on disk either: in test mode that would be a write. Declared residue: a restart forgets it, and
pays once more for each e-mail still unlabelled.

WHAT JIRA REFUSED IS HELD; WHAT WAS NEVER DECIDED IS RETRIED

A create Jira refused — it answered, and the answer was no — was refused for something that does
not change on its own, so it is held. A create nobody decided is retried by the next pass, and pays
for the model again: the request never reached Jira (no status at all, the network was down), or
Jira could not decide (5xx, or 429 asking to come back later). A passing failure held until a
restart is a task that is never created — nobody restarts the process on purpose — and that is
worse than paying for one summary too many.

Environment:

    WRIKE_TASKS_ENABLED            "true" to run at all; default off
    WRIKE_TASKS_WRITE_TO_JIRA      "true" to pass --apply; default off, even with the duty on
    WRIKE_TASKS_INTERVAL_MINUTES   minutes between passes, default 5
    WRIKE_TASKS_MAX_EMAILS         e-mails per pass, default 5
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mossy.duties.base import Duty, EnqueueRequest, register

if TYPE_CHECKING:
    from mossy.runtime.core import Runtime

logger = logging.getLogger(__name__)

_SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "gmail-wrike-tasks" / "scripts"
FETCH_SCRIPT = "fetch_wrike_notifications.py"
PROCESS_SCRIPT = "process_wrike_notification.py"
_LIST_TIMEOUT_SECONDS = 120
#: One e-mail: Gmail, Wrike, a model call, Jira and a read-back.
_PROCESS_TIMEOUT_SECONDS = 300

DEFAULT_INTERVAL_MINUTES = 5
DEFAULT_MAX_EMAILS = 5
#: check() fires within this many seconds of the minute; dedupe_key collapses the repeats.
_FIRE_WINDOW_SECONDS = 8

SCRIPT_TIMED_OUT = "script_timed_out"
SCRIPT_OUTPUT_UNREADABLE = "script_output_unreadable"

DONE = "done"
DESCRIBED = "described"
RETRY = "retry"
HOLD = "hold"
PERSON = "needs_a_person"
STOP = "stop_the_pass"

#: Every later e-mail in the pass would hit the same wall. A person fixes these, in .env or Gmail.
#: ``label_not_found`` never arrives as the envelope's reason — ``process`` wraps every label failure
#: as ``label_failed`` — so ``classify`` looks under ``label`` for it. ``no_allowed_senders`` is not
#: here: it can only come from the listing, which ``evaluate`` handles before ``classify``.
STOP_THE_PASS = frozenset({
    "gmail_token_refused", "gmail_not_configured", "label_not_found",
    "me_unresolved", "authentication_not_proven", "credentials_check_failed",
})
LABEL_FAILED = "label_failed"
#: Running again reads only, and changes nothing until a person looks.
NEEDS_A_PERSON = frozenset({"no_summary", "comments_unreadable", "plan_stopped"})
#: Running again would post the summarised comments as duplicates.
SUMMARISED_NOT_RECORDED = "summarised_comments_not_recorded"
CREATE_FAILED = "create_failed"

#: message id -> the verdict that held it. Module level on purpose: see the docstring.
_HELD: dict[str, dict[str, Any]] = {}


def _enabled() -> bool:
    return os.environ.get("WRIKE_TASKS_ENABLED", "false").strip().lower() == "true"


def _write_to_jira() -> bool:
    """Whether a pass may pass --apply. Off unless explicitly on — and the duty's to decide."""
    return (os.environ.get("WRIKE_TASKS_WRITE_TO_JIRA") or "").strip().lower() == "true"


def _interval_minutes() -> int:
    raw = os.environ.get("WRIKE_TASKS_INTERVAL_MINUTES", str(DEFAULT_INTERVAL_MINUTES))
    try:
        return max(1, min(1440, int(raw)))
    except ValueError:
        logger.warning("WRIKE_TASKS_INTERVAL_MINUTES=%r is not a number; using %d",
                       raw, DEFAULT_INTERVAL_MINUTES)
        return DEFAULT_INTERVAL_MINUTES


def _max_emails() -> int:
    raw = os.environ.get("WRIKE_TASKS_MAX_EMAILS", str(DEFAULT_MAX_EMAILS))
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("WRIKE_TASKS_MAX_EMAILS=%r is not a number; using %d",
                       raw, DEFAULT_MAX_EMAILS)
        return DEFAULT_MAX_EMAILS


def _refused_by_jira(status: Any) -> bool:
    """Jira answered and said no. No status means Jira was never reached; 5xx and 429 mean it
    could not decide. Neither is a refusal."""
    return isinstance(status, int) and not (status == 429 or 500 <= status <= 599)


def classify(out: dict[str, Any], *, write: bool) -> dict[str, Any]:
    """What one ``process_wrike_notification`` envelope means for the duty. Pure."""
    key = str(out.get("write_reason") or out.get("reason") or out.get("action") or "")
    if key == LABEL_FAILED:
        # process wraps every label failure; the label's own reason is the one that decides.
        label = out.get("label") if isinstance(out.get("label"), dict) else {}
        inner = str(label.get("reason") or "")
        if inner in STOP_THE_PASS:
            key = inner
    create = out.get("create") if isinstance(out.get("create"), dict) else {}
    proposed = create.get("would_create") if isinstance(create.get("would_create"), dict) else {}
    paid_for_the_model = bool(proposed.get("analysis")) and not create.get("created")

    def verdict(kind: str, *, hold: bool = False, **extra: Any) -> dict[str, Any]:
        return {"verdict": kind, "reason": key, "hold": hold, **extra}

    if out.get("ok"):
        if not write:
            # Test mode: whatever it described, running it again describes it again.
            return verdict(DESCRIBED, hold=True)
        return verdict(DONE)
    if key == CREATE_FAILED:
        status = create.get("status")
        if not _refused_by_jira(status):
            return verdict(RETRY, status=status)
        return verdict(HOLD, hold=True, status=status)
    if key in STOP_THE_PASS:
        return verdict(STOP, hold=paid_for_the_model)
    if key == SUMMARISED_NOT_RECORDED:
        return verdict(HOLD, hold=True)
    if key in NEEDS_A_PERSON:
        return verdict(PERSON, hold=paid_for_the_model)
    if paid_for_the_model:
        return verdict(HOLD, hold=True)
    return verdict(RETRY)


async def _run_script(script: str, args: list[str], timeout: int) -> dict[str, Any]:
    """Run one skill script and return its JSON envelope. Never raises."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, script, *args,
        cwd=str(_SKILL_SCRIPTS),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "reason": SCRIPT_TIMED_OUT, "script": script}
    try:
        data = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        detail = stderr.decode("utf-8", errors="replace").strip()[:300]
        return {"ok": False, "reason": SCRIPT_OUTPUT_UNREADABLE, "script": script,
                "detail": detail}
    if not isinstance(data, dict):
        return {"ok": False, "reason": SCRIPT_OUTPUT_UNREADABLE, "script": script}
    return data


@register
class WrikeTasks(Duty):
    name = "wrike_tasks"

    async def check(self, now: datetime) -> list[EnqueueRequest]:
        """Clock only. No network, no disk, no Gmail — this runs on every tick."""
        if not _enabled():
            return []
        minute_of_day = now.hour * 60 + now.minute
        if minute_of_day % _interval_minutes() != 0 or now.second >= _FIRE_WINDOW_SECONDS:
            return []
        window = now.strftime("%Y-%m-%dT%H:%M")
        return [
            EnqueueRequest(
                kind="evaluate_duty",
                payload={"duty": self.name, "window": window},
                dedupe_key=f"wrike_tasks:{window}",
            )
        ]

    async def evaluate(
        self, payload: dict[str, Any], runtime: Runtime
    ) -> list[EnqueueRequest]:
        """One pass, in Python. Returns no follow-up: nothing here is handed to an agent."""
        if not _enabled():
            return []
        write = _write_to_jira()
        mode = "" if write else " (test mode: nothing written)"
        limit = _max_emails()

        listing = await _run_script(FETCH_SCRIPT, ["list", "--max-results", str(limit)],
                                    _LIST_TIMEOUT_SECONDS)
        if not listing.get("ok"):
            logger.warning("wrike_tasks: listing failed (%s)%s — %s",
                           listing.get("reason"), mode, listing.get("detail") or "")
            return []

        for message in (listing.get("messages") or [])[:limit]:
            message_id = str((message or {}).get("id") or "").strip()
            if not message_id:
                continue
            if message_id in _HELD:
                logger.info("wrike_tasks: %s held (%s); not run again in this process",
                            message_id, _HELD[message_id]["reason"])
                continue
            args = ["--message-id", message_id]
            if write:
                args.append("--apply")
            out = await _run_script(PROCESS_SCRIPT, args, _PROCESS_TIMEOUT_SECONDS)
            seen = classify(out, write=write)
            if seen["hold"]:
                _HELD[message_id] = seen
            if seen["verdict"] in (DONE, DESCRIBED, RETRY):
                logger.info("wrike_tasks: %s -> %s (%s)%s",
                            message_id, seen["verdict"], seen["reason"], mode)
            else:
                logger.warning("wrike_tasks: %s -> %s (%s)%s — a person should look",
                               message_id, seen["verdict"], seen["reason"], mode)
            if seen["verdict"] == STOP:
                break
        return []
