"""The "Settings" tab: hub settings from the page (owner decision 2026-10-11). The page writes hub.json itself — only
whitelisted keys, a value is checked by type and bounds, every edit is a journal event; secrets, paths, commands and
lists are never changed from the page (otherwise the page = running any command on the machine)."""
import tests  # noqa: F401 — keeps the machine's settings out
import json
import os
import tempfile
import unittest
from unittest import mock

from modules.web import settings
from tests import test_web
from tests.fakes import tz
from tests.fakes import cut


class Rules(unittest.TestCase):
    def test_only_whitelisted_keys(self):
        for key in ("voice.cmd", "extensions.x.run", "mcp", "rules", "hub.cwd", "browser.env", "projects", "sets",
                    "browsers.main.headers", "testchrome.path", "__proto__", "", "title.x"):
            self.assertIsNotNone(settings.validate(key, True)[1], key)

    def test_values_are_checked_by_type_and_range(self):
        ok = {"effort": "high", "model": "sonnet", "park_after_hours": 4, "critic.enabled": True, "timezone": "Europe/Paris",
              "update.branch": "main", "compact_idle.min_tokens": 300000, "watch.off": True,
              "watch.recap_steps": 20, "watch.recap_task": 1000, "restart_wait_minutes": 0}
        for key, value in ok.items():
            if key == "timezone" and not tz.HAVE:
                continue   # Windows without tzdata knows no named zone: the setting is refused there (core/zones.py)
            self.assertEqual(settings.validate(key, value), (value, None), key)
        bad = {"effort": "ultra", "model": "gpt", "park_after_hours": -1, "critic.enabled": "yes", "timezone": "Mars/Base",
               "update.branch": "--upload-pack=x", "compact_idle.min_tokens": 10, "park_after_hours ": 2,
               "critic.model": "a b; rm -rf", "update.every": True, "watch.recap_steps": 0, "watch.recap_task": 99999,
               "restart_wait_minutes": -5}
        for key, value in bad.items():
            self.assertIsNotNone(settings.validate(key, value)[1], key)

    def test_model_can_go_back_to_the_claude_default(self):
        self.assertEqual(settings.validate("model", ""), ("", None))
        cfg = {"model": "opus"}
        settings.put(cfg, "model", "")
        self.assertNotIn("model", cfg)

    def test_view_has_groups_simple_and_advanced_and_current_values(self):
        rows = settings.view({"effort": "high", "critic": {"enabled": True}}, {"off": True})   # the critic is on — its group is there
        by = {r["key"]: r for r in rows}
        self.assertEqual(by["effort"]["value"], "high")
        self.assertEqual(by["park_after_hours"]["value"], 2)          # not in the file — the default value
        # a module is switched on in the "Modules" tab (owner decision 2026-10-11); here only its settings, under its name
        self.assertNotIn("critic.enabled", by)
        self.assertNotIn("watch.off", by)
        self.assertEqual(by["critic.engine"]["group"], "Critic before a decision")
        self.assertFalse(by["critic.engine"]["advanced"])
        self.assertTrue(by["critic.model"]["advanced"])
        self.assertTrue(by["effort"]["advanced"])                    # a fallback — under "Advanced" (owner decision 2026-10-11)
        self.assertFalse(by["park_after_hours"]["advanced"])
        self.assertTrue(by["compact_idle.after_minutes"]["advanced"])
        self.assertTrue(all(r["group"] and r["label"] for r in rows))
        self.assertNotIn("voice.cmd", by)

    def test_place_splits_the_page_and_old_clients_keep_group_and_advanced(self):
        # the 2026-10-16 simplification of "Settings": place is where a row goes on the page; group and advanced are as
        # before (old clients and pages lay out by them); rows hidden from the screen stay in the snapshot and the whitelist
        rows = settings.view({"critic": {"enabled": True}, "voice_reply": {"enabled": True}}, {})
        by = {r["key"]: r for r in rows}
        main = {k for k, r in by.items() if r["place"] == "main"}
        self.assertEqual(main, {"title", "timezone", "language", "executors.parallel", "permissions.auto_allow"})
        self.assertEqual(by["critic.model"]["place"], "module:critic")
        self.assertEqual(by["update.tests_timeout"]["place"], "module:update")
        self.assertEqual(by["telegram.quiet"]["place"], "module:telegram")
        self.assertEqual(by["voice_reply.voice"]["place"], "module:voice_reply")
        self.assertEqual(by["effort"]["place"], "more")
        self.assertEqual(by["web.upload_max_mb"]["place"], "more")
        self.assertEqual(by["journal.rotate_mb"]["place"], "more")   # the owner asked to see it (2026-10-11)
        for key in ("update.every", "update.branch"):
            self.assertEqual(by[key]["place"], "hidden")
            self.assertIsNone(settings.validate(key, by[key]["value"])[1])   # the hub and clients still set it
        self.assertEqual((by["critic.model"]["group"], by["critic.model"]["advanced"]), ("Critic before a decision", True))
        self.assertEqual((by["journal.rotate_mb"]["group"], by["journal.rotate_mb"]["advanced"]), ("Hub", True))
        self.assertTrue(all({"group", "advanced", "place"} <= r.keys() for r in rows))
        self.assertTrue(all(r["place"] in ("main", "more", "hidden") or r["place"].startswith("module:") for r in rows))

    def test_a_module_that_is_off_has_no_settings_group(self):
        # owner decision 2026-10-11: settings of modules that are off are not shown — a module's group exists only when it is on
        groups = {r["group"] for r in settings.view({"critic": {"enabled": False}}, {})}
        self.assertNotIn("Critic before a decision", groups)
        self.assertNotIn("Voice reply in Telegram", groups)       # off by default
        self.assertIn("Updates", groups)                          # on by default
        groups = {r["group"] for r in settings.view({"critic": {"enabled": True}, "update": {"enabled": False}}, {})}
        self.assertIn("Critic before a decision", groups)
        self.assertNotIn("Updates", groups)

    def test_plugin_update_restart_is_an_advanced_session_switch_off_by_default(self):
        # restarting sessions after their plugin is updated (PR #109) — a switch in the tab, not only in hub.json
        self.assertEqual(settings.validate("plugin_update_restart", True), (True, None))
        self.assertIsNotNone(settings.validate("plugin_update_restart", "yes")[1])
        by = {r["key"]: r for r in settings.view({}, {})}
        row = by["plugin_update_restart"]
        self.assertEqual((row["kind"], row["value"], row["group"], row["advanced"]), ("bool", False, "Sessions", True))
        self.assertTrue({r["key"]: r for r in settings.view({"plugin_update_restart": True}, {})}
                        ["plugin_update_restart"]["value"])
        cfg = {}
        settings.put(cfg, "plugin_update_restart", True)
        self.assertEqual(cfg, {"plugin_update_restart": True})   # a top-level key — the core reads it

    def test_the_update_checks_time_limit_is_an_advanced_row_of_the_update_module(self):
        # PR #517: update.tests_timeout — a slow machine with its own core edits raises it; the page keeps it within
        # the bounds the updater reads (modules/update tests_timeout), in whole seconds
        from modules.update import update
        self.assertEqual(settings.validate("update.tests_timeout", 3600), (3600, None))
        for bad in (600, update.TESTS_TIMEOUT_MAX + 1, 1500.5, "3600", True):
            self.assertIsNotNone(settings.validate("update.tests_timeout", bad)[1], bad)
        row = {r["key"]: r for r in settings.view({}, {})}["update.tests_timeout"]
        self.assertEqual((row["kind"], row["value"], row["advanced"], row["place"], row["min"], row["max"]),
                         ("num", update.TESTS_TIMEOUT, True, "module:update", update.TESTS_TIMEOUT,
                          update.TESTS_TIMEOUT_MAX))
        cfg = {"update": {"enabled": True}}
        settings.put(cfg, "update.tests_timeout", 3600)
        self.assertEqual(update.tests_timeout(cfg), 3600)

    def test_the_mailbox_check_mode_is_seen_and_set_here(self):
        # admit.mode (core/admit.py): auto by default, under "Advanced" of the hub; the auto choice names the mode in force
        for value in ("auto", "flag", "refuse"):
            self.assertEqual(settings.validate("admit.mode", value), (value, None))
        for value in ("deny", "", True, None):
            self.assertIsNotNone(settings.validate("admit.mode", value)[1], value)
        row = {r["key"]: r for r in settings.view({}, {}, "en")}["admit.mode"]
        self.assertEqual((row["value"], row["choices"], row["place"], row["group"]),
                         ("auto", ["auto", "flag", "refuse"], "more", "Hub"))
        self.assertEqual(row["names"], {"auto": "Automatic (now: flag)", "flag": "Only flag", "refuse": "Refuse"})
        row = {r["key"]: r for r in settings.view({"admit": {"mode": "flag"}}, {}, "en", "refuse")}["admit.mode"]
        self.assertEqual((row["value"], row["names"]["auto"]), ("flag", "Automatic (now: refuse)"))
        cfg = {}
        settings.put(cfg, "admit.mode", "flag")
        self.assertEqual(cfg, {"admit": {"mode": "flag"}})
        from core import admit
        self.assertEqual(admit.setting(cfg), "flag")   # what the page writes the core reads

    def test_the_pair_of_one_agents_executors_is_allowed_by_key_and_checked(self):
        # executors.agents.<agent>.model|effort: the agents are not known in advance, so the allowlist takes the key by shape
        for key, value in (("executors.agents.maps.model", "opus"), ("executors.agents.maps.effort", "high"),
                           ("executors.agents.maps.model", ""), ("executors.agents.maps.effort", ""),
                           ("executors.agents.a.b-c_d.model", "claude-opus-4-5")):
            self.assertEqual(settings.validate(key, value), (value, None), key)
        for key, value in (("executors.agents.maps.model", "gpt-9"), ("executors.agents.maps.effort", "ultra"),
                           ("executors.agents.maps.model", True), ("executors.agents.maps.cmd", "opus"),
                           ("executors.agents.Maps.model", "opus"), ("executors.agents..model", "opus"),
                           ("executors.agents.-x.model", "opus"), ("executors.agents.maps.model.x", "opus"),
                           ("executors.agents.maps", "opus"), ("executors.agents.x y.model", "opus"),
                           ("executors.agents." + "a" * 41 + ".model", "opus")):
            self.assertIsNotNone(settings.validate(key, value)[1], (key, value))

    def test_the_pair_of_an_agent_is_written_under_its_name_and_empty_removes_it(self):
        cfg = {"executors": {"normal": {"effort": "low"}}}
        self.assertIsNone(settings.put(cfg, "executors.agents.maps.model", "opus"))
        self.assertEqual(settings.put(cfg, "executors.agents.maps.effort", "high"), None)
        self.assertEqual(cfg["executors"], {"normal": {"effort": "low"}, "agents": {"maps": {"model": "opus", "effort": "high"}}})
        self.assertEqual(settings.put(cfg, "executors.agents.v1.2.model", "sonnet"), None)   # a dot in the name is the name's
        self.assertEqual(cfg["executors"]["agents"]["v1.2"], {"model": "sonnet"})
        self.assertEqual(settings.put(cfg, "executors.agents.maps.model", ""), "opus")      # empty: not a stored empty string
        self.assertEqual(cfg["executors"]["agents"]["maps"], {"effort": "high"})
        settings.put(cfg, "executors.agents.maps.effort", "")
        self.assertNotIn("maps", cfg["executors"]["agents"])                                # the agent leaves with its last half
        self.assertEqual(settings.put(cfg, "executors.agents.nobody.model", ""), None)
        self.assertNotIn("nobody", cfg["executors"]["agents"])
        from core import modelrule
        self.assertEqual(modelrule.own("v1.2", cfg), {"model": "sonnet"})                    # what the page writes the core reads
        cfg = {"critic": {"engine": "claude"}}
        settings.put(cfg, "executors.agents.maps.model", "opus")
        settings.put(cfg, "executors.agents.maps.model", "")
        self.assertEqual(cfg, {"critic": {"engine": "claude"}})                              # the emptied parents go with the agent

    def test_every_agent_has_a_pair_row_empty_by_default(self):
        cfg = {"executors": {"agents": {"maps": {"model": "opus", "effort": "high"}, "gone": {"effort": "max"}}}}
        rows = settings.view(cfg, {}, "en", agents=["maps", "docs"])
        by = {r["key"]: r for r in rows}
        self.assertEqual((by["executors.agents.maps.model"]["value"], by["executors.agents.maps.effort"]["value"]), ("opus", "high"))
        self.assertEqual((by["executors.agents.docs.model"]["value"], by["executors.agents.docs.effort"]["value"]), ("", ""))
        # an agent that was set up and has gone is still shown: otherwise its pair could not be cleared from the page
        self.assertEqual(by["executors.agents.gone.effort"]["value"], "max")
        row = by["executors.agents.maps.model"]
        self.assertEqual((row["agent"], row["group"], row["place"], row["kind"]), ("maps", "Executors", "more", "enum"))
        self.assertEqual(row["choices"][0], "")
        self.assertEqual(by["executors.agents.maps.effort"]["choices"], ["", "low", "medium", "high", "xhigh", "max"])
        self.assertIn("maps", row["label"])
        self.assertEqual([r["agent"] for r in rows if r["key"].endswith(".model") and "agent" in r], ["maps", "docs", "gone"])
        self.assertFalse([r for r in settings.view({}, {}, "en") if "agent" in r])             # no agents — no rows

    def test_a_pair_the_file_holds_but_the_page_does_not_list_is_still_shown(self):
        row = {r["key"]: r for r in settings.view({"executors": {"agents": {"maps": {"model": "claude-opus-4-5"}}}}, {},
                                                  agents=["maps"])}["executors.agents.maps.model"]
        self.assertEqual(row["value"], "claude-opus-4-5")
        self.assertIn("claude-opus-4-5", row["choices"])                                       # a full id stays a choice
        junk = {"executors": {"agents": {"maps": "opus", "x": ["a"], "bad name": {"model": "opus"}}}}
        by = {r["key"] for r in settings.view(junk, {}, agents=["maps"])}                     # a broken file does not break the page
        self.assertIn("executors.agents.maps.model", by)
        self.assertFalse([k for k in by if "bad name" in k or ".x." in k])

    def test_a_file_with_a_wrong_shape_does_not_break_the_page_or_the_save(self):
        # executors.agents is a list, executors is a string: the rows are still there, an empty save is a no-op, not a traceback
        for cfg in ({"executors": {"agents": ["maps"]}}, {"executors": {"agents": "x"}}, {"executors": "x"}):
            keys = {r["key"] for r in settings.view(cfg, {}, "en", None, ["maps"])}
            self.assertIn("executors.agents.maps.model", keys, cfg)
            for key in ("executors.agents.maps.model", "model"):
                self.assertIsNone(settings.put(cfg, key, ""), (cfg, key))
        cfg = {"executors": "x"}
        settings.put(cfg, "executors.agents.maps.model", "opus")                                # a real value replaces the junk
        self.assertEqual(cfg["executors"], {"agents": {"maps": {"model": "opus"}}})

    def test_nested_put_keeps_the_rest_of_the_section(self):
        cfg = {"critic": {"engine": "claude"}}
        settings.put(cfg, "critic.enabled", True)
        self.assertEqual(cfg, {"critic": {"engine": "claude", "enabled": True}})


class Endpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-settings-")
        self.cfg = os.path.join(self.tmp, "hub.json")
        with open(self.cfg, "w") as f:
            json.dump({"dev": {"enabled": False}, "critic": {"engine": "claude"}, "voice": {"cmd": "whisper {wav}"}}, f)
        self.env = mock.patch.dict(os.environ, {"HUB_CONFIG": self.cfg,
                                                "HUB_WATCH_CONFIG": os.path.join(self.tmp, "watch.json")})
        self.env.start()
        self.h = test_web.WebTest("test_page_and_snapshot")
        self.h.setUp()

    def tearDown(self):
        self.h.tearDown()
        self.env.stop()

    def saved(self):
        with open(self.cfg) as f:
            return json.load(f)

    def test_set_writes_hub_json_and_the_journal(self):
        self.h.seed()
        code, out = self.h.req("POST", "/api/settings/set", {"key": "critic.enabled", "value": True})
        self.assertEqual(code, 200, out)
        self.assertEqual(self.saved()["critic"], {"engine": "claude", "enabled": True})
        self.assertEqual(self.saved()["voice"], {"cmd": "whisper {wav}"})          # what others put in the file stays as it was
        evs = [e for e in self.h.spooled() if e["type"] == "settings.changed"]
        self.assertEqual({k: evs[-1]["data"][k] for k in ("what", "old", "new")}, {"what": "critic.enabled", "old": None, "new": True})
        code, out = self.h.req("POST", "/api/settings/set", {"key": "watch.off", "value": False})   # "Session watch" is switched off
        self.assertEqual(code, 200, out)
        with open(os.path.join(self.tmp, "watch.json")) as f:
            self.assertEqual(json.load(f), {"off": True})

    def test_the_pair_of_an_agent_is_saved_and_listed_in_the_snapshot(self):
        # the agents of the hub (role agent) and the hub itself get a pair row; a worker or an executor does not
        for name, role, kind in (("maps", "agent", None), ("docs", "agent", None), ("w", "worker", None), ("hub", "worker", "hub")):
            self.h.j.append("session.requested", name, {"session_id": f"s-{name}", "cwd": "/w", "role": role,
                                                        **({"kind": kind} if kind else {})})
        self.h.settle()
        rows = {r["key"]: r for r in self.h.snap()["settings"]}
        self.assertEqual({r["agent"] for r in rows.values() if "agent" in r}, {"hub", "maps", "docs"})
        self.assertEqual(rows["executors.agents.maps.model"]["value"], "")
        code, out = self.h.req("POST", "/api/settings/set", {"key": "executors.agents.maps.model", "value": "opus"})
        self.assertEqual(code, 200, out)
        code, out = self.h.req("POST", "/api/settings/set", {"key": "executors.agents.maps.effort", "value": "high"})
        self.assertEqual(code, 200, out)
        self.assertEqual(self.saved()["executors"]["agents"], {"maps": {"model": "opus", "effort": "high"}})
        self.assertEqual(self.saved()["critic"], {"engine": "claude"})                        # the rest of the file stays
        evs = [e for e in self.h.spooled() if e["type"] == "settings.changed"]
        self.assertEqual({k: evs[-1]["data"][k] for k in ("what", "old", "new")},
                         {"what": "executors.agents.maps.effort", "old": None, "new": "high"})
        self.assertIn("maps", json.loads(out)["text"])                                       # "Saved: Model: maps"
        code, out = self.h.req("POST", "/api/settings/set", {"key": "executors.agents.maps.model", "value": ""})
        self.assertEqual(code, 200, out)
        self.assertEqual(self.saved()["executors"]["agents"], {"maps": {"effort": "high"}})
        for key, value in (("executors.agents.maps.model", "gpt-9"), ("executors.agents.Bad.model", "opus"),
                           ("executors.agents.maps.cmd", "x")):
            self.assertEqual(self.h.req("POST", "/api/settings/set", {"key": key, "value": value})[0], 400, key)
        self.assertEqual(self.saved()["executors"]["agents"], {"maps": {"effort": "high"}})

    def test_the_agents_are_read_again_when_a_fold_changes_the_sessions_in_between(self):
        # the page thread reads the reader's dicts without its lock: "dictionary changed size during iteration" is retried
        from types import SimpleNamespace
        from modules.web import server

        class Busy(dict):
            left = 2

            def items(self):
                if self.left:
                    self.left -= 1
                    raise RuntimeError("dictionary changed size during iteration")
                return super().items()
        state = {"sessions": Busy(maps={"role": "agent", "state": "idle"}, w={"role": "worker"})}
        web = SimpleNamespace(tail=SimpleNamespace(reader=SimpleNamespace(state=state)))
        rows = server.Web.settings_view(web, {}, "en")
        self.assertEqual([r["agent"] for r in rows if r["key"].endswith(".model") and "agent" in r], ["maps"])
        state["sessions"].left = 99                                                    # busy for good: the last list, not none
        rows = server.Web.settings_view(web, {}, "en")
        self.assertEqual([r["agent"] for r in rows if r["key"].endswith(".model") and "agent" in r], ["maps"])

    def test_update_manual_is_a_row_of_a_hub_on_a_code_layer_only(self):
        from types import SimpleNamespace
        from unittest import mock
        from core import packaged
        from modules.web import server
        web = SimpleNamespace(tail=SimpleNamespace(reader=SimpleNamespace(state={"sessions": {}})),
                              t=lambda key, **kw: key)
        keys = lambda: [r["key"] for r in server.Web.settings_view(web, {}, "en")]   # noqa: E731
        self.assertNotIn("update.manual", keys())                                       # from git: it does nothing
        with mock.patch.object(packaged, "info", lambda: {"format": "app", "runtime": 1}):
            self.assertNotIn("update.manual", keys())                                   # the app from git, no layer
        with mock.patch.object(packaged, "info", lambda: {"format": "app", "runtime": 1, "layout": "layer"}):
            self.assertIn("update.manual", keys())
        code, out = self.h.req("POST", "/api/settings/set", {"key": "update.manual", "value": True})
        self.assertEqual(code, 200, out)
        self.assertIs(self.saved()["update"]["manual"], True)

    def test_executor_effort_is_saved_for_each_kind_of_work(self):
        # 19.10: effort "high" for Opus/Sonnet was not saved (the hub in memory ran old code without these keys)
        self.h.seed()
        for work in ("risky", "normal"):
            code, out = self.h.req("POST", "/api/settings/set", {"key": f"executors.{work}.effort", "value": "high"})
            self.assertEqual(code, 200, out)
        self.assertEqual({w: self.saved()["executors"][w]["effort"] for w in ("risky", "normal")},
                         {"risky": "high", "normal": "high"})

    def test_page_and_market_writers_keep_both_changes(self):
        # the page (hub name, setting, plugin) and `hub market` read hub.json, edited and wrote it whole: the later
        # write lost the other's edit. The first reads and waits (up to a second) until the second has read
        import threading
        from core import catalog
        self.h.seed()

        def race(first, second):
            real, first_read, second_read, readers = catalog.read_settings, threading.Event(), threading.Event(), []

            def read(path):
                cfg = real(path)
                if not readers:
                    readers.append(1)
                    first_read.set()
                    second_read.wait(1)
                else:
                    second_read.set()
                return cfg
            with mock.patch.object(catalog, "read_settings", read):
                a = threading.Thread(target=first)
                a.start()
                self.assertTrue(first_read.wait(10))
                b = threading.Thread(target=second)
                b.start()
                a.join(10)
                b.join(10)
        def market(key):   # what `hub market` writes (install, remove) goes through its editing()
            with self.h.web.market().editing() as cfg:
                cfg.setdefault("sets", []).append({"name": key})
        race(lambda: self.h.req("POST", "/api/settings/title", {"title": "Owl"}), lambda: market("alice"))
        race(lambda: self.h.req("POST", "/api/settings/set", {"key": "critic.enabled", "value": True}),
             lambda: market("bob"))
        race(lambda: self.h.web.plugin_toggle("flow", False), lambda: market("carol"))
        cfg = self.saved()
        self.assertEqual((cfg["title"], cfg["critic"]["enabled"]), ("Owl", True))
        self.assertEqual(sorted(s["name"] for s in cfg["sets"]), ["alice", "bob", "carol"])
        self.assertTrue(cfg["extensions"]["flow"]["disabled"])
        with open(self.cfg, "rb") as f:
            before = f.read()
        self.assertEqual(self.h.web.plugin_toggle("nope", True)[0], 404)
        with open(self.cfg, "rb") as f:
            self.assertEqual(f.read(), before)   # no such plugin — the file is not rewritten

    def test_forbidden_key_or_bad_value_is_refused_and_nothing_is_written(self):
        self.h.seed()
        before = self.saved()
        for body in ({"key": "voice.cmd", "value": "rm -rf /"}, {"key": "effort", "value": "ultra"},
                     {"key": "extensions.x.run", "value": ["sh"]}, {"value": True}):
            self.assertEqual(self.h.req("POST", "/api/settings/set", body)[0], 400, body)
        self.assertEqual(self.saved(), before)

    @cut.needs("stats")
    def test_module_switches_live_on_the_modules_tab_and_go_through_the_whitelist(self):
        self.h.seed()
        mods = {m["id"]: m for m in json.loads(self.h.req("GET", "/api/modules")[1])["modules"]}
        for mid in ("critic", "video", "update", "watch", "surrender", "promise", "voice_reply", "stats"):
            self.assertTrue(mods[mid].get("toggle"), mid)
        self.assertFalse(mods["promise"]["enabled"])                       # the promise reminder is off
        self.assertEqual(self.h.req("POST", "/api/modules/toggle", {"id": "critic", "enabled": True})[0], 200)
        self.assertEqual(self.saved()["critic"], {"engine": "claude", "enabled": True})
        self.assertEqual(self.h.req("POST", "/api/modules/toggle", {"id": "watch", "enabled": False})[0], 200)
        with open(os.path.join(self.tmp, "watch.json")) as f:
            self.assertEqual(json.load(f), {"off": True})                  # "Session watch" is off — off in its file
        mods = {m["id"]: m for m in json.loads(self.h.req("GET", "/api/modules")[1])["modules"]}
        self.assertEqual((mods["critic"]["enabled"], mods["watch"]["enabled"]), (True, False))
        self.assertEqual(self.h.req("POST", "/api/modules/toggle", {"id": "files", "enabled": True})[0], 400)
        self.assertEqual(self.h.req("POST", "/api/modules/toggle", {"id": "promise", "enabled": True})[0], 200)
        with open(os.path.join(self.tmp, "watch.json")) as f:
            self.assertEqual(json.load(f), {"off": True, "promise": True})
        mods = {m["id"]: m for m in json.loads(self.h.req("GET", "/api/modules")[1])["modules"]}
        self.assertEqual((mods["promise"]["state"], mods["promise"]["enabled"]), ("part", True))   # without the watch's model
        self.assertTrue(mods["promise"]["detail"])

    def test_snapshot_carries_the_settings(self):
        self.h.seed()
        snap = json.loads(self.h.req("GET", "/api/snapshot")[1])
        keys = {r["key"] for r in snap["settings"]}
        self.assertIn("effort", keys)
        self.assertIn("title", keys)

    def test_packaged_hub_cannot_turn_updates_on(self):
        self.h.seed()
        with mock.patch("core.packaged.info", return_value={}):
            code, _ = self.h.req("POST", "/api/settings/set", {"key": "update.enabled", "value": True})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
