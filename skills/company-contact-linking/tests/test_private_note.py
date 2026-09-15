"""The private note on the ticket: posted once, privately, and confirmed by reading it back."""

from __future__ import annotations

import pytest

from apply_link import NOTE_MARKER, NOTE_TEMPLATE, _post_private_note


class FakeClient:
    """Freshdesk as far as the note is concerned, including the habit that once doubled a note.

    ``add_private_note`` strips HTML comments out of the body exactly as Freshdesk does, so a
    test cannot accidentally pass by relying on a hidden marker that would not survive.
    """

    def __init__(self, existing: list[dict] | None = None, force_public: bool = False,
                 swallow: bool = False) -> None:
        self.entries = list(existing or [])
        self.force_public = force_public
        self.swallow = swallow
        self.posted: list[dict] = []

    def list_ticket_notes(self, ticket_id: int) -> list[dict]:
        return list(self.entries)

    def add_private_note(self, ticket_id: int, body_html: str) -> dict:
        import re
        body_html = re.sub(r"<!--.*?-->", "", body_html, flags=re.S)
        self.posted.append({"ticket_id": ticket_id, "body": body_html})
        if self.swallow:
            return {"id": 1}
        entry = {"id": 900 + len(self.entries), "body": body_html,
                 "private": not self.force_public}
        self.entries.append(entry)
        return entry


class TestTheText:
    def test_it_is_the_client_wording_with_the_company_in_place(self):
        body = NOTE_TEMPLATE.format(company="Muster Sanitär AG")
        assert body == (
            "Die KI hat erkannt, dass der Kontakt zur Firma Muster Sanitär AG gehört, "
            "und den Ticket-Ersteller entsprechend verknüpft."
        )

    def test_it_names_nothing_technical(self):
        """Service staff read this, not developers."""
        body = NOTE_TEMPLATE.format(company="Muster AG").lower()
        for forbidden in ("apply_link", ".py", "no_match", "link", "cf_", "mossy",
                          "score", "threshold", "tag"):
            assert forbidden not in body, f"{forbidden!r} has no business in a note for staff"


class TestPosting:
    def test_it_posts_privately_and_confirms(self):
        client, notes = FakeClient(), []
        result = _post_private_note(client, 42, "Muster AG", notes)
        assert result["posted"] is True
        assert client.posted[0]["ticket_id"] == 42
        assert "Muster AG" in client.posted[0]["body"]
        assert notes == []

    def test_a_note_that_never_arrives_is_not_reported_as_posted(self):
        client, notes = FakeClient(swallow=True), []
        result = _post_private_note(client, 42, "Muster AG", notes)
        assert result["posted"] is False
        assert result["reason"] == "not_confirmed"

    def test_a_public_note_is_a_failure_not_a_success(self):
        """Posting publicly sends the customer a message about their own record."""
        client, notes = FakeClient(force_public=True), []
        result = _post_private_note(client, 42, "Muster AG", notes)
        assert result["posted"] is False
        assert result["reason"] == "posted_publicly"


class TestIdempotency:
    def test_a_second_run_adds_nothing(self):
        client, notes = FakeClient(), []
        _post_private_note(client, 42, "Muster AG", notes)
        second = _post_private_note(client, 42, "Muster AG", [])
        assert second["posted"] is False
        assert second["reason"] == "already_noted"
        assert len(client.posted) == 1, "the ticket must not collect a second note"

    def test_recognised_even_when_the_company_name_differs(self):
        """The marker is the invariant opening, not the whole sentence."""
        client = FakeClient([{"id": 1, "private": True,
                              "body": f"<p>{NOTE_TEMPLATE.format(company='Alte AG')}</p>"}])
        result = _post_private_note(client, 42, "Neue AG", [])
        assert result["reason"] == "already_noted"
        assert client.posted == []

    def test_recognised_through_freshdesk_html_and_entities(self):
        """Freshdesk returns its own markup, not the string we sent."""
        client = FakeClient([{"id": 1, "private": True, "body":
            "<div><p>Die&nbsp;KI hat erkannt, dass der Kontakt zur <b>Firma</b> "
            "Muster AG geh&ouml;rt, und den Ticket-Ersteller entsprechend verkn&uuml;pft.</p></div>"}])
        result = _post_private_note(client, 42, "Muster AG", [])
        assert result["reason"] == "already_noted"

    def test_an_unrelated_note_does_not_block_ours(self):
        client = FakeClient([{"id": 1, "private": True,
                              "body": "<p>Kunde hat telefonisch nachgefragt.</p>"}])
        result = _post_private_note(client, 42, "Muster AG", [])
        assert result["posted"] is True

    def test_the_marker_is_not_a_hidden_comment(self):
        """HTML comments are stripped by Freshdesk, so idempotency must not depend on one."""
        assert "<!--" not in NOTE_MARKER
        assert NOTE_MARKER in NOTE_TEMPLATE.format(company="X")
