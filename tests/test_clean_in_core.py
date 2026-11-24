"""Clean, the core's default theme (decision 10.11), is part of the page: its styles and code are served with the page,
and the Clean plugin of the catalog it came from is no longer loaded — two copies of the same code would change the page
twice. The plugin's themes leave the list; "Plugins" says it is built into the hub now and can be removed."""
import tests  # noqa: F401 — keep the machine's settings out
import json
import os
import tempfile
import unittest
from unittest import mock

import platforms
from tests import test_web


class CleanInCore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-clean-")
        self.addCleanup(platforms.current.rmtree, self.tmp)
        plugins = {}
        for name in ("kalimullin:clean", "alice:neon"):   # the plugin that moved into the core, and an ordinary one
            folder = os.path.join(self.tmp, name.replace(":", "-"))
            os.makedirs(folder)
            with open(os.path.join(folder, "main.js"), "w") as f:
                f.write("export default function register(hub) {}\n")
            with open(os.path.join(folder, "look.json"), "w") as f:
                json.dump({"name": name, "colors": {"background": "#ffffff", "foreground": "#000000", "primary": "#4f6cf6"}}, f)
            with open(os.path.join(folder, "hub-plugin.json"), "w") as f:
                json.dump({"name": name.split(":")[1], "version": "1.0.0", "hub": "^1.30", "web": "main.js",
                           "themes": ["look.json"]}, f)
            plugins[name] = {"path": folder, "web": "main.js", "themes": ["look.json"], "source": f"{name.split(':')[0]}/x",
                             "hub": "^1.30"}
        cfg = os.path.join(self.tmp, "hub.json")
        with open(cfg, "w") as f:
            json.dump({"dev": {"enabled": False}, "update": {"enabled": False}, "extensions": plugins}, f)
        env = mock.patch.dict(os.environ, {"HUB_CONFIG": cfg})
        env.start()
        self.addCleanup(env.stop)
        self.h = test_web.WebTest("test_page_and_snapshot")
        self.h.setUp()
        self.addCleanup(self.h.tearDown)

    def test_the_catalog_plugin_is_not_loaded_and_plugins_say_it_can_be_removed(self):
        snap = self.h.snap()
        self.assertEqual([e["name"] for e in snap["extensions"] if ":" in e["name"]], ["alice:neon"])
        themes = json.loads(self.h.req("GET", "/api/themes")[1])["themes"]
        self.assertEqual([t["id"] for t in themes], ["alice:neon:look.json"])
        self.assertEqual(self.h.req("GET", "/ext/kalimullin:clean/main.js")[0], 404)
        self.assertEqual(self.h.req("GET", "/ext/alice:neon/main.js")[0], 200)
        rows = {p["name"]: p for p in snap["plugins"]}
        self.assertIn("built into the hub", rows["kalimullin:clean"]["warning"])
        self.assertNotIn("built into the hub", rows["alice:neon"]["warning"] or "")

    def test_the_page_skips_it_too_while_an_old_server_runs(self):
        """A page server that has not restarted after the update still lists the plugin: the page skips it by the same list."""
        import re
        from modules.web import server
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "modules", "web", "static", "js", "ext.js"), encoding="utf-8") as f:
            page = re.search(r"const IN_CORE = new Set\((\[.*?\])\);", f.read()).group(1)
        self.assertEqual(sorted(json.loads(page)), sorted(server.IN_CORE))

    def test_the_page_carries_clean(self):
        page = self.h.req("GET", "/")[1].decode()
        for path in ("clean/palette.css", "clean/clean.css"):   # always linked: they act only under html.th-clean
            self.assertRegex(page, r'href="/static/(v/[0-9a-f]{12}/)?%s"' % path)
        for path, kind in (("clean/clean.js", "text/javascript"), ("clean/palette.css", "text/css"),
                           ("clean/f/GeistVariable.woff2", "font/woff2"), ("clean/i/send.svg", "image/svg+xml"),
                           ("clean/a/clay-plum-crown.png", "image/png"),
                           ("clean/LICENSE", None)):
            code, _ = self.h.req("GET", "/static/" + path)
            self.assertEqual(code, 200 if kind else 404, path)   # the licence is in the repository, not served as a page file
        main = self.h.req("GET", "/static/js/main.js")[1].decode()
        self.assertIn('from "../clean/clean.js"', main)

    def test_clean_keeps_its_licence_and_the_fonts_theirs(self):
        root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "modules", "web", "static", "clean")
        with open(os.path.join(root, "LICENSE"), encoding="utf-8") as f:
            self.assertIn("MIT License", f.read())
        for name in ("Geist-OFL.txt", "Lora-OFL.txt"):
            with open(os.path.join(root, "f", name), encoding="utf-8") as f:
                self.assertIn("SIL OPEN FONT LICENSE", f.read().upper())
        with open(os.path.join(root, "..", "..", "..", "..", "THIRD-PARTY-NOTICES.md"), encoding="utf-8") as f:
            notices = f.read()
        for needle in ("modules/web/static/clean", "MIT", "Geist", "Lora", "Icons8"):
            self.assertIn(needle, notices)


if __name__ == "__main__":
    unittest.main()
