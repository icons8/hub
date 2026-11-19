"""Settings -> Modules (owner decision 2026-10-03: optional modules such as the voice assistant and browsers live in the
Modules tab, which shows everything, Tailscale included, with a detailed description of what each gives)."""
import tests  # noqa: F401 — keep the machine's settings out of the test
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from core import i18n
from platforms import current
from tests.fakes import cut

EXAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "module-hello")


class ModulesTab(unittest.TestCase):
    def setUp(self):
        from tests.test_web import WebTest
        self.helper = WebTest("test_page_and_snapshot")
        self.helper.setUp()

    def tearDown(self):
        self.helper.tearDown()

    @cut.needs("stats")
    def test_all_modules_with_what_they_give(self):
        h = self.helper
        h.seed()
        with open(os.path.join(h.state, "tailnet.json"), "w") as f:
            json.dump({"ok": True, "ts": time.time(), "urls": {"web": "http://100.1.2.3:8787"}}, f)
        mods = {m["id"]: m for m in json.loads(h.req("GET", "/api/modules")[1])["modules"]}
        for mid in ("telegram", "tailnet", "watch", "browsers", "voice", "jira", "files", "scheduler", "update",
                    "directions", "contacts", "agents", "coach", "plugins", "stats", "surrender", "promise"):
            self.assertIn(mid, mods)
            self.assertGreater(len(mods[mid]["gives"]), 40, mid)          # a detailed description of what it gives
            self.assertIn(mods[mid]["state"], ("on", "off", "part"))
        self.assertEqual(mods["tailnet"]["state"], "on")
        self.assertIn("100.1.2.3", mods["tailnet"]["detail"])
        self.assertEqual(mods["browsers"]["section"], "browsers")
        self.assertNotIn("jev", mods)                                       # 10-05: the jev module was replaced by Haiku, there is no key
        self.assertEqual(mods["watch"]["state"], "on")
        self.assertNotIn("jev", json.loads(h.req("GET", "/api/snapshot")[1]))
        js = h.req("GET", "/static/js/settings.js")[1].decode()
        self.assertIn('id: "modules"', js)
        self.assertNotIn('id: "jev"', js)                                   # jev and browsers live inside "Modules"


    def test_the_parts_without_a_switch_say_the_hub_needs_them(self):
        # the tab intro says some modules cannot be switched off: those cards carry the mark, switchable ones do not
        h = self.helper
        h.seed()
        mods = {m["id"]: m for m in json.loads(h.req("GET", "/api/modules")[1])["modules"]}
        for mid in ("files", "scheduler", "directions", "contacts", "agents", "coach"):
            self.assertTrue(mods[mid].get("core"), mid)
            self.assertFalse(mods[mid].get("toggle"), mid)
        for mid, m in mods.items():
            if m.get("toggle"):
                self.assertFalse(m.get("core"), mid)
        js = h.req("GET", "/static/js/modules.js")[1].decode()
        self.assertIn('t("web.modules.core")', js)
        for lang in ("en", "ru"):
            self.assertNotIn("works without them", i18n.tr(lang, "web.modules.intro"))

    @cut.needs("stats")
    def test_stats_is_seen_in_settings_with_what_goes_and_what_went(self):
        # 10-08, owner report: telemetry settings were not visible — "Modules" now has stats with a switch, and right
        # there: what will go out next time and what has already gone (before, only hub stats / hub stats log in the terminal)
        h = self.helper
        h.seed()
        mods = {m["id"]: m for m in json.loads(h.req("GET", "/api/modules")[1])["modules"]}
        st = mods["stats"]
        self.assertIn("telemetry", st["title"].lower())
        self.assertTrue(st["toggle"])
        self.assertEqual(st["section"], "stats")
        self.assertIn("15 minutes", st["gives"])
        self.assertNotIn("a day", st["gives"])
        code, body = h.req("GET", "/api/stats?what=next")
        self.assertEqual(code, 200)
        text = json.loads(body)["text"]
        self.assertIn('"sessions"', text)                                   # the same JSON as hub stats
        self.assertIn(i18n.t("core.stats.preview_off", url="")[:20], text)
        code, body = h.req("GET", "/api/stats?what=sent")
        self.assertEqual((code, json.loads(body)["text"]), (200, i18n.t("core.stats.log_empty")))
        self.assertEqual(h.req("GET", "/api/stats?what=x")[0], 400)
        js = h.req("GET", "/static/js/modules.js")[1].decode()
        self.assertIn("/api/stats?what=", js)

    @cut.needs("stats")
    def test_what_goes_next_is_built_by_a_child_not_in_the_page_process(self):
        # 07.11: the report parses the whole hub state; built in the page process, its memory stayed after the card opened
        from unittest import mock
        from modules.stats import stats
        h = self.helper
        h.seed()
        with mock.patch.object(stats, "build", side_effect=AssertionError("built in the page process")):
            code, body = h.req("GET", "/api/stats?what=next")
        self.assertEqual(code, 200)
        self.assertIn('"sessions"', json.loads(body)["text"])

    def test_switched_off_modules_come_first_in_the_original_order(self):
        # 10-12, owner request: switched-off modules go first — what can be enabled is in plain sight, not under what already runs
        h = self.helper
        h.seed()
        states = [m["state"] for m in json.loads(h.req("GET", "/api/modules")[1])["modules"]]
        self.assertIn("off", states)
        self.assertGreater(len(set(states)), 1)                       # there are both off and other states — the order means something
        first_other = next(i for i, st in enumerate(states) if st != "off")
        self.assertNotIn("off", states[first_other:])                 # no off module after the first non-off one

    def test_the_order_inside_each_group_is_kept(self):
        from modules.web import modinfo
        import tempfile
        mods = modinfo.modules(tempfile.mkdtemp(), {}, {"sessions": []})
        ids = [m["id"] for m in mods]
        off = [m["id"] for m in mods if m["state"] == "off"]
        rest = [m["id"] for m in mods if m["state"] != "off"]
        self.assertEqual(ids, off + rest)
        self.assertLess(off.index("push"), off.index("reactions"))   # inside a group — as modules() adds them
        self.assertLess(rest.index("coach"), rest.index("secrets"))   # Secrets is on unless turned off (spec d2345345)

    def test_about_the_hub(self):
        h = self.helper
        h.seed()
        a = json.loads(h.req("GET", "/api/about")[1])
        for k in ("code", "core_started", "web_started", "plugin_api", "python", "repo", "state", "sessions", "journal"):
            self.assertIn(k, a)
        self.assertGreater(a["web_started"], 0)
        js = h.req("GET", "/static/js/settings.js")[1].decode()
        self.assertIn('id: "about", title: t("web.settings.tab.about")', js)
        from core import i18n
        self.assertEqual(i18n.t("web.settings.tab.about"), "About")

    def test_installed_modules_are_listed_apart_as_third_party(self):
        # owner decision 2026-10-18: show them, but mark them all as third-party — Module API modules (hub module add)
        # sit in the tab apart from the core parts: origin, commit, API, state, rights
        from unittest import mock
        from tests.test_modapi_sources import Source, manifest
        from core import modsrc
        h = self.helper
        src = Source(self, {"module.json": manifest("notes", "1.2.0", api="^2.3", capabilities=["net", "core.write"],
                                                    description="Notes"), "main.py": "\n"})
        cfg = os.path.join(h.state, "hub.json")
        with open(cfg, "w") as f:
            json.dump({"modules": {"paths": [EXAMPLE]}}, f)
        env = mock.patch.dict(os.environ, {"HUB_CONFIG": cfg})
        env.start()
        self.addCleanup(env.stop)
        modsrc.add(h.state, cfg, src.bare)
        h.seed()
        body = json.loads(h.req("GET", "/api/modules")[1])
        self.assertNotIn("hello", [m["id"] for m in body["modules"]])       # core parts are listed separately
        got = {m["name"]: m for m in body["installed"]}
        hello, notes = got["hello"], got["notes"]
        self.assertEqual((hello["origin"], hello["source"], hello["enabled"], hello["state"]),
                         ("path", EXAMPLE, False, "disabled"))                # found in a folder, not enabled
        self.assertEqual(hello["api"], "^2.3")
        self.assertIn("http", hello["capabilities"])
        self.assertEqual((notes["origin"], notes["source"], notes["enabled"]), ("git", src.bare, True))
        self.assertRegex(notes["commit"], r"^[0-9a-f]{40}$")                # the pinned commit
        self.assertEqual(notes["state"], "blocked")                         # core.write without consent — an error with a reason
        self.assertIn("core_write", notes["reason"])
        self.assertTrue(notes["core_write"])
        self.assertFalse(hello["core_write"])

    def test_a_module_without_a_process_is_listed_as_static(self):
        import tempfile
        h = self.helper
        root = tempfile.mkdtemp(prefix="hubmods-")
        self.addCleanup(current.rmtree, root)
        os.makedirs(os.path.join(root, "look", "web"))
        with open(os.path.join(root, "look", "module.json"), "w") as f:
            json.dump({"name": "look", "version": "1.0.0", "api": "^2.10", "web": "web/main.js"}, f)
        cfg = os.path.join(h.state, "hub.json")
        with open(cfg, "w") as f:
            json.dump({"modules": {"paths": [root], "enabled": ["look"]}}, f)
        from unittest import mock
        env = mock.patch.dict(os.environ, {"HUB_CONFIG": cfg})
        env.start()
        self.addCleanup(env.stop)
        h.seed()
        got = {m["name"]: m for m in json.loads(h.req("GET", "/api/modules")[1])["installed"]}
        self.assertEqual((got["look"]["state"], got["look"]["enabled"], got["look"]["halt"]), ("static", True, "ok"))

    def test_enable_and_disable_write_modules_enabled_under_the_lock(self):
        from unittest import mock
        h = self.helper
        cfg = os.path.join(h.state, "hub.json")
        with open(cfg, "w") as f:
            json.dump({"theme": "x", "modules": {"paths": [EXAMPLE], "enabled": ["other"]}}, f)
        env = mock.patch.dict(os.environ, {"HUB_CONFIG": cfg})
        env.start()
        self.addCleanup(env.stop)
        h.seed()
        code, body = h.req("POST", "/api/modules/enable", {"name": "hello", "enabled": True})
        self.assertEqual(code, 200, body)
        with open(cfg) as f:
            saved = json.load(f)
        self.assertEqual(saved["modules"]["enabled"], ["other", "hello"])
        self.assertEqual(saved["theme"], "x")                               # other keys in hub.json stay in place
        got = {m["name"]: m for m in json.loads(h.req("GET", "/api/modules")[1])["installed"]}
        self.assertTrue(got["hello"]["enabled"])
        self.assertEqual(h.req("POST", "/api/modules/enable", {"name": "hello", "enabled": False})[0], 200)
        with open(cfg) as f:
            self.assertEqual(json.load(f)["modules"]["enabled"], ["other"])
        # an unknown name is refused, hub.json is not touched
        self.assertEqual(h.req("POST", "/api/modules/enable", {"name": "nope", "enabled": True})[0], 404)
        with open(cfg) as f:
            self.assertEqual(json.load(f)["modules"]["enabled"], ["other"])
        self.assertIn("settings.changed", [e["type"] for e in h.spooled()])   # the edit goes to the journal as an event

    def test_an_embeds_modules_are_listed_and_switch_and_the_cores_stay_hidden(self):
        # an embed's module (embed/<name>/modules/<module>/module.json) is the build's code, on by its manifest: a row with
        # the switch, as one from hub module add was; a name clash with the core's module — a blocked row with the reason
        from core import modules
        h = self.helper
        top = tempfile.mkdtemp(prefix="hubembed-")
        self.addCleanup(current.rmtree, top)

        def put(rel, data):
            os.makedirs(os.path.join(top, *rel.split("/")[:-1]), exist_ok=True)
            with open(os.path.join(top, *rel.split("/")), "w") as f:
                json.dump(data, f)
        man = {"version": "1.0.0", "api": "^2.0", "run": ["python3", "main.py"]}
        put("embed/acme/embed.json", {"embed": 1, "api": "1.1"})
        put("embed/acme/modules/hello/module.json", {"name": "hello", **man})
        put("embed/acme/modules/quiet/module.json", {"name": "quiet", "default_off": True, **man})
        put("embed/acme/modules/greeter/module.json", {"name": "core-one", **man})
        put("modules/core-one/module.json", {"name": "core-one", **man})
        cfg = os.path.join(h.state, "hub.json")
        with open(cfg, "w") as f:
            json.dump({"theme": "x"}, f)
        for p in (mock.patch.dict(os.environ, {"HUB_CONFIG": cfg}), mock.patch.object(modules, "REPO", top)):
            p.start()
            self.addCleanup(p.stop)
        h.seed()

        def rows():
            return {(m["name"], m["source"]): m for m in json.loads(h.req("GET", "/api/modules")[1])["installed"]}
        got = rows()
        at = lambda rel: os.path.join(top, *rel.split("/"))  # noqa: E731
        self.assertEqual(sorted(got), sorted([("hello", at("embed/acme/modules/hello")),
                                              ("quiet", at("embed/acme/modules/quiet")),
                                              ("core-one", at("embed/acme/modules/greeter"))]))   # the core's is not listed
        hello, quiet, clash = (got[("hello", at("embed/acme/modules/hello"))], got[("quiet", at("embed/acme/modules/quiet"))],
                               got[("core-one", at("embed/acme/modules/greeter"))])
        self.assertEqual((hello["enabled"], hello["state"] != "disabled"), (True, True))
        self.assertEqual((quiet["enabled"], quiet["state"]), (False, "disabled"))
        self.assertEqual(clash["state"], "blocked")
        self.assertIn(at("embed/acme/modules/greeter"), clash["reason"])
        self.assertEqual(hello["embed"], "acme")
        self.assertEqual(h.req("POST", "/api/modules/enable", {"name": "quiet", "enabled": True, "embed": True})[0], 200)
        self.assertEqual(h.req("POST", "/api/modules/enable", {"name": "hello", "enabled": False, "embed": True})[0], 200)
        changed = [e["data"] for e in h.spooled() if e["type"] == "settings.changed"]
        self.assertIn({"what": "modules.off", "old": [], "new": ["hello"]}, [{k: d.get(k) for k in ("what", "old", "new")}
                                                                             for d in changed])
        with open(cfg) as f:
            saved = json.load(f)
        self.assertEqual((saved["theme"], saved["modules"]), ("x", {"enabled": ["quiet"], "off": ["hello"]}))
        got = rows()
        self.assertEqual((got[("hello", at("embed/acme/modules/hello"))]["enabled"],
                          got[("hello", at("embed/acme/modules/hello"))]["state"]), (False, "disabled"))
        self.assertTrue(got[("quiet", at("embed/acme/modules/quiet"))]["enabled"])
        self.assertEqual(h.req("POST", "/api/modules/enable", {"name": "hello", "enabled": True, "embed": True})[0], 200)
        with open(cfg) as f:
            self.assertEqual(json.load(f)["modules"], {"enabled": ["quiet", "hello"], "off": []})
        # the row of a copy with its name (a folder of modules.paths) switches modules.enabled only: the embed's runs on
        self.assertEqual(h.req("POST", "/api/modules/enable", {"name": "hello", "enabled": False, "embed": False})[0], 200)
        with open(cfg) as f:
            self.assertEqual(json.load(f)["modules"], {"enabled": ["quiet"], "off": []})
        # a page from before the embed field: the server finds the embed's module by the name
        self.assertEqual(h.req("POST", "/api/modules/enable", {"name": "hello", "enabled": False})[0], 200)
        with open(cfg) as f:
            self.assertEqual(json.load(f)["modules"], {"enabled": ["quiet"], "off": ["hello"]})
        offs = [e["data"] for e in h.spooled() if e["type"] == "settings.changed" and e["data"].get("what") == "modules.off"]
        self.assertTrue(offs and all(d["old"] != d["new"] for d in offs), offs)   # no event for a list that stayed

    def test_a_source_address_is_shown_without_its_login(self):
        # review: a source address with a login and token (https://u:tok@host/…) is hidden on the page and in hub modules --json
        from unittest import mock
        from core import modules, modsrc
        fake = [{"name": "notes", "version": "1.0.0", "dir": "/x", "origin": "git", "manifest": {}, "errors": [],
                 "source": "https://u:ghp_secret@example.invalid/m.git", "commit": "a" * 40, "ref": None}]
        with mock.patch.object(modsrc, "discover", return_value=fake):
            (row,) = [r for r in modules.rows({}, self.helper.state, {}) if r["origin"] == "git"]
        self.assertEqual(row["source"], "https://***@example.invalid/m.git")

    def test_enable_keeps_entries_it_does_not_understand(self):
        # review: an entry in modules.enabled that is not a string (hand-edited) is no reason to erase the other enabled ones
        from unittest import mock
        h = self.helper
        cfg = os.path.join(h.state, "hub.json")
        with open(cfg, "w") as f:
            json.dump({"modules": {"paths": [EXAMPLE], "enabled": ["a", 3]}}, f)
        env = mock.patch.dict(os.environ, {"HUB_CONFIG": cfg})
        env.start()
        self.addCleanup(env.stop)
        self.assertEqual(h.req("POST", "/api/modules/enable", {"name": "hello", "enabled": True})[0], 200)
        with open(cfg) as f:
            self.assertEqual(json.load(f)["modules"]["enabled"], ["a", 3, "hello"])

    def test_reset_and_forget_go_to_the_core_as_the_owner(self):
        h = self.helper
        h.seed()
        h.spooled()
        h.core_decides()
        self.assertEqual(h.req("POST", "/api/modules/reset", {"name": "Bad Name"})[0], 400)
        code, body = h.req("POST", "/api/modules/reset", {"name": "hello"})
        self.assertEqual(code, 409)                                         # it had not failed — the core refuses in words
        # in the page's language (the tests' page is English): the core's phrase from its marks
        self.assertEqual(i18n.t("core.modrun.reset_nothing", name="hello"), json.loads(body)["error"])
        for _ in range(3):
            h.j.append("module.error", "hello", {"module": "hello", "hook": "call", "kind": "error", "error": "boom",
                                                 "channel": "core", "owner": False})
        code, body = h.req("POST", "/api/modules/reset", {"name": "hello"})
        self.assertEqual(code, 202, body)
        ev = [e for e in __import__("core.journal").journal.read(h.j.path) if e["type"] == "module.reset"]
        self.assertEqual((ev[-1]["data"]["module"], ev[-1]["data"]["owner"], ev[-1]["data"]["channel"]),
                         ("hello", True, "web"))
        code, body = h.req("POST", "/api/modules/forget", {"name": "hello"})
        self.assertEqual(code, 409)                                         # it wrote nothing to the core state — nothing to clear

    def test_core_remembers_when_it_started(self):
        from core import reducer
        st, _, _ = reducer.reduce(reducer.empty(), {"seq": 1, "ts": 123.0, "type": "core.started", "entity": None,
                                                     "data": {}})
        self.assertEqual(st["started"], 123.0)


