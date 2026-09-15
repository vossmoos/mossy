"""Invocation ledger: one JSON line per run, including the runs that crash.

A ledger that only records successes cannot answer the question it exists for. On the sibling
skill two silent exits — an exception after a write, and an argparse failure — were
indistinguishable from "the script never ran", and telling them apart cost an investigation.

Local, append-only, and never a source of truth: Freshdesk is. Failing to write here never
fails a run.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# The ledger is evidence about what this deployment actually executed, which makes it useless
# the moment anything else writes to it. A test suite driving the writer against a fake account
# filled it with 1400 rows about tickets that do not exist, and a real run afterwards could not
# be read out of it. Tests point this at a temporary file instead.
_LOG_DIR = Path(os.environ.get("LINKING_LEDGER_DIR") or (Path(__file__).resolve().parents[1] / "logs"))
_LOG_FILE = _LOG_DIR / "invocations.jsonl"
_RECORDED_KEYS = (
    "ok", "crashed", "ticket_id", "contact_id", "linked", "in_scope",
    "candidate_count", "reason", "error",
)
_recorded = False


def record(script: str, *, result: dict[str, Any]) -> None:
    global _recorded
    _recorded = True
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "script": script,
            "pid": os.getpid(),
            "argv": sys.argv[1:],
            "result": {k: result.get(k) for k in _RECORDED_KEYS if k in result},
        }
        with _LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 - an unwritable log must not fail the script
        pass


def normalize_argv() -> None:
    """Collapse three or more leading dashes to two.

    ``run_skill_script`` builds each flag as ``--`` plus the key it is given, so a caller that
    writes the key as ``--ticket-id`` produces ``----ticket-id`` and argparse rejects the run.
    Accepting the malformed form costs one line and loses nothing.
    """
    sys.argv[:] = [
        ("--" + token.lstrip("-")) if re.match(r"^-{3,}[^-]", token) else token
        for token in sys.argv
    ]


class _Tee:
    """Write to both streams: the operator keeps their console, the ledger keeps the reason."""

    def __init__(self, *streams: Any) -> None:
        self._streams = streams

    def write(self, text: str) -> int:
        for stream in self._streams:
            stream.write(text)
        return len(text)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


def guarded(script: str, main: Callable[[], None]) -> None:
    """Run an entry point so no exit path leaves the ledger silent."""
    normalize_argv()
    stderr = io.StringIO()
    try:
        # argparse writes its usage and its complaint to stderr and then exits, so the ledger
        # used to record only "exited before reporting a result" — a script called with a flag
        # it does not have looked exactly like a crash, and cost an afternoon to tell apart.
        # Tee it: the operator still sees it, and the ledger keeps the reason.
        with contextlib.redirect_stderr(_Tee(sys.stderr, stderr)):
            main()
    except SystemExit as exc:
        if not _recorded:
            complaint = stderr.getvalue().strip().splitlines()
            detail = complaint[-1] if complaint else ""
            record(script, result={
                "ok": False,
                "error": detail or f"exited with status {exc.code} before reporting a result",
                "exit_code": exc.code,
                "argv": sys.argv[1:],
            })
        raise
    except BaseException as exc:  # noqa: BLE001 - re-raised; the point is the trace
        record(script, result={"ok": False, "crashed": True, "error": f"{type(exc).__name__}: {exc}"})
        raise


__all__ = ["guarded", "normalize_argv", "record"]
