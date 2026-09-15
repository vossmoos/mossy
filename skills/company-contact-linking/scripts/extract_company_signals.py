# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python, which may lack this project's dependencies.
# Without one it falls back to sys.executable, i.e. the interpreter running Mossy.
"""The mechanical half of signal detection. Deterministic, no model, nothing written.

What a script can find without judgement: the sender's email domain, URLs, a Swiss UID or a
German VAT number, and phone numbers with a country hint. The other half — the company name in
a signature, a legal form, a department line — needs a reader and is added afterwards, guided
by references/detection-rules.md.

**This script verifies nothing.** Every value it emits, and every value the reader adds, goes
through ``verify_signals.py`` before it may reach a search. Nothing here is proof that a value
belongs to this requester; it is only proof that the extractor saw it.

With one exception, and it is why each value carries its provenance: the sender's email domain
comes from the **Freshdesk contact record**, not from the ticket text. It cannot be found in the
body, so a check that looked for it there would discard the strongest signal the skill has.
Values like that are marked ``"verification": "not_required"`` — verified by construction,
because they were never read from prose in the first place.

Region matters as much as presence. Text is split into the sender's own message, the signature
block, and any quoted or forwarded remainder; every value records which of those it came from.
A URL sitting only in a quoted block is still reported — it exists — but the gate will drop it,
because it belongs to whoever wrote that message.

Output JSON: ok, ticket_id, signals{...}, provenance{...}, regions{...}, notes[].
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from _args import add_ticket_id_argument, resolved_ticket_id
from _audit import guarded, record
from _config import ConfigError, generic_email_domains
from _freshdesk import FreshdeskClient, FreshdeskError
from _text import email_domain, html_to_text, quoted_split, signature_block, url_host

# A Swiss company identifier: CHE-123.456.789, with or without the dots, and the MWST/TVA/IVA
# suffix the register prints. German VAT ids are accepted too — the wholesalers are not all Swiss.
_UID_RE = re.compile(
    r"\b(CHE[-\s]?\d{3}[.\s]?\d{3}[.\s]?\d{3}(?:\s?(?:MWST|TVA|IVA))?|DE\s?\d{9})\b",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"\bhttps?://[^\s<>\"')\]]+|\bwww\.[^\s<>\"')\]]+", re.IGNORECASE)
# Phone numbers as they are actually written on Swiss and German letterheads: an international
# prefix, or a national number in groups. Deliberately loose — this layer proposes, the reader
# and the gate discard.
_PHONE_RE = re.compile(
    r"(?:(?:\+|00)\s?\d{1,3}[\s./-]?)?(?:\(0\)\s?)?\d{2,4}(?:[\s./-]\d{2,4}){2,4}"
)
_COUNTRY_PREFIXES = {"41": "CH", "49": "DE", "43": "AT", "33": "FR", "39": "IT", "34": "ES"}
# Digit runs that are dates, order numbers or postcodes rather than phone numbers.
_MIN_PHONE_DIGITS = 9
_MAX_PHONE_DIGITS = 15


def _clean_uid(value: str) -> str:
    """The identifier without the tax-register suffix.

    The register prints ``CHE-123.456.789 MWST``; the identifier is the number. Keeping the
    suffix would make the value compare unequal to the same UID stored on a company record.
    """
    return re.sub(r"\s*(MWST|TVA|IVA)\s*$", "", value.strip(), flags=re.IGNORECASE)


def _fail(payload: dict) -> None:
    record("extract_company_signals.py", result=payload)
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(1)


def _phone_country(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if raw.strip().startswith("+") or digits.startswith("00"):
        digits = digits[2:] if digits.startswith("00") else digits
        for length in (3, 2, 1):
            if _COUNTRY_PREFIXES.get(digits[:length]):
                return _COUNTRY_PREFIXES[digits[:length]]
    return None


def _find(pattern: re.Pattern[str], regions: dict[str, str]) -> list[tuple[str, str]]:
    """Every match with the region it came from, in region order, without duplicates."""
    seen: set[str] = set()
    found: list[tuple[str, str]] = []
    for region in ("signature", "own_text", "quoted"):
        for match in pattern.finditer(regions.get(region) or ""):
            value = match.group(0).strip().rstrip(".,;)")
            key = value.lower()
            if key and key not in seen:
                seen.add(key)
                found.append((value, region))
    return found


def _phones(
    regions: dict[str, str], uid_digits: set[str]
) -> list[tuple[str, str, str | None]]:
    """Phone-shaped strings, minus the ones that are really a UID.

    A Swiss UID reads as ``123.456.789`` once the CHE prefix is off, which is exactly the shape
    of a grouped phone number — measured on the first real ticket, where the company's own UID
    came back as a fourth phone. A candidate whose digits sit inside a UID is dropped.
    """
    phones: list[tuple[str, str, str | None]] = []
    seen: set[str] = set()
    for value, region in _find(_PHONE_RE, regions):
        digits = re.sub(r"\D", "", value)
        if not (_MIN_PHONE_DIGITS <= len(digits) <= _MAX_PHONE_DIGITS):
            continue
        if digits in seen or any(digits in uid for uid in uid_digits):
            continue
        seen.add(digits)
        phones.append((value, region, _phone_country(value)))
    return phones


def extract(ticket: dict, contact: dict) -> dict:
    """Build the signals and their provenance. Judgement-free by design."""
    notes: list[str] = []

    # The HTML is the source of truth for structure. Freshdesk's own ``description_text`` comes
    # back with every newline flattened to a space for tickets written in its UI — measured on
    # two of them — and without line breaks there is no signature block and no quoted part, so
    # another company's details end up attributed to the sender. The HTML still carries the
    # <br> tags that say where the lines were.
    html_body = ticket.get("description")
    body_text = html_to_text(html_body)
    plain = (ticket.get("description_text") or "").strip()
    if not body_text:
        body_text = plain
        if body_text:
            notes.append("The ticket has no HTML body; Freshdesk's plain text was used as is.")
    elif plain and "\n" not in plain:
        notes.append(
            "Freshdesk's plain text arrived without line breaks; the HTML was used to recover "
            "the line structure."
        )

    own_text, quoted_text = quoted_split(body_text)
    signature = signature_block(body_text)
    regions = {"own_text": own_text, "signature": signature, "quoted": quoted_text}

    address = contact.get("email")
    domain = email_domain(address)
    generic = domain in generic_email_domains() if domain else False
    if domain and generic:
        notes.append(f"The sender writes from {domain}, which says nothing about an employer.")
    if not domain:
        notes.append("The contact has no email address; there is no sender domain to use.")

    urls = _find(_URL_RE, regions)
    uids = [(_clean_uid(value), region) for value, region in _find(_UID_RE, regions)]
    phones = _phones(regions, {re.sub(r"\D", "", value) for value, _region in uids})
    if quoted_text:
        notes.append(
            "The message carries a quoted or forwarded part; values found only there are "
            "reported with region 'quoted' and belong to whoever wrote that message."
        )
    if not signature:
        notes.append("No signature block was found; that is not an error.")

    signals = {
        # --- mechanical, this script ------------------------------------------------
        "emailDomain": domain or None,
        "emailDomainIsGeneric": generic,
        # Hosts, not links: a company's directory entry stores "muster.example.ch", so that is the only
        # shape a URL can be compared in.
        "urls": [host for host in (url_host(value) for value, _region in urls) if host],
        "uid": uids[0][0] if uids else None,
        "phones": [value for value, _region, _country in phones],
        # --- read, filled in by the reader against detection-rules.md ---------------
        # Placeholders for the reading layer, which is a person or an agent reading the
        # signature — this script reads no prose. The flag below is the declaration that the
        # step happened, and it is false here because it has not.
        "readSignalsAttempted": False,
        "companyNameCandidates": [],
        "legalForm": None,
        "departmentLine": None,
        "addresses": [],
        "companyEmails": [],
        "readSignals": [],
        "counterSignals": [],
        "confidence": None,
        "reason": None,
    }

    def entry(source: str, *, verification: str = "required", **extra: object) -> dict:
        return {"origin": "mechanical", "source": source, "verification": verification, **extra}

    provenance: dict[str, object] = {
        "emailDomain": entry(
            # Freshdesk's own record of the contact, not something read out of prose: a check
            # against the ticket body would throw away the strongest signal there is.
            "freshdesk_contact",
            verification="not_required",
            detail="the contact's email address as Freshdesk stores it",
        ),
        "emailDomainIsGeneric": entry(
            "config", verification="not_required", detail="LINKING_GENERIC_EMAIL_DOMAINS"
        ),
        "urls": [
            entry(region, printed=value)
            for value, region in urls
            if url_host(value)
        ],
        "uid": entry(uids[0][1]) if uids else None,
        "phones": [
            entry(region, country=country) for _value, region, country in phones
        ],
        # The reader fills these; it records its own provenance as it does.
        "readSignalsAttempted": False,
        "companyNameCandidates": [],
        "legalForm": None,
        "departmentLine": None,
    }
    if len(uids) > 1:
        notes.append(
            f"{len(uids)} UID/VAT numbers were found; the first is reported and the rest are "
            "in the notes: " + ", ".join(v for v, _r in uids[1:])
        )

    return {
        "ok": True,
        "ticket_id": ticket.get("id"),
        "signals": signals,
        "provenance": provenance,
        "regions": regions,
        "notes": notes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract the mechanical company signals from a ticket."
    )
    add_ticket_id_argument(parser)
    args = parser.parse_args()
    ticket_id = resolved_ticket_id(args, parser)

    try:
        client = FreshdeskClient.from_env()
        ticket = client.get_ticket(ticket_id)
        requester_id = ticket.get("requester_id")
        contact = client.get_contact(int(requester_id)) if requester_id else {}
    except (ConfigError, FreshdeskError) as exc:
        _fail({"ok": False, "ticket_id": ticket_id, "error": str(exc)})
        return

    result = extract(ticket, contact)
    result["ticket_id"] = ticket_id
    if not contact:
        result["notes"].append("The ticket has no requester contact; no sender domain was read.")

    record("extract_company_signals.py", result={"ok": True, "ticket_id": ticket_id})
    signals = result["signals"]
    print(
        f"Ticket {ticket_id}: domain {signals['emailDomain'] or '—'}"
        f"{' (generic)' if signals['emailDomainIsGeneric'] else ''}, "
        f"{len(signals['urls'])} URL(s), {len(signals['phones'])} phone(s), "
        f"UID {signals['uid'] or '—'}.",
        file=sys.stderr,
        flush=True,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    guarded("extract_company_signals.py", main)
