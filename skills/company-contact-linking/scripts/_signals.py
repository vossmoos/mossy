# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python, which may lack this project's dependencies.
# Without one it falls back to sys.executable, i.e. the interpreter running Mossy.
"""Where the signals live inside whatever the caller handed over — defined once.

Every script in this flow takes a signals object, and each one arrives wrapped differently:
``extract_company_signals.py`` returns ``{"ok":…, "signals":{…}, "provenance":…}``,
``verify_signals.py`` returns ``{"ok":…, "verified":{…}, "discarded":…}``, and a caller
assembling a payload by hand usually passes the fields flat. Reading that shape is not
interesting enough to be written three times, and writing it three times is how two of them
end up disagreeing.

**An unreadable payload raises.** It does not quietly reduce to an empty signal set. Three
times in this project a shape mismatch produced an empty result that read exactly like a
truthful "nothing found": an `emailDomain` whose provenance was missing, an `addresses` list
read under a singular key, and a signals envelope this module now understands. Each one looked
like a clean run. "I found no company" and "I could not read what you sent me" must never look
the same from outside, because only one of them means the ticket has nothing in it.
"""

from __future__ import annotations

from typing import Any

# Every field the documented signals schema defines (see references/detection-rules.md). Used
# to recognise a flat payload as signals — not to judge what is worth acting on, which is the
# decision's job and a stricter question.
KNOWN_SIGNAL_FIELDS = frozenset({
    "emailDomain", "emailDomainIsGeneric", "urls", "uid", "phones", "addresses",
    "companyEmails", "companyNameCandidates", "legalForm", "departmentLine",
    "readSignals", "counterSignals", "confidence", "reason",
})

# The keys a signals object is wrapped in. Order matters: ``verified`` is the proven set and
# wins over the raw ``signals`` when a payload somehow carries both.
ENVELOPE_KEYS = ("verified", "signals")


class SignalsShapeError(ValueError):
    """The payload is not a signals object in any shape this flow produces."""


def signals_from(payload: Any) -> dict[str, Any]:
    """The signals inside ``payload``, whichever way it was wrapped.

    Accepts ``{"verified": {…}}``, ``{"signals": {…}}``, ``{"signals": {"verified": {…}}}`` and
    a flat object carrying signal fields directly. Raises ``SignalsShapeError`` for anything
    else, naming what did arrive so the mistake is visible at once.

    An envelope that is present but empty is **not** an error: ``{"verified": {}}`` is a real
    answer from the verification gate, meaning nothing survived it.
    """
    if not isinstance(payload, dict):
        raise SignalsShapeError(
            f"The signals must be a JSON object; got {type(payload).__name__}."
        )

    for key in ENVELOPE_KEYS:
        block = payload.get(key)
        if isinstance(block, dict):
            inner = block.get("verified")
            return inner if isinstance(inner, dict) else block

    if any(field in payload for field in KNOWN_SIGNAL_FIELDS):
        return payload

    present = ", ".join(sorted(payload)[:12]) or "nothing"
    raise SignalsShapeError(
        "No signals could be read from this payload. Expected the output of "
        "extract_company_signals.py or verify_signals.py, or the signal fields themselves; "
        f"its keys are: {present}. Passing the wrong shape would otherwise look exactly like "
        "a ticket with no company in it."
    )
