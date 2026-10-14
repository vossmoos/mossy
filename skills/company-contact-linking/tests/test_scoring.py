"""The scoring scale: what makes a company a candidate, and what it is worth.

This is the function that decides, on real data, who a contact works for. Every value here is
the one documented in references/matching-rules.md; if the table and this file disagree, one of
them is wrong and it matters.
"""

from __future__ import annotations

import pytest

from _config import scores
from match_company import _company_domains, _company_uids, _evaluate


@pytest.fixture()
def weights():
    return scores()


def company(name="Muster Sanitär AG", domains=None, **fields):
    return {"id": 1, "name": name, "domains": list(domains or []), "custom_fields": dict(fields)}


class TestTheScale:
    """The documented values. A change here is a change to who gets linked."""

    def test_a_matching_domain_is_worth_one(self, weights):
        result = _evaluate(company(domains=["muster-sanitaer.example.ch"]),
                           {"emailDomain": "muster-sanitaer.example.ch"}, weights)
        assert result["score"] == 1.0
        assert result["matched_on"] == ["corporate_email_domain"]

    def test_a_matching_uid_is_worth_one(self, weights):
        result = _evaluate(company(uid="CHE-123.456.789"),
                           {"uid": "CHE-123.456.789"}, weights)
        assert result["score"] == 1.0
        assert result["matched_on"] == ["uid_number"]

    def test_an_exact_name_is_worth_zero_point_nine(self, weights):
        result = _evaluate(company(), {"companyNameCandidates": ["Muster Sanitär AG"]}, weights)
        assert result["score"] == 0.9
        assert result["matched_on"] == ["name_exact"]

    def test_a_contained_name_is_worth_zero_point_six(self, weights):
        result = _evaluate(company(), {"companyNameCandidates": ["Muster"]}, weights)
        assert result["score"] == 0.6
        assert result["matched_on"] == ["name_contains"]

    def test_nothing_in_common_is_not_a_candidate(self, weights):
        assert _evaluate(company(), {"companyNameCandidates": ["Ganz Andere AG"]}, weights) is None
        assert _evaluate(company(), {}, weights) is None


class TestTheStrongestSignalCarriesIt:
    """The score is the maximum, never a sum — three weak hits must not clear a threshold."""

    def test_two_weak_hits_do_not_add_up(self, weights):
        """Both are partial matches — 'Muster Sanitär' would be an exact one, since the legal
        form is dropped before comparing, which is why the pair below is deliberate."""
        result = _evaluate(
            company(name="Muster Sanitär AG", domains=[]),
            {"companyNameCandidates": ["Muster", "Sanitär"]},
            weights,
        )
        assert result["matched_on"] == ["name_contains"]
        assert result["score"] == 0.6, "contained-name hits were summed"

    def test_a_weak_hit_never_lifts_a_strong_one(self, weights):
        result = _evaluate(company(domains=["muster-sanitaer.example.ch"]),
                           {"emailDomain": "muster-sanitaer.example.ch",
                            "companyNameCandidates": ["Muster"]}, weights)
        assert result["score"] == 1.0

    def test_a_strong_hit_is_not_dragged_down_by_a_weak_one(self, weights):
        result = _evaluate(company(domains=["muster-sanitaer.example.ch"]),
                           {"emailDomain": "muster-sanitaer.example.ch",
                            "companyNameCandidates": ["Muster"]}, weights)
        assert result["score"] > 0.6

    def test_the_score_is_capped_at_one(self, monkeypatch):
        monkeypatch.setenv("LINKING_SCORE_DOMAIN", "5.0")
        result = _evaluate(company(domains=["muster-sanitaer.example.ch"]),
                           {"emailDomain": "muster-sanitaer.example.ch"}, scores())
        assert result["score"] == 1.0

    def test_every_hit_is_still_reported(self, weights):
        """The note must be able to name the evidence without inventing it."""
        result = _evaluate(company(domains=["muster-sanitaer.example.ch"]),
                           {"emailDomain": "muster-sanitaer.example.ch",
                            "companyNameCandidates": ["Muster Sanitär AG"]}, weights)
        assert result["matched_on"] == ["corporate_email_domain", "name_exact"]
        assert len(result["evidence"]) == 2


class TestWhatMustNeverScore:
    def test_a_free_mail_domain_is_not_evidence_about_an_employer(self, weights):
        result = _evaluate(company(domains=["example.com"]),
                           {"emailDomain": "example.com", "emailDomainIsGeneric": True}, weights)
        assert result is None, "a free-mail domain matched a company"

    def test_a_phone_number_identifies_nobody(self, weights):
        assert _evaluate(company(), {"phones": ["+41 41 000 00 01"]}, weights) is None

    def test_an_address_alone_identifies_nobody(self, weights):
        assert _evaluate(company(), {"addresses": ["Musterstrasse 12"]}, weights) is None

    def test_a_company_with_no_name_cannot_be_matched_by_name(self, weights):
        assert _evaluate(company(name=""), {"companyNameCandidates": ["Muster"]},
                         weights) is None

    def test_an_empty_name_candidate_matches_nothing(self, weights):
        assert _evaluate(company(), {"companyNameCandidates": ["", None]}, weights) is None


class TestNormalisationInScoring:
    def test_spelling_differences_still_match(self, weights):
        for spelling in ("Muster Sanitaer AG", "muster sanitär ag", "Muster Sanitär AG.",
                         "Muster SanitärAG"):
            result = _evaluate(company(), {"companyNameCandidates": [spelling]}, weights)
            assert result is not None and result["score"] == 0.9, spelling

    def test_the_legal_form_does_not_decide_identity(self, weights):
        result = _evaluate(company(name="Muster Sanitär GmbH"),
                           {"companyNameCandidates": ["Muster Sanitär AG"]}, weights)
        assert result["score"] == 0.9, "AG and GmbH of the same name are candidates"

    def test_a_domain_matches_case_insensitively(self, weights):
        result = _evaluate(company(domains=["Muster-Sanitaer.Example.CH"]),
                           {"emailDomain": "muster-sanitaer.example.ch"}, weights)
        assert result is not None

    def test_a_uid_matches_through_punctuation(self, weights):
        result = _evaluate(company(uid="CHE 123 456 789"), {"uid": "CHE-123.456.789"}, weights)
        assert result is not None and result["score"] == 1.0


class TestWhereTheValuesAreRead:
    def test_domains_are_read_from_the_company_record(self):
        assert _company_domains({"domains": ["A.EXAMPLE.CH", "b.example.ch"]}) == ["a.example.ch", "b.example.ch"]

    def test_a_company_without_domains_yields_none(self):
        assert _company_domains({}) == []

    def test_a_uid_is_found_in_a_custom_field(self):
        assert _company_uids({"custom_fields": {"uid": "CHE-1.2.3"}})

    def test_weights_are_configurable(self, monkeypatch):
        monkeypatch.setenv("LINKING_SCORE_NAME_EXACT", "0.4")
        result = _evaluate(company(), {"companyNameCandidates": ["Muster Sanitär AG"]}, scores())
        assert result["score"] == 0.4
