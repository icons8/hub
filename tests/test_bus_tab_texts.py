"""The words of the tab Settings → External hubs (the hub bus): a dash is a punctuation of a terminal, not of a page, so
no string of the tab has one in any language; one question has one label by its place (who the invitation is for, who
sent it, whose hub this is); a pair code has two names, not three."""
import tests  # noqa: F401 — keep the machine's settings out
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHES = "‒–—―"
SOURCE = os.path.join(ROOT, "modules", "web", "static", "js", "hubs.js")


def catalog(language):
    with open(os.path.join(ROOT, "i18n", language + ".json"), encoding="utf-8") as f:
        return json.load(f)


def languages():
    with open(os.path.join(ROOT, "i18n", "stage.json"), encoding="utf-8") as f:
        return json.load(f)["ready"]


def texts(language, prefix="web.bus."):
    """Every text of the tab in a language, plural forms too: [(key, text)]."""
    out = []
    for key, value in catalog(language).items():
        if key.startswith(prefix):
            out.extend((key, v) for v in (value.values() if isinstance(value, dict) else [value]))
    return out


class BusTabTexts(unittest.TestCase):
    def test_no_language_has_a_dash_in_a_text_of_the_tab(self):
        found = {}
        for language in languages():
            bad = [k for k, v in texts(language) if any(d in v for d in DASHES)]
            if bad:
                found[language] = sorted(set(bad))
        self.assertEqual(found, {})

    def test_the_three_places_of_a_contact_have_three_different_labels(self):
        for language in languages():
            cat = catalog(language)
            labels = [cat["web.bus." + k] for k in ("person_invite", "person_from", "person_title")]
            self.assertEqual(len(set(labels)), 3, (language, labels))
        en = catalog("en")
        self.assertEqual([en["web.bus." + k] for k in ("person_invite", "person_from", "person_title")],
                         ["Who is the invitation for", "Who sent it", "Contact of this hub"])

    def test_the_empty_choice_of_a_contact_is_a_phrase_not_a_dash(self):
        self.assertEqual(catalog("en")["web.bus.person_nobody"], "Not sure yet")
        self.assertEqual(catalog("en")["web.bus.person_hint"],
                         "Pick a person from your contacts, so your liaison knows who writes. If you skip it, the liaison "
                         "treats the hub as a new person")

    def test_every_form_of_the_page_names_its_contact_field_by_its_place(self):
        with open(SOURCE, encoding="utf-8") as f:
            src = f.read()
        sites = [m for m in re.findall(r"personField\([^\n]*", src) if not m.startswith("personField(o,")]
        self.assertEqual(len(sites), 3, sites)
        for key in ("person_invite", "person_from", "person_title"):
            self.assertEqual(sum(f'label: t("web.bus.{key}")' in s for s in sites), 1, (key, sites))
        # the select is named by the same label a person reads above it
        head = src.split("function personField", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('sel.setAttribute("aria-label", label)', head)

    def test_a_pair_code_has_two_names_and_the_button_names_them(self):
        en = catalog("en")
        self.assertEqual(en["web.bus.code"], "Pair code")
        self.assertEqual(en["web.bus.me_code"], "This hub's code: {code}")
        self.assertEqual(en["web.bus.matches"], "Codes match")
        self.assertEqual([en["web.bus.verified"], en["web.bus.unverified"]], ["Code checked", "Code not checked"])
        old = ("Link code", "Hub check code", "«Matches»", "Hubs page", "code compared")
        left = [(k, w) for k, v in texts("en") + texts("en", "bus.flow.code.") for w in old if w in v]
        self.assertEqual(left, [])


if __name__ == "__main__":
    unittest.main()
