"""The agent-instruction review button and its confirmation are translated into every language, not left in English."""
import tests  # noqa: F401
import json
import os
import re
import unittest

from core.i18n import LANGS

CATALOGS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "i18n")
KEYS = ("web.dialogs.train_ok", "web.dialogs.train_text", "web.dialogs.train_title", "web.main.train",
        "web.main.train_hint")


def load(lang):
    with open(os.path.join(CATALOGS, lang + ".json"), encoding="utf-8") as f:
        return json.load(f)


class InstructionReviewTranslated(unittest.TestCase):
    def test_every_language_has_its_own_text_for_the_button_and_the_confirmation(self):
        english = load("en")
        for lang in LANGS[2:]:
            catalog = load(lang)
            for key in KEYS:
                with self.subTest(lang=lang, key=key):
                    self.assertIn(key, catalog)
                    self.assertNotEqual(catalog[key], english[key])
                    self.assertEqual(sorted(re.findall(r"\{\w+\}", catalog[key])),
                                     sorted(re.findall(r"\{\w+\}", english[key])))


if __name__ == "__main__":
    unittest.main()
