# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python, which may lack this project's dependencies.
# Without one it falls back to sys.executable, i.e. the interpreter running Mossy.
"""The only writer. Links a contact to a company, creating that company when it has to.

Three habits, each of them the answer to a way this goes wrong quietly:

**It re-derives the decision before writing.** The contact's current company is read again, the
signals are verified again against the ticket's raw text, the directory is searched again and
the decision is recomputed. Only if that agrees with the decision it was handed does anything
happen. Whatever assembles that argument upstream can pass a stale, half-filled or hand-edited
object, and by the time it arrives the contact record is one API call away.

**It checks for a duplicate by name before creating.** By domain is not enough: with creation
switched on, every unmatched ticket leaves a permanent record, so "Muster Sanitär AG" arriving
from an unknown domain must not become a second company beside "Muster AG". The whole directory
is compared on the normalised name, not just the domains.

**It reads everything back.** ``linked: true`` means Freshdesk shows the contact attached to
that company, not that a PUT returned 200.

After linking it appends one German line to the contact's log field — the client's wording,
stamped in the reader's own timezone rather than Freshdesk's UTC — and reads it back. It is an
append: whatever was in the field stays, and a contact that already carries such a line does
not collect a second one.

Then one private note on the ticket, in German, naming the company the contact was attached
to — read back to confirm it is there and that it is private.

Output JSON: ok, ticket_id, decision, linked, company_id, company_name, created, logged,
log_field, log_line, noted, note_id, verified, skipped, reason, notes[].
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from _args import add_ticket_id_argument, resolved_ticket_id
from _signals import signals_from
from _audit import guarded, record
from _config import (
    ConfigError,
    all_processed_tags,
    declined_note_enabled,
    outcome_tags,
    company_field_names,
    contact_log_field,
    enabled,
    is_in_scope,
    max_candidates,
    processed_tag,
    scores,
)
from _freshdesk import FreshdeskClient, FreshdeskError
from _text import compare_key, html_to_text, name_key, quoted_split, signature_block
from _time import log_timestamp
from link_decision import DirectorySearchMissing, decide
from match_company import _evaluate
from verify_signals import ReadingLayerMissing, verify

# Decisions that write. Everything else is reported and left alone.
WRITING_DECISIONS = ("link", "create")

# The client's wording, to the character. The bracketed stamp is the date and time in the
# reader's own timezone, not UTC.
LOG_LINE_TEMPLATE = "Durch KI am [{stamp}] mit der Firma verknüpft."
# What makes a line one of ours, whatever timestamp it carries. Used to keep a reprocessed
# contact from collecting the same sentence twice.
LOG_LINE_MARKER = "mit der Firma verknüpft."

# The client's wording for the ticket note, to the character, with the company name in place
# of the placeholder. German always: the reader is Brausewerk service staff, and both texts are
# internal whatever language the customer wrote in.
NOTE_TEMPLATE = (
    "Die KI hat erkannt, dass der Kontakt zur Firma {company} gehört, und den "
    "Ticket-Ersteller entsprechend verknüpft."
)
# The invariant opening of that sentence — everything before the company name. It is what
# identifies a note as ours on a ticket that comes back around.
#
# It has to be the text itself. Freshdesk strips HTML comments out of note bodies, so a hidden
# marker survives the POST and is gone by the time anything reads it back; that once cost a
# ticket two identical notes. The tag on the ticket is the first guard and
# stops the run far earlier — this is the one that still holds when the tag was never written
# or somebody removed it by hand.
NOTE_MARKER = "Die KI hat erkannt, dass der Kontakt zur Firma"

# What each non-writing outcome says in the result line. English: this is the operator's trace,
# not the note. The German the reader sees is `reason`, which travels from the decision.
_OUTCOME_DETAIL = {
    "no_signals": "the ticket names no company; ignored as the client asked",
    "no_match": "a company was named but the directory has no match and none could be created",
    "ambiguous": "several companies fit and none may be chosen",
    "already_linked": "the contact already has a company",
    "out_of_scope": "the ticket is not in a group this deployment acts on",
}

# NOT APPROVED BY THE CLIENT. Kept here so the path is real and testable, and reachable only
# behind LINKING_DECLINED_NOTE_ENABLED, which is off by default. Do not turn it on until the
# wording in references/log-and-note-texts.md comes back signed off: a note on a live ticket
# cannot be recalled, and these sentences tell a customer's file what an automation concluded.
# Each entry is (invariant marker, body); ``{reason}`` is the German reason from the decision.
DECLINED_NOTE_TEXTS = {
    "no_signals": (
        "Keine Firmenangaben im Ticket",
        "<p><b>Keine Firmenangaben im Ticket</b></p><p>{reason}</p>",
    ),
    "no_match": (
        "Keine passende Firma gefunden",
        "<p><b>Keine passende Firma gefunden</b></p><p>{reason}</p>",
    ),
    "ambiguous": (
        "Firma nicht eindeutig",
        "<p><b>Firma nicht eindeutig</b></p><p>{reason}</p>",
    ),
}


def _fail(payload: dict) -> None:
    record("apply_link.py", result=payload)
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(1)


def _load_signals_argument(raw: str | None, path: str | None) -> dict[str, Any]:
    """The verified signals, from a string or a file, in any shape the flow produces."""
    data = json.loads(Path(path).read_text(encoding="utf-8")) if path else json.loads(raw or "")
    return signals_from(data)


def _load(raw: str | None, path: str | None) -> dict[str, Any]:
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    elif raw:
        data = json.loads(raw)
    else:
        raise ValueError("A decision is required: pass --decision '<json>' or --decision-file.")
    if not isinstance(data, dict) or not data.get("decision"):
        raise ValueError("That is not a decision from link_decision.py.")
    return data


def _regions(ticket: dict[str, Any]) -> dict[str, str]:
    body = html_to_text(ticket.get("description")) or (ticket.get("description_text") or "").strip()
    own, quoted = quoted_split(body)
    return {"own_text": own, "signature": signature_block(body), "quoted": quoted}


def _rederive(
    client: FreshdeskClient, ticket: dict[str, Any], contact: dict[str, Any], given: dict[str, Any]
) -> dict[str, Any]:
    """Work the decision out again from the ticket, ignoring what the caller concluded."""
    signals = given.get("signals") if isinstance(given.get("signals"), dict) else {}
    checked = verify({"signals": signals}, _regions(ticket))

    companies, _notes = client.list_companies()
    weights = scores()
    candidates = [
        candidate
        for candidate in (_evaluate(company, checked["verified"], weights) for company in companies)
        if candidate
    ]
    candidates.sort(key=lambda c: (-c["score"], str(c["company_name"] or "")))
    candidates = candidates[: max_candidates()]

    return decide(
        {
            "verified": checked["verified"],
            "candidates": candidates,
            "searched": [f"directory listing ({len(companies)} companies)"],
            "linked": bool(contact.get("company_id")),
            "company_id": contact.get("company_id"),
            "in_scope": is_in_scope(ticket.get("group_id")),
        }
    )


def _agrees(given: dict[str, Any], fresh: dict[str, Any]) -> tuple[bool, str]:
    """Do the two decisions say the same thing about the same company?"""
    if given.get("decision") != fresh.get("decision"):
        return False, (
            f"the decision no longer holds: it said {given.get('decision')!r} and the ticket now "
            f"says {fresh.get('decision')!r}"
        )
    if fresh["decision"] == "link" and str(given.get("company_id")) != str(fresh.get("company_id")):
        return False, (
            f"it names company {given.get('company_id')} and the ticket now matches "
            f"{fresh.get('company_id')}"
        )
    if fresh["decision"] == "create":
        given_name = ((given.get("create") or {}).get("name")) or given.get("company_name")
        fresh_name = ((fresh.get("create") or {}).get("name")) or fresh.get("company_name")
        if name_key(given_name) != name_key(fresh_name):
            return False, f"it would create {given_name!r} and the ticket now says {fresh_name!r}"
    return True, ""


def _append_contact_log(
    client: FreshdeskClient, contact: dict[str, Any], notes: list[str]
) -> dict[str, Any]:
    """Append the German line to the contact's log field, and read it back.

    An append, never a write-over: whatever the field already holds is kept and the new line
    goes underneath. And once only — a contact that already carries one of our lines is left
    alone, so reprocessing a ticket cannot stack the same sentence.
    """
    try:
        field = contact_log_field()
    except ConfigError as exc:
        # The link is the job; the log is the record of it. Not being able to write the record
        # must not be reported as if the link had failed, nor hidden.
        notes.append(f"The contact log line was not written: {exc}")
        return {"logged": False, "reason": "no_log_field_configured"}

    existing = (contact.get("custom_fields") or {}).get(field)
    existing_text = str(existing).strip() if existing else ""
    if LOG_LINE_MARKER in existing_text:
        notes.append(
            "The contact already carries a linking line; a second one was not appended."
        )
        return {"logged": False, "reason": "already_logged", "field": field}

    stamp, stamp_note = log_timestamp()
    if stamp_note:
        notes.append(stamp_note)
    line = LOG_LINE_TEMPLATE.format(stamp=stamp)
    updated = f"{existing_text}\n{line}".strip() if existing_text else line
    client.update_contact_custom_field(int(contact["id"]), field, updated)

    # Read-back: the field shows the line, or it did not happen.
    live = client.get_contact(int(contact["id"]))
    live_text = str((live.get("custom_fields") or {}).get(field) or "")
    if line not in live_text:
        return {"logged": False, "reason": "not_confirmed", "field": field, "line": line}
    if existing_text and existing_text not in live_text:
        # Should be impossible, and worth catching if it ever is: the append ate what was there.
        return {"logged": False, "reason": "previous_content_lost", "field": field}
    return {"logged": True, "field": field, "line": line}


def _post_private_note(
    client: FreshdeskClient, ticket_id: int, company_name: str, notes: list[str]
) -> dict[str, Any]:
    """The note for a ticket whose contact was linked."""
    return _post_private_note_body(
        client, ticket_id, NOTE_TEMPLATE.format(company=company_name), NOTE_MARKER, notes
    )


def _post_private_note_body(
    client: FreshdeskClient, ticket_id: int, body: str, marker_text: str, notes: list[str]
) -> dict[str, Any]:
    """Put a German note on the ticket, once, privately — and confirm it is there.

    Private is not a detail: the note says an automation read the sender's signature and
    reached a conclusion about them. It is written for the service desk, and the sender is the
    one person who must not receive it.

    ``marker_text`` is the invariant opening of this particular note. Each kind carries its own,
    so a ticket that was declined and later linked collects both, and neither blocks the other.
    It can never be a hidden HTML comment: Freshdesk strips comments out of note bodies.
    """
    marker = compare_key(marker_text)

    def ours(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
        for entry in entries:
            if marker and marker in compare_key(html_to_text(entry.get("body") or "")):
                return entry
        return None

    existing = ours(client.list_ticket_notes(ticket_id))
    if existing is not None:
        notes.append("The ticket already carries this note; a second one was not added.")
        return {"posted": False, "reason": "already_noted", "note_id": existing.get("id")}

    client.add_private_note(ticket_id, body if body.lstrip().startswith("<") else f"<p>{body}</p>")

    # Read-back: the note is on the ticket and it is private, or this did not happen. A note
    # that posted as public would be a message to the customer, which is worse than none.
    posted = ours(client.list_ticket_notes(ticket_id))
    if posted is None:
        return {"posted": False, "reason": "not_confirmed", "body": body}
    if not posted.get("private"):
        return {"posted": False, "reason": "posted_publicly", "note_id": posted.get("id")}
    return {"posted": True, "note_id": posted.get("id"), "body": body}


def _close_without_writing(
    client: FreshdeskClient,
    ticket_id: int,
    fresh: dict[str, Any],
    notes: list[str],
    dry_run: bool,
) -> dict[str, Any]:
    """Finish a ticket the skill will not act on: tag it, and say so out loud.

    Three decisions end here. ``no_signals`` means the ticket names no company at all and the
    client's instruction is to ignore it. ``no_match`` means it does name one and the directory
    has no answer. ``ambiguous`` means it names one too well — several companies fit and none
    may be chosen. None of them writes to a contact.

    Every one of them leaves a tag, so the duty stops re-evaluating the ticket on every pass,
    and every one of them returns an explicit outcome. A run that decided to do nothing and a
    run that never happened must not look the same from outside: the first is a finding about
    the ticket and the second is a gap in the coverage.
    """
    decision = fresh["decision"]
    tag = outcome_tags().get(decision, "")
    outcome: dict[str, Any] = {
        "outcome": decision,
        "outcome_detail": _OUTCOME_DETAIL.get(decision, "kein Schreibvorgang"),
        "tagged": False,
        "tag": tag or None,
        "noted": False,
    }
    if dry_run:
        notes.append("Dry run: the ticket was not tagged and no note was posted.")
        return outcome

    if not tag:
        # Only reachable if someone empties the variable. Say it rather than looping forever.
        notes.append(
            f"No tag is configured for the outcome {decision!r}; the ticket will be evaluated "
            "again on the next pass."
        )
    else:
        client.add_ticket_tags(ticket_id, [tag])
        # Read-back: Freshdesk decides whether the tag is on the ticket, not the status code.
        if tag in [str(t) for t in (client.get_ticket(ticket_id).get("tags") or [])]:
            outcome["tagged"] = True
        else:
            notes.append(
                f"The tag {tag!r} was not confirmed on the ticket; it will be evaluated again."
            )

    if not declined_note_enabled():
        notes.append(
            "No note was posted: the wording for this outcome is not approved by the client "
            "yet (LINKING_DECLINED_NOTE_ENABLED is off)."
        )
        return outcome

    entry = DECLINED_NOTE_TEXTS.get(decision)
    if not entry:
        notes.append(f"No note text exists for the outcome {decision!r}; none was posted.")
        return outcome
    marker_text, template = entry
    note = _post_private_note_body(
        client, ticket_id, template.format(reason=fresh.get("reason", "")), marker_text, notes
    )
    outcome["noted"] = bool(note.get("posted"))
    outcome["note_id"] = note.get("note_id")
    return outcome


def _duplicate(companies: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    """A company that is already this one under another spelling.

    Compared on the normalised name — legal form and punctuation removed — because that is how
    a directory collects "Muster AG", "Muster Sanitär AG" and "Muster Sanitaer AG" as three
    records that nobody ever cleans up.
    """
    key = name_key(name)
    if not key:
        return None
    for company in companies:
        if name_key(company.get("name")) == key:
            return company
    return None


def _company_payload(create: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Build the create body from configured field names only."""
    fields = company_field_names()
    payload: dict[str, Any] = {fields["name"] or "name": create["name"]}
    notes: list[str] = []
    if create.get("domain"):
        # The domain is what makes the company findable next time without reading anything.
        payload["domains"] = [create["domain"]]
    for key in ("address", "phone", "email"):
        value = create.get(key)
        if not value:
            continue
        field = fields.get(key)
        if not field:
            notes.append(
                f"The ticket carries a company {key} but no field is configured for it "
                f"(LINKING_COMPANY_FIELD_{key.upper()}); it was not written."
            )
            continue
        payload.setdefault("custom_fields", {})[field] = value
    return payload, notes


