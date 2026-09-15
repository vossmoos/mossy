# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python, which may lack this project's dependencies.
# Without one it falls back to sys.executable, i.e. the interpreter running Mossy.
"""The gate between reading a ticket and searching the directory. Deterministic, read-only.

A value survives two questions, and both halves matter:

1. **Is it in the ticket at all?** Compared on a key of letters and digits only, so a signature
   typed as ``Muster Sanitär AG.`` still matches ``muster sanitaer ag``. A value that is not
   there was not read, it was produced: ``not_in_text``.
2. **Is it in a region that belongs to this sender?** A value found only in the quoted or
   forwarded part was written by somebody else — the firm that mailed *them*, a supplier, an
   installer — and linking a contact to it is a wrong link that looks right afterwards:
   ``wrong_region``.

Presence alone is not enough, which is the whole reason for the second half. Measured on two
tickets written in Freshdesk's UI, the quoted company's phone and website sat in the same body
as the sender's own and would have passed a check that only asked "is this string present".

**What this gate does not do.** It never judges whether a company is the right one; that is
`link_decision.py` reading `references/matching-rules.md`. It only removes values that cannot
serve as evidence at all.

Values whose provenance says ``verification: "not_required"`` skip the text check. The sender's
email domain comes from the Freshdesk contact record rather than from prose, so looking for it
in the body would discard the strongest signal the skill has.

``confidence``, ``counterSignals``, ``readSignals`` and ``reason`` are the reader's judgement,
not claims about the text, so there is nothing to verify. They are passed through under
``unverified`` — kept for the note and the trace, and out of reach of anything that treats
``verified`` as evidence.

Output JSON: ok, ticket_id, verified{...}, discarded[], unverified{...}, signals{...}, notes[].
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from _args import add_ticket_id_argument, resolved_ticket_id
from _audit import guarded, record
from _config import ConfigError
from _freshdesk import FreshdeskClient, FreshdeskError
from _text import compare_key, html_to_text, quoted_split, signature_block

# The reader's judgement. Not claims about the text, so not verifiable against it.
JUDGEMENT_FIELDS = ("confidence", "counterSignals", "readSignals", "reason")
# Booleans and structures that carry no text to look for.
# Carried through untouched. These are not claims about the ticket's text, so looking for them
# in it would discard them every time: one is a fact about configuration, the other is the
# reading layer's declaration that it ran.
PASSTHROUGH_FIELDS = ("emailDomainIsGeneric", "readSignalsAttempted")
# Fields that are never claims about the ticket text, whatever provenance arrives with them.
# The sender's domain is the Freshdesk contact record; checking it against the body discards
# the strongest signal the skill has whenever the message does not happen to mention it.
# Relying on the caller to say so is not enough: a flat signals object with no provenance is a
# perfectly normal way to call this script, and the exemption must not depend on it.
NEVER_FROM_TEXT_FIELDS = ("emailDomain", "emailDomainIsGeneric")

_DETAIL_DE = {
    "not_in_text": "kommt im Ticket nicht vor",
    "wrong_region": "nur im zitierten Teil der Nachricht gefunden",
}


def _fail(payload: dict) -> None:
    record("verify_signals.py", result=payload)
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(1)


def _load(raw: str | None, path: str | None) -> dict[str, Any]:
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    elif raw:
        data = json.loads(raw)
    else:
        raise ValueError("Signals are required: pass --signals '<json>' or --signals-file <path>.")
    if not isinstance(data, dict):
        raise ValueError("The signals must be a JSON object.")
    return data


def _regions_from_ticket(ticket: dict[str, Any]) -> dict[str, str]:
    """Rebuild the regions from the ticket, the same way the extractor does.

    The HTML is used rather than ``description_text``: Freshdesk flattens every newline in that
    field for tickets written in its own UI, and without line breaks there is no signature and
    no quoted part to tell apart.
    """
    body = html_to_text(ticket.get("description")) or (ticket.get("description_text") or "").strip()
    own, quoted = quoted_split(body)
    return {"own_text": own, "signature": signature_block(body), "quoted": quoted}


def _provenance_for(provenance: dict[str, Any], field: str, index: int | None) -> dict[str, Any]:
    entry = provenance.get(field)
    if isinstance(entry, list):
        if index is not None and index < len(entry) and isinstance(entry[index], dict):
            return entry[index]
        return {}
    return entry if isinstance(entry, dict) else {}


class ReadingLayerMissing(ValueError):
    """The signature was never read, so there is nothing here worth verifying."""


def _reading_happened(signals: dict[str, Any]) -> None:
    """Refuse a payload the reading layer never touched.

    ``extract_company_signals.py`` reads no prose. It finds a domain, a UID, phone numbers and
    URLs by pattern, and leaves every company **name** to a reader — a person or an agent —
    working from the signature. Measured end to end through the duty: the reader was skipped on
    every ticket, its placeholders travelled onward empty, and the result was a run that could
    never link by name and could never create a company. Nothing failed; everything came back
    ``no_match``, which is a claim about the directory and was a lie about the ticket.

    The check cannot be "are there any names", because **a ticket with no signature is
    legitimate** and must reach ``no_signals`` honestly. So the reader declares that it looked:
    ``readSignalsAttempted: true``. An empty result beside that flag is a finding; an empty
    result without it is a step that did not happen, and closing one silence by opening another
    would be no improvement.
    """
    if signals.get("readSignalsAttempted") is True:
        return
    raise ReadingLayerMissing(
        "The reading layer has not run: 'readSignalsAttempted' is not true. "
        "extract_company_signals.py finds only the mechanical signals — domain, UID, phones, "
        "URLs — and leaves every company name to you. Read the signature in the 'regions' "
        "block, add companyNameCandidates, addresses, companyEmails, legalForm, readSignals, "
        "counterSignals, confidence and a German reason, then set readSignalsAttempted to true "
        "and pass the whole thing back. If the ticket carries no signature, set the flag with "
        "empty values and say so in the reason — that is a real answer, and this refusal is "
        "only for the case where nobody looked."
    )


def verify(payload: dict[str, Any], regions: dict[str, str]) -> dict[str, Any]:
    """Split the signals into what may be searched with and what may not."""
    signals = payload.get("signals") if isinstance(payload.get("signals"), dict) else payload
    _reading_happened(signals)
    provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}

    # The sender's own words: their message plus their signature. The quoted part is somebody
    # else's, so it is a separate corpus rather than part of this one.
    own_corpus = compare_key((regions.get("own_text") or "") + " " + (regions.get("signature") or ""))
    quoted_corpus = compare_key(regions.get("quoted") or "")

    verified: dict[str, Any] = {}
    discarded: list[dict[str, Any]] = []
    unverified: dict[str, Any] = {}
    notes: list[str] = []

    def check(field: str, value: Any, index: int | None = None) -> bool:
        if field in NEVER_FROM_TEXT_FIELDS:
            return True
        meta = _provenance_for(provenance, field, index)
        if meta.get("verification") == "not_required":
            return True
        key = compare_key(value)
        if not key:
            return True  # nothing to look for; an empty value is harmless
        if key in own_corpus:
            return True
        code = "wrong_region" if key in quoted_corpus else "not_in_text"
        discarded.append(
            {
                "field": field,
                "value": value,
                "code": code,
                "detail": _DETAIL_DE[code],
                "source": meta.get("source"),
            }
        )
        return False

    for field, value in signals.items():
        if field in JUDGEMENT_FIELDS:
            unverified[field] = value
            continue
        if field in PASSTHROUGH_FIELDS or value is None or value == [] or value == "":
            verified[field] = value
            continue
        if isinstance(value, list):
            kept = [item for index, item in enumerate(value) if check(field, item, index)]
            verified[field] = kept
        elif check(field, value):
            verified[field] = value
        # A scalar that failed is simply absent from `verified`; `discarded` says why.

    if not regions.get("quoted"):
        notes.append("The ticket carries no quoted part; no value could fail on its region.")
    if discarded:
        wrong_region = sum(1 for d in discarded if d["code"] == "wrong_region")
        if wrong_region:
            notes.append(
                f"{wrong_region} value(s) came from the quoted part of the message and belong to "
                "whoever wrote it, not to this contact."
            )
    if unverified:
        notes.append(
            "confidence, counterSignals, readSignals and reason are the reader's judgement: "
            "they are carried for the note and the trace, and are not evidence."
        )

    return {
        "ok": True,
        "verified": verified,
        "discarded": discarded,
        "unverified": unverified,
        "signals": signals,
        "notes": notes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Prove the signals against the ticket text.")
    add_ticket_id_argument(parser)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--signals", "--signals_json", dest="signals")
    source.add_argument("--signals-file", "--signals_file", dest="signals_file")
    # The extractor emits signals, regions and provenance as three blocks, so a caller holding
    # its output naturally passes three flags. Refusing two of them made argparse read the JSON
    # as the positional ticket id and exit 2 — which reached the ledger as "exited before
    # reporting a result" and read like an intermittent crash. Accept them: the shape a caller
    # already has is the shape this should take.
    parser.add_argument("--regions", "--regions_json", dest="regions")
    parser.add_argument("--provenance", "--provenance_json", dest="provenance")
    args = parser.parse_args()
    ticket_id = resolved_ticket_id(args, parser)

    try:
        payload = _load(args.signals, args.signals_file)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        _fail({"ok": False, "ticket_id": ticket_id, "error": str(exc)})
        return

    for name in ("regions", "provenance"):
        raw = getattr(args, name, None)
        if not raw or isinstance(payload.get(name), dict):
            continue
        try:
            block = json.loads(raw)
        except json.JSONDecodeError as exc:
            _fail({"ok": False, "ticket_id": ticket_id, "error": f"--{name} is not JSON: {exc}"})
            return
        if isinstance(block, dict):
            payload[name] = block

    regions = payload.get("regions") if isinstance(payload.get("regions"), dict) else None
    if not regions:
        # The caller did not carry the extractor's regions, so read the ticket again rather
        # than verify against nothing.
        try:
            client = FreshdeskClient.from_env()
            regions = _regions_from_ticket(client.get_ticket(ticket_id))
        except (ConfigError, FreshdeskError) as exc:
            _fail({"ok": False, "ticket_id": ticket_id, "error": str(exc)})
            return

    try:
        result = verify(payload, regions)
    except ReadingLayerMissing as exc:
        # A refusal, not a crash: the flow stopped one step short and the operator has to know
        # which step, not be handed an empty result that reads like an answer.
        _fail({"ok": False, "ticket_id": ticket_id, "error": str(exc),
               "missing_step": "reading_layer"})
        return
    result["ticket_id"] = ticket_id
    record(
        "verify_signals.py",
        result={"ok": True, "ticket_id": ticket_id, "candidate_count": len(result["discarded"])},
    )
    print(
        f"Ticket {ticket_id}: {len(result['discarded'])} value(s) discarded "
        f"({', '.join(sorted({d['code'] for d in result['discarded']})) or 'none'}).",
        file=sys.stderr,
        flush=True,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    guarded("verify_signals.py", main)
