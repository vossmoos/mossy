"""The gate, against the two tickets that were written in Freshdesk's own UI.

The case that matters: Dritte Firma AG's phone, website and name sit in the same body as the
sender's own, and a check that only asked "is this string present" would pass all three.
"""

from __future__ import annotations

import pytest

from extract_company_signals import extract
from reading_layer import as_read
from verify_signals import verify


def _extracted(ticket: dict) -> dict:
    return extract(ticket, {"id": 1, "email": ticket["requester_email"]})


def _with_read_signals(payload: dict, **read: object) -> dict:
    """Add what a reader would have added, with the provenance the reference asks for."""
    payload["signals"].update(read)
    if "companyNameCandidates" in read:
        payload["provenance"]["companyNameCandidates"] = [
            {"origin": "read", "source": "signature", "verification": "required"}
            for _ in read["companyNameCandidates"]
        ]
    return payload


def _codes(result: dict, field: str) -> list[str]:
    return [d["code"] for d in result["discarded"] if d["field"] == field]


class TestWrongRegion:
    """A value from the quoted part belongs to whoever wrote that message."""

    def test_the_quoted_companys_phone_is_discarded(self, ticket_64):
        payload = _extracted(ticket_64)
        result = verify(as_read(payload), payload["regions"])
        assert "+41 44 999 99 99" not in result["verified"]["phones"]
        assert "+41 41 000 00 01" in result["verified"]["phones"]
        assert _codes(result, "phones") == ["wrong_region"]

    def test_the_quoted_companys_url_is_discarded(self, ticket_65):
        payload = _extracted(ticket_65)
        result = verify(as_read(payload), payload["regions"])
        assert result["verified"]["urls"] == ["saxag.example.ch"]
        assert _codes(result, "urls") == ["wrong_region"]

    def test_a_company_name_read_from_a_quoted_signature_is_discarded(self, ticket_65):
        payload = _extracted(ticket_65)
        payload["signals"]["companyNameCandidates"] = ["SAXAG Test AG", "Dritte Firma AG"]
        payload["provenance"]["companyNameCandidates"] = [
            {"origin": "read", "source": "signature", "verification": "required"},
            {"origin": "read", "source": "quoted", "verification": "required"},
        ]
        result = verify(as_read(payload), payload["regions"])
        assert result["verified"]["companyNameCandidates"] == ["SAXAG Test AG"]
        discarded = [d for d in result["discarded"] if d["value"] == "Dritte Firma AG"]
        assert discarded and discarded[0]["code"] == "wrong_region"
        assert discarded[0]["detail"] == "nur im zitierten Teil der Nachricht gefunden"


class TestNotInText:
    def test_an_invented_company_name_is_discarded(self, ticket_64):
        payload = _with_read_signals(
            _extracted(ticket_64), companyNameCandidates=["Erfunden AG"]
        )
        result = verify(as_read(payload), payload["regions"])
        assert result["verified"]["companyNameCandidates"] == []
        assert _codes(result, "companyNameCandidates") == ["not_in_text"]
        entry = next(d for d in result["discarded"] if d["value"] == "Erfunden AG")
        assert entry["detail"] == "kommt im Ticket nicht vor"

    def test_a_real_name_survives_punctuation_and_accents(self, ticket_64):
        payload = _with_read_signals(
            _extracted(ticket_64), companyNameCandidates=["saxag test ag."]
        )
        result = verify(as_read(payload), payload["regions"])
        assert result["verified"]["companyNameCandidates"] == ["saxag test ag."]


class TestNotRequired:
    def test_the_email_domain_survives_even_when_the_body_never_mentions_it(self, ticket_65):
        """The point of not_required: the contact record is the source, not the prose.

        This sender's domain does appear in the body (the signature carries the website), so the
        test uses a contact whose domain is nowhere in the text — the case where a text check
        would throw away the strongest signal the skill has.
        """
        payload = extract(ticket_65, {"id": 1, "email": "jonas@nirgendwo-erwaehnt.example.ch"})
        assert "nirgendwo-erwaehnt" not in payload["regions"]["own_text"].lower()
        result = verify(as_read(payload), payload["regions"])
        assert result["verified"]["emailDomain"] == "nirgendwo-erwaehnt.example.ch"
        assert not [d for d in result["discarded"] if d["field"] == "emailDomain"]

    def test_a_free_mail_domain_is_still_not_checked(self, ticket_64):
        payload = extract(ticket_64, {"id": 1, "email": "privat.person@gmail.com"})
        result = verify(as_read(payload), payload["regions"])
        assert result["verified"]["emailDomain"] == "gmail.com"
        assert result["verified"]["emailDomainIsGeneric"] is True


    def test_the_exemption_does_not_depend_on_provenance(self, ticket_65):
        """A flat signals object with no provenance is a normal way to call this.

        Measured: without this, a caller passing {"emailDomain": "…"} on its own had the
        domain checked against the body and discarded when the message never mentioned it.
        """
        payload = {"signals": {"emailDomain": "nirgendwo-erwaehnt.example.ch"}}
        regions = {"own_text": "Guten Tag", "signature": "", "quoted": ""}
        result = verify(as_read(payload), regions)
        assert result["verified"]["emailDomain"] == "nirgendwo-erwaehnt.example.ch"
        assert result["discarded"] == []


class TestJudgementFields:
    def test_confidence_and_counter_signals_are_carried_but_not_evidence(self, ticket_64):
        payload = _extracted(ticket_64)
        payload["signals"].update(
            confidence="high",
            counterSignals=["third_party_mention"],
            readSignals=["signature_company_name"],
            reason="Die Signatur nennt „SAXAG Test AG“.",
        )
        result = verify(as_read(payload), payload["regions"])
        for field in ("confidence", "counterSignals", "readSignals", "reason"):
            assert field in result["unverified"]
            assert field not in result["verified"]
        assert result["unverified"]["confidence"] == "high"

    def test_they_are_never_discarded_for_not_being_in_the_text(self, ticket_64):
        payload = _extracted(ticket_64)
        payload["signals"]["confidence"] = "low"
        result = verify(as_read(payload), payload["regions"])
        assert not [d for d in result["discarded"] if d["field"] == "confidence"]


class TestShape:
    def test_the_uid_survives_when_it_is_in_the_signature(self, ticket_64):
        payload = _extracted(ticket_64)
        result = verify(as_read(payload), payload["regions"])
        assert result["verified"]["uid"] == "CHE-123.456.789"

    def test_an_absent_value_is_not_a_discard(self, ticket_65):
        """Ticket 65 has no UID at all; null passes through without a finding."""
        payload = _extracted(ticket_65)
        result = verify(as_read(payload), payload["regions"])
        assert result["verified"]["uid"] is None
        assert not [d for d in result["discarded"] if d["field"] == "uid"]

    def test_the_gate_does_not_judge_which_company_is_right(self, ticket_64):
        """Both names are the sender's own here; choosing between them is not this step's job."""
        payload = _extracted(ticket_64)
        payload["signals"]["companyNameCandidates"] = ["SAXAG Test AG", "Abteilung Einkauf"]
        payload["provenance"]["companyNameCandidates"] = [
            {"origin": "read", "source": "signature", "verification": "required"}
        ] * 2
        result = verify(as_read(payload), payload["regions"])
        assert result["verified"]["companyNameCandidates"] == ["SAXAG Test AG", "Abteilung Einkauf"]
