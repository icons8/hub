"""Settings -> Plugins, "Claude and Codex plugins" (core/engineplugins.py): what the hub installed from the engines' own
catalogs, listed apart from the shared list — its catalog, what is inside (skills, commands, MCP servers), what will not
work as shipped (an author's placeholder in a server's settings instead of its sign-in data). "Give to a session…" is the
owner's word from the page, the same fact as `hub tools grant`; "Remove" is the catalog installer's uninstall."""
import tests  # noqa: F401 — keep the machine's settings out of the test
import json
import os
import tempfile
import unittest
from unittest import mock

import platforms
from core import engineplugins, journal, marketplaces
from tests import test_web
from tests.test_marketplace import Catalog
from tests.test_mpinstall import OWL, cat, owl


def write(root, files):
    for rel, body in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body if isinstance(body, str) else json.dumps(body))


MAPS = {".claude-plugin/plugin.json": {"name": "maps", "description": "Tiles and routes"},
        "skills/tiles/SKILL.md": "---\nname: tiles\n---\n", "skills/routes/SKILL.md": "---\nname: routes\n---\n",
        "skills/notes.txt": "not a skill", "skills/empty/README.md": "no SKILL.md",
        "commands/plan.md": "plan", "commands/build.md": "build", "commands/_conventions.md": "notes for the others",
        ".mcp.json": {"mcpServers": {"tiles": {"type": "http", "url": "https://maps.example.test/mcp",
                                               "oauth": {"client_id": "<MAPS_PUBLIC_CLIENT_ID>"}},
                                     "local": {"command": "node", "args": ["server.js", "<b>bold</b>"]}}}}


class Contents(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="engplug-")
        self.addCleanup(platforms.current.rmtree, self.root)

    def test_skills_commands_servers_and_a_placeholder_left_by_the_author(self):
        write(self.root, MAPS)
        got = engineplugins.contents(self.root)
        self.assertEqual(got["description"], "Tiles and routes")
        self.assertEqual(got["skills"], ["routes", "tiles"])          # a folder with SKILL.md is a skill
        self.assertEqual(got["commands"], ["build", "plan"])           # _conventions.md: notes, not a command
        self.assertEqual(got["mcp"], ["local", "tiles"])
        self.assertEqual(got["warnings"], [{"code": "placeholder", "server": "tiles", "value": "<MAPS_PUBLIC_CLIENT_ID>"}])

    def test_a_codex_manifest_names_its_parts_and_its_screen_card_describes_it(self):
        write(self.root, {".codex-plugin/plugin.json": {"name": "maps", "skills": "./kit/",
                                                        "mcpServers": "./servers.json",
                                                        "interface": {"shortDescription": "Maps in the thread"}},
                          "kit/tiles/SKILL.md": "x", "skills/ignored/SKILL.md": "x",
                          "servers.json": {"mcpServers": {"tiles": {"url": "https://maps.example.test/mcp"}}}})
        got = engineplugins.contents(self.root)
        self.assertEqual((got["description"], got["skills"], got["mcp"], got["warnings"]),
                         ("Maps in the thread", ["ignored", "tiles"], ["tiles"], []))   # the default folder stays

    def test_named_paths_add_to_the_default_folders_and_servers_merge_as_claude_code_loads_them(self):
        # Claude Code: custom paths supplement skills/ and commands/; servers — .mcp.json, then the manifest's
        # mcpServers in order (core/catalog.plugin_servers), a later name replaces an earlier one
        write(self.root, {".claude-plugin/plugin.json": {"name": "maps", "commands": ["./extra/deploy.md"],
                                                         "mcpServers": [{"browser": {"command": "<BROWSER_BIN>"}},
                                                                        "./more.json", "./pack.mcpb"]},
                          "commands/plan.md": "x", "extra/deploy.md": "x",
                          ".mcp.json": {"api": {"url": "https://api.example.test"}},
                          "more.json": {"mcpServers": {"api": {"url": "<API_URL>"}, "docs": {"url": "https://d.example.test"}}}})
        got = engineplugins.contents(self.root)
        self.assertEqual((got["commands"], got["mcp"]), (["deploy", "plan"], ["api", "browser", "docs"]))
        self.assertEqual([(w["server"], w["value"]) for w in got["warnings"]], [("api", "<API_URL>"), ("browser", "<BROWSER_BIN>")])

    def test_a_manifest_path_outside_the_plugin_folder_is_not_read(self):
        outside = tempfile.mkdtemp(prefix="engplug-out-")
        self.addCleanup(platforms.current.rmtree, outside)
        write(outside, {"skills/secret/SKILL.md": "x", ".mcp.json": {"mcpServers": {"far": {"url": "<FAR_KEY>"}}}})
        rel = os.path.relpath(outside, self.root)
        write(self.root, {".claude-plugin/plugin.json": {"name": "maps", "skills": os.path.join(rel, "skills"),
                                                         "mcpServers": os.path.join(rel, ".mcp.json")}})
        got = engineplugins.contents(self.root)
        self.assertEqual((got["skills"], got["mcp"], got["warnings"]), ([], [], []))

    def test_unreadable_files_give_an_empty_part_not_an_error(self):
        write(self.root, {".claude-plugin/plugin.json": "{not json", ".mcp.json": "[1, 2"})
        got = engineplugins.contents(self.root)
        self.assertEqual((got["skills"], got["commands"], got["mcp"], got["description"]), ([], [], [], ""))


