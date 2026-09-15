"""The decision branches nothing reached, and the gate that refuses a stale decision."""

from __future__ import annotations

import pytest

from apply_link import _agrees
from link_decision import decide


class TestBranchesThatStopEarly:
    def test_a_contact_with_a_company_is_left_alone(self):
        """This skill fills a gap; it does not correct somebody's work."""
        result = decide({"verified": {"emailDomain": "muster.example.ch"}, "candidates": [], "searched": ["listing"],
                         "linked": True, "company_id": 5, "company_name": "Andere AG"})
        assert result["decision"] == "already_linked"
        assert result["company_id"] == 5
        assert "bereits" in result["reason"]

    def test_company_id_alone_is_enough_to_stop(self):
        result = decide({"verified": {"emailDomain": "muster.example.ch"}, "company_id": 5,
                         "candidates": [], "searched": ["listing"]})
        assert result["decision"] == "already_linked"

    def test_a_ticket_outside_the_groups_is_not_touched(self):
        result = decide({"verified": {"emailDomain": "muster.example.ch"}, "candidates": [], "searched": ["listing"],
                         "in_scope": False})
        assert result["decision"] == "out_of_scope"
        assert "Gruppen" in result["reason"]

    def test_already_linked_wins_over_out_of_scope(self):
        """Order matters only in that neither may write; both must stop."""
        result = decide({"verified": {}, "candidates": [], "searched": ["listing"], "linked": True, "in_scope": False})
        assert result["decision"] in ("already_linked", "out_of_scope")
        assert "create" not in result


class TestNoMatchHasTwoReasons:
    def test_creation_switched_off_says_so(self, monkeypatch):
        monkeypatch.setenv("LINKING_ALLOW_COMPANY_CREATION", "false")
        result = decide({"verified": {"companyNameCandidates": ["Neu AG"],
                                      "emailDomain": "neu-ag.example.ch"}, "candidates": [], "searched": ["listing"]})
        assert result["decision"] == "no_match"
        assert "nicht aktiv" in result["reason"]
        assert "create" not in result

    def test_no_usable_name_says_so(self):
        """A domain alone would create a company named after a mailbox."""
        result = decide({"verified": {"emailDomain": "neu-ag.example.ch"}, "candidates": [], "searched": ["listing"]})
        assert result["decision"] == "no_match"
        assert "Firmenname" in result["reason"]
        assert "create" not in result

    def test_the_two_situations_read_differently(self, monkeypatch):
        """A person reading the note has to be able to tell which one happened.

        Creation being off is checked first and on purpose: when nothing may be created, the
        absence of a name is beside the point, so that case reports the switch and not the
        name. The two reasons therefore belong to two configurations, not to one.
        """
        named = {"verified": {"companyNameCandidates": ["Neu AG"], "emailDomain": "neu-ag.example.ch"},
                 "candidates": [], "searched": ["listing"]}
        unnamed = {"verified": {"emailDomain": "neu-ag.example.ch"}, "candidates": [], "searched": ["listing"]}

        monkeypatch.setenv("LINKING_ALLOW_COMPANY_CREATION", "false")
        switch_off = decide(named)["reason"]
        monkeypatch.setenv("LINKING_ALLOW_COMPANY_CREATION", "true")
        no_name = decide(unnamed)["reason"]

        assert switch_off != no_name
        assert "nicht aktiv" in switch_off and "Firmenname" in no_name

    def test_candidates_below_the_threshold_are_reported(self):
        """Otherwise 'no match' hides that something was close."""
        result = decide({"verified": {"companyNameCandidates": ["Neu AG"]},
                         "candidates": [{"company_id": 1, "company_name": "Nah AG",
                                         "score": 0.6}], "searched": ["listing"]})
        assert any("none reached" in note for note in result["notes"])

    def test_a_candidate_below_the_threshold_never_links(self):
        result = decide({"verified": {"companyNameCandidates": ["Neu AG"]},
                         "candidates": [{"company_id": 1, "company_name": "Nah AG",
                                         "score": 0.89}], "searched": ["listing"]})
        assert result["decision"] != "link"


class TestTheThresholdBoundary:
    def test_exactly_at_the_threshold_links(self):
        result = decide({"verified": {"emailDomain": "x.example.ch"},
                         "candidates": [{"company_id": 1, "company_name": "X AG", "score": 0.9}], "searched": ["listing"]})
        assert result["decision"] == "link"

    def test_just_below_does_not(self):
        result = decide({"verified": {"emailDomain": "x.example.ch"},
                         "candidates": [{"company_id": 1, "company_name": "X AG",
                                         "score": 0.8999}], "searched": ["listing"]})
        assert result["decision"] != "link"

    def test_three_above_the_threshold_is_still_ambiguous(self):
        result = decide({"verified": {"emailDomain": "x.example.ch"}, "candidates": [
            {"company_id": i, "company_name": f"X{i} AG", "score": 0.95} for i in range(3)], "searched": ["listing"]})
        assert result["decision"] == "ambiguous"
        assert len(result["candidates_above_threshold"]) == 3
        assert "company_id" not in result


class TestTheReDerivationGate:
    """What may be written is decided twice, and both times must agree."""

    def test_a_changed_decision_is_refused(self):
        agrees, why = _agrees({"decision": "create"}, {"decision": "link"})
        assert agrees is False and "no longer holds" in why

    def test_the_same_decision_agrees(self):
        agrees, _ = _agrees({"decision": "link", "company_id": 7},
                            {"decision": "link", "company_id": 7})
        assert agrees is True

    def test_the_same_decision_about_a_different_company_is_refused(self):
        """The dangerous near-miss: right verb, wrong object."""
        agrees, why = _agrees({"decision": "link", "company_id": 7},
                              {"decision": "link", "company_id": 8})
        assert agrees is False and why

    def test_a_different_name_to_create_is_refused(self):
        agrees, why = _agrees({"decision": "create", "create": {"name": "Muster AG"}},
                              {"decision": "create", "create": {"name": "Andere AG"}})
        assert agrees is False and why
