# No shebang on purpose: the skill script runner dispatches on the shebang first, and
# /usr/bin/env python3 is the system Python. Without one it falls back to sys.executable.
"""Semantic-duplicate cache for bug reports, backed by DuckDB + OpenAI embeddings.

Self-contained: no ``import mossy`` and no ``python-dotenv`` — the skill script runtime
does not have mossy on its path, and config comes straight from the environment. The only external
write this module makes is to the OpenAI embeddings API; everything else is local DuckDB.

It remembers a bug (its Jira key/URL, the Freshdesk ticket + conversation it came from, a summary
and the problem text) together with an embedding of the problem text, and finds a prior duplicate
by cosine similarity within a rolling window of **working days** (weekends skipped, no holidays).

Environment (all optional except the API key, which is only needed for real embedding calls):

    DEDUPE_DB_PATH                  default ~/.local/share/mossy/dedupe.duckdb
    DEDUPE_SIMILARITY_THRESHOLD     default 0.85
    DEDUPE_LOOKBACK_WORKING_DAYS    default 3
    DEDUPE_EMBEDDING_MODEL          default text-embedding-3-small
    OPENAI_API_KEY                  required for embed()

CLI (named flags only — the skill tool passes no positionals):

    dedupe_cache.py remember --jira-key PROJ-1 --jira-url https://… --ticket-id 100 \
        --conversation-id 5 --summary "…" --problem-text "…"
    dedupe_cache.py find-similar --problem-text "…"
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb

_DEFAULT_DB = "~/.local/share/mossy/dedupe.duckdb"
_DEFAULT_THRESHOLD = 0.85
_DEFAULT_LOOKBACK_WORKING_DAYS = 3
_DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
_OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"


# --------------------------------------------------------------------------- config
def _db_path() -> Path:
    return Path(os.environ.get("DEDUPE_DB_PATH") or _DEFAULT_DB).expanduser()


def _threshold() -> float:
    try:
        return float(os.environ.get("DEDUPE_SIMILARITY_THRESHOLD") or _DEFAULT_THRESHOLD)
    except (TypeError, ValueError):
        return _DEFAULT_THRESHOLD


def _lookback_working_days() -> int:
    try:
        return int(os.environ.get("DEDUPE_LOOKBACK_WORKING_DAYS") or _DEFAULT_LOOKBACK_WORKING_DAYS)
    except (TypeError, ValueError):
        return _DEFAULT_LOOKBACK_WORKING_DAYS


def _embedding_model() -> str:
    return (os.environ.get("DEDUPE_EMBEDDING_MODEL") or _DEFAULT_EMBEDDING_MODEL).strip()


# --------------------------------------------------------------------------- storage
def _connect(db_path: Path | str | None = None) -> duckdb.DuckDBPyConnection:
    """Open the cache DB and ensure the table exists."""
    path = Path(db_path).expanduser() if db_path else _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS duplicate_cache (
            jira_key VARCHAR PRIMARY KEY,
            jira_url VARCHAR,
            ticket_id BIGINT,
            conversation_id BIGINT,
            summary VARCHAR,
            problem_text VARCHAR,
            embedding DOUBLE[],
            created_at VARCHAR
        )
        """
    )
    return conn