class Installed(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="engplug-")
        self.addCleanup(platforms.current.rmtree, self.root)
        write(os.path.join(self.root, "maps"), MAPS)

    def rec(self, catalog, entry="maps", path=None, **more):
        return {"name": catalog, "entry": entry, "plugin": True, "market": catalog, "commit": "c" * 40,
                "path": path or os.path.join(self.root, "maps"), "source": "acme/plugins", **more}

    def test_only_what_came_from_the_engines_catalogs_by_key(self):
        cfg = {"marketplaces": {"owlcorp": {"source": "https://example.test/o.git", "builtin": True}},
               "sets": [self.rec("openai-curated"), self.rec("shop"),                  # the hub's catalog: not here
                        self.rec("owlcorp", entry="tiles"),                            # marked built-in in hub.json
                        {"name": "my-skills", "path": self.root},                       # a whole skill set
                        self.rec("claude-plugins-official", entry="gone", path=os.path.join(self.root, "nowhere")),
                        "a typo", {**self.rec("openai-curated", entry=""), "entry": ""}]}
        got = engineplugins.installed(cfg)
        self.assertEqual([x["key"] for x in got], ["gone@claude-plugins-official", "maps@openai-curated", "tiles@owlcorp"])
        maps = got[1]
        self.assertEqual((maps["catalog"], maps["entry"], maps["version"], maps["missing"], maps["counts"]),
                         ("openai-curated", "maps", "ccccccc", False, {"skills": 2, "commands": 2, "mcp": 2}))
        self.assertEqual(maps["warnings"][0]["value"], "<MAPS_PUBLIC_CLIENT_ID>")
        self.assertEqual((got[0]["missing"], got[0]["counts"]), (True, {"skills": 0, "commands": 0, "mcp": 0}))

    def test_the_key_is_the_cores_tool_id_and_an_unreadable_folder_keeps_its_row(self):
        rec = {**self.rec("openai-curated"), "name": "my-set"}   # catalog._one_plugin: <entry>@<the set's name>
        with mock.patch.object(engineplugins, "contents", side_effect=PermissionError("denied")):
            (got,) = engineplugins.installed({"sets": [rec]})
        self.assertEqual((got["key"], got["catalog"], got["skills"]), ("maps@my-set", "openai-curated", []))

    def test_a_long_list_comes_cut_with_its_whole_count(self):
        write(os.path.join(self.root, "maps"), {f"skills/s{i:02}/SKILL.md": "x" for i in range(55)})
        (got,) = engineplugins.installed({"sets": [self.rec("openai-curated")]})
        self.assertEqual((len(got["skills"]), got["counts"]["skills"]), (engineplugins.SHOWN, 57))

    def test_hub_json_of_the_wrong_shape_lists_nothing(self):
        for cfg in ({}, {"sets": {"x": 1}}, {"sets": None, "marketplaces": []}):
            self.assertEqual(engineplugins.installed(cfg), [])


