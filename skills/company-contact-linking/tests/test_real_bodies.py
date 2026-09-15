"""Three more bodies written in Freshdesk's own UI, for the shapes the first two lacked.

Captured from Freshdesk like tickets 64 and 65, with names and domains replaced by invented ones,
and for the same reason: a UI-written ticket comes
back with **no newlines at all** in ``description_text``, and a hand-written fixture with tidy
line breaks tests a shape Freshdesk never sends.

* **84** — no closing, so no signature block.
* **85** — a forward inside a forward: three companies, one of them the sender's.
* **86** — one sender with a head office and a branch: two addresses, two phones.
"""

from __future__ import annotations

import pytest

from extract_company_signals import extract
from link_decision import decide
from reading_layer import as_read
from verify_signals import verify


def _extract(ticket: dict) -> dict:
    return extract(ticket, {"id": 1, "email": ticket["requester_email"]})


class TestTheseAreRealBodies:
    def test_none_of_them_carries_a_newline(self, ui_ticket):
        """The property that broke region detection. If a fixture gains newlines it was edited."""
        assert "\n" not in ui_ticket["description_text"]

    def test_the_html_is_what_carries_the_structure(self, ui_ticket):
        assert "<br>" in ui_ticket["description"] or "<div" in ui_ticket["description"]


class TestNoSignature:
    """Ticket 84. Plenty of messages have no signature; inventing one hands the matcher a
    phantom company that no gate downstream can disprove."""

    def test_the_signature_block_is_empty(self, ticket_84):
        assert _extract(ticket_84)["regions"]["signature"] == ""

    def test_the_message_is_still_read_as_the_senders_own(self, ticket_84):
        regions = _extract(ticket_84)["regions"]
        assert "Wann können Sie vorbeikommen" in regions["own_text"]
        assert regions["quoted"] == ""

    def test_nothing_is_invented_from_it(self, ticket_84):
        signals = _extract(ticket_84)["signals"]
        assert signals["phones"] == []
        assert signals["urls"] == []
        assert signals["uid"] is None
        assert not signals.get("companyNameCandidates")

    def test_the_email_domain_still_arrives(self, ticket_84):
        """It comes from the contact record, not the text, so no signature does not remove it."""
        assert _extract(ticket_84)["signals"]["emailDomain"] == "keine-signatur.example.ch"

    def test_a_corporate_domain_is_still_evidence_without_a_signature(self, ticket_84):
        """No signature does not mean no company signal.

        The sender writes from a corporate domain, which is how Freshdesk itself decides who a
        contact belongs to. There is something to search on, so this is `no_match` — the
        directory has no answer — and not `no_signals`, which claims the ticket says nothing.
        Nothing is created, because a domain alone would name a company after a mailbox.
        """
        result = _extract(ticket_84)
        checked = verify(as_read(result), result["regions"])
        decision = decide({"verified": checked["verified"], "candidates": [], "searched": ["listing"]})
        assert decision["decision"] == "no_match"
        assert "Firmenname" in decision["reason"]
        assert "create" not in decision

    def test_a_free_mail_sender_without_a_signature_is_ignored(self, ticket_84):
        """The same body from a free-mail address genuinely says nothing: ignore it."""
        result = _extract(ticket_84)
        signals = {**result["signals"], "emailDomainIsGeneric": True}
        checked = verify(as_read({"signals": signals}), result["regions"])
        assert decide({"verified": checked["verified"], "candidates": [], "searched": ["listing"]})["decision"] \
            == "no_signals"


class TestTwoNestedForwards:
    """Ticket 85. Three companies in one body; only the first is the sender's."""

    def test_the_cut_is_at_the_first_forward(self, ticket_85):
        regions = _extract(ticket_85)["regions"]
        assert regions["quoted"].startswith("Von: Zweite Firma AG")
        assert "Ursprüngliche Nachricht" in regions["quoted"], "the inner forward must be inside"

    def test_the_senders_own_company_is_in_their_own_region(self, ticket_85):
        regions = _extract(ticket_85)["regions"]
        assert "Erste Firma Test AG" in regions["signature"]
        assert "Erste Firma Test AG" in regions["own_text"]

    @pytest.mark.parametrize("stranger", ["Zweite Firma AG", "Dritte Firma GmbH"])
    def test_no_other_company_reaches_the_senders_region(self, ticket_85, stranger):
        regions = _extract(ticket_85)["regions"]
        assert stranger not in regions["own_text"]
        assert stranger not in regions["signature"]

    def test_the_quoted_phones_are_labelled_quoted(self, ticket_85):
        result = _extract(ticket_85)
        assert result["signals"]["phones"] == ["+41 31 222 22 22", "+41 41 333 33 33"]
        assert all(entry["source"] == "quoted" for entry in result["provenance"]["phones"])

    def test_the_gate_discards_all_three_strangers(self, ticket_85):
        """The reader is made to attribute every company to the sender. Six must not survive."""
        result = _extract(ticket_85)
        signals = {
            **result["signals"],
            "companyNameCandidates": ["Erste Firma Test AG", "Zweite Firma AG",
                                      "Dritte Firma GmbH"],
            "addresses": ["Erstweg 1, 8000 Zürich", "Zweitstrasse 2, 3000 Bern",
                          "Drittplatz 3, 6000 Luzern"],
        }
        checked = verify(as_read({"signals": signals}), result["regions"])

        assert checked["verified"]["companyNameCandidates"] == ["Erste Firma Test AG"]
        assert checked["verified"]["addresses"] == ["Erstweg 1, 8000 Zürich"]
        assert checked["verified"]["phones"] == []
        assert len(checked["discarded"]) == 6

    def test_every_discard_is_wrong_region_not_invention(self, ticket_85):
        """The distinction that matters: these values are in the ticket, they are somebody
        else's. Calling them invented would blame the reader for reading correctly."""
        result = _extract(ticket_85)
        signals = {**result["signals"],
                   "companyNameCandidates": ["Zweite Firma AG", "Dritte Firma GmbH"]}
        checked = verify(as_read({"signals": signals}), result["regions"])
        assert {d["code"] for d in checked["discarded"]} == {"wrong_region"}

    def test_only_the_senders_company_can_be_created(self, ticket_85):
        result = _extract(ticket_85)
        signals = {**result["signals"],
                   "companyNameCandidates": ["Erste Firma Test AG", "Zweite Firma AG"]}
        checked = verify(as_read({"signals": signals}), result["regions"])
        decision = decide({"verified": checked["verified"], "candidates": [], "searched": ["listing"]})
        assert decision["create"]["name"] == "Erste Firma Test AG"


