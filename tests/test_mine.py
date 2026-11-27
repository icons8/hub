"""A person's own "superpowers" go into the hub catalog (owner decision 2026-10-02: colleagues forwarded their own env and
settings into sessions, while the hub should see their skills, plugins and MCP itself and add them to the catalog on
request; sessions get them as needed). Config values (keys in args/headers) are not printed."""
import tests  # noqa: F401 — don't see the machine's settings
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from core import catalog, mine
from tests.fakes import lang

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# a Russian word of the language data with a Cyrillic capital: its UTF-8 has byte 0x98, which cp1251 lacks
WORD = lang.word("words", "topic_label", 1).capitalize()


class Mine(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="mine-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.state = os.path.join(self.home, "state")
        self.cfg_path = os.path.join(self.home, "hub.json")
        os.makedirs(self.state)
        os.makedirs(os.path.join(self.home, "code", "shop", ".git"))
        c = os.path.join(self.home, ".claude")
        market = os.path.join(c, "plugins", "marketplaces", "team-tools")
        os.makedirs(os.path.join(market, ".claude-plugin"))
        with open(os.path.join(market, ".claude-plugin", "marketplace.json"), "w") as f:
            json.dump({"name": "team-tools", "plugins": [{"name": "deploy", "source": "./deploy"}]}, f)
        with open(os.path.join(c, "plugins", "known_marketplaces.json"), "w") as f:
            json.dump({"team-tools": {"installLocation": market}}, f)
        with open(os.path.join(c, "settings.json"), "w") as f:
            json.dump({"enabledPlugins": {"deploy@team-tools": True}}, f)
        skill = os.path.join(c, "skills", "release-notes")
        os.makedirs(skill)
        with open(os.path.join(skill, "SKILL.md"), "w") as f:
            f.write("---\nname: release-notes\ndescription: Writes release notes from git log\n---\nbody\n")
        os.makedirs(os.path.join(c, "skills", "folded"))
        with open(os.path.join(c, "skills", "folded", "SKILL.md"), "w") as f:
            f.write("---\nname: folded\ndescription: >-\n  Two lines\n  of text\nlicense: x\n---\n")
        with open(os.path.join(self.home, ".claude.json"), "w") as f:
            json.dump({"projects": {os.path.join(self.home, "code", "shop"): {}, "/gone/old": {}},
                       "mcpServers": {"tracker": {"type": "http", "url": "https://t.example/mcp",
                                                  "headers": {"Authorization": "Bearer fake-secret-123"}}}}, f)
        with open(self.cfg_path, "w") as f:
            json.dump({"timezone": "UTC"}, f)
        self.env = {"HOME": self.home, "HUB_CONFIG": self.cfg_path}

    def found(self):
        return mine.discover(self.state, self.env)

    def cfg(self):
        with open(self.cfg_path, encoding="utf-8") as f:
            return json.load(f)

    def test_discover_lists_sets_skills_and_mcp_without_values(self):
        got = self.found()
        self.assertEqual([(x["name"], x["in_hub"]) for x in got["sets"]], [("team-tools", False)])
        self.assertEqual(got["sets"][0]["enabled"], ["deploy"])
        self.assertEqual([(x["name"], x["description"]) for x in got["skills"]],
                         [("folded", "Two lines of text"), ("release-notes", "Writes release notes from git log")])
        self.assertEqual([(x["name"], x["type"]) for x in got["mcp"]], [("tracker", "http")])
        self.assertNotIn("fake-secret", json.dumps(got))
        self.assertEqual([(x["name"], x["in_hub"]) for x in got["projects"]], [("shop", False)])  # not on disk — no

    def test_import_set_mcp_and_skill_into_the_catalog(self):
        mine.import_set(self.state, "team-tools", self.env)
        mine.import_mcp(self.state, "tracker", "Team tasks", ["external"], self.env)
        mine.import_skill(self.state, "release-notes", [], self.env)
        cfg = self.cfg()
        self.assertEqual(cfg["timezone"], "UTC")                      # the rest of the settings untouched
        self.assertEqual(cfg["mcp"]["tracker"]["server"]["headers"]["Authorization"], "Bearer fake-secret-123")
        self.assertEqual((cfg["mcp"]["tracker"]["description"], cfg["mcp"]["tracker"]["risk"]),
                         ("Team tasks", ["external"]))
        tools = catalog.build({**catalog.DEFAULTS, **cfg})
        self.assertIn("deploy@team-tools", tools)
        self.assertIn("tracker", tools)
        self.assertIn("release-notes@my-skills", tools)
        skill_md = os.path.join(tools["release-notes@my-skills"]["path"], "skills", "release-notes", "SKILL.md")
        self.assertTrue(os.path.exists(skill_md))                     # the same skill, not a copy
        found = self.found()
        self.assertEqual([x["in_hub"] for x in (found["sets"][0], found["skills"][1], found["mcp"][0])],
                         [True, True, True])
        self.assertFalse(found["skills"][0]["in_hub"])

    def test_a_session_never_imports_an_mcp_riskless(self):
        # 18.11: `hub tools import mcp <name>` from the hub without --risk wrote the owner's server as riskless — over an
        # entry with his risk too, dropping its only/exclusive — and any session could then grant it to itself
        mine.import_mcp(self.state, "tracker", "Tasks", [], self.env, session=True)
        self.assertEqual(self.cfg()["mcp"]["tracker"]["risk"], mine.SESSION_RISK)
        with catalog.edit_settings(self.cfg_path) as c:
            c["mcp"]["tracker"].update(risk=["money"], only=["maps"], exclusive=True)
        mine.import_mcp(self.state, "tracker", "Tasks again", [], self.env, session=True)
        got = self.cfg()["mcp"]["tracker"]
        self.assertEqual((got["risk"], got["only"], got["exclusive"], got["description"]),
                         (["money"], ["maps"], True, "Tasks again"))
        mine.import_mcp(self.state, "tracker", "Tasks", ["external"], self.env, session=True)
        self.assertEqual(self.cfg()["mcp"]["tracker"]["risk"], ["external", "money"])      # a session only adds
        with catalog.edit_settings(self.cfg_path) as c:                                    # one word, by hand
            c["mcp"]["tracker"]["risk"] = "prod"
        mine.import_mcp(self.state, "tracker", "Tasks", [], self.env, session=True)
        self.assertEqual(self.cfg()["mcp"]["tracker"]["risk"], ["prod"])
        mine.import_mcp(self.state, "tracker", "Tasks", [], self.env)                      # the owner writes it as given
        self.assertEqual(self.cfg()["mcp"]["tracker"], {"server": self.cfg()["mcp"]["tracker"]["server"],
                                                        "description": "Tasks", "risk": []})

    def test_a_sessions_skill_import_keeps_the_skills_risk(self):
        market = os.path.join(self.state, mine.MY_SKILLS, ".claude-plugin", "marketplace.json")

        def keywords():
            with open(market, encoding="utf-8") as f:
                return next(p["keywords"] for p in json.load(f)["plugins"] if p["name"] == "release-notes")
        mine.import_skill(self.state, "release-notes", ["prod"], self.env)
        mine.import_skill(self.state, "release-notes", [], self.env, session=True)
        self.assertEqual(keywords(), ["risk:prod"])
        mine.import_skill(self.state, "release-notes", ["money"], self.env, session=True)
        self.assertEqual(keywords(), ["risk:money", "risk:prod"])
        mine.import_skill(self.state, "release-notes", [], self.env)                       # the owner writes it as given
        self.assertEqual(keywords(), [])

    def test_the_hubs_cli_import_marks_the_session(self):
        state = os.path.join(self.home, "hub-state")
        os.makedirs(state)
        with open(os.path.join(state, "journal.jsonl"), "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"seq": 1, "ts": 1000, "src": "web", "type": "session.requested", "entity": "hub",
                                "data": {"cwd": self.home, "session_id": "s-hub", "kind": "hub", "channel": "web",
                                         "owner": True}}) + "\n")
        env = {**os.environ, **self.env, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0", "HUB_SESSION": "hub"}
        env.pop("CLAUDE_CONFIG_DIR", None)
        r = subprocess.run([sys.executable, os.path.join(REPO, "bin", "hub"), "tools", "import", "mcp", "tracker",
                            "--about", "Tasks"], env=env, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.cfg()["mcp"]["tracker"]["risk"], mine.SESSION_RISK)

    def test_import_twice_and_unknown(self):
        mine.import_set(self.state, "team-tools", self.env)
        mine.import_set(self.state, "team-tools", self.env)
        self.assertEqual(len(self.cfg()["sets"]), 1)
        with self.assertRaises(ValueError):
            mine.import_mcp(self.state, "nope", "x", [], self.env)

    def test_a_sets_risk_goes_to_every_plugin_and_a_typo_does_not_crash(self):
        # a set whose manifest is not the owner's (a vendor's marketplace clone) had no place for a risk of its own:
        # `import set --risk code,prod` dropped the words, and the catalog showed only what the files run
        market = os.path.join(self.home, ".claude", "plugins", "marketplaces", "team-tools")
        os.makedirs(os.path.join(market, "deploy", ".claude-plugin"))
        with open(os.path.join(market, "deploy", ".claude-plugin", "plugin.json"), "w") as f:
            json.dump({"name": "deploy", "description": "Deploys"}, f)
        risk = lambda value: catalog.build({**catalog.DEFAULTS, "sets": [   # noqa: E731
            {"name": "team-tools", "path": market, "risk": value}]})["deploy@team-tools"]["risk"]
        self.assertEqual(risk(["prod", "code", "prod"]), ["code", "prod"])
        self.assertEqual(risk("prod"), ["prod"])                      # one word by hand
        for junk in ("", -1, 0, None, {"a": 1}, [None, "", 3], True):
            self.assertEqual(risk(junk), [], junk)
        self.assertEqual(catalog.set_risk({"risk": ["a", "", None, "b"]}), ["a", "b"])
        self.assertEqual(catalog.set_risk("not a record"), [])

    def test_import_set_with_a_risk_writes_it_and_a_session_only_adds(self):
        mine.import_set(self.state, "team-tools", self.env, risk=["prod", ""])
        self.assertEqual(self.cfg()["sets"], [{"name": "team-tools", "path": self.found()["sets"][0]["path"],
                                               "risk": ["prod"]}])
        mine.import_set(self.state, "team-tools", self.env, risk=["code"], session=True)
        self.assertEqual(self.cfg()["sets"][0]["risk"], ["code", "prod"])                   # a session only adds
        mine.import_set(self.state, "team-tools", self.env, session=True)
        self.assertEqual(self.cfg()["sets"][0]["risk"], ["code", "prod"])                   # no words: as it was
        mine.import_set(self.state, "team-tools", self.env, risk=["external"])
        self.assertEqual(self.cfg()["sets"][0]["risk"], ["external"])                       # the owner: as given
        mine.import_set(self.state, "team-tools", self.env)
        self.assertEqual((len(self.cfg()["sets"]), self.cfg()["sets"][0]["risk"]), (1, ["external"]))
        with self.assertRaises(ValueError):
            mine.import_set(self.state, "nope", self.env, risk=["prod"])

    def test_the_risk_of_an_mcp_a_skill_and_a_set_and_what_is_refused(self):
        mine.import_set(self.state, "team-tools", self.env)
        mine.import_mcp(self.state, "tracker", "Tasks", ["external"], self.env)
        mine.import_skill(self.state, "release-notes", [], self.env)
        self.assertEqual(mine.set_risk(self.state, "tracker", ["prod"], self.env), ["prod"])
        self.assertEqual(self.cfg()["mcp"]["tracker"]["risk"], ["prod"])                   # the owner: as given
        self.assertEqual(mine.set_risk(self.state, "tracker", ["money"], self.env, session=True), ["money", "prod"])
        self.assertEqual(self.cfg()["mcp"]["tracker"]["description"], "Tasks")              # the rest stays
        self.assertEqual(mine.set_risk(self.state, "release-notes@my-skills", ["code"], self.env), ["code"])
        self.assertEqual(mine.set_risk(self.state, "release-notes@my-skills", ["prod"], self.env, session=True),
                         ["code", "prod"])
        self.assertEqual(mine.set_risk(self.state, "team-tools", ["prod"], self.env), ["prod"])
        self.assertEqual(self.cfg()["sets"][0]["risk"], ["prod"])
        tools = catalog.build({**catalog.DEFAULTS, **self.cfg()})
        self.assertEqual([tools[t]["risk"] for t in ("tracker", "release-notes@my-skills", "deploy@team-tools")],
                         [["money", "prod"], ["code", "prod"], ["prod"]])
        with self.assertRaisesRegex(ValueError, "team-tools"):   # a plugin of a set: the set carries the risk
            mine.set_risk(self.state, "deploy@team-tools", ["prod"], self.env)
        for bad in ("nope", "nope@my-skills", "deploy@nope", "@my-skills"):
            with self.assertRaises(ValueError, msg=bad):
                mine.set_risk(self.state, bad, ["prod"], self.env)
        self.assertEqual(mine.set_risk(self.state, "tracker", [], self.env), [])             # the owner takes it off

    def test_remove_takes_an_mcp_a_skill_and_a_set_out_and_leaves_the_skill_itself(self):
        mine.import_set(self.state, "team-tools", self.env)
        mine.import_mcp(self.state, "tracker", "Tasks", ["external"], self.env)
        mine.import_skill(self.state, "release-notes", [], self.env)
        mine.import_skill(self.state, "folded", [], self.env)
        before = catalog.build({**catalog.DEFAULTS, **self.cfg()})
        wrapper = before["release-notes@my-skills"]["path"]
        with open(os.path.join(wrapper, "skills", "release-notes", "SKILL.md"), encoding="utf-8") as f:
            text = f.read()
        for bad in ("deploy@team-tools", "nope", "nope@my-skills"):   # a plugin of a set, nothing the settings hold
            with self.assertRaises(ValueError, msg=bad):
                mine.remove(self.state, bad, self.env)
        mine.remove(self.state, "release-notes@my-skills", self.env)
        self.assertFalse(os.path.lexists(wrapper))                                          # the wrapper is gone
        skill = os.path.join(self.home, ".claude", "skills", "release-notes", "SKILL.md")
        with open(skill, encoding="utf-8") as f:
            self.assertEqual(f.read(), text)                                                # the skill itself stays
        self.assertEqual(self.my_skills(), [("folded", "Two lines of text")])
        mine.remove(self.state, "tracker", self.env)
        mine.remove(self.state, "team-tools", self.env)
        cfg = self.cfg()
        self.assertEqual((cfg["mcp"], cfg["sets"][0]["name"], len(cfg["sets"]), cfg["timezone"]),
                         ({}, mine.MY_SKILLS, 1, "UTC"))
        after = catalog.build({**catalog.DEFAULTS, **cfg})
        self.assertEqual(sorted(set(before) - set(after)), ["deploy@team-tools", "release-notes@my-skills", "tracker"])
        self.assertIn("folded@my-skills", after)
        self.assertTrue(os.path.isdir(self.found()["sets"][0]["path"]))                     # the set's folder stays
        with self.assertRaises(ValueError):
            mine.remove(self.state, "tracker", self.env)                                     # gone already
        mine.remove(self.state, mine.MY_SKILLS, self.env)
        self.assertEqual(self.cfg()["sets"], [])

    def test_the_hubs_cli_removes_and_sets_the_risk_and_imports_a_set_with_it(self):
        state = os.path.join(self.home, "hub-state")
        os.makedirs(state)
        with open(os.path.join(state, "journal.jsonl"), "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps({"seq": 1, "ts": 1000, "src": "web", "type": "session.requested", "entity": "hub",
                                "data": {"cwd": self.home, "session_id": "s-hub", "kind": "hub", "channel": "web",
                                         "owner": True}}) + "\n")
            f.write(json.dumps({"seq": 2, "ts": 1001, "src": "web", "type": "session.requested", "entity": "docs",
                                "data": {"cwd": self.home, "session_id": "s-docs", "channel": "web", "owner": True,
                                         "tools": ["tracker"]}}) + "\n")
        env = {**os.environ, **self.env, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0", "HUB_SESSION": "hub",
               "HUB_LANG": "en"}
        env.pop("CLAUDE_CONFIG_DIR", None)

        def hub(*words):
            return subprocess.run([sys.executable, os.path.join(REPO, "bin", "hub"), "tools", *words], env=env,
                                  capture_output=True, text=True, encoding="utf-8")
        r = hub("import", "set", "team-tools", "--risk", "code,prod")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.cfg()["sets"][0]["risk"], ["code", "prod"])                  # the words were dropped before
        mine.import_mcp(self.state, "tracker", "Tasks", ["external"], self.env)
        r = hub("risk", "tracker", "prod")
        self.assertEqual((r.returncode, self.cfg()["mcp"]["tracker"]["risk"]), (0, ["external", "prod"]), r.stderr)
        self.assertIn("tracker: risk external, prod", r.stdout)
        r = hub("risk", "deploy@team-tools", "prod")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("team-tools", r.stderr)
        r = hub("remove", "tracker")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("tracker", self.cfg()["mcp"])
        self.assertIn("tracker", r.stdout)
        self.assertIn("docs", r.stdout)                                                     # who holds it still
        r = hub("remove", "tracker")
        self.assertNotEqual(r.returncode, 0)
        r = hub("remove")                                                                   # the form, not a crash
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.cfg()["sets"][0]["risk"], ["code", "prod"])

    def test_a_skill_name_that_is_a_path_is_refused(self):
        # the name went into the path as is: "../x" took ~/.claude/x for a skill and wrote its plugin outside my-skills
        outside = os.path.join(self.home, ".claude", "x")
        os.makedirs(outside)
        with open(os.path.join(outside, "SKILL.md"), "w") as f:
            f.write("---\nname: x\ndescription: not from the skills list\n---\n")
        for bad in ("../x", "..", ".", "", "a/b", "a\\b"):
            with self.assertRaises(ValueError, msg=bad):
                mine.import_skill(self.state, bad, [], self.env)
        self.assertFalse(os.path.exists(os.path.join(self.state, mine.MY_SKILLS)))

    def test_projects_written_with_windows_separators(self):
        # Claude Code on Windows stores paths as C:\...: a fork's working copy and the hub's own folders are not projects
        # either; the name is the last folder; the hub folder matches by folder boundary, not by string prefix (state-old is not state)
        wt = "C:\\Users\\me\\code\\shop\\.claude\\worktrees\\fix-1"
        self.assertFalse(mine._a_project(wt, "C:\\hub\\state"))
        self.assertFalse(mine._a_project("C:\\hub\\state\\sandbox", "C:\\hub\\state"))
        self.assertTrue(mine._a_project("C:\\hub\\state-old\\repo", "C:\\hub\\state"))
        self.assertTrue(mine._a_project("C:\\Users\\me\\code\\shop", "C:\\hub\\state"))
        self.assertFalse(mine._a_project("/home/x/state/repo", "/home/x/state"))
        self.assertTrue(mine._a_project("/home/x/state-old/repo", "/home/x/state"))
        self.assertEqual(mine._project_name("C:\\Users\\me\\code\\shop\\"), "shop")
        self.assertEqual(mine._project_name("/home/x/code/shop/"), "shop")

    def test_imports_write_under_the_settings_lock(self):
        # the import wrote hub.json bypassing the lock: a page edit between its read and write was lost
        calls, real = [], catalog.edit_settings

        def spy(path, **kw):
            calls.append(path)
            return real(path, **kw)
        with mock.patch.object(catalog, "edit_settings", spy):
            mine.import_mcp(self.state, "tracker", "Tasks", [], self.env)
        self.assertEqual(calls, [self.cfg_path])
        self.assertEqual(os.listdir(self.home).count("hub.json"), 1)
        self.assertFalse([n for n in os.listdir(self.home) if n.endswith(".tmp")])

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_an_imported_mcp_server_is_written_private(self):
        # keys from ~/.claude.json (0600) went into hub.json with umask permissions
        os.chmod(self.cfg_path, 0o644)
        mine.import_mcp(self.state, "tracker", "Tasks", [], self.env)
        self.assertEqual(os.stat(self.cfg_path).st_mode & 0o777, 0o600)

    def test_import_keeps_settings_with_bom_and_refuses_unreadable(self):
        # review 2026-10-05: hub.json with a BOM (PowerShell 5.1) or with a Cyrillic capital under cp1251 was read as {} — the import wrote
        # over it a single new entry, and the owner's projects and timezone vanished
        with open(self.cfg_path, "w", encoding="utf-8-sig") as f:
            json.dump({"timezone": "Europe/Berlin", "projects": {"p": {"path": "/x", "about": WORD}}}, f,
                      ensure_ascii=False)
        mine.import_mcp(self.state, "tracker", "Tasks", [], self.env)
        with open(self.cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertEqual((cfg["timezone"], cfg["projects"]["p"]["about"], list(cfg["mcp"])),
                         ("Europe/Berlin", WORD, ["tracker"]))
        with open(self.cfg_path, "w", encoding="utf-16") as f:
            json.dump({"timezone": "UTC"}, f)
        with self.assertRaisesRegex(ValueError, "not in UTF-8"):
            mine.import_mcp(self.state, "tracker", "Tasks", [], self.env)
        with open(self.cfg_path, encoding="utf-16") as f:
            self.assertEqual(json.load(f), {"timezone": "UTC"})  # not overwritten


    def my_skills(self):
        with open(os.path.join(self.state, mine.MY_SKILLS, ".claude-plugin", "marketplace.json"),
                  encoding="utf-8") as f:
            return [(p["name"], p["description"]) for p in json.load(f)["plugins"]]

    def test_skill_import_keeps_skills_written_in_the_locale_and_writes_utf8(self):
        # review of PR #18: the old code wrote the list of own skills in the locale encoding (cp1251, KOI8-R); reading became
        # UTF-8 — the catalog silently lost skills, and the next import started the list over and erased the old ones
        root = os.path.join(self.state, mine.MY_SKILLS, ".claude-plugin")
        os.makedirs(root)
        with open(os.path.join(root, "marketplace.json"), "w", encoding="cp1251") as f:
            json.dump({"name": mine.MY_SKILLS, "owner": {"name": "owner"},
                       "plugins": [{"name": "alpha", "source": "./plugins/alpha", "description": WORD}]}, f,
                      ensure_ascii=False)
        with (mock.patch("core.catalog.locale.getpreferredencoding", return_value="utf-8"),
              mock.patch("core.catalog.locale.getencoding", return_value="cp1251")):
            mine.import_skill(self.state, "release-notes", [], self.env)
        self.assertEqual(self.my_skills(), [("alpha", WORD), ("release-notes", "Writes release notes from git log")])

    def test_skill_description_is_utf8_under_any_locale(self):
        # a Cyrillic capital (byte 0x98 in cp1251) or an emoji in SKILL.md crashed the import: the file was read in the locale encoding
        skill = os.path.join(self.home, ".claude", "skills", "release-notes", "SKILL.md")
        with open(skill, "w", encoding="utf-8") as f:
            f.write(f"---\nname: release-notes\ndescription: {WORD} release 😀\n---\n")
        code = ("import json, sys; sys.path.insert(0, sys.argv[1]); from core import mine; "
                "mine.import_skill(sys.argv[2], 'release-notes', [], json.loads(sys.argv[3]))")
        out = subprocess.run([sys.executable, "-c", code, REPO, self.state, json.dumps(self.env)],
                             env={**os.environ, "PYTHONUTF8": "0"}, capture_output=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8", "replace"))
        self.assertEqual(self.my_skills(), [("release-notes", f"{WORD} release 😀")])


if __name__ == "__main__":
    unittest.main()
