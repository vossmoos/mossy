"""Semantic-duplicate cache, offline.

The embedding client is mocked with deterministic vectors — no network, no OpenAI key. The module
is loaded by file path (it lives under a skill's scripts/, not on the import path) and its
``embed`` function is monkeypatched, so remember/find_similar exercise the real DuckDB storage,
cosine similarity, and working-day window.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "dedupe_cache.py"
)
_spec = importlib.util.spec_from_file_location("dedupe_cache", _MODULE_PATH)
dedupe_cache = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dedupe_cache)


# Deterministic vectors. Printer texts are ~10° apart (cosine ~0.985); the login text is
# orthogonal to them (cosine 0.0). Unknown texts get a neutral vector.
_VECTORS = {
    "printer catches fire when printing": [1.0, 0.0, 0.0],
    "the printer is burning during print jobs": [0.985, 0.174, 0.0],
    "user cannot log in to the portal": [0.0, 1.0, 0.0],
}


def _fake_embed(text: str) -> list[float]:
    return _VECTORS.get(text, [0.5, 0.5, 0.5])


_MONDAY = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)          # a Monday
_TUE_BEFORE = datetime(2025, 12, 30, 9, 0, tzinfo=timezone.utc)     # previous Tuesday (out of window)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Mock embeddings and clear ambient config so the defaults (0.85, 3 working days) rule."""
    monkeypatch.setattr(dedupe_cache, "embed", _fake_embed)
    for var in (
        "DEDUPE_SIMILARITY_THRESHOLD",
        "DEDUPE_LOOKBACK_WORKING_DAYS",
        "DEDUPE_DB_PATH",
        "DEDUPE_EMBEDDING_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)


def _remember(db, key, text, when):
    dedupe_cache.remember(
        key, f"https://example.atlassian.net/browse/{key}", 100, 5, "a bug", text,
        created_at=when.isoformat(), db_path=db,
    )


def test_near_identical_within_window_is_found(tmp_path):
    db = tmp_path / "d.duckdb"
    _remember(db, "PROJ-1", "printer catches fire when printing", _MONDAY)
    res = dedupe_cache.find_similar(
        "the printer is burning during print jobs", now=_MONDAY, db_path=db
    )
    assert res["match"] is not None
    assert res["match"]["jira_key"] == "PROJ-1"
    assert res["score"] >= 0.85


def test_different_text_returns_none(tmp_path):
    db = tmp_path / "d.duckdb"
    _remember(db, "PROJ-1", "printer catches fire when printing", _MONDAY)
    res = dedupe_cache.find_similar("user cannot log in to the portal", now=_MONDAY, db_path=db)
    # Orthogonal vectors -> score 0.0, well under the 0.85 threshold. MUTATION: set the default
    # threshold to 0.0 and this assertion goes red (0.0 would then count as a match).
    assert res["match"] is None
    assert res["score"] == 0.0


def test_identical_but_outside_window_returns_none(tmp_path):
    db = tmp_path / "d.duckdb"
    # Same text as the query, but stored on the Tuesday before the Monday -> outside the window.
    _remember(db, "PROJ-1", "printer catches fire when printing", _TUE_BEFORE)
    res = dedupe_cache.find_similar(
        "printer catches fire when printing", now=_MONDAY, db_path=db
    )
    assert res["match"] is None
    assert res["score"] is None  # nothing was even in the window


# (reference `now`, in-window record date, out-of-window record date). With 3 working days and an
# early-week reference, the cutoff always lands the weekend behind, so a naive 3-*calendar*-day
# window would wrongly drop the in-window record.
_WINDOW_CASES = [
    # from Monday: previous Wednesday is in, previous Tuesday is out.
    ("from-monday",
     datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc),
     datetime(2025, 12, 31, 9, 0, tzinfo=timezone.utc),
     datetime(2025, 12, 30, 9, 0, tzinfo=timezone.utc)),
    # from Wednesday: previous Friday is in, previous Thursday is out.
    ("from-wednesday",
     datetime(2026, 1, 7, 12, 0, tzinfo=timezone.utc),
     datetime(2026, 1, 2, 9, 0, tzinfo=timezone.utc),
     datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)),
    # from Tuesday: previous Thursday is in, previous Wednesday is out.
    ("from-tuesday",
     datetime(2026, 1, 6, 12, 0, tzinfo=timezone.utc),
     datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc),
     datetime(2025, 12, 31, 9, 0, tzinfo=timezone.utc)),
]


@pytest.mark.parametrize(
    "label,now,in_when,out_when", _WINDOW_CASES, ids=[c[0] for c in _WINDOW_CASES]
)
def test_working_day_window_decides_what_enters(tmp_path, label, now, in_when, out_when):
    """The window is decided by real remember()/find_similar(), not by date arithmetic.

    The in-window record must be found and the out-of-window one must not, from Monday,
    Wednesday and Tuesday references. Similarity is not the variable here — both records use a
    text near-identical to the query — so only the working-day window changes the outcome.
    """
    query = "the printer is burning during print jobs"

    db_in = tmp_path / f"{label}-in.duckdb"
    _remember(db_in, "PROJ-IN", "printer catches fire when printing", in_when)
    res_in = dedupe_cache.find_similar(query, now=now, db_path=db_in)
    assert res_in["match"] is not None and res_in["match"]["jira_key"] == "PROJ-IN", (
        f"{label}: the in-window record must be found"
    )

    db_out = tmp_path / f"{label}-out.duckdb"
    _remember(db_out, "PROJ-OUT", "printer catches fire when printing", out_when)
    res_out = dedupe_cache.find_similar(query, now=now, db_path=db_out)
    assert res_out["match"] is None, f"{label}: the out-of-window record must not be found"


def test_remember_twice_same_key_keeps_one_row(tmp_path):
    db = tmp_path / "d.duckdb"
    _remember(db, "PROJ-1", "first problem text", _MONDAY)
    _remember(db, "PROJ-1", "second problem text", _MONDAY)
    conn = duckdb.connect(str(db))
    try:
        count = conn.execute(
            "SELECT count(*) FROM duplicate_cache WHERE jira_key = 'PROJ-1'"
        ).fetchone()[0]
        stored = conn.execute(
            "SELECT problem_text FROM duplicate_cache WHERE jira_key = 'PROJ-1'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1
    assert stored == "second problem text"  # the upsert replaced, not appended