class TestTwoLegitimateValues:
    """Ticket 86. Both belong to the sender, both pass the gate, and one gets written."""

    def test_both_phones_are_read(self, ticket_86):
        assert _extract(ticket_86)["signals"]["phones"] == [
            "+41 44 111 11 11", "+41 61 222 22 22"]

    def test_both_survive_verification(self, ticket_86):
        result = _extract(ticket_86)
        signals = {**result["signals"],
                   "companyNameCandidates": ["Doppel Haustechnik AG"],
                   "addresses": ["Zentralweg 10, 8000 Zürich", "Nebenstrasse 5, 4000 Basel"]}
        checked = verify(as_read({"signals": signals}), result["regions"])
        assert len(checked["verified"]["addresses"]) == 2
        assert len(checked["verified"]["phones"]) == 2
        assert checked["discarded"] == []

    def _decision(self, ticket_86: dict) -> dict:
        result = _extract(ticket_86)
        signals = {**result["signals"],
                   "companyNameCandidates": ["Doppel Haustechnik AG"],
                   "addresses": ["Zentralweg 10, 8000 Zürich", "Nebenstrasse 5, 4000 Basel"]}
        checked = verify(as_read({"signals": signals}), result["regions"])
        return decide({"verified": checked["verified"], "candidates": [], "searched": ["listing"]})

    def test_the_first_of_each_is_written(self, ticket_86):
        """A Freshdesk company field holds one value; writing both would change what the
        client sees in their CRM, which is not ours to decide."""
        create = self._decision(ticket_86)["create"]
        assert create["address"] == "Zentralweg 10, 8000 Zürich"
        assert create["phone"] == "+41 44 111 11 11"

    def test_dropping_the_second_is_never_silent(self, ticket_86):
        """The fourth time a verified value would have disappeared without a word."""
        notes = " ".join(self._decision(ticket_86)["notes"])
        assert "Nebenstrasse 5, 4000 Basel" in notes, "the dropped address is not named"
        assert "+41 61 222 22 22" in notes, "the dropped phone is not named"

    def test_the_note_says_how_many_and_which_one_was_written(self, ticket_86):
        notes = self._decision(ticket_86)["notes"]
        address_note = next(n for n in notes if "address" in n)
        assert "2 verified" in address_note
        assert "'Zentralweg 10, 8000 Zürich' was written" in address_note

    def test_a_single_value_produces_no_note(self, ticket_86):
        """The note must mean something; one on every ticket would mean nothing."""
        result = _extract(ticket_86)
        checked = verify(
            as_read({"signals": {**result["signals"],
                                 "companyNameCandidates": ["Doppel Haustechnik AG"],
                                 "addresses": ["Zentralweg 10, 8000 Zürich"],
                                 "phones": ["+41 44 111 11 11"]}}),
            result["regions"],
        )
        decision = decide({"verified": checked["verified"], "candidates": [], "searched": ["listing"]})
        assert not [n for n in decision["notes"] if "verified" in n]


class TestTheNoteReachesTheResult:
    """A note that dies inside the decision object is the same silence, one level up."""

    def test_it_survives_into_the_written_result(self, account, run_apply, ticket_86):
        contact = account.add_contact("mia@doppel.example.ch")
        ticket = account.add_ticket(ticket_86["description"], contact)
        result, code = run_apply(ticket, {
            "decision": "create",
            "signals": {"readSignalsAttempted": True,
                        "emailDomain": "doppel.example.ch",
                        "companyNameCandidates": ["Doppel Haustechnik AG"],
                        "addresses": ["Zentralweg 10, 8000 Zürich",
                                      "Nebenstrasse 5, 4000 Basel"],
                        "phones": ["+41 44 111 11 11", "+41 61 222 22 22"]},
            "reason": "Grund.", "create": {"name": "Doppel Haustechnik AG"},
            "company_name": "Doppel Haustechnik AG"})

        assert code == 0 and result["created"] is True
        notes = " ".join(result["notes"])
        assert "Nebenstrasse 5, 4000 Basel" in notes
        assert account.companies[result["company_id"]]["custom_fields"]["mossy_address"] \
            == "Zentralweg 10, 8000 Zürich"
