"""The emoji names of the session icon search (modules/web/static/emoji/<language>.json, tools/emoji_names.py): a file for
every language of the hub, whole, and every emoji in it is one the core takes as a session icon."""
import json
import os
import unicodedata
import unittest

from core import i18n
from core.layout import icon_ok

DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "modules", "web", "static", "emoji")


def load(lang):
    with open(os.path.join(DIR, lang + ".json"), encoding="utf-8") as f:
        return json.load(f)


class EmojiNames(unittest.TestCase):
    def test_a_file_for_every_language_of_the_hub_and_its_words_are_the_languages_own(self):
        # the rocket's name: a Latin-script language — the word itself, the others — the script of the language
        latin = {"en": "rocket", "de": "rakete", "es": "cohete", "fr": "fus\u00e9e", "tr": "roket", "pt-BR": "foguete"}
        script = {"ru": "CYRILLIC", "uk": "CYRILLIC", "ar": "ARABIC", "hi": "DEVANAGARI", "ko": "HANGUL"}   # ja and zh: CJK or kana
        self.assertEqual(sorted(i18n.LANGS), sorted([*latin, *script, "ja", "zh-CN"]))
        en = list(load("en"))
        for lang in i18n.LANGS:
            with self.subTest(lang=lang):
                data = load(lang)
                name = data["\U0001F680"].split("|")[0]
                if lang in latin:
                    self.assertIn(latin[lang], data["\U0001F680"].lower())
                else:
                    letters = [unicodedata.name(ch) for ch in name if ch.isalpha()]
                    want = script.get(lang)
                    self.assertTrue(letters and all(n.startswith(want) if want else n.split()[0] in ("CJK", "KATAKANA", "HIRAGANA")
                                                    for n in letters), (lang, name))
                # the emoji of the Unicode list in its order; the language may lack a few (CLDR has no text), English has all
                self.assertEqual(list(data), [e for e in en if e in data])
                self.assertGreater(len(data), len(en) * 0.95)

    def test_every_emoji_is_one_the_core_takes_and_none_is_a_skin_tone_variant(self):
        for lang in i18n.LANGS:
            for emoji, text in load(lang).items():
                self.assertTrue(icon_ok(emoji), (lang, emoji))
                self.assertFalse(any("\U0001F3FB" <= ch <= "\U0001F3FF" for ch in emoji), (lang, emoji))
                self.assertTrue(text and "||" not in text and not text.startswith("|"), (lang, emoji, text))


if __name__ == "__main__":
    unittest.main()
