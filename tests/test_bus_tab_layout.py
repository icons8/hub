"""The layout of the tab Settings → External hubs (the hub bus), #769 change 2: two cards that are always there, in the
first the fields come before the button; the invitation just made stays in its card; the name is one line and the mode
is in «Advanced». The browser side of it is in test_web_e2e (HubBus and the tab's tests); here the texts the cards say and
the order the page draws them in."""
import tests  # noqa: F401 — keep the machine's settings out
import json
import os
import unittest

CSS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "modules", "web", "static", "css", "hubs.css")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "modules", "web", "static", "js", "hubs.js")
NEW = ("invite_card", "sign_as", "ready", "ready_how", "make_another", "name_label", "name_change", "advanced_what",
       "me_code_for", "mode_chip")


def catalog(language):
    with open(os.path.join(ROOT, "i18n", language + ".json"), encoding="utf-8") as f:
        return json.load(f)


def function(src, name):
    """The text of a top-level function of hubs.js, from its line to the closing brace at the start of a line."""
    return src.split("function " + name, 1)[1].split("\n}\n", 1)[0]


class BusTabLayout(unittest.TestCase):
    def setUp(self):
        with open(SOURCE, encoding="utf-8") as f:
            self.src = f.read()

    def test_the_cards_and_their_words_are_in_every_language(self):
        with open(os.path.join(ROOT, "i18n", "stage.json"), encoding="utf-8") as f:
            ready = json.load(f)["ready"]
        for language in ready:
            cat = catalog(language)
            for key in NEW:
                self.assertTrue(cat.get("web.bus." + key), (language, key))
        en = catalog("en")
        self.assertEqual([en["web.bus." + k] for k in ("invite_card", "paste_title", "make_another", "sign_as", "ttl_label")],
                         ["Invite a hub", "Accept an invitation", "Make another", "Sign with another name", "Valid for"])
        self.assertEqual(en["web.bus.mode_remote"], "Let linked hubs run this hub")

    def test_the_invitation_card_draws_its_fields_before_its_button(self):
        card = function(self.src, "linkPanel")
        order = [card.index(x) for x in ("personField(V.invitePerson", "ttlField()", "inviteName(v)", "row.append(make)")]
        self.assertEqual(order, sorted(order), order)
        # the term has a label to read, not only one for a screen reader
        self.assertNotIn("aria-label", function(self.src, "ttlField"))
        self.assertIn('t("web.bus.ttl_label")', function(self.src, "ttlField"))

    def test_the_accept_card_is_always_drawn_and_the_old_folding_is_gone(self):
        self.assertIn("out.push(linkPanel(v), pastePanel(v))", self.src)
        self.assertNotIn("pasteFolded", self.src)
        self.assertNotIn("pasteOpen", self.src)
        self.assertNotIn("return null", function(self.src, "pastePanel"))

    def test_the_made_invitation_is_shown_in_its_card_not_opened_in_the_list(self):
        self.assertIn("function invitationReady", self.src)
        adopt = function(self.src, "adopt")
        self.assertNotIn("V.open.add", adopt)
        self.assertIn("V.texts.set(j.done.id, j.value)", adopt)

    def test_the_mode_switch_is_in_advanced_and_the_name_is_a_line_once_it_is_given(self):
        tab = function(self.src, "tabNodes")
        self.assertNotIn("modePanel", self.src)
        self.assertIn("more.append(modeRow(v))", tab)
        self.assertLess(tab.index("more.append(modeRow(v))"), tab.index('"web.bus.advanced_text"'))
        self.assertIn("bus-name-line", function(self.src, "namePanel"))

    def test_a_field_label_is_not_bigger_than_the_title_of_its_card(self):
        # #775 review: the labels took the page's «small» size (14.93 px, bigger than the card's 14 px title in Clean)
        with open(CSS, encoding="utf-8") as f:
            css = f.read()
        rule = next(line for line in css.split("\n") if line.startswith(".bus-ttl-field > span"))
        self.assertIn("font-size: inherit", rule)
        self.assertNotIn("--text-sm", rule)


if __name__ == "__main__":
    unittest.main()