class UpdatesOfAHubFromTheLine(unittest.TestCase):
    """A hub from the install line updates by packages: its code folder is a layer, `git remote` there answers nothing
    (the stop file), and the card must not call the hub "the shared repository itself"."""

    def card(self, update):
        from modules.web import modinfo
        none = subprocess.CompletedProcess([], 128, "", "fatal: not a git repository")
        with mock.patch.object(modinfo.subprocess, "run", return_value=none):
            (m,) = [x for x in modinfo.modules(tempfile.mkdtemp(prefix="mi-"), {"update": update}, {}, lang="en")
                    if x["id"] == "update"]
        return m

    def test_the_update_by_packages_is_on_without_a_remote(self):
        m = self.card({"enabled": True, "remote": "origin", "app": {"url": "https://updates.example.test"}})
        self.assertEqual((m["state"], m["detail"]), ("on", ""))

    def test_an_app_setting_that_is_no_object_is_no_update_by_packages(self):   # the updater reads only an object
        for app in (None, {}, "x", True):
            self.assertEqual(self.card({"enabled": True, "remote": "origin", "app": app})["state"],
                             "on" if app == {} else "off", app)

    def test_the_shared_repository_itself_has_no_remote(self):
        m = self.card({"enabled": True, "remote": "origin"})
        self.assertEqual(m["state"], "off")


if __name__ == "__main__":
    unittest.main()
