"""The decision, with attention to the fields that reach the company being created."""

from __future__ import annotations

import pytest

from link_decision import decide


def _payload(**verified) -> dict:
    base = {"emailDomain": "neu.example", "companyNameCandidates": ["Neu AG"]}
    base.update(verified)
    return {"verified": base, "candidates": [], "searched": ["listing"]}


class TestCreateBlock:
    """Every verified detail the client asked for has to arrive, not just the phone."""

    def test_plural_signals_reach_create(self):
        """What the reader actually emits: plural lists, one entry per thing it saw."""
        result = decide(_payload(
            phones=["+41 31 444 55 66"],
            addresses=["Bergstrasse 4, 3000 Bern"],
            emails=["info@neu.example"],
        ))
        assert result["decision"] == "create"
        assert result["create"]["phone"] == "+41 31 444 55 66"
        assert result["create"]["address"] == "Bergstrasse 4, 3000 Bern"
        assert result["create"]["email"] == "info@neu.example"

    def test_singular_signals_reach_create_too(self):
        """A reader that writes a scalar must not lose the value silently."""
        result = decide(_payload(
            phone="+41 31 444 55 66",
            address="Bergstrasse 4, 3000 Bern",
            companyEmail="info@neu.example",
        ))
        assert result["create"]["phone"] == "+41 31 444 55 66"
        assert result["create"]["address"] == "Bergstrasse 4, 3000 Bern"
        assert result["create"]["email"] == "info@neu.example"

    def test_the_address_bug_that_shipped(self):
        """Ticket 74: addresses verified, phone arrived, address did not."""
        result = decide(_payload(
            phones=["+41 31 444 55 66"], addresses=["Bergstrasse 4, 3000 Bern"]
        ))
        assert result["create"]["address"] == "Bergstrasse 4, 3000 Bern", (
            "a verified address must reach the company that is created from it"
        )

    def test_absent_details_are_none_not_invented(self):
        result = decide(_payload())
        assert result["create"]["phone"] is None
        assert result["create"]["address"] is None
        assert result["create"]["email"] is None

    def test_empty_entries_are_skipped(self):
        result = decide(_payload(addresses=[None, "", "Bergstrasse 4"], phones=[]))
        assert result["create"]["address"] == "Bergstrasse 4"
        assert result["create"]["phone"] is None

    def test_a_generic_domain_is_not_written_as_the_company_domain(self):
        result = decide({
            "verified": {
                "emailDomain": "example.com",
                "emailDomainIsGeneric": True,
                "companyNameCandidates": ["Neu AG"],
                "uid": "CHE-123.456.789",
            },
            "candidates": [], "searched": ["listing"],
        })
        assert result["decision"] == "create"
        assert result["create"]["domain"] is None


class TestBranches:
    def test_no_evidence_is_no_signals_and_never_create(self):
        """A phone identifies nobody, so it cannot make a ticket worth acting on."""
        result = decide({"verified": {"phones": ["+41 31 444 55 66"]}, "candidates": [], "searched": ["listing"]})
        assert result["decision"] == "no_signals"
        assert "create" not in result

    def test_the_reason_says_a_phone_was_there(self):
        """'Nothing found' must not read as 'the ticket was empty' when it was not."""
        result = decide({"verified": {"phones": ["+41 31 444 55 66"]}, "candidates": [], "searched": ["listing"]})
        assert "Telefonnummer" in result["reason"]

    def test_no_signals_and_no_match_are_different_outcomes(self):
        """One means the ticket says nothing; the other means the directory has no answer."""
        empty = decide({"verified": {}, "candidates": [], "searched": ["listing"]})
        named = decide({"verified": {"companyNameCandidates": ["Unbekannt AG"]},
                        "candidates": [], "searched": ["listing"]})
        assert empty["decision"] == "no_signals"
        assert named["decision"] in ("no_match", "create")
        assert empty["decision"] != named["decision"]

    def test_two_candidates_above_threshold_is_ambiguous(self):
        result = decide({
            "verified": {"companyNameCandidates": ["Acme"]},
            "candidates": [
                {"company_id": 1, "company_name": "Acme Inc", "score": 0.95},
                {"company_id": 2, "company_name": "Acme AG", "score": 0.9},
            ], "searched": ["listing"],
        })
        assert result["decision"] == "ambiguous"
        assert "company_id" not in result

    def test_one_candidate_above_threshold_links(self):
        result = decide({
            "verified": {"emailDomain": "acme.example.com"},
            "candidates": [{"company_id": 1, "company_name": "Acme Inc", "score": 1.0,
                            "matched_on": ["corporate_email_domain"]}],
            "searched": ["directory listing"],
        })
        assert result["decision"] == "link"
        assert result["company_id"] == 1

    def test_judgement_fields_are_never_evidence(self):
        """confidence and counterSignals cannot make an empty ticket linkable."""
        result = decide({
            "verified": {"confidence": "high", "counterSignals": [], "readSignals": ["x"]},
            "candidates": [], "searched": ["listing"],
        })
        assert result["decision"] == "no_signals"
