"""Files takes the hub page's scripts (/web/js/*.js), and those take their strings from /i18n.json of the address they
came from: Files serves it itself, in the same language as the page, otherwise keys show up in place of texts."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import unittest
from unittest import mock

from core import i18n
from tests.fakes import lang
from tests.test_files import FilesTest


class FilesStrings(FilesTest):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ)   # without the language pinned for tests (tests/__init__.py HUB_LANG)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HUB_LANG", None)
    def test_files_serve_the_page_strings_in_the_page_language(self):
        code, got = self.req("GET", "/i18n.json", headers={"Accept-Language": "de"})
        self.assertEqual(code, 200)
        self.assertEqual((got["lang"], got["dir"]), ("de", "ltr"))
        self.assertEqual(got["messages"], i18n.page_bundle("de")["messages"])
        self.cfg["language"] = "ru"
        got = self.req("GET", "/i18n.json", headers={"Accept-Language": "de"})[1]
        self.assertEqual(got["lang"], "ru")
        self.assertEqual(got["fallback"], {})
        code, js = self.req("GET", "/web/js/i18n.js")
        self.assertEqual(code, 200)

    def test_the_owner_language_is_read_from_the_journal(self):
        # the owner's language comes from their messages in the journal, as on the hub page (Files too, without comments)
        from core import journal
        j = journal.Journal(os.path.join(self.dir, "journal.jsonl"))
        j.append("message.received", None, {"to": "hub", "text": lang.LETTERS["uk"],   # Ukrainian letters
                                            "channel": "web", "owner": True})
        self.assertEqual(self.req("GET", "/i18n.json", headers={"Accept-Language": "en"})[1]["lang"], "uk")

    def test_the_files_page_is_rendered_in_its_language(self):
        self.cfg["language"] = "ru"   # a language other than the page source's English
        code, page = self.req("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn('lang="ru"', page.decode())
        self.assertNotIn(i18n.tr("en", "files.page_title"), page.decode())
        self.assertIn(i18n.tr("ru", "files.page_title"), page.decode())


def load_tests(loader, standard, pattern):
    """FilesStrings inherits FilesTest for the server; FilesTest's own tests run in test_files."""
    suite = unittest.TestSuite()
    suite.addTests(FilesStrings(n) for n in FilesStrings.__dict__ if n.startswith("test"))
    return suite


if __name__ == "__main__":
    unittest.main()
