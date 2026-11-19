"""Slice I5, group F: the owner-facing texts of the sign-in pool, the providers, the voice reply and the Hub Developer's
description come from the catalog; the Russian ones are the previous literals, the counted forms follow the count."""
import tests  # noqa: F401 — hide the machine's settings
import unittest

from core import dev, i18n, providers, reducer
from tests.fakes import cut


class Counted(unittest.TestCase):
    def test_voicing_failures_take_the_form_of_their_count(self):
        forms = {n: i18n.tr("ru", "core.providers.speech_failed", count=n, why="w") for n in (1, 2, 5)}
        self.assertEqual(len(set(forms.values())), 3)
        for n, text in forms.items():
            self.assertTrue(text.startswith(f"{n} "), text)
            self.assertTrue(text.endswith(": w"), text)
        self.assertEqual(i18n.tr("en", "core.providers.speech_failed", count=2, why="w"), "2 voicings in a row failed: w")

    def test_the_fold_writes_the_reason_in_the_owner_language(self):
        st = reducer.empty()
        st["ui_lang_default"] = "ru"
        st["providers"] = {"speech": {"state": "module", "module": "m"}}
        for _ in range(providers.SPEECH_FAILS):
            providers.said(st, {"src": "telegram", "ts": 1}, {"module": "m", "ok": False, "why": "boom"})
        want = i18n.tr("ru", "core.providers.speech_failed", count=providers.SPEECH_FAILS, why="boom")
        self.assertEqual(st["providers"]["speech"]["why"], want)

    def test_report_failures_take_the_form_of_their_count(self):
        one, few = (i18n.tr("ru", "core.providers.report_failed", count=n, error="e") for n in (1, 3))
        self.assertNotEqual(one, few)
        self.assertEqual(i18n.tr("en", "core.providers.report_failed", count=3, error="e"),
                         "3 report sends in a row did not arrive: e")


class Lines(unittest.TestCase):
    @cut.needs("profile", "speech", "stats")
    def test_hub_modules_lines_carry_the_labels_and_the_hold(self):
        recs = {"stats": {"state": "builtin", "module": "m", "hold": {"module": "m", "version": ""}}}
        line = "".join(providers.lines(recs))
        self.assertIn(i18n.t(providers.LABEL["stats"]), line)
        self.assertIn(i18n.t(providers.STATE_LABEL["builtin"]), line)
        self.assertIn(i18n.t("core.providers.held_until_new", module="m", version="").replace("  ", " "), line)
        self.assertNotIn("{", line)
        self.assertNotIn("core.", line)

    def test_every_label_is_a_catalog_key(self):
        for key in [*providers.LABEL.values(), *providers.STATE_LABEL.values(), dev.ABOUT, dev.APP_ABOUT]:
            for lang in ("en", "ru"):
                self.assertNotEqual(i18n.tr(lang, key), key, (lang, key))


class Developer(unittest.TestCase):
    def test_the_spec_describes_the_agent_in_the_process_language(self):
        name, data = dev.spec({})
        self.assertEqual(data["about"], i18n.t(dev.ABOUT))
        self.assertNotEqual(data["about"], dev.ABOUT)


if __name__ == "__main__":
    unittest.main()
