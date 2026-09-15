"""The three decisions that write nothing: tagged, explained, and never silent."""

from __future__ import annotations

import pytest

import apply_link
from apply_link import DECLINED_NOTE_TEXTS, _OUTCOME_DETAIL, _close_without_writing


class FakeClient:
    def __init__(self, tag_sticks: bool = True) -> None:
        self.tags: list[str] = []
        self.tag_sticks = tag_sticks
        self.notes: list[str] = []

    def add_ticket_tags(self, ticket_id: int, tags: list[str]) -> dict:
        if self.tag_sticks:
            self.tags.extend(tags)
        return {}

    def get_ticket(self, ticket_id: int) -> dict:
        return {"tags": list(self.tags)}

    def list_ticket_notes(self, ticket_id: int) -> list[dict]:
        return [{"id": i, "body": b, "private": True} for i, b in enumerate(self.notes)]

    def add_private_note(self, ticket_id: int, body_html: str) -> dict:
        self.notes.append(body_html)
        return {"id": len(self.notes)}


DECISIONS = ["no_signals", "no_match", "ambiguous"]


def _fresh(decision: str) -> dict:
    return {"decision": decision, "reason": f"Deutscher Grund für {decision}."}


@pytest.fixture(autouse=True)
def _tags_configured(monkeypatch):
    for name in ("LINKING_TAG_NO_SIGNALS", "LINKING_TAG_NO_MATCH", "LINKING_TAG_AMBIGUOUS",
                 "LINKING_DECLINED_NOTE_ENABLED"):
        monkeypatch.delenv(name, raising=False)


class TestEveryPathIsExplicit:
    @pytest.mark.parametrize("decision", DECISIONS)
    def test_it_reports_an_outcome_and_a_detail(self, decision: str):
        """'Nothing was done' and 'the check never ran' must not look the same."""
        client, notes = FakeClient(), []
        out = _close_without_writing(client, 1, _fresh(decision), notes, dry_run=False)
        assert out["outcome"] == decision
        assert out["outcome_detail"] and out["outcome_detail"] != "kein Schreibvorgang"

    @pytest.mark.parametrize("decision", DECISIONS)
    def test_the_reason_is_german_and_travels(self, decision: str):
        assert _fresh(decision)["reason"].startswith("Deutscher Grund")
        assert decision in _OUTCOME_DETAIL


class TestTagging:
    @pytest.mark.parametrize("decision,expected", [
        ("no_signals", "mossy-company-no-signals"),
        ("no_match", "mossy-company-no-match"),
        ("ambiguous", "mossy-company-ambiguous"),
    ])
    def test_each_outcome_gets_its_own_tag(self, decision: str, expected: str):
        """One tag per outcome: what may be revisited later differs by outcome."""
        client, notes = FakeClient(), []
        out = _close_without_writing(client, 1, _fresh(decision), notes, dry_run=False)
        assert out["tag"] == expected
        assert out["tagged"] is True
        assert client.tags == [expected]

    def test_no_outcome_uses_the_linked_tag(self):
        """Tagging a declined ticket as linked would claim something that did not happen."""
        for decision in DECISIONS:
            client, notes = FakeClient(), []
            out = _close_without_writing(client, 1, _fresh(decision), notes, dry_run=False)
            assert out["tag"] != "mossy-company-linked"

    def test_a_tag_that_does_not_stick_is_reported(self):
        """Otherwise the ticket is silently re-evaluated forever."""
        client, notes = FakeClient(tag_sticks=False), []
        out = _close_without_writing(client, 1, _fresh("no_match"), notes, dry_run=False)
        assert out["tagged"] is False
        assert any("not confirmed" in n for n in notes)

    def test_dry_run_writes_nothing(self):
        client, notes = FakeClient(), []
        out = _close_without_writing(client, 1, _fresh("no_match"), notes, dry_run=True)
        assert client.tags == [] and out["tagged"] is False

    def test_emptying_the_variable_falls_back_to_the_default(self, monkeypatch):
        """Fail-safe, not fail-closed: an empty tag would re-evaluate the ticket forever.

        Unlike LINKING_GROUP_IDS, where empty deliberately means no group, an empty tag has no
        useful meaning — it only removes the thing that stops the loop. The default stands.
        """
        monkeypatch.setenv("LINKING_TAG_NO_MATCH", "")
        client, notes = FakeClient(), []
        out = _close_without_writing(client, 1, _fresh("no_match"), notes, dry_run=False)
        assert out["tag"] == "mossy-company-no-match"
        assert out["tagged"] is True

    def test_a_renamed_tag_is_honoured(self, monkeypatch):
        monkeypatch.setenv("LINKING_TAG_NO_MATCH", "kundendienst-keine-firma")
        client, notes = FakeClient(), []
        out = _close_without_writing(client, 1, _fresh("no_match"), notes, dry_run=False)
        assert out["tag"] == "kundendienst-keine-firma"
        assert client.tags == ["kundendienst-keine-firma"]


class TestTheNoteStaysOff:
    """The wording is not approved. Nothing may reach a live ticket until it is."""

    @pytest.mark.parametrize("decision", DECISIONS)
    def test_nothing_is_posted_by_default(self, decision: str):
        client, notes = FakeClient(), []
        out = _close_without_writing(client, 1, _fresh(decision), notes, dry_run=False)
        assert out["noted"] is False
        assert client.notes == [], "an unapproved note reached a ticket"
        assert any("not approved" in n for n in notes)

    @pytest.mark.parametrize("decision", DECISIONS)
    def test_the_path_works_once_switched_on(self, decision: str, monkeypatch):
        monkeypatch.setenv("LINKING_DECLINED_NOTE_ENABLED", "true")
        client, notes = FakeClient(), []
        out = _close_without_writing(client, 1, _fresh(decision), notes, dry_run=False)
        assert out["noted"] is True
        assert f"Deutscher Grund für {decision}." in client.notes[0]

    def test_switched_on_it_still_posts_only_once(self, monkeypatch):
        monkeypatch.setenv("LINKING_DECLINED_NOTE_ENABLED", "true")
        client, notes = FakeClient(), []
        _close_without_writing(client, 1, _fresh("no_match"), notes, dry_run=False)
        _close_without_writing(client, 1, _fresh("no_match"), [], dry_run=False)
        assert len(client.notes) == 1

    def test_each_outcome_has_its_own_marker(self, monkeypatch):
        """A declined note must not be mistaken for the linking note, or for another outcome."""
        markers = [marker for marker, _body in DECLINED_NOTE_TEXTS.values()]
        assert len(set(markers)) == len(markers)
        assert apply_link.NOTE_MARKER not in markers

    def test_a_declined_note_does_not_block_the_linking_note(self, monkeypatch):
        """A ticket declined today may be linked tomorrow; it must still get its note."""
        monkeypatch.setenv("LINKING_DECLINED_NOTE_ENABLED", "true")
        client, notes = FakeClient(), []
        _close_without_writing(client, 1, _fresh("no_match"), notes, dry_run=False)
        posted = apply_link._post_private_note(client, 1, "Muster AG", [])
        assert posted["posted"] is True
        assert len(client.notes) == 2


class TestIdempotencyAcrossOutcomes:
    def test_every_outcome_tag_counts_as_processed(self):
        """A declined ticket must not be re-evaluated on every pass of the duty."""
        from _config import all_processed_tags, outcome_tags, processed_tag
        assert processed_tag() in all_processed_tags()
        for tag in outcome_tags().values():
            assert tag in all_processed_tags()
