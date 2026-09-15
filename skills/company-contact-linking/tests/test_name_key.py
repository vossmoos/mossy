"""The company comparison key, and the glued legal form that used to slip past it."""

from __future__ import annotations

import pytest

from _text import name_key


class TestGluedLegalForms:
    """A missing space before the legal form must not create a second company.

    This is the case the verification gate cannot catch: compare_key drops spaces, so the
    glued and the spaced spelling look identical to it, and only this key can tell that
    "Moser Bad TestGmbH" is the company the directory already holds as "Moser Bad Test GmbH".
    """

    @pytest.mark.parametrize(
        "glued,spaced",
        [
            ("Moser Bad TestGmbH", "Moser Bad Test GmbH"),
            ("Muster SanitärAG", "Muster Sanitär AG"),
            ("Bergbad TestSA", "Bergbad Test SA"),
            ("Haus TechnikSàrl", "Haus Technik Sàrl"),
            ("Haus TechnikSARL", "Haus Technik SARL"),
            ("Bau ServiceGmbH", "Bau Service GmbH"),
            ("Alpen BadSAGL", "Alpen Bad SAGL"),
        ],
    )
    def test_glued_and_spaced_are_the_same_company(self, glued: str, spaced: str):
        assert name_key(glued) == name_key(spaced)

    def test_the_reported_case(self):
        """Reported from a real run: the glued name kept its gmbh and missed the directory."""
        assert name_key("Moser Bad TestGmbH") == "moserbadtest"

    def test_an_accented_sarl_is_recognised_as_a_legal_form(self):
        """It never was: the raw token 'sàrl' did not match 'sarl' in the list."""
        assert name_key("Haus Technik Sàrl") == "haustechnik"
        assert name_key("Haus Technik Sàrl") == name_key("Haus Technik AG")


class TestNoFalsePositives:
    """Ending in a legal form's letters is not the same as carrying one."""

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Tesco", "tesco"),          # co
            ("Cisco", "cisco"),          # co
            ("Montag", "montag"),        # ag
            ("Sonntag", "sonntag"),      # ag
            ("Zug", "zug"),              # ug
            ("Betrug", "betrug"),        # ug
            ("Prosa", "prosa"),          # sa
            ("Visa", "visa"),            # sa
            ("Sarlat", "sarlat"),        # sarl, but more letters follow
            ("Agfa", "agfa"),            # ag at the start
        ],
    )
    def test_ordinary_words_keep_every_letter(self, name: str, expected: str):
        assert name_key(name) == expected

    def test_an_all_caps_name_ending_in_ag_is_left_alone(self):
        """All capitals ending in AG's letters is a real shape in a company directory; splitting it
        would merge distinct companies."""
        assert name_key("SAXAG Test AG") == "saxagtest"
        assert name_key("SAXAG Test AG") != name_key("SAB Test AG")

    @pytest.mark.parametrize(
        "left,right",
        [
            ("Tesco", "Tes"),
            ("Montag AG", "Mont AG"),
            ("Zug Sanitär", "Z Sanitär"),
            ("Prosa GmbH", "Pro GmbH"),
        ],
    )
    def test_two_different_companies_stay_different(self, left: str, right: str):
        assert name_key(left) != name_key(right)

    def test_a_company_named_only_a_legal_form_is_not_emptied(self):
        """Stripping everything would give an empty key that matches every other empty one."""
        for name in ("Sagl", "AG", "GmbH"):
            assert name_key(name), f"{name!r} reduced to nothing"


class TestUnchangedBehaviour:
    """What the key already did, still done — the change must not move these."""

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Muster AG", "muster"),
            ("Muster GmbH", "muster"),
            ("Muster Sanitär AG", "mustersanitaer"),
            ("Muster Sanitaer AG", "mustersanitaer"),
            ("Die Muster AG", "muster"),
            ("Muster Sanitär AG.", "mustersanitaer"),
            ("", ""),
        ],
    )
    def test_known_keys(self, name: str, expected: str):
        assert name_key(name) == expected

    def test_the_whole_live_directory_keeps_its_keys(self):
        """Captured from the sandbox before the change; none of these may move."""
        directory = {
            "Acme Inc": "acmeinc",
            "Advanced Machinery": "advancedmachinery",
            "Alpenbad Test GmbH": "alpenbadtest",
            "Berg Haustechnik AG": "berghaustechnik",
            "Brändli Haustechnik AG": "braendlihaustechnik",
            "Feld Haustechnik AG": "feldhaustechnik",
            "Freshworks": "freshworks",
            "Global Learning Inc": "globallearninginc",
            "Jet Propulsion Laboratory , NASA": "jetpropulsionlaboratorynasa",
            "Neu Sanitär AG": "neusanitaer",
            "SAXAG Test AG": "saxagtest",
            "Seebad Test AG": "seebadtest",
            "Tal Sanitär AG": "talsanitaer",
            "Testfirma XYZ AG": "testfirmaxyz",
        }
        for name, expected in directory.items():
            assert name_key(name) == expected, f"{name!r} changed key"

    def test_no_two_directory_names_collide(self):
        names = ["Acme Inc", "Advanced Machinery", "Alpenbad Test GmbH", "Berg Haustechnik AG",
                 "Brändli Haustechnik AG", "Feld Haustechnik AG", "Freshworks",
                 "Global Learning Inc", "Jet Propulsion Laboratory , NASA", "Neu Sanitär AG",
                 "SAXAG Test AG", "Seebad Test AG", "Tal Sanitär AG", "Testfirma XYZ AG"]
        keys = [name_key(n) for n in names]
        assert len(set(keys)) == len(keys), "the change merged two real companies"
