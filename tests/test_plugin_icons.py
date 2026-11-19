"""A plugin has an icon, "what it does" and "why" (owner decision 2026-10-03: every plugin must have an icon and a
description of what it does and why): icon in hub-plugin.json is a path to svg/png/webp in the repo; in the catalog —
an address from the author's repo, for an installed one — from its own disk via /api/plugins/icon; what is missing is
visible (missing)."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
from unittest import mock

from modules.market import market
from tests import test_market, test_web


class Manifest(test_market.Market):
    def test_icon_is_part_of_the_manifest(self):
        man = market.valid({"name": "x", "web": "main.js", "icon": "assets/icon.svg", "description": "d", "why": "w"})
        self.assertEqual((man["icon"], man["missing"]), ("assets/icon.svg", []))
        self.assertIsNone(market.valid({"name": "x", "web": "main.js", "icon": "../etc/passwd"}))
        self.assertIsNone(market.valid({"name": "x", "web": "main.js", "icon": "icon.exe"}))
        self.assertEqual(market.valid({"name": "x", "web": "main.js"})["missing"], ["icon", "description", "why"])

    def test_a_plugin_by_link_gives_the_icon_address_in_the_authors_repo_at_the_reviewed_commit(self):
        self.repo("bob", "hub-plugin-owl", {"name": "owl", "web": "main.js", "icon": "owl.svg", "description": "d",
                                            "why": "w"}, {"main.js": "", "owl.svg": "<svg/>"})
        m = self.m()
        rec = m.stage("bob/hub-plugin-owl")
        (p,) = [x for x in m.search("") if x["name"] == "owl"]
        self.assertEqual(p["icon_url"], f"https://raw.githubusercontent.com/bob/hub-plugin-owl/{rec['commit']}/owl.svg")


class Served(test_web.WebTest):
    def test_installed_icon_is_served_from_its_folder_only(self):
        plug = os.path.join(self.state, "market", "bob", "owl")
        os.makedirs(plug)
        with open(os.path.join(plug, "hub-plugin.json"), "w") as f:
            json.dump({"name": "owl", "web": "main.js", "icon": "owl.svg", "description": "d", "why": "w"}, f)
        with open(os.path.join(plug, "owl.svg"), "w") as f:
            f.write('<svg xmlns="http://www.w3.org/2000/svg"/>')
        with open(os.path.join(self.state, "hub.json"), "w") as f:
            json.dump({"extensions": {"bob:owl": {"path": plug, "source": "bob/owl", "web": "main.js"}}}, f)
        env = mock.patch.dict(os.environ, {"HUB_CONFIG": os.path.join(self.state, "hub.json")})
        env.start()
        self.addCleanup(env.stop)
        self.seed()
        (p,) = [x for x in json.loads(self.req("GET", "/api/snapshot")[1])["plugins"] if not x["bundled"]]
        self.assertEqual(p["icon"], "/api/plugins/icon?name=bob:owl")
        code, body = self.req("GET", "/api/plugins/icon?name=bob:owl")
        self.assertEqual((code, body[:4]), (200, b"<svg"))
        self.assertEqual(self.req("GET", "/api/plugins/icon?name=nobody")[0], 404)


def load_tests(loader, standard, pattern):
    import unittest
    suite = unittest.TestSuite()
    for cls in (Manifest, Served):
        suite.addTests(cls(n) for n in cls.__dict__ if n.startswith("test"))
    return suite
