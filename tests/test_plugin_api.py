"""Plugins v1 (owner decision 2026-10-03): themes through the plugin interface; plugins can add sections to settings,
to the sidebar in various places and to the top bar; core versions, so that there is a warning when a plugin will not
be supported."""
import tests  # noqa: F401 — keeps the machine's settings out
import json
import os
import threading
import unittest
import unittest.mock

from core import version
from modules.market import market
from tests.test_market import Market as MarketCase


class Versions(unittest.TestCase):
    def test_compat(self):
        c = lambda w: version.compat(w, "1.3")[0]  # noqa: E731
        self.assertEqual([c("^1.0"), c(">=1.2"), c("1"), c("1.3")], ["ok"] * 4)
        self.assertEqual(c("^1.5"), "newer")                     # a newer hub is needed — update the hub
        self.assertEqual(c("^2.0"), "incompatible")
        self.assertEqual(c("^0.9"), "incompatible")              # the plugin is outdated
        self.assertEqual(c(None), "unknown")
        self.assertEqual(c("abc"), "unknown")
        self.assertIn("outdated", version.compat("^0.9", "1.3")[1])
        self.assertIn("update the hub", version.compat("^1.5", "1.3")[1])

    def test_manifest_fields(self):
        m = market.valid({"name": "nord", "themes": ["themes/nord.json"], "hub": "^1.0", "version": "1.2.0"})
        self.assertEqual((m["themes"], m["hub"], m["version"]), (["themes/nord.json"], "^1.0", "1.2.0"))
        self.assertEqual(market.kinds(m), ["themes"])
        self.assertIsNone(market.valid({"name": "x", "themes": ["../../etc/passwd"]}))


class Install(MarketCase):
    def test_versions_on_install(self):
        self.repo("alice", "hub-plugin-nord", {"name": "nord", "hub": "^1.0", "version": "1.0.0",
                                              "themes": ["nord.json"]}, {"nord.json": json.dumps({"name": "Nord"})})
        self.repo("alice", "hub-plugin-old", {"name": "old", "hub": "^0.1", "web": "main.js"}, {"main.js": "x"})
        self.repo("alice", "hub-plugin-next", {"name": "next", "hub": "^1.99", "web": "main.js"}, {"main.js": "x"})
        m = self.m()
        got = self.put(m, "alice/hub-plugin-nord")
        ext = self.cfg()["extensions"]["alice:nord"]
        self.assertEqual((ext["themes"], ext["version"]), (["nord.json"], "1.0.0"))
        self.assertIn("themes", got)
        with self.assertRaises(market.Error) as e:
            self.put(m, "alice/hub-plugin-old")
        self.assertIn("outdated", str(e.exception))
        self.assertIn("update the hub", self.put(m, "alice/hub-plugin-next"))   # installed, but with a warning
        listed = {p["name"]: p for p in m.search("")}                        # the old one is still under review
        self.assertEqual(listed["old"]["compat"], "incompatible")


