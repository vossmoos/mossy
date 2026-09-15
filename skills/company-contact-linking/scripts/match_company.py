# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python, which may lack this project's dependencies.
# Without one it falls back to sys.executable, i.e. the interpreter running Mossy.
"""Propose company candidates for a set of signals. Read-only: nothing is written.

**This script does not decide.** It returns every company it can justify, each with the signal
that produced it and a score, and leaves the choice to ``link_decision.py``. Ranking is not
deciding: a candidate at the top of this list may still be too weak to link, or too close to
the second one to be unambiguous.

Two ways of reading the directory, merged, because neither is reliable alone. The search index
lags a minute or two and flaps while it catches up, and the listing is not instant either — a
company created during this work showed up in ``/companies`` only on the second read. The search
also accepts **only** a domain query: a name comes back HTTP 400, so a company name can only be
matched against the listing. Results are merged by company id, and a company that only the
search knew about is called out in the notes.

``LINKING_MIN_MATCH_SCORE`` is 0.9, confirmed with the client. It is read here only to mark
each candidate ``meets_threshold`` and to count how many clear the bar — **two candidates at or
above it mean the case is ambiguous and nothing may be linked**, which is a fact this script
reports and ``link_decision.py`` acts on.

Output JSON: ok, ticket_id, min_match_score, candidate_count, above_threshold_count,
candidates[], searched[], notes[].
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from _args import add_ticket_id_argument, resolved_ticket_id
from _audit import guarded, record
from _config import ConfigError, max_candidates, min_match_score, scores
from _freshdesk import FreshdeskClient, FreshdeskError
from _signals import signals_from
from _text import compare_key, name_key


def _fail(payload: dict) -> None:
    record("match_company.py", result=payload)
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(1)


def _load_signals(raw: str | None, path: str | None) -> dict[str, Any]:
    if path:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    elif raw:
        data = json.loads(raw)
    else:
        raise ValueError(
            "Signals are required: pass --signals '<json>' or --signals-file <path>. Use the "
            "verified signals, not the raw ones."
        )
    # Every shape this flow produces is understood, and anything else raises rather than
    # matching against an empty signal set — which returns no candidates and reads exactly
    # like a directory with nothing in it.
    return signals_from(data)


def _company_domains(company: dict[str, Any]) -> list[str]:
    domains = company.get("domains")
    return [str(d).strip().lower() for d in domains if d] if isinstance(domains, list) else []


def _company_uids(company: dict[str, Any]) -> list[str]:
    """Any custom field whose value looks like a UID/VAT id, without naming the field.

    Accounts store it under different names, and hardcoding one would break in the next
    deployment. Anything that normalises to the same key as the signal counts.
    """
    fields = company.get("custom_fields")
    if not isinstance(fields, dict):
        return []
    return [compare_key(v) for v in fields.values() if isinstance(v, str) and v.strip()]


def _evaluate(company: dict[str, Any], signals: dict[str, Any], weights: dict[str, float]) -> dict | None:
    """Score one company against the signals. None when nothing connects them."""
    hits: list[dict[str, Any]] = []

    domain = str(signals.get("emailDomain") or "").strip().lower()
    if domain and not signals.get("emailDomainIsGeneric") and domain in _company_domains(company):
        hits.append({"signal": "corporate_email_domain", "value": domain, "score": weights["domain"]})

    uid = compare_key(signals.get("uid")) if signals.get("uid") else ""
    if uid and uid in _company_uids(company):
        hits.append({"signal": "uid_number", "value": signals.get("uid"), "score": weights["uid"]})

    company_key = name_key(company.get("name"))
    for candidate_name in signals.get("companyNameCandidates") or []:
        signal_key = name_key(candidate_name)
        if not signal_key or not company_key:
            continue
        if signal_key == company_key:
            hits.append({"signal": "name_exact", "value": candidate_name, "score": weights["name_exact"]})
        elif signal_key in company_key or company_key in signal_key:
            hits.append(
                {"signal": "name_contains", "value": candidate_name, "score": weights["name_contains"]}
            )

    if not hits:
        return None
    # The strongest signal carries the score; the rest are corroboration, listed so a decision
    # (and the German note) can name the evidence without inventing it.
    best = max(hit["score"] for hit in hits)
    return {
        "company_id": company.get("id"),
        "company_name": company.get("name"),
        "company_domains": _company_domains(company),
        "score": round(min(best, 1.0), 4),
        "matched_on": sorted({hit["signal"] for hit in hits}),
        "evidence": hits,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Propose company candidates for a ticket.")
    add_ticket_id_argument(parser)
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--signals",
        "--signals_json",
        dest="signals",
        help="Verified signals as JSON (the output of verify_signals.py, or its 'verified' block).",
    )
    source.add_argument(
        "--signals-file",
        "--signals_file",
        dest="signals_file",
        help="Path to the same JSON, for signals too long for a command line.",
    )
    # Accepted and ignored: a caller holding the extractor's three blocks passes all three, and
    # refusing two of them made argparse read the JSON as the positional ticket id and exit 2.
    # Matching needs neither, but dying on them is worse than taking them.
    parser.add_argument("--regions", "--regions_json", dest="regions")
    parser.add_argument("--provenance", "--provenance_json", dest="provenance")
    args = parser.parse_args()
    ticket_id = resolved_ticket_id(args, parser)

    try:
        threshold = min_match_score()  # unset stops the run with an explanation, not a guess
        signals = _load_signals(args.signals, args.signals_file)
        weights = scores()
    except (ConfigError, ValueError, OSError, json.JSONDecodeError) as exc:
        _fail({"ok": False, "ticket_id": ticket_id, "error": str(exc)})
        return

    notes: list[str] = []
    searched: list[str] = []
    directory: dict[Any, dict[str, Any]] = {}
    try:
        client = FreshdeskClient.from_env()
        listed, listing_notes = client.list_companies()
        notes.extend(listing_notes)
        searched.append(f"directory listing ({len(listed)} companies)")
        for company in listed:
            directory[company.get("id")] = company

        # Only a domain can be searched for: a name query is rejected with HTTP 400. Names are
        # matched against the listing above, which is why the listing is read every time rather
        # than kept as a fallback.
        domain = str(signals.get("emailDomain") or "").strip().lower()
        if domain and not signals.get("emailDomainIsGeneric"):
            found, search_notes = client.search_companies_by_domain(domain)
            notes.extend(search_notes)
            searched.append(f"domain search {domain!r} ({len(found)} hit(s))")
            for company in found:
                if company.get("id") not in directory:
                    directory[company.get("id")] = company
                    notes.append(
                        f"Company {company.get('name')!r} came from the search index and was "
                        "not in the directory listing."
                    )
    except (ConfigError, FreshdeskError) as exc:
        _fail({"ok": False, "ticket_id": ticket_id, "error": str(exc)})
        return

    candidates = [c for c in (_evaluate(co, signals, weights) for co in directory.values()) if c]
    candidates.sort(key=lambda c: (-c["score"], str(c["company_name"] or "")))
    limit = max_candidates()
    if len(candidates) > limit:
        notes.append(f"{len(candidates)} companies matched; the {limit} strongest are reported.")
        candidates = candidates[:limit]
    for candidate in candidates:
        candidate["meets_threshold"] = candidate["score"] >= threshold
    above = [c for c in candidates if c["meets_threshold"]]
    if not candidates:
        notes.append("No company in the directory could be connected to these signals.")
    elif len(above) > 1:
        # Reported, not decided: the rule that two clear candidates mean "do not link" lives in
        # link_decision.py, and stating it here would put the same rule in two places.
        notes.append(
            f"{len(above)} candidates reach the threshold ({', '.join(str(c['company_name']) for c in above)}); "
            "more than one means the case is ambiguous and nothing may be linked."
        )
    elif not above:
        notes.append(f"No candidate reaches the threshold of {threshold}.")

    payload = {
        "ok": True,
        "ticket_id": ticket_id,
        # The verified signals travel on. Each step's output is meant to be the next step's
        # input, and this one used to break that chain: its output carried candidates but not
        # the signals they were found from, so handing it to the writer — which is what the
        # flow says to do — failed with "no signals could be read". The agent had followed the
        # instructions exactly.
        "signals": signals,
        "min_match_score": threshold,
        "candidate_count": len(candidates),
        "above_threshold_count": len(above),
        "candidates": candidates,
        "searched": searched,
        "notes": notes,
    }
    record(
        "match_company.py",
        result={"ok": True, "ticket_id": ticket_id, "candidate_count": len(candidates)},
    )
    print(
        f"Ticket {ticket_id}: {len(candidates)} candidate(s) from {len(directory)} company "
        f"record(s), {len(above)} at or above {threshold}.",
        file=sys.stderr,
        flush=True,
    )
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    guarded("match_company.py", main)
