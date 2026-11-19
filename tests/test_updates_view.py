"""It is visible that an update exists and what is in it (owner decision 2026-10-02: the mechanism shows that an
update exists and lets one see what changed): CHANGES.md is the human-readable list of changes; the update module takes
new items from it; the core remembers updates; the page shows them, and for a shared hub also what is not yet published."""
import tests  # noqa: F401 — hide the machine's settings
import os
import unittest

from core import reducer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Notes(unittest.TestCase):
    def test_new_entries_between_two_versions(self):
        from modules.update import update
        old = "# Changes\n\n## 2026-10-01\n- old\n"
        new = "# Changes\n\n## 2026-10-02\n- new routes\n- files in Telegram\n\n## 2026-10-01\n- old\n"
        self.assertEqual(update.new_notes(old, new), ["2026-10-02", "new routes", "files in Telegram"])
        self.assertEqual(update.new_notes(new, new), [])

    def test_changes_file_exists_with_a_dated_entry(self):
        with open(os.path.join(ROOT, "CHANGES.md")) as f:
            text = f.read()
        self.assertRegex(text, r"\n## \d{4}-\d{2}-\d{2}")


class Core(unittest.TestCase):
    def test_updates_are_remembered(self):
        st = reducer.empty()
        for i, (t, d) in enumerate([("update.applied", {"from": "a", "to": "b", "commits": ["x"], "notes": ["n1"]}),
                                    ("update.failed", {"why": "tests", "notes": ["n2"], "tail": "FAILED"})], 1):
            st, _, why = reducer.reduce(st, {"seq": i, "ts": 100 + i, "type": t, "entity": None,
                                             "data": {**d, "channel": "cli", "owner": True}})
            self.assertIsNone(why)
        a, f = st["updates"]
        self.assertEqual((a["ok"], a["notes"], f["ok"], f["why"]), (True, ["n1"], False, "tests"))
        from core import views
        r = views.Reader()
        r.state = st
        self.assertEqual(len(r.snapshot()["updates"]), 2)

    def test_tests_of_an_update_are_not_a_waiting_restart(self):
        # owner report 2026-10-04: the page still said a hub restart was required. The module merged the new code and
        # ran tests for ~5 minutes while the page already said "waiting for restart"; the check result (applied /
        # failed) clears "checking"
        from core import views
        st, r = reducer.empty(), views.Reader()
        for i, (t, d) in enumerate([("update.testing", {"from": "a", "upstream": "u1"}),
                                    ("update.failed", {"why": "tests", "notes": []}),
                                    ("update.testing", {"from": "a", "upstream": "u2"})], 1):
            st, _, why = reducer.reduce(st, {"seq": i, "ts": 100 + i, "type": t, "entity": None,
                                             "data": {**d, "channel": "cli", "owner": True}})
            self.assertIsNone(why)
            r.state = st
            self.assertEqual(bool(r.snapshot()["update_testing"]), t == "update.testing")
        st, _, _ = reducer.reduce(st, {"seq": 4, "ts": 104, "type": "update.applied", "entity": None,
                                       "data": {"from": "a", "to": "b", "channel": "cli", "owner": True}})
        r.state = st
        self.assertIsNone(r.snapshot()["update_testing"])

    def test_page_says_checking_while_tests_run(self):
        with open(os.path.join(ROOT, "modules/web/static/js/updates.js")) as f:
            js = f.read()
        self.assertIn("update_testing", js)
        self.assertNotIn("within 15 seconds", js)                                   # tests take minutes, not seconds
        self.assertIn('"/api/send", { session: hub.name', js)                   # "Install" is a request to the hub
        self.assertIn("available:", js)

    def test_checking_lasts_as_long_as_the_hubs_check_limit(self):
        # review of PR #517: with update.tests_timeout of 4000 s the page said "the update did not take" after 30 min
        # while the tests still ran; the window is the limit from the Settings row (at most its max) and 10 min more,
        # 30 min without the row (a server of an earlier release)
        with open(os.path.join(ROOT, "modules/web/static/js/updates.js")) as f:
            js = f.read()
        window = js[js.index("function testingWindow"):js.index("export function testingVersion")]
        self.assertIn('"update.tests_timeout"', window)
        self.assertIn("row?.max", window)
        self.assertIn("Math.max(1800, limit + 600)", window)
        self.assertIn("< testingWindow()", js[js.index("export function testingVersion"):])

    def test_a_failed_update_merged_by_hand_is_not_shown_as_not_installed(self):
        # 08.10: a conflict was resolved by hand, and the hub is on the latest version (check: current), yet "Settings"
        # and "About the hub" kept saying for a day that a new version was not installed, going by the last
        # update.failed record
        with open(os.path.join(ROOT, "modules/web/static/js/updates.js")) as f:
            js = f.read()
        stale = js[js.index("export function staleVersion"):js.index("function changesList")]
        self.assertIn("update_check", stale)
        self.assertIn('result === "current"', stale)
        self.assertIn("ts >= last?.ts", stale)


if __name__ == "__main__":
    unittest.main()