class Page(unittest.TestCase):
    """The page: themes from plugins, plugin info for "Settings"; a disabled and an incompatible plugin are not loaded."""

    def setUp(self):
        from tests.test_web import WebTest
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.helper = WebTest("test_page_and_snapshot")
        self.helper.setUp()
        self.cfg = os.environ["HUB_CONFIG"]
        with open(self.cfg) as f:
            self.saved = f.read()
        ex = lambda n: os.path.join(root, "examples", n)  # noqa: E731
        with open(self.cfg, "w") as f:
            json.dump({"dev": {"enabled": False}, "update": {"enabled": False}, "extensions": {
                "alice:nord": {"path": ex("plugin-theme-nord"), "themes": ["nord.json"], "source": "alice/x",
                               "hub": "^1.0", "version": "1.0.0"},
                "alice:slots": {"path": ex("plugin-slots"), "web": "main.js", "source": "alice/y", "hub": "^1.0"},
                "bob:old": {"path": ex("plugin-slots"), "web": "main.js", "source": "bob/z", "hub": "^0.3"},
                "mine": {"path": ex("plugin-slots"), "web": "main.js", "disabled": True}}}, f)

    def tearDown(self):
        with open(self.cfg, "w") as f:
            f.write(self.saved)
        self.helper.tearDown()

    def test_the_version_shown_is_the_installed_one(self):
        # 04.10: the plugin was updated in its folder (1.3.0), but the list showed 1.0.0 from the install record
        import tempfile
        d = tempfile.mkdtemp(prefix="plugver-")
        with open(os.path.join(d, "hub-plugin.json"), "w") as f:
            json.dump({"name": "feed", "version": "1.3.0", "hub": "^1.2"}, f)
        with open(self.cfg) as f:
            cfg = json.load(f)
        cfg["extensions"]["feed"] = {"path": d, "version": "1.0.0"}
        with open(self.cfg, "w") as f:
            json.dump(cfg, f)
        self.helper.seed()
        snap = json.loads(self.helper.req("GET", "/api/snapshot")[1])
        self.assertEqual({p["name"]: p["version"] for p in snap["plugins"]}["feed"], "1.3.0")

    def test_page(self):
        h = self.helper
        h.seed()
        snap = json.loads(h.req("GET", "/api/snapshot")[1])
        self.assertEqual([e["name"] for e in snap["extensions"]], ["flow", "usage", "alice:slots"])   # a theme without code, an outdated and a disabled one are absent; "Flow" and "Usage" are preinstalled
        plugins = {p["name"]: p for p in snap["plugins"]}
        self.assertEqual((plugins["bob:old"]["compat"], plugins["mine"]["disabled"]), ("incompatible", True))
        self.assertIn("outdated", plugins["bob:old"]["warning"])
        self.assertEqual(snap["plugin_api"], version.PLUGIN_API)
        themes = json.loads(h.req("GET", "/api/themes")[1])
        self.assertEqual([(t["id"], t["theme"]["name"]) for t in themes["themes"]], [("alice:nord:nord.json", "Nord")])
        js = h.req("GET", "/static/js/ext.js")[1].decode()
        for slot in ("sidebar.tiles", "sidebar.header", "sidebar.top", "sidebar.nav", "sidebar.footer", "sidebar.session", "topbar",
                     "session.header.start", "session.header", "session.composer"):
            self.assertIn(f'"{slot}"', js)
        # 1.5: the slot in the sidebar header — since 17.10 in the search row, before ☰ (there is no hub name row)
        page = h.req("GET", "/")[1].decode()
        row = page[page.index('<div class="search-row">'):]
        row = row[:row.index('id="navmenu"')]
        self.assertIn('id="slot-sidebar-header"', row)
        self.assertIn('slotNodes("sidebar.header")', h.req("GET", "/static/js/sidebar.js")[1].decode())
        # 1.30: tiles are above the search row and before it in the markup (the Tab order follows the markup)
        self.assertLess(page.index('id="slot-sidebar-tiles"'), page.index('<div class="search-row">'))

    def test_toggle_from_the_page(self):
        h = self.helper
        h.seed()
        code, out = h.req("POST", "/api/plugins/toggle", {"name": "mine", "enabled": True})
        self.assertEqual(code, 200)
        snap = json.loads(h.req("GET", "/api/snapshot")[1])
        self.assertIn("mine", [e["name"] for e in snap["extensions"]])
        self.assertEqual(h.req("POST", "/api/plugins/toggle", {"name": "nobody", "enabled": True})[0], 404)
        # 03.10, switching it on seemed to do nothing — so an action on a plugin is a fact in the journal: the page
        # gets a new snapshot at once (and reloads with the plugin), and the journal shows who installed what
        facts = [(e["type"], e["data"].get("what"), e["data"].get("name")) for e in h.spooled() if e["type"] == "plugins.changed"]
        self.assertEqual(facts, [("plugins.changed", "toggle", "mine")])

    def test_enabling_a_plugin_with_a_process_starts_it(self):
        h = self.helper
        with open(self.cfg) as f:
            cfg = json.load(f)
        cfg["extensions"]["mine"]["run"] = ["python3", "main.py"]
        with open(self.cfg, "w") as f:
            json.dump(cfg, f)
        h.seed()
        h.req("POST", "/api/plugins/toggle", {"name": "alice:slots", "enabled": False})
        h.req("POST", "/api/plugins/toggle", {"name": "mine", "enabled": True})
        asked = [e["data"]["parts"] for e in h.spooled() if e["type"] == "hub.restart_requested"]
        self.assertEqual(asked, [["ext:mine"]])
        from core import reducer
        st, _, why = reducer.reduce(reducer.empty(), {"seq": 1, "ts": 1, "type": "plugins.changed", "entity": None,
                                                     "data": {"what": "toggle", "name": "mine", "channel": "web", "owner": True}})
        self.assertIsNone(why)                                             # just a fact: the core records it, does not argue

    def test_fix_and_pr_goes_to_the_hub_developer(self):
        # 03.10, owner decision: an item here can be fixed by yourself and sent as a PR/merge request to the plugin's owner
        h = self.helper
        h.seed()
        h.spooled()
        code, out = h.req("POST", "/api/plugins/fix", {"name": "bob:old"})
        self.assertEqual(code, 202)
        (ev,) = [e for e in h.spooled() if e["type"] == "message.received"]
        self.assertEqual((ev["data"]["to"], ev["data"]["owner"]), ("hub-dev", True))
        for word in ("bob/z", "PR", "gh repo fork", "outdated"):
            self.assertIn(word, ev["data"]["text"])
        self.assertEqual(ev["data"]["button"], "Fix and send a PR to the author")   # the feed names the button, not the task
        self.assertEqual(h.req("POST", "/api/plugins/fix", {"name": "mine"})[0], 404)    # one's own plugin is not from the marketplace

    def test_a_repeated_press_sends_one_message(self):
        # one press of "Fix" brought the hub developer five identical messages (9 ms apart: the browser repeating the
        # request; a second apart: repeated clicks): the task goes to the session once
        h = self.helper
        h.seed()
        h.spooled()
        results = []

        def press():
            results.append(h.req("POST", "/api/plugins/fix", {"name": "bob:old"}))
        threads = [threading.Thread(target=press) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([code for code, _ in results], [202] * 5)       # the owner sees the same "task handed over"
        self.assertEqual(len({str(body) for _, body in results}), 1)
        self.assertEqual(len([e for e in h.spooled() if e["type"] == "message.received"]), 1)
        # a refused press takes nothing: a plugin that is not there gets a refusal, and a real press after it goes out
        self.assertEqual(h.req("POST", "/api/plugins/fix", {"name": "nope"})[0], 404)
        self.assertEqual(h.req("POST", "/api/plugins/fix", {"name": "alice:slots"})[0], 202)
        self.assertEqual(len([e for e in h.spooled() if e["type"] == "message.received"]), 1)
        # time passed: pressing again is possible (the task was closed and did not help)
        h.web.sent_actions.clear()
        self.assertEqual(h.req("POST", "/api/plugins/fix", {"name": "bob:old"})[0], 202)
        self.assertEqual(len([e for e in h.spooled() if e["type"] == "message.received"]), 1)



def load_tests(loader, standard, pattern):
    """Only our own: Market's tests run in test_market (as an import and the base of Install they ran twice more)."""
    return unittest.TestSuite(cls(n) for cls in (Versions, Install, Page) for n in cls.__dict__
                              if n.startswith("test"))


if __name__ == "__main__":
    unittest.main()
