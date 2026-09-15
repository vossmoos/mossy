"""The text layer, against bodies that actually arrived from Freshdesk's UI."""

from __future__ import annotations

from _text import html_to_text, quoted_split, signature_block, url_host


class TestFlattenedPlainText:
    """Freshdesk flattens every newline in description_text for UI-written tickets."""

    def test_the_fixture_really_is_flattened(self, ticket_64):
        assert "\n" not in ticket_64["description_text"]
        assert "Von: Dritte Firma AG" in ticket_64["description_text"]

    def test_quote_marker_is_found_mid_line(self, ticket_64):
        own, quoted = quoted_split(ticket_64["description_text"])
        assert quoted.startswith("Von: Dritte Firma AG")
        assert "Dritte Firma" not in own

    def test_the_other_companys_phone_is_not_the_senders(self, ticket_64):
        """The failure this cost: a quoted firm's phone attributed to the writer."""
        own, quoted = quoted_split(ticket_64["description_text"])
        assert "+41 44 999 99 99" in quoted
        assert "+41 44 999 99 99" not in own

    def test_signature_is_found_without_line_breaks(self, ticket_65):
        signature = signature_block(ticket_65["description_text"])
        assert "SAXAG Test AG" in signature
        assert "Dritte Firma AG" not in signature


class TestHtmlConversion:
    def test_line_structure_survives(self, ticket_64):
        text = html_to_text(ticket_64["description"])
        assert text.count("\n") > 5
        own, quoted = quoted_split(text)
        assert quoted.startswith("Von: Dritte Firma AG")

    def test_words_on_neighbouring_lines_are_not_glued(self, ticket_64):
        """'SAXAG Test AGLuzernstrasse 12' would defeat every name comparison."""
        text = html_to_text(ticket_64["description"])
        assert "AGLuzernstrasse" not in text
        assert "SAXAG Test AG" in text
        assert "Luzernstrasse 12" in text

    def test_a_tag_never_disappears_without_a_separator(self):
        """Block tags become line breaks, inline tags become a space. Never nothing."""
        lines = [l for l in html_to_text("<div>Muster AG</div><div>Musterweg 1</div>").splitlines() if l]
        assert lines == ["Muster AG", "Musterweg 1"]
        # An inline tag must not glue its neighbours either; a blank line between blocks is fine.
        assert html_to_text("Muster<span>AG</span>") == "Muster AG"

    def test_signature_from_html(self, ticket_64):
        signature = signature_block(html_to_text(ticket_64["description"]))
        assert signature.startswith("Jonas Beispiel")
        assert "SAXAG Test AG" in signature
        assert "Dritte Firma AG" not in signature


class TestUrlHost:
    def test_markdown_link_becomes_a_bare_host(self):
        """Freshdesk renders anchors as markdown in plain text; a company domain is a host."""
        assert url_host("[www.saxag.example.ch](https://www.saxag.example.ch)") == "saxag.example.ch"

    def test_scheme_www_path_and_punctuation_are_stripped(self):
        assert url_host("https://www.muster.example.ch/kontakt?x=1") == "muster.example.ch"
        assert url_host("http://muster.example.ch.") == "muster.example.ch"
        assert url_host("<https://muster.example.ch:8443/a>") == "muster.example.ch"

    def test_a_bare_host_survives_untouched(self):
        assert url_host("saxag.example.ch") == "saxag.example.ch"

    def test_empty_stays_empty(self):
        assert url_host(None) == ""
        assert url_host("   ") == ""
