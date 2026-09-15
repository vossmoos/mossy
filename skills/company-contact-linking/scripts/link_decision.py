# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python, which may lack this project's dependencies.
# Without one it falls back to sys.executable, i.e. the interpreter running Mossy.
"""One decision, with the German reason it was made for. Deterministic; nothing is written.

It reads the **verified** signals and the candidates, and nothing else. ``confidence``,
``counterSignals`` and ``reason`` are the reader's judgement rather than claims about the
ticket, and a judgement that cannot be checked must not decide who a contact works for.

The decisions:

``already_linked``  the contact has a company; this skill only fills a gap.
``out_of_scope``    the ticket's group is not one this deployment acts on.
``no_signals``      the ticket says nothing about any company. Distinct from ``no_match`` on
                    purpose: one means the ticket is empty of company evidence, the other means
                    the evidence is there and the directory has no answer for it. They call for
                    different things — the first is finished forever, the second may not be.
``no_match``        evidence was found, but no company matches and none can be created.
``ambiguous``       two or more candidates clear the threshold. **Nothing is linked** — when the
                    evidence does not distinguish two companies, a tie-break would produce a
                    link indistinguishable from a correct one afterwards.
``link``            exactly one candidate clears the threshold.
``create``          no candidate, but the ticket names a company clearly enough to create it.

**No verified signals at all means no_match, never create.** The client was explicit: with
nothing to go on, leave the ticket alone. A company invented from an empty ticket is a
permanent record nobody asked for.

Output JSON: ok, decision, reason, company_id, company_name, create{...}, threshold,
evidence{...}, signals{...}, notes[].
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from _audit import guarded, record
from _signals import signals_from
from _config import ConfigError, allow_company_creation, min_match_score
from _text import name_key

# Signals that can carry evidence about which company this is. The judgement fields are not
# here on purpose, and neither are the phones: a phone number identifies nobody on its own.
EVIDENCE_FIELDS = ("emailDomain", "companyNameCandidates", "uid", "urls")


def _fail(payload: dict) -> None:
    record("link_decision.py", result=payload)
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(1)


def _load(raw: str | None, path: str | None) -> dict[str, Any]:
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    elif raw:
        data = json.loads(raw)
    else:
        raise ValueError("Input is required: pass --json '<json>' or --json-file <path>.")
    if not isinstance(data, dict):
        raise ValueError("The input must be a JSON object.")
    return data


def _verified(payload: dict[str, Any]) -> dict[str, Any]:
    """The verified block, wherever the caller put it.

    Shape reading lives in ``_signals`` so that this script and ``match_company.py`` cannot
    drift into disagreeing about what a valid payload looks like — they already had, and the
    one that understood fewer shapes returned no candidates without saying why.

    A payload it cannot read raises instead of reducing to an empty set: an empty set here
    means ``no_match``, which is a claim about the ticket, and an unreadable argument is not.
    """
    return signals_from(payload)


def _evidence(verified: dict[str, Any]) -> dict[str, Any]:
    """What survived verification and can say something about a company."""
    evidence: dict[str, Any] = {}
    for field in EVIDENCE_FIELDS:
        value = verified.get(field)
        if field == "emailDomain" and value and verified.get("emailDomainIsGeneric"):
            # A free-mail domain is not evidence about an employer.
            continue
        if value:
            evidence[field] = value
    return evidence


def _company_name(verified: dict[str, Any]) -> str | None:
    return _first(verified, "companyNameCandidates")


def _usable(verified: dict[str, Any], *names: str) -> list[str]:
    """Every usable value under any of these keys, list or scalar, in order."""
    found: list[str] = []
    for name in names:
        value = verified.get(name)
        items = value if isinstance(value, list) else [value]
        for item in items:
            if item not in (None, "", []):
                found.append(str(item).strip())
    return found


def _one_of(
    verified: dict[str, Any], notes: list[str], label: str, *names: str
) -> str | None:
    """The first usable value — and a note when that means dropping the others.

    A Freshdesk company field holds one value, so a signature carrying a head office and a
    branch loses one of them here. That is a real choice and not ours to change: writing both
    would change what the client sees in their CRM. What must not happen is losing the second
    one **quietly**. Every value here survived verification, which means it is in the ticket
    and it is the sender's own — the strongest thing this flow can say about a value — and
    dropping one without a word is how the address bug went unnoticed for a fortnight.

    The note names the count and which value was written, so the one that was dropped can be
    found in the ticket by whoever needs it.
    """
    values = _usable(verified, *names)
    if not values:
        return None
    if len(values) > 1:
        notes.append(
            f"The ticket carries {len(values)} verified {label} values; the company field holds "
            f"one, so {values[0]!r} was written and the rest were not: "
            + ", ".join(repr(v) for v in values[1:])
            + "."
        )
    return values[0]


def _first(verified: dict[str, Any], *names: str) -> str | None:
    """The first usable value under any of these keys, list or scalar.

    Signals are plural lists — ``phones``, ``urls``, ``addresses`` — because a signature can
    carry several of each. Reading one of them as a scalar silently yields nothing: measured on
    a real ticket, a verified address never reached the company that was created from it,
    while the phone beside it did. Accepting both shapes costs a line and removes a whole class
    of that failure.
    """
    for name in names:
        value = verified.get(name)
        if isinstance(value, list):
            for item in value:
                if item not in (None, "", []):
                    return str(item).strip()
        elif value not in (None, "", []):
            return str(value).strip()
    return None


class DirectorySearchMissing(ValueError):
    """The directory was never searched, so 'no candidate' is not a fact about it."""


def _directory_was_searched(payload: dict[str, Any]) -> None:
    """Refuse a payload that never went through the matcher.

    ``match_company.py`` reports what it searched — the directory listing, any domain queries —
    in ``searched``. Carrying that field forward is what proves the candidates in this payload
    are the answer to a real question rather than a list nobody filled in.

    Measured through the duty: the matcher found the contact's company and scored it 1.0, and
    the decision arrived with an empty candidate list because the agent rebuilt the payload by
    hand and left the candidates behind. The decision came out ``create`` for a company that
    already existed. The writer's re-derivation refused it, which is the system working — but a
    refusal at the last step is a poor place to learn that the second step was thrown away.
    """
    if payload.get("searched"):
        return
    raise DirectorySearchMissing(
        "The directory search is missing: this payload carries no 'searched' field, so an "
        "empty candidate list cannot be read as 'no company matched'. Pass the output of "
        "match_company.py through — candidates and searched together — rather than "
        "assembling a new object. If the matcher really found nothing, its output still says "
        "what it looked in, and that is what makes no_match an answer instead of a gap."
    )


def decide(payload: dict[str, Any]) -> dict[str, Any]:
    _directory_was_searched(payload)
    threshold = min_match_score()
    verified = _verified(payload)
    candidates = payload.get("candidates") or []
    notes: list[str] = []

    def result(decision: str, reason: str, **extra: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "decision": decision,
            "reason": reason,
            "threshold": threshold,
            "evidence": _evidence(verified),
            # Carried so apply_link.py can re-derive this decision instead of trusting it.
            "signals": verified,
            "candidates": candidates,
            "notes": notes,
            **extra,
        }

    if payload.get("linked") or payload.get("company_id"):
        return result(
            "already_linked",
            "Der Kontakt ist bereits einer Firma zugeordnet; es wird nichts geändert.",
            company_id=payload.get("company_id"),
            company_name=payload.get("company_name"),
        )
    if payload.get("in_scope") is False:
        return result(
            "out_of_scope",
            "Das Ticket gehört nicht zu den Gruppen, die dieser Ablauf bearbeitet.",
        )

    evidence = _evidence(verified)
    if not evidence:
        # Nothing survived verification that could name a company: no domain, no name, no UID,
        # no website. The client's instruction is to leave the ticket alone — "if no company
        # credentials found, just do not create task, just ignore".
        #
        # A verified phone number can be sitting right here and this still holds. A phone
        # identifies nobody on its own, so it cannot start a search or justify a record; it is
        # named in the reason so the outcome does not read as "the ticket was empty" when it
        # was not.
        seen = [field for field in ("phones", "addresses") if verified.get(field)]
        detail = (
            " Vorhandene Angaben wie Telefonnummer oder Adresse lassen für sich genommen "
            "keine Firma bestimmen."
            if seen
            else ""
        )
        return result(
            "no_signals",
            "Im Ticket liess sich kein belastbarer Hinweis auf eine Firma finden; das Ticket "
            "wird nicht weiter bearbeitet." + detail,
        )

    above = [c for c in candidates if float(c.get("score") or 0) >= threshold]
    if len(above) > 1:
        names = ", ".join(str(c.get("company_name")) for c in above)
        return result(
            "ambiguous",
            f"Mehrere Firmen kommen gleichermassen in Frage ({names}); es wird bewusst keine "
            "Zuordnung vorgenommen.",
            candidates_above_threshold=[c.get("company_name") for c in above],
        )
    if len(above) == 1:
        winner = above[0]
        matched = ", ".join(winner.get("matched_on") or []) or "Übereinstimmung"
        return result(
            "link",
            f"Die Firma „{winner.get('company_name')}“ passt eindeutig zum Absender "
            f"({matched}).",
            company_id=winner.get("company_id"),
            company_name=winner.get("company_name"),
        )

    if candidates:
        notes.append(
            f"{len(candidates)} candidate(s) were found but none reached {threshold}."
        )

    name = _company_name(verified)
    if not allow_company_creation():
        return result(
            "no_match",
            "Im Firmenverzeichnis gibt es keinen passenden Eintrag; das Anlegen neuer Firmen "
            "ist in dieser Installation nicht aktiv.",
        )
    if not name:
        # A company needs a name. A domain alone would create a record called after a mailbox.
        return result(
            "no_match",
            "Im Firmenverzeichnis gibt es keinen passenden Eintrag, und im Ticket steht kein "
            "Firmenname, aus dem sich einer anlegen liesse.",
        )

    domain = None if verified.get("emailDomainIsGeneric") else verified.get("emailDomain")
    return result(
        "create",
        f"Im Firmenverzeichnis gibt es keinen Eintrag für „{name}“; die Firma wird aus den "
        "Angaben im Ticket angelegt und der Kontakt ihr zugeordnet.",
        company_name=name,
        create={
            "name": name,
            "domain": domain,
            # Plural first, singular accepted: the reader writes one or the other and a
            # mismatch here loses the value without a word.
            "phone": _one_of(verified, notes, "phone", "phones", "phone"),
            "address": _one_of(verified, notes, "address", "addresses", "address"),
            "email": _one_of(
                verified, notes, "company email",
                "companyEmails", "emails", "companyEmail", "email",
            ),
            "name_key": name_key(name),
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Decide what to do with these signals.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--json", dest="json_str")
    source.add_argument("--json-file", "--json_file", dest="json_file")
    args = parser.parse_args()

    try:
        result = decide(_load(args.json_str, args.json_file))
    except DirectorySearchMissing as exc:
        _fail({"ok": False, "error": str(exc), "missing_step": "directory_search"})
        return
    except (ConfigError, ValueError, OSError, json.JSONDecodeError) as exc:
        _fail({"ok": False, "error": str(exc)})
        return

    record("link_decision.py", result={"ok": True, "reason": result["decision"]})
    print(f"Decision: {result['decision']}.", file=sys.stderr, flush=True)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    guarded("link_decision.py", main)
