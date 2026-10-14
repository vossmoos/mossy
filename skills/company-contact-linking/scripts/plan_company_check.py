# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python, which may lack this project's dependencies.
# Without one it falls back to sys.executable, i.e. the interpreter running Mossy.
"""Decide which tickets deserve a company-check task. Read-only: nothing is written.

This is the pre-check, and it is the reason the hook exists at all. Asking "does this contact
already have a company?" needs Freshdesk, so it can only run where network calls are allowed —
inside a duty's ``evaluate()``, never in ``check()``, which the sentinel calls once a second.

A ticket is planned only when all of these hold:

* the ticket's group is in ``LINKING_GROUP_IDS`` (empty means no group, so nothing is planned);
* the requester's contact has no company yet;
* the ticket does not already carry the processed tag.

Everything else is reported under ``skipped`` with a reason, because "no task was created" and
"the check never ran" look identical from the outside otherwise.

Output JSON: ok, planned[], skipped[], notes[].
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from _audit import guarded, record
from _config import ConfigError, all_processed_tags, enabled, group_ids, is_in_scope
from _freshdesk import FreshdeskClient, FreshdeskError


def _ticket_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    ids: list[int] = []
    for part in raw.replace(" ", ",").split(","):
        if part.strip():
            ids.append(int(part.strip()))
    return ids


def _window() -> int:
    """The retry window the dedupe key expires with, in whole periods since the epoch."""
    try:
        seconds = max(30, int(os.environ.get("LINKING_RETRY_WINDOW_SECONDS", "300")))
    except ValueError:
        seconds = 300
    return int(time.time()) // seconds


def plan(ticket_ids: list[int]) -> dict:
    """Which of these tickets need a company check, and why the others do not."""
    planned: list[dict] = []
    skipped: list[dict] = []
    notes: list[str] = []

    if not enabled():
        notes.append("LINKING_ENABLED is not true; nothing is planned.")
        return {"ok": True, "planned": [], "skipped": [
            {"ticket_id": t, "reason": "skill_disabled"} for t in ticket_ids
        ], "notes": notes}

    scope = group_ids()
    if not scope:
        notes.append(
            "LINKING_GROUP_IDS is empty, which means no group is in scope; nothing is planned."
        )
        return {"ok": True, "planned": [], "skipped": [
            {"ticket_id": t, "reason": "no_scope_configured"} for t in ticket_ids
        ], "notes": notes}

    client = FreshdeskClient.from_env()
    # Every tag that means this ticket has been through the skill, not only the linked one.
    # Checking a single tag left the declined tickets — no signals, no match, ambiguous —
    # looking untouched, so every sweep queued them again and an agent re-ran the whole chain
    # on tickets that were finished. Measured through the duty: 9 checks queued where 5 were due.
    tags = all_processed_tags()

    for ticket_id in ticket_ids:
        try:
            ticket = client.get_ticket(ticket_id)
        except FreshdeskError as exc:
            # One unreadable ticket must not cost the others their check.
            skipped.append({"ticket_id": ticket_id, "reason": "ticket_unreadable", "detail": str(exc)})
            continue

        if ticket.get("deleted") or ticket.get("spam"):
            skipped.append({"ticket_id": ticket_id, "reason": "ticket_deleted_or_spam"})
            continue
        if not is_in_scope(ticket.get("group_id")):
            skipped.append({"ticket_id": ticket_id, "reason": "out_of_scope",
                            "detail": f"group {ticket.get('group_id')}"})
            continue
        carried = {str(t) for t in (ticket.get("tags") or [])} & tags
        if carried:
            skipped.append({"ticket_id": ticket_id, "reason": "already_processed",
                            "detail": ", ".join(sorted(carried))})
            continue

        requester_id = ticket.get("requester_id")
        if not requester_id:
            skipped.append({"ticket_id": ticket_id, "reason": "no_requester"})
            continue
        try:
            contact = client.get_contact(int(requester_id))
        except FreshdeskError as exc:
            skipped.append({"ticket_id": ticket_id, "reason": "contact_unreadable", "detail": str(exc)})
            continue

        if contact.get("company_id"):
            # The whole point of the pre-check: a linked contact costs no task at all.
            skipped.append({
                "ticket_id": ticket_id,
                "reason": "already_linked",
                "detail": f"company {contact.get('company_id')}",
            })
            continue

        planned.append({
            "ticket_id": ticket_id,
            "contact_id": contact.get("id") or int(requester_id),
            "subject": ticket.get("subject"),
            "group_id": ticket.get("group_id"),
            # Per ticket **and window**, not per ticket. A key that never changes is remembered
            # for the life of the process, so a ticket whose first attempt ended early is never
            # queued again: one bad turn and the ticket is dead until a restart. Measured — four
            # tickets each got exactly one attempt, each died in a different place, and 27
            # sweeps later none had been retried.
            #
            # The window makes the key expire. It is not what stops duplicates piling up on a
            # ticket that succeeded — that is the outcome tag in Freshdesk, which is durable and
            # survives restarts. This layer only stops the same ticket being queued twice inside
            # one window.
            "dedupe_key": f"company_check:ticket:{ticket_id}:{_window()}",
            "goal": (
                f"Check the company link for Freshdesk ticket {ticket_id} using the "
                "company-contact-linking skill. Follow the skill flow, and then say in a "
                "sentence or two which company was linked or why none was."
            ),
        })

    return {"ok": True, "planned": planned, "skipped": skipped, "notes": notes}


def main() -> None:
    parser = argparse.ArgumentParser(description="Plan company-check tasks for tickets.")
    parser.add_argument(
        "--ticket-ids", "--ticket_ids", dest="ticket_ids",
        help="Comma-separated ticket ids to consider.",
    )
    args = parser.parse_args()

    try:
        ticket_ids = _ticket_ids(args.ticket_ids)
        if not ticket_ids:
            raise ValueError("No ticket ids given: pass --ticket-ids 1,2,3.")
        result = plan(ticket_ids)
    except (ConfigError, FreshdeskError, ValueError) as exc:
        failure = {"ok": False, "error": str(exc)}
        record("plan_company_check.py", result=failure)
        print(json.dumps(failure, ensure_ascii=False))
        sys.exit(1)

    record("plan_company_check.py", result={"ok": True, "candidate_count": len(result["planned"])})
    print(
        f"{len(result['planned'])} company check(s) planned, {len(result['skipped'])} skipped.",
        file=sys.stderr,
        flush=True,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    guarded("plan_company_check.py", main)
