"""Slice I5, group E: the owner-facing texts of core/budget.py and core/daemon.py come from the catalog and carry every parameter."""
import subprocess
import unittest

from core import budget, daemon, i18n


class BudgetTexts(unittest.TestCase):
    def test_window_names_follow_the_language_asked(self):
        self.assertEqual(budget.window_name("five_hour", "en"), "5 h")
        self.assertEqual(budget.window_name("seven_day", "en"), "week")
        self.assertEqual(budget.window_name("codex_2880m", "en"), "2 d")
        self.assertEqual(budget.window_name("codex_60m", "ru"), i18n.tr("ru", "core.budget.window_hours", n=1))
        self.assertEqual(budget.window_name("codex_30m", "en"), "30 min")
        self.assertIsNone(budget.window_name(None, "en"))

    def test_the_reasons_carry_their_numbers(self):
        for lang in ("ru", "en"):
            text = i18n.tr(lang, "core.budget.why_over_pool", pct=91, at=90)
            self.assertIn("91", text)
            self.assertIn("90", text)
            self.assertNotIn("{", text)
            text = i18n.tr(lang, "core.budget.why_economy_pool", pct=88, window="W", account="acc")
            self.assertTrue(all(x in text for x in ("88", "W", "acc")), text)
            self.assertNotIn("{", i18n.tr(lang, "core.budget.why_gateway", name="acc"))

    def test_the_decision_reason_is_a_catalog_text(self):
        st = {"account": {"seen": {"a": {"windows": {"five_hour": {"used": 0.9, "resets": 10 ** 10}}}}}}
        want = budget.decide(st, ["a", "b"], 0.99, 1000, 0.5)
        self.assertNotIn("core.budget", want["why"])
        self.assertNotIn("{", want["why"])


class DaemonTexts(unittest.TestCase):
    def test_a_git_failure_is_told_by_the_catalog(self):
        e = subprocess.TimeoutExpired(["git", "-C", "/x", "status"], 1)
        self.assertEqual(daemon.git_failure(e), i18n.t("core.daemon.git_timeout", op="git status"))
        self.assertEqual(daemon.git_failure(FileNotFoundError()), i18n.t("core.daemon.git_missing", op="git"))
        self.assertNotIn("{", daemon.git_failure(e))

    def test_the_cards_carry_their_parameters(self):
        for lang in ("ru", "en"):
            for key in ("core.daemon.claude_missing", "core.daemon.claude_missing_app"):
                self.assertIn("/p/claude", i18n.tr(lang, key, path="/p/claude"))
            text = i18n.tr(lang, "core.daemon.private_wait", why="W", minutes=5, line="L")
            self.assertTrue(all(x in text for x in ("W", "5", "L")), text)
            text = i18n.tr(lang, "core.daemon.settings_unreadable", path="/h.json", error="E")
            self.assertTrue(all(x in text for x in ("/h.json", "E")), text)


if __name__ == "__main__":
    unittest.main()
