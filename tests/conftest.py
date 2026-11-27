"""Shared test setup: the repo on the path and no real OpenAI key."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(autouse=True)
def _never_openai(monkeypatch):
    """No test reaches OpenAI: without a key the real ``_chat`` refuses before it builds a request,
    so a forgotten fake fails loudly instead of spending money."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _duckdb_in_tmp(monkeypatch, tmp_path):
    """No test writes ``data/mossy.duckdb``: the HITL store goes to a file of the test's own."""
    monkeypatch.setenv("MOSSY_DUCKDB_PATH", str(tmp_path / "mossy.duckdb"))
