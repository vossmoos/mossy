# No shebang on purpose: run it with the interpreter that runs the tests.
"""Break the code on purpose and check the suite notices. Run it, do not import it.

A passing suite proves the tests run. It does not prove they would fail if the code were
wrong — and in this project four tests passed for the wrong reason before anyone noticed: an
exemption that held by coincidence, a value read under a key nobody wrote, a payload shape that
silently matched nothing, and an exported variable that changed what was being tested. Coverage
cannot catch any of those. It counts lines executed, not assertions that bite.

This replaces one behaviour at a time with a broken version and runs the suite. A mutation the
suite does not notice is a line that is executed and unchecked — the exact shape of a test that
passes without testing what it claims.

    ../../../.venv/bin/python mutation_audit.py            # every mutation
    ../../../.venv/bin/python mutation_audit.py --list     # just show them

Every mutation restores its file, including on interrupt or crash. Nothing here touches
Freshdesk: the suite it runs is entirely offline.

**Adding to this file is part of adding a guarantee.** When you write code that must hold —
a refusal, a read-back, an idempotency check — add the mutation that removes it. If the suite
stays green, the guarantee is decorative.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent / "scripts"

# (label, file, original, broken). The original must match exactly once; a mutation whose
# anchor has drifted is reported rather than skipped silently, because a mutation that no
# longer applies is a guarantee nobody is checking any more.
MUTATIONS: list[tuple[str, str, str, str]] = [
    # --- the writer: refusals ---------------------------------------------------------
    ("the master switch is ignored", "apply_link.py",
     "    if not enabled():", "    if False:"),
    ("a deleted or spam ticket is written to", "apply_link.py",
     '        if ticket.get("deleted") or ticket.get("spam"):', "        if False:"),
    ("a stale decision is applied anyway", "apply_link.py",
     '    if given.get("decision") != fresh.get("decision"):', "    if False:"),
    ("the processed tag no longer stops a second run", "apply_link.py",
     "        if live_tags_now & all_processed_tags():", "        if False:"),
    # --- the writer: permanent records ------------------------------------------------
    ("the duplicate guard never finds a duplicate", "apply_link.py",
     "    key = name_key(name)\n    if not key:", "    key = None\n    if not key:"),
    ("a created company loses its domain", "apply_link.py",
     'payload["domains"] = [create["domain"]]', "pass"),
    ("verified details never reach the company", "apply_link.py",
     'payload.setdefault("custom_fields", {})[field] = value', "pass"),
    ("a failure after creating hides the new record", "apply_link.py",
     '"linked": False, "created": created,\n            "error": str(exc), "notes": notes,',
     '"linked": False, "created": False,\n            "error": str(exc), "notes": notes,'),
    # --- the writer: the contact's own field ------------------------------------------
    ("the contact log overwrites instead of appending", "apply_link.py",
     'updated = f"{existing_text}\\n{line}".strip() if existing_text else line',
     "updated = line"),
    ("the contact collects the same log line twice", "apply_link.py",
     "    if LOG_LINE_MARKER in existing_text:", "    if False:"),
    # --- the writer: read-back --------------------------------------------------------
    ("a link is assumed rather than confirmed", "apply_link.py",
     '        linked = str(live_contact.get("company_id")) == str(company["id"])',
     "        linked = True"),
    ("the tag is written before the work instead of after", "apply_link.py",
     '        client.link_contact_to_company(int(contact["id"]), int(company["id"]))',
     '        client.add_ticket_tags(ticket_id, [tag])\n'
     '        client.link_contact_to_company(int(contact["id"]), int(company["id"]))'),
    # --- the notes --------------------------------------------------------------------
    ("a second note is posted on the same ticket", "apply_link.py",
     "    existing = ours(client.list_ticket_notes(ticket_id))\n    if existing is not None:",
     "    existing = None\n    if existing is not None:"),
    ("a note posted publicly is treated as success", "apply_link.py",
     '    if not posted.get("private"):', "    if False:"),
    ("an unapproved note is published anyway", "apply_link.py",
     "    if not declined_note_enabled():", "    if False:"),
    ("a declined ticket is never tagged", "apply_link.py",
     "        client.add_ticket_tags(ticket_id, [tag])\n"
     "        # Read-back: Freshdesk decides whether the tag is on the ticket",
     "        pass\n"
     "        # Read-back: Freshdesk decides whether the tag is on the ticket"),
    # --- the decision -----------------------------------------------------------------
    ("an already linked contact is re-linked", "link_decision.py",
     '    if payload.get("linked") or payload.get("company_id"):', "    if False:"),
    ("a ticket outside the groups is acted on", "link_decision.py",
     '    if payload.get("in_scope") is False:', "    if False:"),
    ("a company is created where creation is off", "link_decision.py",
     "    if not allow_company_creation():", "    if False:"),
    ("a company is created without a name", "link_decision.py",
     "    if not name:", "    if False:"),
    ("a dropped second address or phone goes unmentioned", "link_decision.py",
     "    if len(values) > 1:", "    if False:"),
    ("the decision's own notes never reach the result", "apply_link.py",
     "        for note in fresh.get(\"notes\") or []:\n            if note not in notes:",
     "        for note in []:\n            if note not in notes:"),
    ("the create block reads only the singular key", "link_decision.py",
     '"address": _one_of(verified, notes, "address", "addresses", "address"),',
     '"address": verified.get("address"),'),
    # --- the scoring ------------------------------------------------------------------
    ("scores are summed instead of taking the strongest", "match_company.py",
     'best = max(hit["score"] for hit in hits)', 'best = sum(hit["score"] for hit in hits)'),
    ("a free-mail domain counts as employer evidence", "match_company.py",
     'if domain and not signals.get("emailDomainIsGeneric") and domain in _company_domains(company):',
     'if domain and domain in _company_domains(company):'),
    ("a partial name scores like an exact one", "match_company.py",
     '{"signal": "name_contains", "value": candidate_name, "score": weights["name_contains"]}',
     '{"signal": "name_contains", "value": candidate_name, "score": weights["name_exact"]}'),
    # --- the reading layer ------------------------------------------------------------
    ("a payload nobody read is verified anyway", "verify_signals.py",
     '    if signals.get("readSignalsAttempted") is True:\n        return',
     "    if True:\n        return"),
    ("the extractor pretends it read the signature", "extract_company_signals.py",
     "        # step happened, and it is false here because it has not.\n"
     '        "readSignalsAttempted": False,',
     "        # step happened, and it is false here because it has not.\n"
     '        "readSignalsAttempted": True,'),
    ("the writer acts on signals nobody read", "apply_link.py",
     "    except (ReadingLayerMissing, DirectorySearchMissing) as exc:",
     "    except NotImplementedError as exc:"),
    # --- finishing a ticket -----------------------------------------------------------
    ("a declined ticket is queued again on every sweep", "plan_company_check.py",
     "    tags = all_processed_tags()", "    tags = {processed_tag()} if False else set()"),
    ("the link check stops a ticket that should continue", "check_contact_link.py",
     "    stop = bool(company_id) or not in_scope", "    stop = True"),
    ("a decision is made without searching the directory", "link_decision.py",
     '    if payload.get("searched"):\n        return', "    if True:\n        return"),
    ("the writer stops accepting signals, so deciding is a step again", "apply_link.py",
     '    from_signals = bool(args.signals or args.signals_file)',
     "    from_signals = False"),
    ("the link check sends the agent back through a separate decide step",
     "check_contact_link.py",
     '"then verify_signals.py, then match_company.py, and finish with "',
     '"then verify_signals.py, then match_company.py, then link_decision.py. "'),
    ("the matcher drops the signals, breaking the chain", "match_company.py",
     '        "signals": signals,\n        "min_match_score": threshold,',
     '        "min_match_score": threshold,'),
    ("a ticket whose attempt died is never queued again", "plan_company_check.py",
     '"dedupe_key": f"company_check:ticket:{ticket_id}:{_window()}",',
     '"dedupe_key": f"company_check:ticket:{ticket_id}",'),
    # --- the verification gate --------------------------------------------------------
    ("the verification gate accepts everything", "verify_signals.py",
     "        if key in own_corpus:\n            return True", "        if True:\n            return True"),
    ("a quoted value is indistinguishable from an invented one", "verify_signals.py",
     'code = "wrong_region" if key in quoted_corpus else "not_in_text"', 'code = "not_in_text"'),
    # --- shared text handling ---------------------------------------------------------
    ("a glued legal form is no longer recognised", "_text.py",
     'text = _GLUED_FORM_RE.sub(r" \\1", str(value or "")).lower()', 'text = str(value or "").lower()'),
    ("accents are no longer folded when comparing", "_text.py",
     'text = text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")',
     "pass"),
    ("html tags are deleted instead of spaced", "_text.py",
     'text = _TAG_RE.sub(" ", text)', 'text = _TAG_RE.sub("", text)'),
    ("nothing is treated as quoted", "_text.py",
     "    cut = _marker_position(text, markers)", "    cut = -1"),
    # --- the payload shape ------------------------------------------------------------
    ("an unreadable payload becomes an empty one", "_signals.py",
     '    raise SignalsShapeError(\n        "No signals could be read',
     '    return {} or SignalsShapeError(\n        "No signals could be read'),
]


def run_suite() -> bool:
    """True when the suite failed, which is what a mutation is supposed to cause.

    Bytecode caching is switched off, and it is not a detail. This script rewrites source files
    in quick succession, and CPython invalidates a ``.pyc`` by the source's (mtime, size): two
    edits inside the same second that leave the file the same length can leave a stale build in
    place. It cost an hour here — a handler that was plainly in the source did not run, and a
    test looked order-dependent when it was not. In an audit that hazard is worse than a bug:
    a mutation would be reported as caught or missed by whichever build happened to be cached,
    and the whole point of the exercise is that its answers can be trusted.
    """
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "--no-header", "-p", "no:cacheprovider"],
        cwd=TESTS, capture_output=True, text=True, env=environment,
    )
    return result.returncode != 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--list", action="store_true", help="show the mutations, run nothing")
    args = parser.parse_args()

    if args.list:
        for label, filename, _old, _new in MUTATIONS:
            print(f"  {filename:24} {label}")
        return 0

    if run_suite():
        print("The suite already fails. Fix that first — a mutation audit needs a green start,")
        print("or every mutation looks caught for the wrong reason.")
        return 2

    # Any build cached from an earlier state must not survive into the run.
    for cache in (SCRIPTS / "__pycache__", TESTS / "__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)

    survivors: list[str] = []
    stale: list[str] = []
    print(f"{len(MUTATIONS)} mutations\n")
    for label, filename, old, new in MUTATIONS:
        target = SCRIPTS / filename
        original = target.read_text(encoding="utf-8")
        if original.count(old) != 1:
            stale.append(f"{filename}: {label}")
            print(f"  [stale ] {label}")
            continue
        try:
            target.write_text(original.replace(old, new, 1), encoding="utf-8")
            caught = run_suite()
        finally:
            target.write_text(original, encoding="utf-8")
        print(f"  [{'caught' if caught else ' MISS ':^6}] {label}")
        if not caught:
            survivors.append(label)

    print()
    if stale:
        print(f"{len(stale)} mutation(s) no longer apply — the code moved and nothing here")
        print("checks that guarantee any more. Re-anchor them:")
        for item in stale:
            print(f"  - {item}")
    if survivors:
        print(f"{len(survivors)} mutation(s) survived. The suite executes this code without")
        print("checking it; each one is a test that could pass while the behaviour is broken:")
        for item in survivors:
            print(f"  - {item}")
    if not stale and not survivors:
        print(f"All {len(MUTATIONS)} caught. Every guarantee here has a test that would fail.")
    return 1 if (survivors or stale) else 0


if __name__ == "__main__":
    raise SystemExit(main())
