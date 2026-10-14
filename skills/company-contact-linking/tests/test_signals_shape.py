"""What counts as a signals payload — the one definition both readers share."""

from __future__ import annotations

import pytest

from _signals import KNOWN_SIGNAL_FIELDS, SignalsShapeError, signals_from
from link_decision import _verified, decide
from match_company import _load_signals


class TestShapesAccepted:
    """Every shape this flow actually produces has to be understood by both readers."""

    @pytest.mark.parametrize(
        "payload",
        [
            {"verified": {"uid": "CHE-1"}},                        # verify_signals.py
            {"signals": {"uid": "CHE-1"}},                         # extract_company_signals.py
            {"signals": {"verified": {"uid": "CHE-1"}}},           # a nested envelope
            {"uid": "CHE-1"},                                      # assembled by hand
        ],
    )
    def test_the_signals_are_found(self, payload: dict):
        assert signals_from(payload) == {"uid": "CHE-1"}

    def test_the_extractor_envelope_is_understood(self):
        """The exact shape that silently produced zero candidates."""
        payload = {"ok": True, "ticket_id": 79, "provenance": {},
                   "signals": {"companyNameCandidates": ["Seebad TestAG"]}}
        assert signals_from(payload) == {"companyNameCandidates": ["Seebad TestAG"]}

    def test_an_empty_envelope_is_an_answer_not_an_error(self):
        """Nothing survived the gate is a real result about the ticket."""
        assert signals_from({"verified": {}}) == {}

    def test_verified_wins_over_raw_signals(self):
        payload = {"verified": {"uid": "proven"}, "signals": {"uid": "raw"}}
        assert signals_from(payload)["uid"] == "proven"

    def test_both_readers_agree_on_every_shape(self):
        """The drift that caused this: two readers, two ideas of what is valid."""
        for payload in ({"verified": {"uid": "X"}}, {"signals": {"uid": "X"}},
                        {"signals": {"verified": {"uid": "X"}}}, {"uid": "X"}):
            assert _verified(payload) == _load_signals(__import__("json").dumps(payload), None)


class TestUnreadableRaises:
    """'I found nothing' and 'I could not read you' must not look the same."""

    @pytest.mark.parametrize(
        "payload",
        [
            {"ok": True, "ticket_id": 79},        # an envelope with the signals left out
            {},                                    # nothing at all
            {"candidates": [], "searched": ["listing"]},                    # the other half of a decision payload
            {"regions": {"own_text": "…"}},        # the extractor's regions, without signals
        ],
    )
    def test_it_raises_instead_of_returning_empty(self, payload: dict):
        with pytest.raises(SignalsShapeError):
            signals_from(payload)

    @pytest.mark.parametrize("payload", ["a string", 42, None, ["a", "list"]])
    def test_a_non_object_raises(self, payload: object):
        with pytest.raises(SignalsShapeError):
            signals_from(payload)

    def test_the_error_names_what_arrived(self):
        with pytest.raises(SignalsShapeError) as exc:
            signals_from({"ok": True, "ticket_id": 79})
        assert "ticket_id" in str(exc.value), "the message must show what was actually passed"

    def test_the_decision_refuses_rather_than_saying_no_match(self):
        """no_match is a claim about the ticket; an unreadable argument is not."""
        with pytest.raises(SignalsShapeError):
            decide({"ok": True, "ticket_id": 79, "candidates": [], "searched": ["listing"]})

    def test_an_empty_signal_set_still_reaches_a_decision(self):
        """The honest empty case must keep working, or this fix broke the normal path."""
        assert decide({"verified": {}, "candidates": [], "searched": ["listing"]})["decision"] == "no_signals"


class TestTheKnownFields:
    def test_the_documented_schema_is_covered(self):
        for field in ("emailDomain", "urls", "uid", "phones", "addresses", "companyEmails",
                      "companyNameCandidates", "legalForm", "departmentLine"):
            assert field in KNOWN_SIGNAL_FIELDS, f"{field} is in the schema but not recognised"

    def test_a_flat_payload_of_only_judgement_is_still_signals(self):
        """The reader emits these; they are carried, not acted on."""
        assert signals_from({"confidence": "high"}) == {"confidence": "high"}
