# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python, which may lack this project's dependencies.
# Without one it falls back to sys.executable, i.e. the interpreter running Mossy.
"""Read what the linking flow needs from one ticket. Read-only: nothing is written.

Returns the subject, the body as plain text, the sender's own signature block, the requester
contact with the company it is currently linked to (if any), and the ticket's group.

The signature is the sender's, not the last one in the text: a forwarded chain carries other
people's signatures, and a company name lifted from one of those belongs to somebody else.
``_text.quoted_split`` cuts the quoted remainder off before the block is looked for, and the
quoted text is returned separately so a later step can prove where a value came from.

Output JSON: ok, ticket_id, intake{...}, requester{...}, notes[].
"""

from __future__ import annotations

import argparse
import json
import sys

from _args import add_ticket_id_argument, resolved_ticket_id
from _audit import guarded, record
from _config import ConfigError
from _freshdesk import FreshdeskClient, FreshdeskError
from _text import email_domain, html_to_text, quoted_split, signature_block


def _fail(payload: dict) -> None:
    record("get_ticket_intake.py", result=payload)
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Read a ticket for company linking.")
    add_ticket_id_argument(parser)
    args = parser.parse_args()
    ticket_id = resolved_ticket_id(args, parser)

    notes: list[str] = []
    try:
        client = FreshdeskClient.from_env()
        ticket = client.get_ticket(ticket_id)
        contact: dict = {}
        requester_id = ticket.get("requester_id")
        if requester_id:
            contact = client.get_contact(int(requester_id))
        else:
            notes.append("The ticket carries no requester id; the contact could not be read.")
    except (ConfigError, FreshdeskError) as exc:
        _fail({"ok": False, "ticket_id": ticket_id, "error": str(exc)})
        return

    if ticket.get("deleted"):
        notes.append("Ticket is deleted in Freshdesk.")
    if ticket.get("spam"):
        notes.append("Ticket is marked as spam in Freshdesk.")

    # description_text is Freshdesk's own plain text; fall back to stripping the HTML when the
    # account does not populate it.
    body_text = (ticket.get("description_text") or "").strip()
    if not body_text:
        body_text = html_to_text(ticket.get("description"))
        if body_text:
            notes.append("Freshdesk returned no plain-text body; the HTML was converted.")

    own_text, quoted_text = quoted_split(body_text)
    signature = signature_block(body_text)
    if not signature:
        notes.append("No signature block was found; that is not an error.")
    if quoted_text:
        notes.append("The message contains a quoted or forwarded part; it is reported separately.")

    company_id = contact.get("company_id")
    email = contact.get("email")
    payload = {
        "ok": True,
        "ticket_id": ticket_id,
        "intake": {
            "subject": ticket.get("subject"),
            "body_text": body_text,
            "own_text": own_text,
            "quoted_text": quoted_text,
            "signature_block": signature,
            "created_at": ticket.get("created_at"),
            "group_id": ticket.get("group_id"),
            "tags": [str(t) for t in (ticket.get("tags") or []) if t],
        },
        "requester": {
            "contact_id": contact.get("id") or requester_id,
            "name": contact.get("name"),
            "email": email,
            "email_domain": email_domain(email),
            "company_id": company_id,
            "linked": bool(company_id),
        },
        "notes": notes,
    }
    record("get_ticket_intake.py", result={"ok": True, "ticket_id": ticket_id})
    print(
        f"Ticket {ticket_id}: signature {'found' if signature else 'not found'}, "
        f"contact {'linked' if company_id else 'unlinked'}.",
        file=sys.stderr,
        flush=True,
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    guarded("get_ticket_intake.py", main)
