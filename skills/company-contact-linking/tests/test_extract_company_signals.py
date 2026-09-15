"""The mechanical extractor, driven by the two real UI-written tickets."""

from __future__ import annotations

from extract_company_signals import extract


def _signals(ticket: dict, email: str | None = None) -> tuple[dict, dict, dict]:
    contact = {"id": 1, "email": email or ticket["requester_email"]}
    result = extract(ticket, contact)
    return result["signals"], result["provenance"], result["regions"]


class TestRegions:
    """A value in the quoted part belongs to whoever wrote that message."""

    def test_regions_are_found_in_a_ui_written_ticket(self, ticket_64):
        _s, _p, regions = _signals(ticket_64)
        assert "SAXAG Test AG" in regions["signature"]
        assert regions["quoted"].startswith("Von: Dritte Firma AG")
        assert "Dritte Firma" not in regions["own_text"]

    def test_the_quoted_companys_phone_is_labelled_quoted(self, ticket_64):
        signals, provenance, _r = _signals(ticket_64)
        by_region = dict(zip(signals["phones"], [p["source"] for p in provenance["phones"]]))
        assert by_region["+41 41 000 00 01"] == "signature"
        assert by_region["+41 44 999 99 99"] == "quoted"

    def test_the_quoted_companys_url_is_labelled_quoted(self, ticket_65):
        signals, provenance, _r = _signals(ticket_65)
        by_region = dict(zip(signals["urls"], [p["source"] for p in provenance["urls"]]))
        assert by_region["saxag.example.ch"] == "signature"
        assert by_region["dritte-firma.example.ch"] == "quoted"


class TestValues:
    def test_urls_are_bare_hosts(self, ticket_64):
        signals, _p, _r = _signals(ticket_64)
        assert signals["urls"] == ["saxag.example.ch", "dritte-firma.example.ch"]
        for url in signals["urls"]:
            assert "[" not in url and "://" not in url and not url.startswith("www.")

    def test_uid_is_read_without_its_tax_suffix(self, ticket_64):
        signals, provenance, _r = _signals(ticket_64)
        assert signals["uid"] == "CHE-123.456.789"
        assert provenance["uid"]["source"] == "signature"

    def test_a_uid_is_never_reported_as_a_phone(self, ticket_64):
        signals, _p, _r = _signals(ticket_64)
        assert all("123.456.789" not in phone for phone in signals["phones"])

    def test_absent_uid_stays_none(self, ticket_65):
        """Ticket 65 carries no UID at all — an empty answer, not a failure."""
        signals, _p, _r = _signals(ticket_65)
        assert signals["uid"] is None


class TestProvenance:
    def test_the_email_domain_needs_no_verification(self, ticket_64):
        """It comes from the contact record, so a check against the body would discard it."""
        signals, provenance, _r = _signals(ticket_64)
        assert signals["emailDomain"] == "saxag.example.ch"
        assert provenance["emailDomain"]["verification"] == "not_required"
        assert provenance["emailDomain"]["source"] == "freshdesk_contact"

    def test_everything_read_from_the_text_needs_verification(self, ticket_64):
        _s, provenance, _r = _signals(ticket_64)
        for entry in provenance["urls"] + provenance["phones"] + [provenance["uid"]]:
            assert entry["verification"] == "required"

    def test_a_free_mail_sender_is_flagged(self, ticket_64):
        signals, _p, _r = _signals(ticket_64, email="privat.person@gmail.com")
        assert signals["emailDomain"] == "gmail.com"
        assert signals["emailDomainIsGeneric"] is True

    def test_the_read_fields_are_left_for_the_reader(self, ticket_64):
        signals, _p, _r = _signals(ticket_64)
        assert signals["companyNameCandidates"] == []
        assert signals["legalForm"] is None
        assert signals["confidence"] is None
