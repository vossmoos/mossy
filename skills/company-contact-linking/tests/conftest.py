"""Fixtures for the linking skill's tests. Offline: no Freshdesk, no model."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_FIXTURES = Path(__file__).resolve().parent / "fixtures_freshdesk_ui_tickets.json"


@pytest.fixture(autouse=True)
def _ledger_goes_to_a_scratch_file(tmp_path_factory, monkeypatch):
    """Never write the deployment's ledger from a test.

    It is the record of what really ran, and a suite that appends to it destroys exactly the
    evidence it exists to provide — measured after a run through the duty could not be read
    apart from 1400 rows about a fake account.
    """
    import _audit
    directory = tmp_path_factory.mktemp("ledger")
    monkeypatch.setenv("LINKING_LEDGER_DIR", str(directory))
    monkeypatch.setattr(_audit, "_LOG_DIR", directory)
    monkeypatch.setattr(_audit, "_LOG_FILE", directory / "invocations.jsonl")


@pytest.fixture(autouse=True)
def _clean_linking_env(monkeypatch):
    """Run every test against the defaults, whatever the shell happens to carry.

    These scripts are configured entirely through LINKING_* variables, so a developer who
    exported one to drive a live ticket silently changes what the suite is testing — measured
    once, when an exported LINKING_GENERIC_EMAIL_DOMAINS turned two passing tests red without a
    line of code changing. A test that depends on the shell is not evidence about the code.
    Tests that need a variable set it themselves with monkeypatch.
    """
    for name in [n for n in os.environ if n.startswith("LINKING_") and n != "LINKING_LEDGER_DIR"]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def ui_tickets() -> dict:
    """Two tickets as Freshdesk returns them when they were written in its own UI.

    Captured from Freshdesk and never reformatted — only names and domains were replaced with
    invented ones: their ``description_text`` carries **no newlines at all**, which is exactly
    what broke region detection. A ticket created through the API
    does not reproduce this, so a hand-written fixture would have tested the wrong thing.
    """
    return json.loads(_FIXTURES.read_text(encoding="utf-8"))


@pytest.fixture(params=["84", "85", "86"])
def ui_ticket(request, ui_tickets: dict) -> dict:
    """Each of the three bodies captured from Freshdesk's UI for the harder shapes."""
    return ui_tickets[request.param]


@pytest.fixture()
def ticket_84(ui_tickets: dict) -> dict:
    """No closing, therefore no signature block. Nothing may be invented from it."""
    return ui_tickets["84"]


@pytest.fixture()
def ticket_85(ui_tickets: dict) -> dict:
    """A forward inside a forward: three companies, only the first one is the sender's."""
    return ui_tickets["85"]


@pytest.fixture()
def ticket_86(ui_tickets: dict) -> dict:
    """One sender, two legitimate addresses and two phones: head office and branch."""
    return ui_tickets["86"]


@pytest.fixture()
def ticket_64(ui_tickets: dict) -> dict:
    return ui_tickets["64"]


@pytest.fixture()
def ticket_65(ui_tickets: dict) -> dict:
    return ui_tickets["65"]


@pytest.fixture()
def account(monkeypatch):
    """A Freshdesk account in memory, wired into apply_link and match_company.

    Tests drive ``apply_link.main`` itself rather than its parts: the orchestration is where
    the ordering lives, and the ordering is what leaves permanent records behind.
    """
    import apply_link
    import match_company
    from fake_freshdesk import FakeFreshdesk, FakeFreshdeskError

    fake = FakeFreshdesk()

    class _Client:
        @classmethod
        def from_env(cls):
            return fake

    monkeypatch.setattr(apply_link, "FreshdeskClient", _Client)
    monkeypatch.setattr(match_company, "FreshdeskClient", _Client)
    # The writer catches FreshdeskError; the fake has to raise something it catches.
    monkeypatch.setattr(apply_link, "FreshdeskError", FakeFreshdeskError)

    monkeypatch.setenv("LINKING_ENABLED", "true")
    monkeypatch.setenv("LINKING_GROUP_IDS", "1")
    monkeypatch.setenv("LINKING_CONTACT_LOG_FIELD", "mossy_log")
    monkeypatch.setenv("LINKING_COMPANY_FIELD_ADDRESS", "mossy_address")
    monkeypatch.setenv("LINKING_COMPANY_FIELD_PHONE", "mossy_phone")
    monkeypatch.setenv("LINKING_COMPANY_FIELD_EMAIL", "mossy_email")
    return fake


@pytest.fixture()
def run_apply(account, monkeypatch, capsys):
    """Run apply_link.main against the fake account and return its parsed JSON result."""
    import apply_link

    def _run(ticket_id: int, decision: dict | None = None, *,
             signals: dict | None = None, extra_argv: list[str] | None = None):
        source = (["--signals", json.dumps(signals)] if signals is not None
                  else ["--decision", json.dumps(decision)])
        argv = ["apply_link.py", "--ticket-id", str(ticket_id), *source, *(extra_argv or [])]
        monkeypatch.setattr(sys, "argv", argv)
        code = 0
        try:
            apply_link.main()
        except SystemExit as exc:      # _fail exits non-zero after printing its JSON
            code = exc.code or 0
        out = capsys.readouterr().out
        return json.loads(out), code

    return _run
