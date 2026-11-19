"""MCP brought by a plugin (`.mcp.json` in its folder): --strict-mcp-config drops such servers, and the session was
left without tools (Atlassian: Jira and Confluence). The core passes them itself under the name
`plugin:<plugin>:<server>` - Claude Code keeps the login (OAuth) under that name, so the server connects without a
new login."""
import tests  # noqa: F401 - ignore the machine's settings
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from core import catalog
from tests import winsafe


class PluginMcp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="plugmcp-")
        market = os.path.join(self.dir, "set")
        plug = os.path.join(market, "plugins", "wiki")
        os.makedirs(os.path.join(market, ".claude-plugin"))
        os.makedirs(os.path.join(plug, ".claude-plugin"))
        os.makedirs(os.path.join(market, "plugins", "plain"))
        with open(os.path.join(market, ".claude-plugin", "marketplace.json"), "w") as f:
            json.dump({"name": "set", "plugins": [{"name": "wiki", "source": "./plugins/wiki", "description": "w"},
                                                  {"name": "plain", "source": "./plugins/plain", "description": "p"}]}, f)
        with open(os.path.join(plug, ".claude-plugin", "plugin.json"), "w") as f:
            json.dump({"name": "wiki"}, f)
        with open(os.path.join(plug, ".mcp.json"), "w") as f:
            json.dump({"mcpServers": {"docs": {"type": "http", "url": "https://example.test/mcp"}}}, f)
        self.cfg = {**catalog.load_config(self.dir), "sets": [{"name": "set", "path": market}]}
        self.tools = catalog.build(self.cfg)

    def flags(self, tools):
        return catalog.launch_flags(tools=tools, cfg=self.cfg, catalog=self.tools, kind="work", cwd=self.dir,
                                    project="all", state_dir=self.dir, name="s")

    def test_the_server_of_a_plugin_reaches_the_session(self):
        flags = self.flags(["wiki@set"])
        self.assertIn("--strict-mcp-config", flags)
        with open(flags[flags.index("--mcp-config") + 1]) as f:
            self.assertEqual(json.load(f)["mcpServers"],
                             {"plugin:wiki:docs": {"type": "http", "url": "https://example.test/mcp"}})

    def test_mcp_deny_drops_a_plugin_server_only(self):
        # the owner switched off one plugin server (no token: 401 in every session); the plugin's other servers stay
        plug = os.path.join(self.dir, "set", "plugins", "wiki")
        with open(os.path.join(plug, ".mcp.json"), "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"docs": {"type": "http", "url": "https://example.test/mcp"},
                                      "crowd": {"type": "http", "url": "https://example.test/crowd"}}}, f)
        self.cfg["mcp_deny"] = ["plugin:wiki:crowd"]
        flags = self.flags(["wiki@set"])
        with open(flags[flags.index("--mcp-config") + 1], encoding="utf-8") as f:
            self.assertEqual(set(json.load(f)["mcpServers"]), {"plugin:wiki:docs"})

    def test_mcp_deny_with_a_malformed_entry_does_not_break_the_launch(self):
        # hub.json is edited by hand: an entry that is not a string must not stop every session from starting
        plug = os.path.join(self.dir, "set", "plugins", "wiki")
        with open(os.path.join(plug, ".mcp.json"), "w", encoding="utf-8") as f:
            json.dump({"mcpServers": {"docs": {"type": "http", "url": "https://example.test/mcp"},
                                      "crowd": {"type": "http", "url": "https://example.test/crowd"}}}, f)
        self.cfg["mcp_deny"] = [["x"], {"a": 1}, None, 5, "plugin:wiki:crowd"]
        flags = self.flags(["wiki@set"])
        with open(flags[flags.index("--mcp-config") + 1], encoding="utf-8") as f:
            self.assertEqual(set(json.load(f)["mcpServers"]), {"plugin:wiki:docs"})

    def test_mcp_deny_leaves_a_catalog_server_named_like_a_plugin_one(self):
        # the setting switches off what a plugin brought; a catalog server that is granted (here under the plugin's
        # server name by `as`) or a plain catalog id is not a plugin's and stays
        srv = {"type": "http", "url": "https://example.test/own"}
        self.cfg["mcp"] = {"docs": {"server": srv, "as": "plugin:wiki:docs"}, "plain": {"server": srv}}
        self.cfg["mcp_deny"] = ["plugin:wiki:docs", "plain"]
        self.tools = catalog.build(self.cfg)
        flags = self.flags(["docs", "plain"])
        with open(flags[flags.index("--mcp-config") + 1], encoding="utf-8") as f:
            self.assertEqual(json.load(f)["mcpServers"], {"plugin:wiki:docs": srv, "plain": srv})

    def test_a_plugin_server_doubled_in_the_catalog_is_not_raised(self):
        # the server is in the catalog (with a login) and also arrives with the plugin (plugin:<plugin>:<same>, waiting
        # for its own login): two connections to one server; a session granted the catalog one gets only that one, the
        # plugin's twin is not started; if the catalog one is not granted, the plugin's server stays, nothing blocks it
        self.tools["docs"] = {"kind": "mcp", "server": {"type": "http", "url": "https://example.test/own"}}
        for tools, want in ((["wiki@set", "docs"], {"docs"}), (["wiki@set"], {"plugin:wiki:docs"})):
            flags = self.flags(tools)
            servers = set()
            if "--mcp-config" in flags:
                with open(flags[flags.index("--mcp-config") + 1]) as f:
                    servers = set(json.load(f)["mcpServers"])
            self.assertEqual(servers, want)

    def test_a_catalog_server_may_be_named_as_in_the_session(self):
        # the owner's sign-in (OAuth) is kept by Claude Code under the server's name in the session, and the plain id
        # may be taken by another server: `as` in hub.json -> mcp.<id> names the server that goes to the session
        self.cfg["mcp"] = {"docs-oauth": {"server": {"type": "http", "url": "https://example.test/own"},
                                          "as": "plugin:wiki:docs"},
                           "plain": {"server": {"type": "http", "url": "https://example.test/plain"}}}
        self.tools = catalog.build(self.cfg)
        flags = self.flags(["docs-oauth", "plain"])
        with open(flags[flags.index("--mcp-config") + 1]) as f:
            self.assertEqual(json.load(f)["mcpServers"], {"plugin:wiki:docs": {"type": "http", "url":
                             "https://example.test/own"}, "plain": {"type": "http", "url": "https://example.test/plain"}})

    def test_an_entry_named_as_its_plugin_server_is_not_taken_for_the_plugins_twin(self):
        # id "docs" with as "plugin:wiki:docs": the twin rule drops a plugin's server whose last part is a granted catalog
        # id — the catalog's own server under that name must stay, with or without the plugin
        srv = {"type": "http", "url": "https://example.test/own"}
        self.cfg["mcp"] = {"docs": {"server": srv, "as": "plugin:wiki:docs"}}
        self.tools = catalog.build(self.cfg)
        for tools, want in ((["docs"], {"plugin:wiki:docs": srv}), (["docs", "wiki@set"], {"docs": srv})):
            flags = self.flags(tools)
            self.assertIn("--mcp-config", flags, tools)
            with open(flags[flags.index("--mcp-config") + 1]) as f:
                self.assertEqual(json.load(f)["mcpServers"], want, tools)

    def test_an_as_that_is_not_a_clean_name_is_dropped(self):
        # mcp_login_confirm holds the stripped, printable name: an `as` with spaces around it or a control character
        # would reach the session under another name and pass the owner's gate
        self.cfg["mcp"] = {k: {"server": {"type": "http", "url": "https://example.test/x"}, "as": v} for k, v in
                           (("a", "plugin:x:pay "), ("b", "pl\nx"), ("c", "x" * 201), ("d", ""), ("e", 5), ("f", "ok:1"))}
        self.tools = catalog.build(self.cfg)
        self.assertEqual({k: self.tools[k].get("as") for k in "abcdef"},
                         {"a": None, "b": None, "c": None, "d": None, "e": None, "f": "ok:1"})

    def test_an_as_that_clashes_is_dropped_and_the_entry_keeps_its_id(self):
        # as = another tool's id, as = another entry's `as`, as = a plugin server's name (found at launch): the clash is
        # refused and the entry goes to the session under its own id, not over someone else's server
        srv = lambda u: {"type": "http", "url": f"https://example.test/{u}"}  # noqa: E731
        self.cfg["mcp"] = {"a": {"server": srv("a"), "as": "plain"}, "plain": {"server": srv("plain")},
                           "b": {"server": srv("b"), "as": "shared"}, "c": {"server": srv("c"), "as": "shared"},
                           "d": {"server": srv("d"), "as": "plugin:wiki:docs"}, "e": {"server": srv("e"), "as": "e"}}
        self.tools = catalog.build(self.cfg)
        self.assertEqual({k: v.get("as") for k, v in self.tools.items() if k in "abcde" or k == "plain"},
                         {"a": None, "plain": None, "b": "shared", "c": None, "d": "plugin:wiki:docs", "e": "e"})
        flags = self.flags(["a", "plain", "b", "c", "d", "e", "wiki@set"])
        with open(flags[flags.index("--mcp-config") + 1]) as f:
            got = json.load(f)["mcpServers"]
        self.assertEqual(sorted(got), ["a", "c", "d", "e", "plain", "plugin:wiki:docs", "shared"])
        self.assertEqual(got["plain"], srv("plain"))
        self.assertEqual(got["plugin:wiki:docs"], {"type": "http", "url": "https://example.test/mcp"})  # the plugin's own
        self.assertEqual(got["shared"], srv("b"))

    def test_the_plugin_root_is_filled_in(self):
        # a plugin's local server: its path goes through ${CLAUDE_PLUGIN_ROOT}; Claude Code does not substitute it from --mcp-config
        plug = os.path.join(self.dir, "set", "plugins", "wiki")
        with open(os.path.join(plug, ".mcp.json"), "w") as f:
            json.dump({"mcpServers": {"local": {"command": "node", "args": ["${CLAUDE_PLUGIN_ROOT}/server.js"],
                                                "env": {"DATA": "${CLAUDE_PLUGIN_ROOT}/data"}}}}, f)
        flags = self.flags(["wiki@set"])
        with open(flags[flags.index("--mcp-config") + 1]) as f:
            srv = json.load(f)["mcpServers"]["plugin:wiki:local"]
        # ${CLAUDE_PLUGIN_ROOT}/server.js on Windows is "...\\wiki/server.js": the same path
        self.assertEqual([os.path.normpath(a) for a in srv["args"]], [os.path.join(plug, "server.js")])
        self.assertEqual(os.path.normpath(srv["env"]["DATA"]), os.path.join(plug, "data"))

    def test_plugin_options_are_filled_in_from_owner_settings(self):
        # a plugin's server takes settings from ${user_config.*} - Claude Code substitutes them only for its own
        # servers; from --mcp-config the string would arrive as is. Values come from pluginConfigs in the owner's
        # settings, a missing one from `default` of userConfig in plugin.json; a non-string goes as JSON
        plug = os.path.join(self.dir, "set", "plugins", "wiki")
        with open(os.path.join(plug, ".mcp.json"), "w") as f:
            json.dump({"mcpServers": {"local": {"command": "node", "env": {
                "URL": "${user_config.url}", "USER": "${user_config.user}", "MODE": "${user_config.mode}",
                "DEBUG": "${user_config.debug}"}}}}, f)
        with open(os.path.join(plug, ".claude-plugin", "plugin.json"), "w") as f:
            json.dump({"name": "wiki", "userConfig": {"mode": {"type": "string", "default": "fast"},
                                                      "url": {"type": "string", "default": "https://other.example.test"}}}, f)
        claude = os.path.join(self.dir, "claude")
        os.makedirs(claude)
        with open(os.path.join(claude, "settings.json"), "w") as f:
            json.dump({"pluginConfigs": {"wiki@set": {"options": {"url": "https://example.test", "user": "u",
                                                                      "debug": True}}}}, f)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": claude}):
            flags = self.flags(["wiki@set"])
        with open(flags[flags.index("--mcp-config") + 1]) as f:
            env = json.load(f)["mcpServers"]["plugin:wiki:local"]["env"]
        self.assertEqual(env, {"URL": "https://example.test", "USER": "u", "MODE": "fast", "DEBUG": "true"})

    def test_an_option_without_a_value_does_not_start_the_server(self):
        # no value and no default (null in settings is the same; a secret field is stored elsewhere): in the address,
        # command, arguments, headers or env the server would connect with an empty value, or exit at once
        # (CONNECTION_CLOSED), so it is not started and the log names the option; other servers of the plugin come up.
        # An env key whose name is already in the session environment is the one exception: the key is left out and the
        # server reads that variable itself
        plug = os.path.join(self.dir, "set", "plugins", "wiki")
        servers = {"url": {"url": "${user_config.url}"}, "arg": {"command": "node", "args": ["${user_config.script}"]},
                   "head": {"url": "https://example.test/mcp", "headers": {"X": "${user_config.header}"}},
                   "env": {"command": "node", "env": {"FOO_PASS": "${user_config.pass}"}},
                   "ok": {"command": "node", "env": {"A": "${user_config.a}"}}}
        with open(os.path.join(plug, ".mcp.json"), "w") as f:
            json.dump({"mcpServers": servers}, f)
        claude = os.path.join(self.dir, "claude")
        os.makedirs(claude)
        with open(os.path.join(claude, "settings.json"), "w") as f:
            json.dump({"pluginConfigs": {"wiki@set": {"options": {"header": None, "url": None, "a": "x"}}}}, f)
        catalog._plugin_skipped.clear()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": claude}), mock.patch("sys.stderr", new=io.StringIO()) as err:
            flags = self.flags(["wiki@set"])
        with open(flags[flags.index("--mcp-config") + 1]) as f:
            got = json.load(f)["mcpServers"]
        self.assertEqual(sorted(k for k in got if k.startswith("plugin:wiki:")), ["plugin:wiki:ok"])
        for name in ("user_config.url", "user_config.script", "user_config.header", "user_config.pass"):
            self.assertIn(name, err.getvalue())
        catalog._plugin_skipped.clear()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": claude, "FOO_PASS": "value-from-env"}):
            flags = self.flags(["wiki@set"])
        with open(flags[flags.index("--mcp-config") + 1]) as f:
            got = json.load(f)["mcpServers"]
        self.assertEqual(got["plugin:wiki:env"], {"command": "node", "env": {}})

    def test_a_set_from_claude_code_marketplaces_uses_the_installed_copy(self):
        # the toolset points at a clone in <claude>/plugins/marketplaces - Claude Code does not run it and there are no
        # dependencies (node_modules) there: the plugin's server crashed at start (CONNECTION_CLOSED). Its copy from the cache is run
        claude = os.path.join(self.dir, "claude")
        market = os.path.join(claude, "plugins", "marketplaces", "mk")
        os.makedirs(os.path.join(market, ".claude-plugin"))
        with open(os.path.join(market, ".claude-plugin", "marketplace.json"), "w") as f:
            json.dump({"name": "mk", "plugins": [{"name": "tk", "source": "./"}, {"name": "other", "source": "./o"}]}, f)
        copy = os.path.join(claude, "plugins", "cache", "mk", "tk", "abc")
        os.makedirs(copy)
        with open(os.path.join(claude, "plugins", "installed_plugins.json"), "w") as f:
            json.dump({"plugins": {"tk@mk": [{"installPath": copy}]}}, f)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": claude}):
            tools = catalog.build({**self.cfg, "sets": [{"name": "mk", "path": market}]})
        self.assertEqual(tools["tk@mk"]["path"], copy)
        self.assertEqual(tools["other@mk"]["path"], os.path.join(market, "o"))   # not installed - as before
        self.assertEqual(self.tools["wiki@set"]["path"], os.path.join(self.dir, "set", "plugins", "wiki"))
        # claude's settings folder is a symlink (dotfiles): the clone is recognized through the link too
        link = os.path.join(self.dir, "claude-link")
        if not winsafe.try_symlink(claude, link):   # Windows without the right to create links
            return
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": link}):
            tools = catalog.build({**self.cfg, "sets": [{"name": "mk", "path": market}]})
        self.assertEqual(tools["tk@mk"]["path"], copy)

    def test_the_user_scope_copy_is_taken_first(self):
        # project/local copies are installed for their own folders and may be older; Claude Code always runs the user-scope copy
        claude = os.path.join(self.dir, "claude")
        copies = {scope: os.path.join(claude, "plugins", "cache", "mk", "tk", scope) for scope in ("project", "user")}
        for path in copies.values():
            os.makedirs(path)
        os.makedirs(os.path.join(claude, "plugins"), exist_ok=True)
        with open(os.path.join(claude, "plugins", "installed_plugins.json"), "w") as f:
            json.dump({"plugins": {"tk@mk": [{"scope": "project", "projectPath": "/x", "installPath": copies["project"]},
                                             {"scope": "user", "installPath": copies["user"]}]}}, f)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": claude}):
            self.assertEqual(catalog.installed_path("tk@mk"), copies["user"])
            os.rmdir(copies["user"])   # no user copy on disk - the next one is taken, as before
            self.assertEqual(catalog.installed_path("tk@mk"), copies["project"])

    def test_the_installed_copy_is_new_by_its_folder_not_by_the_clone(self):
        # the clone was updated (Claude Code's git pull) but the copy is still the old one: the folder hash in the clone
        # must not trigger a restart - the session would come up on the same old copy. A new version is a new copy folder
        claude = os.path.join(self.dir, "claude")
        market = os.path.join(claude, "plugins", "marketplaces", "mk")
        os.makedirs(os.path.join(market, ".claude-plugin"))
        with open(os.path.join(market, ".claude-plugin", "marketplace.json"), "w") as f:
            json.dump({"name": "mk", "plugins": [{"name": "tk", "source": "./tk"}, {"name": "other", "source": "./o"}]}, f)
        for sub in ("tk", "o"):
            os.makedirs(os.path.join(market, sub))
            with open(os.path.join(market, sub, "f"), "w") as f:
                f.write(sub)
        git = ["git", "-C", market, "-c", "user.name=t", "-c", "user.email=t@example.test"]
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "x"]):
            subprocess.run(git + args, check=True, capture_output=True)
        copy = os.path.join(claude, "plugins", "cache", "mk", "tk", "abc")
        os.makedirs(copy)
        with open(os.path.join(claude, "plugins", "installed_plugins.json"), "w") as f:
            json.dump({"plugins": {"tk@mk": [{"scope": "user", "installPath": copy}]}}, f)
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": claude}):
            tools = catalog.build({**self.cfg, "sets": [{"name": "mk", "path": market}]})
        self.assertEqual(tools["tk@mk"]["path"], copy)
        self.assertNotIn("tree", tools["tk@mk"])
        self.assertEqual(tools["tk@mk"]["version"], "abc")   # the version of the running copy, not the clone's HEAD
        self.assertIn("tree", tools["other@mk"])   # not installed - from the clone, by its folder hash, as before

    def test_a_plugin_without_a_server_adds_no_config(self):
        self.assertNotIn("--mcp-config", self.flags(["plain@set"]))


