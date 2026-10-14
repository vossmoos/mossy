"""Stand in for the reading layer, which is an agent and not a script.

`extract_company_signals.py` reads no prose: every company name comes from a reader working
from the signature. Tests that verify signals have to declare that step happened, exactly as
the agent does — otherwise they would be testing a payload the flow now refuses, and the
refusal is the point.
"""

from __future__ import annotations

from typing import Any


def as_read(payload: dict[str, Any], **fields: Any) -> dict[str, Any]:
    """The extractor's output with the reading layer's declaration and findings on it."""
    signals = dict(payload.get("signals") or payload)
    signals["readSignalsAttempted"] = True
    signals.update(fields)
    if payload.get("signals") is not None:
        return {**payload, "signals": signals}
    return signals
