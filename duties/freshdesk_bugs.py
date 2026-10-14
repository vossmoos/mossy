"""Freshdesk mention triage duty: every N minutes, run the gmail-freshdesk-bugs skill.

``check()`` decides whether it is time; ``evaluate()`` enqueues the work. The split is the
framework's rule and it is not cosmetic: ``check()`` runs on every tick of the duty loop, so it
**must not touch the network**. It looks at the clock and at nothing else. Everything that costs
a call — listing the mailbox, downloading, analysing — happens inside the goal that
``evaluate()`` enqueues, where it is a task with a status the queue can report on.

Dedupe is the Gmail label, not state kept here. ``fetch_mention_emails.py list`` excludes
anything already carrying it, so a message processed by an earlier run is never returned. The
label goes on at the **end** of the flow, after the Jira issue exists and has been confirmed:
setting it earlier would skip an e-mail whose work never finished, and lose it silently. This
duty only names the label; it does not set it.

Ships **disabled**. ``FRESHDESK_BUGS_ENABLED`` must be an explicit "true".

Environment:

    FRESHDESK_BUGS_ENABLED           "true" to run at all; default false
    FRESHDESK_BUGS_INTERVAL_MINUTES  minutes between passes, default 5
    FRESHDESK_BUGS_PROCESSED_LABEL   Gmail label marking done work, default "mossy-processed"
    FRESHDESK_BUGS_MAX_EMAILS        e-mails per pass, default 5
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import TYPE_CHECKING, Any

from mossy.duties.base import Duty, EnqueueRequest, register

if TYPE_CHECKING:
    from mossy.runtime.core import Runtime

DEFAULT_INTERVAL_MINUTES = 5
DEFAULT_PROCESSED_LABEL = "mossy-processed"
DEFAULT_MAX_EMAILS = 5

#: How far into the minute a tick still counts as "on the interval". The duty loop does not fire
#: exactly on the second; without a window a pass would be missed whenever it ran a moment late.
_FIRING_WINDOW_SECONDS = 8


def _enabled() -> bool:
    return os.environ.get("FRESHDESK_BUGS_ENABLED", "false").strip().lower() == "true"


def _interval_minutes() -> int:
    try:
        return max(1, int(os.environ.get("FRESHDESK_BUGS_INTERVAL_MINUTES",
                                         str(DEFAULT_INTERVAL_MINUTES))))
    except ValueError:
        return DEFAULT_INTERVAL_MINUTES


def _processed_label() -> str:
    return (os.environ.get("FRESHDESK_BUGS_PROCESSED_LABEL") or "").strip() \
        or DEFAULT_PROCESSED_LABEL


def _max_emails() -> int:
    try:
        return max(1, int(os.environ.get("FRESHDESK_BUGS_MAX_EMAILS", str(DEFAULT_MAX_EMAILS))))
    except ValueError:
        return DEFAULT_MAX_EMAILS


@register
class FreshdeskBugs(Duty):
    name = "freshdesk_bugs"

    async def check(self, now: datetime) -> list[EnqueueRequest]:
        """Clock only. No network, no disk, no Gmail — this runs on every tick."""
        if not _enabled():
            return []
        interval = _interval_minutes()
        if now.minute % interval != 0 or now.second >= _FIRING_WINDOW_SECONDS:
            return []
        window = now.strftime("%Y-%m-%dT%H:%M")
        return [
            EnqueueRequest(
                kind="evaluate_duty",
                payload={"duty": self.name, "window": window},
                # One pass per minute-window, however many times the loop ticks inside it.
                dedupe_key=f"freshdesk_bugs:{window}",
            )
        ]

    async def evaluate(
        self, payload: dict[str, Any], runtime: Runtime
    ) -> list[EnqueueRequest]:
        """Enqueue the skill run. The work itself belongs to the goal, not to the duty."""
        window = str(payload.get("window") or "")
        label = _processed_label()
        goal = (
            "Use the gmail-freshdesk-bugs skill to triage new Freshdesk mention notifications. "
            "Follow its SKILL.md Flow in order and do not improvise: run "
            "scripts/fetch_mention_emails.py list to get the unprocessed e-mails, and for each "
            "one work through parse, analyse, dedupe, and either update the existing Jira issue "
            "or open a new one. "
            f"Mark an e-mail processed with scripts/fetch_mention_emails.py mark-processed only "
            f"after its Jira issue exists and was confirmed — the {label!r} label is what stops "
            "the next pass from picking it up again, so it must never be set before the work is "
            "done. If nothing came back unprocessed, say so and stop."
        )
        return [
            EnqueueRequest(
                kind="goal",
                goal=goal,
                dedupe_key=f"freshdesk_bugs:run:{window}",
                context={
                    "duty": self.name,
                    "window": window,
                    "processed_label": label,
                    "max_emails": _max_emails(),
                },
            )
        ]
