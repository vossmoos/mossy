"""Idempotency at every level, and the ordering that decides what a failure leaves behind."""

from __future__ import annotations

import pytest

from test_write_paths import SIGNALS, SIGNATURE, _decision


@pytest.fixture()
def linked_once(account, run_apply):
    """One completed run: company created, contact linked, log written, note posted, tagged."""
    contact = account.add_contact("anna@muster-sanitaer.example.ch")
    ticket = account.add_ticket(SIGNATURE, contact)
    result, code = run_apply(ticket, _decision(
        "create", create={"name": "Muster Sanitär AG"}, company_name="Muster Sanitär AG"))
    assert code == 0 and result["linked"] is True
    return ticket, contact, result


class TestASecondRunChangesNothing:
    def test_the_tag_stops_it_before_any_write(self, account, run_apply, linked_once):
        ticket, _contact, _first = linked_once
        account.writes.clear()
        result, code = run_apply(ticket, _decision(
            "create", create={"name": "Muster Sanitär AG"}, company_name="Muster Sanitär AG"))
        assert code == 0
        assert result["skipped"] is True and result["reason"] == "already_processed"
        assert account.writes == [], "a second pass wrote to Freshdesk"

    def test_no_second_company_no_second_note_no_second_log_line(
        self, account, run_apply, linked_once
    ):
        ticket, contact, first = linked_once
        companies, notes = len(account.companies), len(account.conversations[ticket])
        log = account.contacts[contact]["custom_fields"]["mossy_log"]
        run_apply(ticket, _decision("create", create={"name": "Muster Sanitär AG"},
                                    company_name="Muster Sanitär AG"))
        assert len(account.companies) == companies
        assert len(account.conversations[ticket]) == notes
        assert account.contacts[contact]["custom_fields"]["mossy_log"] == log


class TestEachGuardHoldsOnItsOwn:
    """The tag is the first guard. These are what still holds when it is gone."""

    def test_the_note_does_not_repeat_when_the_tag_was_removed(
        self, account, run_apply, linked_once
    ):
        ticket, contact, _ = linked_once
        account.tickets[ticket]["tags"] = []          # an agent cleared it
        account.contacts[contact]["company_id"] = None  # and unlinked the contact by hand
        notes_before = len(account.conversations[ticket])

        result, code = run_apply(ticket, _decision(
            "link", company_id=list(account.companies)[0], company_name="Muster Sanitär AG"))

        assert code == 0 and result["linked"] is True, "it must link again"
        assert len(account.conversations[ticket]) == notes_before, "a second note was posted"
        assert result["noted"] is False

    def test_the_log_line_does_not_repeat_when_the_tag_was_removed(
        self, account, run_apply, linked_once
    ):
        ticket, contact, _ = linked_once
        account.tickets[ticket]["tags"] = []
        account.contacts[contact]["company_id"] = None
        before = account.contacts[contact]["custom_fields"]["mossy_log"]

        result, _ = run_apply(ticket, _decision(
            "link", company_id=list(account.companies)[0], company_name="Muster Sanitär AG"))

        assert account.contacts[contact]["custom_fields"]["mossy_log"] == before
        assert result["logged"] is False

    def test_a_declined_tag_also_counts_as_processed(self, account, run_apply):
        contact = account.add_contact("privat@freemail.example.ch")
        ticket = account.add_ticket("<div>Meine Dusche tropft.</div>", contact,
                                    tags=["mossy-company-no-signals"])
        result, code = run_apply(ticket, _decision("no_signals"))
        assert result["skipped"] is True and result["reason"] == "already_processed"
        assert account.writes == []


class TestTheContactLogIsAnAppend:
    def test_it_keeps_what_a_person_wrote(self, account, run_apply):
        contact = account.add_contact("anna@muster-sanitaer.example.ch",
                                      mossy_log="Handnotiz: Kunde ruft morgens an.")
        ticket = account.add_ticket(SIGNATURE, contact)
        run_apply(ticket, _decision("create", create={"name": "Muster Sanitär AG"},
                                    company_name="Muster Sanitär AG"))
        log = account.contacts[contact]["custom_fields"]["mossy_log"]
        assert log.startswith("Handnotiz: Kunde ruft morgens an."), "a person's note was lost"
        assert "mit der Firma verknüpft." in log

    def test_a_contact_that_already_carries_a_line_gets_no_second_one(self, account, run_apply):
        """A second ticket from the same person must not stack the sentence."""
        contact = account.add_contact(
            "anna@muster-sanitaer.example.ch",
            mossy_log="Durch KI am [01.01.2026 / 09:00] mit der Firma verknüpft.")
        ticket = account.add_ticket(SIGNATURE, contact)
        result, _ = run_apply(ticket, _decision("create", create={"name": "Muster Sanitär AG"},
                                                company_name="Muster Sanitär AG"))
        log = account.contacts[contact]["custom_fields"]["mossy_log"]
        assert log.count("mit der Firma verknüpft.") == 1
        assert result["logged"] is False

    def test_without_a_configured_field_the_link_still_happens(self, account, run_apply,
                                                               monkeypatch):
        """The link is the job; the log is the record of it."""
        monkeypatch.delenv("LINKING_CONTACT_LOG_FIELD")
        contact = account.add_contact("anna@muster-sanitaer.example.ch")
        ticket = account.add_ticket(SIGNATURE, contact)
        result, code = run_apply(ticket, _decision("create", create={"name": "Muster Sanitär AG"},
                                                   company_name="Muster Sanitär AG"))
        assert code == 0 and result["linked"] is True
        assert result["logged"] is False
        assert any("not written" in n for n in result["notes"])


