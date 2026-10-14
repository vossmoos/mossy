"""The paths that change Freshdesk, driven through apply_link.main end to end.

These were the whole untested half of the writer. A mistake in any of them leaves something
permanent behind: a contact attached to the wrong company, a company record nobody asked for,
a customer's log field overwritten.
"""

from __future__ import annotations

import pytest

SIGNATURE = (
    "<div>Guten Tag<br><br>Bitte um eine Offerte.<br><br>Freundliche Grüsse<br>"
    "Anna Muster<br>Muster Sanitär AG<br>Musterstrasse 12<br>6000 Luzern<br>"
    "Tel. +41 41 000 00 01<br>info@muster-sanitaer.example.ch<br>UID: CHE-123.456.789</div>"
)

SIGNALS = {
    "readSignalsAttempted": True,
    "emailDomain": "muster-sanitaer.example.ch",
    "companyNameCandidates": ["Muster Sanitär AG"],
    "addresses": ["Musterstrasse 12, 6000 Luzern"],
    "companyEmails": ["info@muster-sanitaer.example.ch"],
    "phones": ["+41 41 000 00 01"],
    "uid": "CHE-123.456.789",
}


def _ticket(account, *, company: int | None = None, tags=None, **kw):
    contact = account.add_contact("anna@muster-sanitaer.example.ch", company_id=company)
    return account.add_ticket(SIGNATURE, contact, tags=tags or [], **kw), contact


def _decision(kind: str, **extra) -> dict:
    return {"decision": kind, "signals": SIGNALS, "reason": "Grund.", **extra}


class TestCreate:
    def test_it_creates_the_company_and_links_the_contact(self, account, run_apply):
        ticket, contact = _ticket(account)
        result, code = run_apply(ticket, _decision(
            "create", create={"name": "Muster Sanitär AG"}, company_name="Muster Sanitär AG"))

        assert code == 0 and result["ok"] is True
        assert result["created"] is True and result["linked"] is True
        assert account.contacts[contact]["company_id"] == result["company_id"]
        assert account.companies[result["company_id"]]["name"] == "Muster Sanitär AG"

    def test_every_verified_detail_reaches_the_company(self, account, run_apply):
        """The address bug: verified, configured, and it never arrived."""
        ticket, _ = _ticket(account)
        result, _ = run_apply(ticket, _decision(
            "create", create={"name": "Muster Sanitär AG"}, company_name="Muster Sanitär AG"))
        fields = account.companies[result["company_id"]]["custom_fields"]
        assert fields["mossy_address"] == "Musterstrasse 12, 6000 Luzern"
        assert fields["mossy_phone"] == "+41 41 000 00 01"
        assert fields["mossy_email"] == "info@muster-sanitaer.example.ch"

    def test_the_domain_makes_it_findable_next_time(self, account, run_apply):
        ticket, _ = _ticket(account)
        result, _ = run_apply(ticket, _decision(
            "create", create={"name": "Muster Sanitär AG"}, company_name="Muster Sanitär AG"))
        assert account.companies[result["company_id"]]["domains"] == ["muster-sanitaer.example.ch"]

    def test_an_unconfigured_field_is_reported_not_dropped(self, account, run_apply, monkeypatch):
        monkeypatch.delenv("LINKING_COMPANY_FIELD_ADDRESS")
        ticket, _ = _ticket(account)
        result, _ = run_apply(ticket, _decision(
            "create", create={"name": "Muster Sanitär AG"}, company_name="Muster Sanitär AG"))
        assert "mossy_address" not in account.companies[result["company_id"]]["custom_fields"]
        assert any("no field is configured" in n for n in result["notes"])


class TestDuplicateGuard:
    """The last net before a permanent record.

    It only fires when a name match is worth **less** than the link threshold — a deployment
    that says "a name alone is not enough to link somebody" — because with the default weights
    a name match already clears the bar and the re-derivation turns the case into `link` before
    the create path is reached. That configuration is exactly where a duplicate would otherwise
    be created: the matcher will not link on the name, but the directory holds the company all
    the same.
    """

    @pytest.fixture(autouse=True)
    def _names_do_not_link(self, monkeypatch):
        monkeypatch.setenv("LINKING_SCORE_NAME_EXACT", "0.5")
        monkeypatch.setenv("LINKING_SCORE_NAME_CONTAINS", "0.4")

    def test_it_links_to_the_existing_company_instead_of_creating_a_second(
        self, account, run_apply
    ):
        """The permanent record this guard exists to prevent."""
        existing = account.add_company("Muster Sanitaer AG", ["andere.example.ch"])
        ticket, contact = _ticket(account)
        before = len(account.companies)

        result, _ = run_apply(ticket, _decision(
            "create", create={"name": "Muster Sanitär AG"}, company_name="Muster Sanitär AG"))

        assert len(account.companies) == before, "a duplicate company was created"
        assert result["ok"] is True
        assert result["created"] is False
        assert account.contacts[contact]["company_id"] == existing
        assert any("already matches" in n for n in result["notes"])

    def test_it_catches_a_glued_legal_form(self, account, run_apply):
        """The spelling the verification gate cannot tell apart."""
        existing = account.add_company("Muster Sanitär AG", ["andere.example.ch"])
        contact = account.add_contact("anna@neu.example.ch")
        ticket = account.add_ticket(
            SIGNATURE.replace("Muster Sanitär AG", "Muster SanitärAG"), contact)
        signals = {**SIGNALS, "companyNameCandidates": ["Muster SanitärAG"],
                   "emailDomain": "neu.example.ch"}
        result, _ = run_apply(ticket, {"decision": "create", "signals": signals,
                                       "reason": "Grund.",
                                       "create": {"name": "Muster SanitärAG"},
                                       "company_name": "Muster SanitärAG"})
        assert result["created"] is False
        assert result["company_id"] == existing


