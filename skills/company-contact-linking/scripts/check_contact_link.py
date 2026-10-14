# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python, which may lack this project's dependencies.
# Without one it falls back to sys.executable, i.e. the interpreter running Mossy.
"""The stop condition. Read-only: nothing is written.

Answers two questions and nothing else: is this contact already linked to a company, and is
this ticket in scope at all. When ``linked`` is true, or ``in_scope`` is false, the flow ends
here and the ticket is left untouched.

Scope fails closed. ``LINKING_GROUP_IDS`` empty means **no** group, not every group: a
deployment that has not configured a scope does nothing rather than reaching into every ticket
in the helpdesk. ``in_scope`` then comes back false with a note saying why, which is a
configuration answer, not an error.

Output JSON: ok, ticket_id, linked, company_id, company_name, in_scope, notes[].
"""

from __future__ import annotations

import argparse
import json
import sys

from _args import add_ticket_id_argument, resolved_ticket_id
from _audit import guarded, record
from _config import ConfigError, enabled, group_ids, is_in_scope
from _freshdesk import FreshdeskClient, FreshdeskError


def _fail(payload: dict) -> None:
    record("check_contact_link.py", result=payload)
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Is this contact already linked, and in scope?")
    add_ticket_id_argument(parser)
    args = parser.parse_args()
    ticket_id = resolved_ticket_id(args, parser)

    notes: list[str] = []
    try:
        client = FreshdeskClient.from_env()
        ticket = client.get_ticket(ticket_id)
        requester_id = ticket.get("requester_id")
        contact = client.get_contact(int(requester_id)) if requester_id else {}
    except (ConfigError, FreshdeskError) as exc:
        _fail({"ok": False, "ticket_id": ticket_id, "error": str(exc)})
        return

    if not enabled():
        notes.append("LINKING_ENABLED is not true; the skill is inert in this deployment.")

    group_id = ticket.get("group_id")
    scope = group_ids()
    in_scope = is_in_scope(group_id)
    if not scope:
        notes.append(
            "LINKING_GROUP_IDS is empty, which means no group is in scope. Nothing will be "
            "linked until it names the groups this skill may act on."
        )
    elif not in_scope:
        notes.append(f"Ticket group {group_id} is not in LINKING_GROUP_IDS ({sorted(scope)}).")

    company_id = contact.get("company_id")
    company_name = None
    if company_id:
        try:
            company_name = (client.get_company(int(company_id)) or {}).get("name")
        except FreshdeskError as exc:
            # The link is what matters; a name we could not read is a note, not a failure.
            notes.append(f"Company {company_id} is linked but its name could not be read: {exc}")

    # The next step, stated rather than left to be remembered. Measured through the duty: on a
    # ticket whose body named no company, the flow stopped here — the agent read the ticket,
    # judged there was nothing to find and never ran the rest, so nothing decided, nothing was
    # tagged, and the ticket came back on every sweep for the rest of its life. "Nothing to
    # find" is a conclusion this flow reaches at the end, in `no_signals`, after a gate has
    # proved it. It is not a reason to stop at the beginning.
    stop = bool(company_id) or not in_scope
    payload = {
        "ok": True,
        "ticket_id": ticket_id,
        "contact_id": contact.get("id") or requester_id,
        "linked": bool(company_id),
        "company_id": company_id,
        "company_name": company_name,
        "in_scope": in_scope,
        "group_id": group_id,
        "stop": stop,
        "next_step": (
            "Report and stop. Nothing is written."
            if stop
            else "Continue with extract_company_signals.py, then read the signature yourself, "
                 "then verify_signals.py, then match_company.py, and finish with "
                 "apply_link.py --signals, passing the matcher's output. That last call is the end of the flow "
                 "whatever the ticket turns out to be: it decides and writes in one step, so "
                 "a ticket that names no company still ends there, as no_signals, and gets "
                 "its tag."
        ),
        "notes": notes,
    }
    record(
        "check_contact_link.py",
        result={"ok": True, "ticket_id": ticket_id, "linked": bool(company_id), "in_scope": in_scope},
    )
    print(
        f"Ticket {ticket_id}: contact {'linked to ' + str(company_name or company_id) if company_id else 'unlinked'}, "
        f"{'in scope' if in_scope else 'out of scope'}.",
        file=sys.stderr,
        flush=True,
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    guarded("check_contact_link.py", main)