class Served(test_web.WebTest):
    def setUp(self):
        super().setUp()
        self.cfg = os.path.join(self.state, "hub.json")
        env = mock.patch.dict(os.environ, {"HUB_CONFIG": self.cfg})
        env.start()
        self.addCleanup(env.stop)
        src = Catalog(self, cat(owl()), {"plugins/owl/hub-plugin.json": OWL, "plugins/owl/main.js": "export default 1\n"})
        with open(self.cfg, "w") as f:
            json.dump({}, f)
        marketplaces.add(self.state, self.cfg, src.bare)
        self.plugin = os.path.join(self.state, "maps")
        write(self.plugin, MAPS)
        with open(self.cfg) as f:
            raw = json.load(f)
        raw["marketplaces"]["shop"]["builtin"] = True   # an engine's catalog, as claude-plugins-official is
        raw["sets"] = [{"name": "shop", "entry": "maps", "plugin": True, "market": "shop", "path": self.plugin,
                        "source": "acme/plugins", "commit": "d" * 40}]
        with open(self.cfg, "w") as f:
            json.dump(raw, f)
        self.seed()
        self.j.append("catalog.loaded", None, {"tools": {"maps@shop": {"kind": "plugin", "path": self.plugin,
                                                                       "description": "Tiles and routes", "risk": ["code"]}}})
        self.settle()

    def test_the_page_gets_the_engines_plugins_with_what_is_inside(self):
        code, body = self.req("GET", "/api/marketplaces")
        got = json.loads(body)
        self.assertEqual((code, [(c["name"], c["builtin"]) for c in got["catalogs"]]), (200, [("shop", True)]))
        (p,) = got["engine"]
        self.assertIs(got["grant"], True)                  # this server takes "Give to a session…"
        self.assertEqual((p["key"], p["skills"], p["commands"], p["mcp"]),
                         ("maps@shop", ["routes", "tiles"], ["build", "plan"], ["local", "tiles"]))
        self.assertEqual(p["warnings"], [{"code": "placeholder", "server": "tiles", "value": "<MAPS_PUBLIC_CLIENT_ID>"}])

    def test_give_to_a_session_is_the_owners_grant_and_the_core_decides(self):
        self.core_decides()
        code, body = self.req("POST", "/api/tools/grant", {"session": "a", "tool": "maps@shop"})
        self.assertEqual(code, 202, body)
        self.assertIn("maps@shop", json.loads(body)["text"])
        (ev,) = [e for e in journal.read(self.j.path) if e["type"] == "tools.granted"]
        self.assertEqual((ev["entity"], ev["data"]["tool"], ev["data"]["owner"], ev["data"]["channel"]),
                         ("a", "maps@shop", True, "web"))
        self.settle()
        self.assertIn("maps@shop", next(s for s in self.snap()["sessions"] if s["name"] == "a")["tools"])
        code, body = self.req("POST", "/api/tools/grant", {"session": "a", "tool": "maps@shop"})
        self.assertEqual(code, 409, body)    # it has it already: the core's refusal, in words

    def test_a_tool_the_hub_does_not_have_and_a_session_that_is_not_there_are_refused(self):
        code, body = self.req("POST", "/api/tools/grant", {"session": "a", "tool": "nowhere@shop"})
        self.assertEqual(code, 409, body)
        self.assertIn("nowhere@shop", json.loads(body)["error"])
        self.assertEqual(self.req("POST", "/api/tools/grant", {"session": "ghost", "tool": "maps@shop"})[0], 404)
        self.assertEqual([e for e in self.spooled() if e["type"] == "tools.granted"], [])

    def test_remove_takes_it_off_through_the_catalog_installer(self):
        code, body = self.req("POST", "/api/plugins/remove", {"name": "maps@shop"})
        self.assertEqual(code, 200, body)
        with open(self.cfg) as f:
            self.assertEqual(json.load(f).get("sets"), [])
        self.assertEqual(json.loads(self.req("GET", "/api/marketplaces")[1])["engine"], [])


def load_tests(loader, standard, pattern):
    suite = unittest.TestSuite()   # Served is a WebTest: only its own tests, not the ones it inherits
    for cls in (Contents, Installed, Served):
        suite.addTests(cls(n) for n in cls.__dict__ if n.startswith("test"))
    return suite