def main() -> None:
    parser = argparse.ArgumentParser(description="Link a contact to its company.")
    add_ticket_id_argument(parser)
    source = parser.add_mutually_exclusive_group()
    # The ordinary call: hand over the verified signals and let this script do the rest.
    source.add_argument("--signals", "--signals_json", dest="signals")
    source.add_argument("--signals-file", "--signals_file", dest="signals_file")
    # Kept for a caller that already holds a decision — link_decision.py is still a script and
    # still testable on its own. It is not a second opinion this script defers to: everything
    # below is recomputed either way, and a decision that disagrees is refused.
    source.add_argument("--decision", dest="decision")
    source.add_argument("--decision-file", "--decision_file", dest="decision_file")
    parser.add_argument("--dry-run", "--dry_run", dest="dry_run", action="store_true")
    args = parser.parse_args()
    ticket_id = resolved_ticket_id(args, parser)

    # Signals or a decision; either way this script decides for itself. Passing signals is the
    # flow's one call, and it exists because a separate "decide" step was a place to stop: an
    # agent that had reached a decision writing nothing saw no reason to call the writer, so
    # the ticket was never tagged and came back on every sweep. There is no deciding without
    # writing now, because there is nothing to decide with.
    from_signals = bool(args.signals or args.signals_file)
    try:
        if from_signals:
            given = {"decision": None, "signals": _load_signals_argument(
                args.signals, args.signals_file)}
        else:
            given = _load(args.decision, args.decision_file)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        _fail({"ok": False, "ticket_id": ticket_id, "error": str(exc)})
        return

    notes: list[str] = []
    if not enabled():
        # The master switch guards the writer too: a deployment that has not turned the skill
        # on must not have its contacts edited because someone ran a script by hand.
        _fail({
            "ok": False, "ticket_id": ticket_id, "linked": False, "created": False,
            "error": "LINKING_ENABLED is not true; refusing to write in a deployment where "
                     "this skill is inert.",
        })
        return

    # Hoisted out of the try on purpose. If the run dies after a company was created, the
    # failure has to be able to say so: the record is already in the CRM and whoever reads this
    # result is the only one who can go and look at it.
    created = False
    created_company: dict[str, Any] | None = None

    try:
        client = FreshdeskClient.from_env()
        ticket = client.get_ticket(ticket_id)
        if ticket.get("deleted") or ticket.get("spam"):
            _fail({
                "ok": False, "ticket_id": ticket_id,
                "error": f"Ticket {ticket_id} is deleted or spam in Freshdesk — refusing to write.",
            })
            return

        tag = processed_tag()
        live_tags_now = {str(t) for t in (ticket.get("tags") or [])}
        if live_tags_now & all_processed_tags():
            # Idempotency: this ticket has been through the skill already.
            result = {
                "ok": True, "ticket_id": ticket_id,
                "decision": given.get("decision") or "already_processed",
                "linked": False, "created": False, "skipped": True,
                "reason": "already_processed",
                "processed_tags": sorted(live_tags_now & all_processed_tags()),
                "notes": notes,
            }
            record("apply_link.py", result=result)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return

        requester_id = ticket.get("requester_id")
        contact = client.get_contact(int(requester_id)) if requester_id else {}
        fresh = _rederive(client, ticket, contact, given)
        agrees, why = (True, "") if from_signals else _agrees(given, fresh)
        if not agrees:
            _fail({
                "ok": False, "ticket_id": ticket_id, "decision": given["decision"],
                "rederived": fresh["decision"], "linked": False, "created": False,
                "error": f"Refusing to write — {why}.",
            })
            return

        # The decision keeps its own notes — values it had to drop, candidates that came
        # close. They are about the ticket, so they belong in the result rather than dying
        # with the object that carried them.
        for note in fresh.get("notes") or []:
            if note not in notes:
                notes.append(note)

        if fresh["decision"] not in WRITING_DECISIONS:
            outcome = _close_without_writing(client, ticket_id, fresh, notes, args.dry_run)
            result = {
                "ok": True, "ticket_id": ticket_id, "decision": fresh["decision"],
                "reason": fresh["reason"], "linked": False, "created": False,
                "skipped": True, **outcome,
                # The decision is no longer a step of its own, so this is the only record of
                # what was decided and on what. A review note that cannot say why is not worth
                # reading, and a trace that stops at "nothing happened" hides whether anything
                # was looked at.
                "evidence": fresh.get("evidence"),
                "candidates": fresh.get("candidates"),
                "threshold": fresh.get("threshold"),
                "notes": notes,
            }
            record("apply_link.py", result={**result, "reason": fresh["decision"]})
            print(
                f"Ticket {ticket_id}: {fresh['decision']} — {outcome.get('outcome_detail')}.",
                file=sys.stderr, flush=True,
            )
            print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
            return

        if args.dry_run:
            print(json.dumps({**fresh, "dry_run": True}, indent=2, ensure_ascii=False, default=str))
            return

        if fresh["decision"] == "create":
            companies, listing_notes = client.list_companies()
            notes.extend(listing_notes)
            create = fresh["create"]
            duplicate = _duplicate(companies, create["name"])
            if duplicate:
                # Creation was the plan, but the directory already holds this company under a
                # different spelling. Linking to it is right; a second record would not be.
                notes.append(
                    f"A company named {duplicate.get('name')!r} already matches "
                    f"{create['name']!r} once the legal form and punctuation are removed; it "
                    "was linked instead of creating a duplicate."
                )
                company = duplicate
            else:
                payload, field_notes = _company_payload(create)
                notes.extend(field_notes)
                company = client.create_company(payload)
                created = True
                created_company = company
        else:
            company = {"id": fresh["company_id"], "name": fresh.get("company_name")}

        client.link_contact_to_company(int(contact["id"]), int(company["id"]))
        # The log line goes on after the link, and reads the contact fresh so it appends to
        # whatever is in the field now rather than to the copy this run started with.
        log = _append_contact_log(client, client.get_contact(int(contact["id"])), notes)
        # The note names the company as Freshdesk now holds it, not as the ticket spelled it:
        # the reader should see the record they will find in the directory.
        note = _post_private_note(
            client, ticket_id, str(company.get("name") or fresh.get("company_name") or ""), notes
        )
        client.add_ticket_tags(ticket_id, [tag])

        # Read-back: Freshdesk decides whether this happened, not the status codes above.
        live_contact = client.get_contact(int(contact["id"]))
        live_company = client.get_company(int(company["id"]))
        live_tags = [str(t) for t in (client.get_ticket(ticket_id).get("tags") or [])]
        linked = str(live_contact.get("company_id")) == str(company["id"])
        missing = []
        if not linked:
            missing.append("the contact is not attached to that company")
        if log.get("reason") in ("not_confirmed", "previous_content_lost"):
            missing.append(f"the contact log line was not confirmed ({log['reason']})")
        if note.get("reason") == "not_confirmed":
            missing.append("the private note is not on the ticket")
        if note.get("reason") == "posted_publicly":
            # Loud on purpose: the customer received a message about their own record.
            missing.append("the note posted publicly instead of privately")
        if tag not in live_tags:
            missing.append(f"the tag {tag!r} is not on the ticket")
    except (ReadingLayerMissing, DirectorySearchMissing) as exc:
        # The decision was built from signals nobody read. Re-deriving it would reach the same
        # refusal, and writing on it would attach a contact to a company that was never named.
        _fail({"ok": False, "ticket_id": ticket_id, "linked": False, "created": created,
               "error": str(exc),
               "missing_step": "reading_layer" if isinstance(exc, ReadingLayerMissing)
               else "directory_search"})
        return
    except (ConfigError, FreshdeskError, KeyError, TypeError) as exc:
        # A failure between creating the company and linking the contact leaves the company
        # standing. Nothing rolls it back: Freshdesk has no transaction, and deleting it blind
        # would be worse, because somebody may already be looking at it. What must not happen
        # is reporting `created: false` while a new record sits in the CRM — that is the read-
        # back invariant read backwards, and it hides the one consequence a person has to know
        # about. The ticket carries no tag, so the next pass finds this company and links to it
        # rather than creating a second.
        failure: dict[str, Any] = {
            "ok": False, "ticket_id": ticket_id, "linked": False, "created": created,
            "error": str(exc), "notes": notes,
        }
        if created_company:
            failure["company_id"] = created_company.get("id")
            failure["company_name"] = created_company.get("name")
            failure["notes"] = [
                *notes,
                f"The company {created_company.get('name')!r} "
                f"(id {created_company.get('id')}) was created before this failure and still "
                "exists; the contact was not attached to it. It was left in place rather than "
                "deleted. The ticket carries no processed tag, so the next run will find this "
                "company and link to it instead of creating another.",
            ]
        _fail(failure)
        return

    if missing:
        _fail({
            "ok": False, "ticket_id": ticket_id, "decision": fresh["decision"],
            "linked": False, "created": created,
            "error": "Write not confirmed on read-back: " + "; ".join(missing) + ".",
        })
        return

    result = {
        "ok": True,
        "ticket_id": ticket_id,
        "decision": fresh["decision"],
        "reason": fresh["reason"],
        "evidence": fresh.get("evidence"),
        "candidates": fresh.get("candidates"),
        "threshold": fresh.get("threshold"),
        "linked": True,
        "created": created,
        "company_id": live_company.get("id"),
        "company_name": live_company.get("name"),
        "company_domains": live_company.get("domains"),
        "contact_id": live_contact.get("id"),
        "logged": bool(log.get("logged")),
        "log_field": log.get("field"),
        "log_line": log.get("line"),
        "noted": bool(note.get("posted")),
        "note_id": note.get("note_id"),
        "verified": True,
        "skipped": False,
        "notes": notes,
    }
    record("apply_link.py", result={**result, "reason": fresh["decision"]})
    print(
        f"Ticket {ticket_id}: contact {live_contact.get('id')} linked to "
        f"{live_company.get('name')!r}{' (created)' if created else ''}, confirmed on read-back.",
        file=sys.stderr,
        flush=True,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    guarded("apply_link.py", main)