class TestWriteOrdering:
    """What a failure part-way through leaves behind."""

    def test_the_company_is_created_before_the_contact_is_linked(self, account, run_apply):
        contact = account.add_contact("anna@muster-sanitaer.example.ch")
        ticket = account.add_ticket(SIGNATURE, contact)
        run_apply(ticket, _decision("create", create={"name": "Muster Sanitär AG"},
                                    company_name="Muster Sanitär AG"))
        kinds = [kind for kind, _ in account.writes]
        assert kinds.index("create_company") < kinds.index("link_contact")

    def test_the_tag_is_written_last(self, account, run_apply):
        """The tag says 'this ticket is done'. Writing it first would strand a half-run."""
        contact = account.add_contact("anna@muster-sanitaer.example.ch")
        ticket = account.add_ticket(SIGNATURE, contact)
        run_apply(ticket, _decision("create", create={"name": "Muster Sanitär AG"},
                                    company_name="Muster Sanitär AG"))
        kinds = [kind for kind, _ in account.writes]
        assert kinds.index("tags") > kinds.index("link_contact")
        assert kinds.index("tags") > kinds.index("note")

    def test_a_failure_after_creating_leaves_no_tag_claiming_success(self, account, run_apply):
        """The ticket must come back around rather than be marked done with nothing linked."""
        from fake_freshdesk import FakeFreshdeskError
        contact = account.add_contact("anna@muster-sanitaer.example.ch")
        ticket = account.add_ticket(SIGNATURE, contact)
        account.fail_on["link_contact_to_company"] = FakeFreshdeskError("network gone")

        result, code = run_apply(ticket, _decision("create", create={"name": "Muster Sanitär AG"},
                                                   company_name="Muster Sanitär AG"))
        assert code != 0 and result["ok"] is False
        assert account.tickets[ticket]["tags"] == [], "the ticket was marked done after a failure"
        assert account.contacts[contact]["company_id"] is None

    def test_a_failure_after_creating_leaves_the_company_behind(self, account, run_apply):
        """Pinning what actually happens, because it is a permanent record.

        The company is created, the link then fails, and the company stays in the directory
        with nobody attached to it. Nothing rolls it back — Freshdesk has no transaction, and
        deleting it blind would be worse, since a person may already have opened it.

        The system recovers on the next pass rather than compounding: no tag was written, so
        the ticket comes back around, and the re-derivation finds the orphan and links to it
        instead of creating a second one. That recovery is asserted here too, because it is the
        only reason leaving the orphan is acceptable.

        The failure must say the company exists. A result reading ``created: false`` while a
        new record sits in the CRM hides the one consequence a person has to know about.
        """
        from fake_freshdesk import FakeFreshdeskError
        contact = account.add_contact("anna@muster-sanitaer.example.ch")
        ticket = account.add_ticket(SIGNATURE, contact)
        account.fail_on["link_contact_to_company"] = FakeFreshdeskError("network gone")
        result, code = run_apply(ticket, _decision("create", create={"name": "Muster Sanitär AG"},
                                                   company_name="Muster Sanitär AG"))

        orphans = [c for c in account.companies.values() if c["name"] == "Muster Sanitär AG"]
        assert len(orphans) == 1, "the company was created and left behind"

        # The failure has to report the record it left in the CRM.
        assert code != 0 and result["ok"] is False
        assert result["created"] is True, "a created company was reported as not created"
        assert result["company_id"] == orphans[0]["id"]
        assert result["company_name"] == "Muster Sanitär AG"
        assert any("still" in n and "exists" in n for n in result["notes"]), \
            "nothing told the reader a new company is sitting in the CRM"
        assert account.tickets[ticket]["tags"] == [], "so the ticket must come back around"

        # Second pass, network restored: it must link to the orphan, not create a twin.
        account.fail_on.clear()
        result, code = run_apply(ticket, _decision(
            "link", company_id=orphans[0]["id"], company_name="Muster Sanitär AG"))
        assert code == 0 and result["linked"] is True
        assert len([c for c in account.companies.values()
                    if c["name"] == "Muster Sanitär AG"]) == 1, "a twin was created on retry"

    def test_a_failure_after_creating_is_reported_not_swallowed(self, account, run_apply):
        from fake_freshdesk import FakeFreshdeskError
        contact = account.add_contact("anna@muster-sanitaer.example.ch")
        ticket = account.add_ticket(SIGNATURE, contact)
        account.fail_on["link_contact_to_company"] = FakeFreshdeskError("network gone")
        result, _ = run_apply(ticket, _decision("create", create={"name": "Muster Sanitär AG"},
                                                company_name="Muster Sanitär AG"))
        assert "network gone" in result["error"]
