"""The refusals and the screen-reader words of the tab Settings → External hubs (the hub bus), #769 change 3: a refusal is
said in the page's words chosen by its code (the core's text is a terminal's, it stays for the codes the page has no words
for); the tab no longer calls itself «Hubs page». The browser side (where the refusal stands, the roles) is in
test_web_e2e (the tab's tests); here the table of codes and the words."""
import tests  # noqa: F401 — keep the machine's settings out
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "modules", "web", "static", "js", "hubs.js")
KEYS = ("not_a_link", "bad_invite", "used", "expired", "network", "relay_other", "own_link", "card", "old_hub")


def catalog(language):
    with open(os.path.join(ROOT, "i18n", language + ".json"), encoding="utf-8") as f:
        return json.load(f)


def languages():
    with open(os.path.join(ROOT, "i18n", "stage.json"), encoding="utf-8") as f:
        return json.load(f)["ready"]


def table():
    """The page's table of refusal codes → catalog keys, as hubs.js writes it: {code: key}."""
    with open(SOURCE, encoding="utf-8") as f:
        body = f.read().split("const REFUSAL = {", 1)[1].split("\n};", 1)[0]
    return dict(re.findall(r'(\w+): "(web\.bus\.refuse\.\w+)"', body))


class BusTabErrors(unittest.TestCase):
    def test_the_refusal_words_and_the_switch_label_are_in_every_language(self):
        for language in languages():
            cat = catalog(language)
            for key in KEYS:
                self.assertTrue(cat.get("web.bus.refuse." + key), (language, key))
            self.assertTrue(cat.get("web.bus.off_label"), language)
        en = catalog("en")
        self.assertEqual(en["web.bus.off_label"], "Turn off external hubs")
        self.assertEqual(en["web.bus.refuse.not_a_link"], "No invitation in this text. Paste the whole invitation")
        self.assertEqual(en["web.bus.refuse.used"],
                         "This invitation was already used or revoked. If it wasn't you, tell the owner who sent it")
        self.assertEqual(en["web.bus.refuse.network"],
                         "The connection server did not answer. Nothing was lost. Try again in a minute")

    def test_the_page_words_are_the_page_s_not_a_terminal_s(self):
        # no command of the terminal, no old name of the tab, and a short text has no closing stop
        for language in languages():
            cat = catalog(language)
            for key in KEYS:
                text = cat["web.bus.refuse." + key]
                self.assertNotIn("hub bus", text, (language, key))
                self.assertNotRegex(text, r"[.。।]$", (language, key))
        self.assertEqual(sorted(set(table().values())), sorted("web.bus.refuse." + k for k in KEYS))

    def test_every_code_in_the_table_is_a_code_the_core_sends_and_the_table_names_the_main_ones(self):
        from core import busflow
        unknown = busflow.why("zz_no_such_code")
        for code in table():
            self.assertNotEqual(busflow.why(code), unknown.replace("zz_no_such_code", code), code)   # a code the core knows
        for code in ("not_a_link", "bad_link", "bad_invite", "used", "revoked", "expired", "network", "unavailable", "relay_other",
                     "own_link", "card", "old_hub"):
            self.assertIn(code, table())
        # a code the page has no words for keeps the core's text: a failed install's too (its command is the remedy)
        for code in ("no_pip", "install_failed", "keygen", "error"):
            self.assertNotIn(code, table())

    def test_the_core_text_of_a_bad_name_names_the_tab_not_a_page_that_is_gone(self):
        for language in languages():
            cat = catalog(language)
            self.assertIn(cat["web.settings.tab.hubs"], cat["contacts.name_bad"], language)
        self.assertNotIn("Hubs page", catalog("en")["contacts.name_bad"])


if __name__ == "__main__":
    unittest.main()