class ManifestServers(unittest.TestCase):
    """Servers in a plugin's manifest (`mcpServers` in plugin.json, as in Claude Code's plugins-reference):
    chrome-devtools-mcp declares its server only there, and the session got the plugin without a browser."""
    def setUp(self):
        self.plug = tempfile.mkdtemp(prefix="plugman-")
        self.addCleanup(shutil.rmtree, self.plug, ignore_errors=True)
        os.makedirs(os.path.join(self.plug, ".claude-plugin"))

    def manifest(self, servers, mcp_json=None, **files):
        with open(os.path.join(self.plug, ".claude-plugin", "plugin.json"), "w") as f:
            json.dump({"name": "tool", "mcpServers": servers}, f)
        if mcp_json is not None:
            with open(os.path.join(self.plug, ".mcp.json"), "w") as f:
                json.dump(mcp_json, f)
        for name, data in files.items():
            with open(os.path.join(self.plug, name), "w") as f:
                json.dump(data, f)
        return catalog.plugin_servers(self.plug)

    def test_the_catalog_keeps_the_name_the_servers_run_under(self):
        """The servers run as plugin:<plugin.json name>:<server>, which may differ from the catalog id: the catalog
        keeps that name, so a refusal for a down server finds the plugin's servers (core/tools.down)."""
        servers = self.manifest({"srv": {"command": "x"}})
        self.assertEqual(list(servers), ["plugin:tool:srv"])
        self.assertEqual(catalog._plugin_name(self.plug), {"plugin_name": "tool"})
        self.assertEqual(catalog._plugin_name(os.path.join(self.plug, "gone")), {})

    def test_the_core_plugins_keep_their_manifest_name(self):
        got = {k: v for k, v in catalog.build({**catalog.DEFAULTS}).items() if v.get("source") == "core"}
        self.assertTrue(got)
        for key, tool in got.items():
            with open(os.path.join(tool["path"], ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
                self.assertEqual(tool.get("plugin_name"), json.load(f)["name"], key)

    def test_environment_references_are_expanded_as_in_claude_code(self):
        """${VAR} and ${VAR:-default} in command, args, env, url and headers of a plugin's server: Claude Code expands
        them for its own servers, from --mcp-config they would arrive as is ("'url' is not a valid URL"). A variable
        that is not set (or empty) takes the default."""
        servers = {"docs": {"type": "http", "url": "${FOO_URL:-https://catalog.example.test}/mcp",
                            "headers": {"Authorization": "Bearer ${FOO_TOKEN:-t}", "X-Mode": "${FOO_MODE:-fast}",
                                        "X-Fixed": "one"}},
                   "local": {"command": "${FOO_BIN:-node}", "args": ["--url=${FOO_URL}", "${FOO_EMPTY:-dflt}"],
                             "env": {"A": "${FOO_EMPTY:-}", "B": "${FOO_TOKEN:-}", "C": "${FOO_NAME}-x"}}}
        with mock.patch.dict(os.environ, {"FOO_EMPTY": "", "FOO_NAME": "n", "FOO_URL": "https://set.example.test"}), \
                mock.patch.object(catalog, "user_env", return_value={}), mock.patch("sys.stderr", new=io.StringIO()) as err:
            got = self.manifest(servers)
        self.assertEqual(got["plugin:tool:docs"], {"type": "http", "url": "https://set.example.test/mcp",
                                                   "headers": {"Authorization": "Bearer t", "X-Mode": "fast",
                                                               "X-Fixed": "one"}})
        self.assertEqual(got["plugin:tool:local"], {"command": "node", "args": ["--url=https://set.example.test", "dflt"],
                                                    "env": {"A": "", "B": "", "C": "n-x"}})
        self.assertNotIn("example.test", err.getvalue())
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(catalog, "user_env", return_value={}):
            got = self.manifest({"docs": servers["docs"]})
        self.assertEqual(got["plugin:tool:docs"]["url"], "https://catalog.example.test/mcp")

    def test_a_server_whose_variable_has_no_value_is_not_started_and_the_others_are(self):
        """${FOO_TOKEN} in a header with no value and no default: the request would go with "Bearer " and the server
        answer 401 (which also turns its OAuth off), so that server is not passed at all, with the reason in the log
        (names only); another server of the plugin comes up. The same line is not repeated at every launch, and
        again once the set of missing variables is another one."""
        servers = {"docs": {"type": "http", "url": "https://catalog.example.test/mcp",
                            "headers": {"Authorization": "Bearer ${FOO_TOKEN}"}},
                   "wiki": {"type": "http", "url": "${FOO_URL}/mcp"},
                   "keyed": {"command": "node", "env": {"KEY": "${FOO_KEY}"}},
                   "plain": {"type": "http", "url": "https://catalog.example.test/plain"}}
        with mock.patch.dict(os.environ, {"FOO_TOKEN": "secret-value", "FOO_KEY": "k"}), mock.patch.object(catalog, "user_env", return_value={}), \
                mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(sorted(self.manifest(servers)), ["plugin:tool:docs", "plugin:tool:keyed", "plugin:tool:plain"])
        catalog._plugin_skipped.clear()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(catalog, "user_env", return_value={}), \
                mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(sorted(self.manifest(servers)), ["plugin:tool:plain"])
            self.assertEqual(sorted(self.manifest(servers)), ["plugin:tool:plain"])
            first = err.getvalue()
        self.assertEqual(first.count("server docs is not started"), 1, first)   # not once more at the second launch
        self.assertIn("FOO_TOKEN", first)
        self.assertEqual(first.count("server wiki is not started"), 1, first)
        self.assertIn("FOO_URL", first)
        self.assertIn("server keyed is not started", first)   # an env key without a value and a default: the same
        with mock.patch.dict(os.environ, {"FOO_TOKEN": ""}, clear=True), mock.patch.object(catalog, "user_env", return_value={}), \
                mock.patch("sys.stderr", new=io.StringIO()) as err:   # an empty variable is no value either
            self.assertEqual(sorted(self.manifest(servers)), ["plugin:tool:plain"])
        self.assertEqual(err.getvalue(), "")
        with mock.patch.dict(os.environ, {"FOO_TOKEN": "x", "FOO_URL": "https://wiki.example.test",
                                           "FOO_KEY": "k"}, clear=True), \
                mock.patch.object(catalog, "user_env", return_value={}):   # the variables are set: it comes up
            self.assertEqual(sorted(self.manifest(servers)),
                             ["plugin:tool:docs", "plugin:tool:keyed", "plugin:tool:plain", "plugin:tool:wiki"])

    def test_the_variables_of_the_session_environment_are_used(self):
        """A variable the owner keeps in the settings env (user_env) reaches the session, so it counts too, and wins."""
        with mock.patch.dict(os.environ, {"FOO_URL": "https://old.example.test"}), \
                mock.patch.object(catalog, "user_env", return_value={"FOO_URL": "https://new.example.test"}):
            got = self.manifest({"docs": {"url": "${FOO_URL}/mcp"}})
        self.assertEqual(got["plugin:tool:docs"]["url"], "https://new.example.test/mcp")

    def test_a_reference_that_is_not_a_variable_is_left_alone(self):
        """${user_config.x} without options and a text that is not a name stay as they are for the existing rules."""
        got = self.manifest({"docs": {"command": "x", "args": ["${not a name}", "$FOO_URL", "${FOO_URL"]}})
        self.assertEqual(got["plugin:tool:docs"]["args"], ["${not a name}", "$FOO_URL", "${FOO_URL"])

    def test_a_link_out_of_the_plugin_is_not_followed(self):
        # a plugin from git also brings symlinks: an .mcp.json or manifest path leading to ~/.claude.json would pull
        # someone else's servers with keys into the session
        home = tempfile.mkdtemp(prefix="plughome-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        with open(os.path.join(home, "claude.json"), "w") as f:
            json.dump({"mcpServers": {"secret": {"command": "x", "env": {"TOKEN": "t"}}}}, f)
        winsafe.symlink(self, os.path.join(home, "claude.json"), os.path.join(self.plug, ".mcp.json"))
        winsafe.symlink(self, os.path.join(home, "claude.json"), os.path.join(self.plug, "servers.json"))
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(self.manifest("./servers.json"), {})
        self.assertIn(".mcp.json", err.getvalue())

    def test_a_link_inside_the_plugin_is_read(self):
        with open(os.path.join(self.plug, "mcp.json"), "w") as f:
            json.dump({"mcpServers": {"db": {"command": "db-mcp"}}}, f)
        winsafe.symlink(self, "mcp.json", os.path.join(self.plug, ".mcp.json"))
        self.assertEqual(list(self.manifest(None)), ["plugin:tool:db"])

    def test_an_unreadable_root_mcp_json_is_reported(self):
        with open(os.path.join(self.plug, ".mcp.json"), "w") as f:
            f.write("{bad")
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(self.manifest(None), {})
        self.assertIn(".mcp.json", err.getvalue())

    def test_a_manifest_path_that_gives_nothing_is_reported(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            got = self.manifest(["./missing.json", "./list.json"], **{"list.json": [1, 2]})
        self.assertEqual(got, {})
        self.assertIn("missing.json", err.getvalue())
        self.assertIn("list.json", err.getvalue())   # the file exists but has no servers - not silent either

    def test_an_inline_server_reaches_the_session(self):
        got = self.manifest({"browser": {"command": "npx", "args": ["-y", "tool-mcp", "${CLAUDE_PLUGIN_ROOT}/x"]}})
        self.assertEqual(list(got), ["plugin:tool:browser"])
        self.assertEqual(os.path.normpath(got["plugin:tool:browser"]["args"][2]), os.path.join(self.plug, "x"))

    def test_a_path_to_a_json_file_is_read(self):
        got = self.manifest("./servers.json", **{"servers.json": {"mcpServers": {"api": {"type": "http", "url": "https://a.test"}}}})
        self.assertEqual(got, {"plugin:tool:api": {"type": "http", "url": "https://a.test"}})

    def test_an_array_mixes_shapes_and_a_later_name_replaces_an_earlier_one(self):
        got = self.manifest(["./servers.json", {"api": {"type": "http", "url": "https://b.test"}}],
                            mcp_json={"mcpServers": {"api": {"type": "http", "url": "https://root.test"},
                                                     "docs": {"type": "http", "url": "https://d.test"}}},
                            **{"servers.json": {"extra": {"type": "http", "url": "https://e.test"}}})
        self.assertEqual({k: v["url"] for k, v in got.items()},
                         {"plugin:tool:api": "https://b.test", "plugin:tool:docs": "https://d.test",
                          "plugin:tool:extra": "https://e.test"})

    def test_a_path_outside_the_plugin_is_ignored(self):
        outside = tempfile.mkdtemp(prefix="plugout-")
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        with open(os.path.join(outside, "s.json"), "w") as f:
            json.dump({"evil": {"command": "x"}}, f)
        rel = os.path.relpath(os.path.join(outside, "s.json"), self.plug)
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(self.manifest(rel), {})
        self.assertIn("s.json", err.getvalue())

    def test_a_path_on_another_drive_does_not_break_the_launch(self):
        with mock.patch("os.path.commonpath", side_effect=ValueError("Paths don't have the same drive")), \
                mock.patch("sys.stderr", new=io.StringIO()):
            self.assertEqual(self.manifest("D:/cfg/servers.json"), {})

    def test_a_bundle_is_skipped_with_a_word_not_silently(self):
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            got = self.manifest(["./server.mcpb", {"api": {"type": "http", "url": "https://a.test"}}])
        self.assertEqual(list(got), ["plugin:tool:api"])
        self.assertIn("server.mcpb", err.getvalue())


if __name__ == "__main__":
    unittest.main()