# --------------------------------------------------------------------------- embeddings
def embed(text: str) -> list[float]:
    """Embed ``text`` with the OpenAI embeddings API using urllib (no ``openai`` library)."""
    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set; cannot embed.")
    payload = json.dumps({"model": _embedding_model(), "input": text}).encode("utf-8")
    req = urllib.request.Request(
        _OPENAI_EMBEDDINGS_URL,
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500] if exc.fp else ""
        raise RuntimeError(f"OpenAI embeddings failed (HTTP {exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OpenAI embeddings unreachable: {exc.reason}") from exc
    try:
        vector = data["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("OpenAI embeddings response had no embedding.") from exc
    return [float(x) for x in vector]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 for empty/mismatched/zero vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# --------------------------------------------------------------------------- working-day window
def _working_day_cutoff(reference: date, working_days: int) -> date:
    """The date that is ``working_days`` working days before ``reference``, skipping Sat/Sun.

    From a Monday with 3 working days: Fri (1), Thu (2), Wed (3) — so the cutoff is the previous
    Wednesday, five calendar days back (the weekend's two days are stepped over, not counted).
    Records on or after this date are inside the window.
    """
    current = reference
    remaining = max(0, working_days)
    while remaining > 0:
        current = current - timedelta(days=1)
        if current.weekday() < 5:  # Mon–Fri are 0–4; Sat=5, Sun=6 do not count
            remaining -= 1
    return current


# --------------------------------------------------------------------------- public API
def remember(
    jira_key: str,
    jira_url: str,
    ticket_id: int,
    conversation_id: int,
    summary: str,
    problem_text: str,
    *,
    created_at: str | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Embed ``problem_text`` and upsert one row keyed by ``jira_key`` (a re-``remember`` replaces)."""
    vector = embed(problem_text)
    timestamp = created_at or datetime.now(timezone.utc).isoformat()
    conn = _connect(db_path)
    try:
        # Upsert by delete-then-insert: guarantees exactly one row per jira_key, no ON CONFLICT
        # version quirks.
        conn.execute("DELETE FROM duplicate_cache WHERE jira_key = ?", [jira_key])
        conn.execute(
            "INSERT INTO duplicate_cache "
            "(jira_key, jira_url, ticket_id, conversation_id, summary, problem_text, embedding, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [jira_key, jira_url, ticket_id, conversation_id, summary, problem_text, vector, timestamp],
        )
    finally:
        conn.close()
    return {"ok": True, "jira_key": jira_key, "created_at": timestamp, "embedding_dim": len(vector)}


def find_similar(
    problem_text: str,
    *,
    now: datetime | None = None,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Best cosine match to ``problem_text`` inside the working-day window, or no match.

    Returns ``{ok, match, score, threshold, cutoff}``. ``match`` is None when nothing in the
    window clears the threshold; ``score`` is the best in-window similarity seen (None if the
    window held no rows at all).
    """
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    cutoff_date = _working_day_cutoff(reference.date(), _lookback_working_days())
    cutoff_dt = datetime.combine(cutoff_date, dtime.min, tzinfo=timezone.utc)
    threshold = _threshold()

    query_vector = embed(problem_text)

    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT jira_key, jira_url, ticket_id, conversation_id, summary, problem_text, "
            "embedding, created_at FROM duplicate_cache"
        ).fetchall()
    finally:
        conn.close()

    best_row: tuple | None = None
    best_score = -1.0
    for row in rows:
        embedding, created_at = row[6], row[7]
        try:
            created_dt = datetime.fromisoformat(str(created_at))
        except (TypeError, ValueError):
            continue
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        if created_dt < cutoff_dt:  # outside the working-day window
            continue
        score = cosine_similarity(query_vector, list(embedding) if embedding is not None else [])
        if score > best_score:
            best_score, best_row = score, row

    result: dict[str, Any] = {
        "ok": True,
        "match": None,
        "score": best_score if best_row is not None else None,
        "threshold": threshold,
        "cutoff": cutoff_dt.isoformat(),
    }
    if best_row is not None and best_score >= threshold:
        result["match"] = {
            "jira_key": best_row[0],
            "jira_url": best_row[1],
            "ticket_id": best_row[2],
            "conversation_id": best_row[3],
            "summary": best_row[4],
            "problem_text": best_row[5],
            "created_at": best_row[7],
        }
    return result


# --------------------------------------------------------------------------- CLI
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Semantic-duplicate cache (DuckDB + embeddings).")
    sub = parser.add_subparsers(dest="command", required=True)

    r = sub.add_parser("remember", help="Embed and store one bug.")
    r.add_argument("--jira-key", required=True)
    r.add_argument("--jira-url", required=True)
    r.add_argument("--ticket-id", type=int, required=True)
    r.add_argument("--conversation-id", type=int, required=True)
    r.add_argument("--summary", required=True)
    r.add_argument("--problem-text", required=True)

    f = sub.add_parser("find-similar", help="Find a prior duplicate in the working-day window.")
    f.add_argument("--problem-text", required=True)

    return parser


def main() -> None:  # pragma: no cover - exercised end to end, not in the offline suite
    args = _build_parser().parse_args()
    if args.command == "remember":
        out = remember(
            args.jira_key,
            args.jira_url,
            args.ticket_id,
            args.conversation_id,
            args.summary,
            args.problem_text,
        )
    elif args.command == "find-similar":
        out = find_similar(args.problem_text)
    else:  # argparse guarantees one of the above
        raise SystemExit(2)
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