class TestLink:
    def test_it_links_to_the_named_company(self, account, run_apply):
        company = account.add_company("Muster Sanitär AG", ["muster-sanitaer.example.ch"])
        ticket, contact = _ticket(account)
        result, code = run_apply(ticket, _decision(
            "link", company_id=company, company_name="Muster Sanitär AG"))
        assert code == 0
        assert result["linked"] is True and result["created"] is False
        assert account.contacts[contact]["company_id"] == company

    def test_nothing_new_is_created_on_the_link_path(self, account, run_apply):
        company = account.add_company("Muster Sanitär AG", ["muster-sanitaer.example.ch"])
        ticket, _ = _ticket(account)
        before = len(account.companies)
        run_apply(ticket, _decision("link", company_id=company,
                                    company_name="Muster Sanitär AG"))
        assert len(account.companies) == before


class TestRefusals:
    def test_the_master_switch_stops_every_write(self, account, run_apply, monkeypatch):
        monkeypatch.setenv("LINKING_ENABLED", "false")
        ticket, contact = _ticket(account)
        result, code = run_apply(ticket, _decision(
            "create", create={"name": "Muster Sanitär AG"}))
        assert code != 0 and result["ok"] is False
        assert account.writes == [], "a disabled deployment wrote to Freshdesk"

    @pytest.mark.parametrize("flag", ["deleted", "spam"])
    def test_a_deleted_or_spam_ticket_is_refused(self, account, run_apply, flag):
        contact = account.add_contact("anna@muster-sanitaer.example.ch")
        ticket = account.add_ticket(SIGNATURE, contact, **{flag: True})
        result, code = run_apply(ticket, _decision(
            "create", create={"name": "Muster Sanitär AG"}))
        assert code != 0 and result["ok"] is False
        assert account.writes == []

    def test_a_stale_decision_is_refused(self, account, run_apply):
        """The gate that caught a duplicate creation twice, live, in this project."""
        company = account.add_company("Muster Sanitär AG", ["muster-sanitaer.example.ch"])
        ticket, _ = _ticket(account)
        result, code = run_apply(ticket, _decision(
            "create", create={"name": "Muster Sanitär AG"}, company_name="Muster Sanitär AG"))
        assert code != 0
        assert "no longer holds" in result["error"]
        assert result["rederived"] == "link"
        assert account.writes == []
        assert len(account.companies) == 1

    def test_a_contact_linked_meanwhile_is_left_alone(self, account, run_apply):
        other = account.add_company("Andere AG", ["andere.example.ch"])
        ticket, contact = _ticket(account, company=other)
        result, code = run_apply(ticket, _decision(
            "create", create={"name": "Muster Sanitär AG"}))
        assert account.contacts[contact]["company_id"] == other, "an existing link was changed"
        assert account.writes == []

    def test_dry_run_writes_nothing(self, account, run_apply):
        ticket, contact = _ticket(account)
        run_apply(ticket, _decision("create", create={"name": "Muster Sanitär AG"},
                                    company_name="Muster Sanitär AG"),
                  extra_argv=["--dry-run"])
        assert account.writes == []
        assert account.contacts[contact]["company_id"] is None


class TestReadBackIsTheOnlyTruth:
    def test_a_link_that_does_not_stick_is_not_reported_as_done(self, account, run_apply,
                                                                monkeypatch):
        """A 200 from Freshdesk is not the same as Freshdesk showing the link."""
        import apply_link
        company = account.add_company("Muster Sanitär AG", ["muster-sanitaer.example.ch"])
        ticket, contact = _ticket(account)

        def silent_link(contact_id, company_id):
            account.writes.append(("link_contact", "swallowed"))
            return {}

        monkeypatch.setattr(account, "link_contact_to_company", silent_link)
        result, code = run_apply(ticket, _decision(
            "link", company_id=company, company_name="Muster Sanitär AG"))
        assert code != 0 and result["ok"] is False
        assert "not confirmed" in result["error"]

    def test_a_tag_that_does_not_stick_is_not_reported_as_done(self, account, run_apply,
                                                              monkeypatch):
        company = account.add_company("Muster Sanitär AG", ["muster-sanitaer.example.ch"])
        ticket, _ = _ticket(account)
        monkeypatch.setattr(account, "add_ticket_tags", lambda *a, **k: {})
        result, code = run_apply(ticket, _decision(
            "link", company_id=company, company_name="Muster Sanitär AG"))
        assert code != 0
        assert "tag" in result["error"]
