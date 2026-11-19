"""Bundled plugins (owner decision 2026-10-03: ship Flow as a bundled plugin, plugin API 1.1): they live in the hub repo
(modules/web/bundled/<name>/), are on by default and update together with the hub; they can be turned off in
Settings -> Plugins (that is all the settings store), not removed. The first one is Flow."""
import tests  # noqa: F401 — keep the machine's settings out
import json
import os
import re
import tempfile
import unittest
from unittest import mock

from core import version
from modules.market import market
from modules.web import server
from tests import test_web


class Bundled(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-bundled-")
        self.cfg = os.path.join(self.tmp, "hub.json")
        with open(self.cfg, "w") as f:
            json.dump({"dev": {"enabled": False}, "update": {"enabled": False}}, f)
        self.env = mock.patch.dict(os.environ, {"HUB_CONFIG": self.cfg})
        self.env.start()
        self.h = test_web.WebTest("test_page_and_snapshot")
        self.h.setUp()

    def tearDown(self):
        self.h.tearDown()
        self.env.stop()

    def snap(self):
        return json.loads(self.h.req("GET", "/api/snapshot")[1])

    def test_flow_ships_as_a_valid_plugin_of_api_1_1(self):
        with open(os.path.join(server.BUNDLED, "flow", "hub-plugin.json")) as f:
            man = market.valid(json.load(f))
        self.assertEqual((man["name"], man["web"], man["missing"]), ("flow", "main.js", []))
        self.assertEqual(version.compat(man["hub"])[0], "ok")
        self.assertEqual(version.compat("^1.1")[0], "ok")
        self.assertFalse(os.path.exists(os.path.join(server.STATIC, "js", "flow.js")))   # no longer in the page

    def test_usage_ships_as_a_valid_plugin(self):
        with open(os.path.join(server.BUNDLED, "usage", "hub-plugin.json")) as f:
            man = market.valid(json.load(f))
        self.assertEqual((man["name"], man["web"], man["missing"]), ("usage", "main.js", []))
        self.assertEqual(version.compat(man["hub"])[0], "ok")

    def test_on_by_default_served_and_listed_as_bundled(self):
        self.h.seed()
        snap = self.snap()
        self.assertIn({"name": "flow", "main": "main.js"}, snap["extensions"])
        (p,) = [p for p in snap["plugins"] if p["name"] == "flow"]
        self.assertEqual((p["bundled"], p["source"], p["disabled"], p["missing"]), (True, None, False, False))
        self.assertTrue(p["description"] and p["icon"])
        self.assertEqual(self.h.req("GET", "/ext/flow/main.js")[0], 200)
        self.assertEqual(self.h.req("GET", "/ext/flow/flow.css")[0], 200)
        self.assertEqual(self.h.req("GET", p["icon"])[0], 200)

    def test_owner_turns_it_off_and_on_but_cannot_remove(self):
        self.h.seed()
        code, out = self.h.req("POST", "/api/plugins/toggle", {"name": "flow", "enabled": False})
        self.assertEqual(code, 200, out)
        with open(self.cfg) as f:
            self.assertEqual(json.load(f)["extensions"]["flow"], {"disabled": True})   # only this — no path, no code
        self.assertNotIn("flow", [e["name"] for e in self.snap()["extensions"]])
        self.assertTrue([p for p in self.snap()["plugins"] if p["name"] == "flow"][0]["disabled"])
        self.assertEqual(self.h.req("GET", "/ext/flow/main.js")[0], 404)
        self.h.req("POST", "/api/plugins/toggle", {"name": "flow", "enabled": True})
        self.assertIn("flow", [e["name"] for e in self.snap()["extensions"]])
        code, _ = self.h.req("POST", "/api/plugins/remove", {"name": "flow"})
        self.assertNotEqual(code, 200)

    def test_default_off_stays_off_until_the_owner_turns_it_on(self):
        # owner decision 2026-10-05: a bundled plugin with default_off in its manifest is off by default; turned on —
        # "enabled" in the settings, turned off again — no entry; one that was disabled earlier stays disabled
        bundled = os.path.join(self.tmp, "bundled")
        os.makedirs(os.path.join(bundled, "demo"))
        with open(os.path.join(bundled, "demo", "hub-plugin.json"), "w") as f:
            json.dump({"name": "demo", "version": "1.0.0", "default_off": True, "hub": "^1.6", "web": "main.js"}, f)
        with open(os.path.join(bundled, "demo", "main.js"), "w") as f:
            f.write("export default function register() {}\n")
        patch = mock.patch.object(server, "BUNDLED", bundled)
        patch.start()
        self.addCleanup(patch.stop)
        self.h.seed()
        (p,) = [p for p in self.snap()["plugins"] if p["name"] == "demo"]
        self.assertTrue(p["disabled"])
        self.assertNotIn("demo", [e["name"] for e in self.snap()["extensions"]])
        self.assertEqual(self.h.req("GET", "/ext/demo/main.js")[0], 404)
        self.assertEqual(self.h.req("POST", "/api/plugins/toggle", {"name": "demo", "enabled": True})[0], 200)
        with open(self.cfg) as f:
            self.assertEqual(json.load(f)["extensions"]["demo"], {"enabled": True})
        self.assertIn({"name": "demo", "main": "main.js"}, self.snap()["extensions"])
        self.h.req("POST", "/api/plugins/toggle", {"name": "demo", "enabled": False})
        with open(self.cfg) as f:
            self.assertNotIn("demo", json.load(f).get("extensions") or {})
        self.assertNotIn("demo", [e["name"] for e in self.snap()["extensions"]])
        with open(self.cfg, "w") as f:
            json.dump({"extensions": {"demo": {"disabled": True}}}, f)
        self.assertNotIn("demo", [e["name"] for e in self.snap()["extensions"]])

    def test_settings_of_a_removed_bundled_plugin_are_ignored(self):
        # 2026-10-18: a bundled plugin was removed from the hub, but hub.json kept its "enabled" or "disabled" mark —
        # without a folder and code it is not a plugin: no row in Plugins, no count in Modules, no address
        with open(self.cfg, "w") as f:
            json.dump({"extensions": {"projects": {"enabled": True}, "gone": {"disabled": True}}}, f)
        self.h.seed()
        snap = self.snap()
        for name in ("projects", "gone"):
            self.assertNotIn(name, [p["name"] for p in snap["plugins"]])
            self.assertNotIn(name, [e["name"] for e in snap["extensions"]])
        self.assertIn("flow", [p["name"] for p in snap["plugins"]])
        self.assertEqual(self.h.req("GET", "/ext/projects/main.js")[0], 404)
        self.assertFalse(os.path.exists(os.path.join(server.BUNDLED, "projects")))
        from modules.web import modinfo
        with open(self.cfg) as f:
            cfg = json.load(f)
        (m,) = [m for m in modinfo.modules(self.tmp, cfg, {"sessions": []}) if m["id"] == "plugins"]
        self.assertEqual(m["state"], "off", m)
        # it cannot be turned off — it does not exist; the settings are left alone
        self.assertEqual(self.h.req("POST", "/api/plugins/toggle", {"name": "projects", "enabled": False})[0], 404)
        with open(self.cfg) as f:
            self.assertEqual(json.load(f), cfg)
        # the mark of a live bundled plugin counts as before
        cfg["extensions"]["usage"] = {"disabled": True}
        (m,) = [m for m in modinfo.modules(self.tmp, cfg, {"sessions": []}) if m["id"] == "plugins"]
        self.assertEqual(m["state"], "on", m)
        # an own entry without a folder is not a mark: it shows in Plugins as broken and can be fixed
        with open(self.cfg, "w") as f:
            json.dump({"extensions": {"broken": {"web": "x.js", "disabled": True}}}, f)
        (p,) = [p for p in self.snap()["plugins"] if p["name"] == "broken"]
        self.assertTrue(p["missing"])

    def test_a_config_entry_cannot_point_a_bundled_name_elsewhere(self):
        with open(self.cfg, "w") as f:
            json.dump({"extensions": {"flow": {"path": self.tmp, "web": "evil.js"}}}, f)
        self.h.seed()
        self.assertIn({"name": "flow", "main": "main.js"}, self.snap()["extensions"])

    def test_no_text_sends_the_reader_to_a_usage_tab_of_settings(self):
        """"Usage" is a page of the ☰ menu, not a tab of Settings: the texts that say where the spending is (the module's
        model right, in every language) and the plugin docs name no "Settings → …" path to it."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        keys = ("web.modules.third_model", "web.modules.third_model_on", "core.modsrc.model_on")
        bad = []
        for name in sorted(os.listdir(os.path.join(root, "i18n"))):
            if not name.endswith(".json") or name.startswith(("sync", "stage", "pending", "glossary")):
                continue
            with open(os.path.join(root, "i18n", name), encoding="utf-8") as f:
                cat = json.load(f)
            path = re.compile(r"☰ [→←] [“«「]?" + re.escape(cat["web.usage.title"]))   # the menu, then the page's own name
            bad += [(name, k) for k in keys if k in cat and not path.search(cat[k])]
        with open(os.path.join(root, "EXTENDING.md"), encoding="utf-8") as f:
            if "Settings → Usage" in f.read():
                bad.append(("EXTENDING.md", "Settings → Usage"))
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
