"""A variable that references itself ("PATH": "<path>:$PATH") in cfg['mcp'][<id>]['server']['env'] must expand to the
real value from the hub's environment, as modules/browser/browsers.py:env() does for browser.env — otherwise the MCP
server gets the literal string "<own path>:$PATH" without the system paths (2026-10-13: an MCP server failed with
CONNECTION_CLOSED on a real action even though the handshake passed). If the variable is not in the environment, a
path element that is entirely a reference is dropped (an empty element would mean "current folder"); a value made of
a single reference leaves the variable unset."""
import tests  # noqa: F401 — keep the machine's settings out
import json
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from core import catalog
from modules.browser import browsers


class McpEnvExpansion(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="mcpenv-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def flags(self, server):
        cfg = {**catalog.load_config(self.dir), "mcp": {"office": {"server": server, "description": "t", "risk": []}}}
        tools = catalog.build(cfg)
        return catalog.launch_flags(tools=["office"], cfg=cfg, catalog=tools, kind="work", cwd=self.dir,
                                    project="all", state_dir=self.dir, name="s")

    def written(self, server):
        flags = self.flags(server)
        path = flags[flags.index("--mcp-config") + 1]
        with open(path) as f:
            return path, json.load(f)["mcpServers"]["office"]

    def test_dollar_path_expands_to_the_real_path(self):
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}):
            _, srv = self.written({"command": "npx", "args": ["-y", "x"], "env": {"PATH": "/my/node/bin:$PATH"}})
        self.assertEqual(srv["env"]["PATH"], "/my/node/bin:/usr/bin:/bin")

    def test_no_env_untouched(self):
        _, srv = self.written({"command": "npx", "args": ["-y", "x"]})
        self.assertNotIn("env", srv)

    def test_missing_or_empty_variable_leaves_no_empty_path_element(self):
        # a path list: a reference element is dropped ("/my/bin:" would also mean the current folder), a reference
        # inside an element becomes empty, as in the shell; everything else stays as written
        env = {k: v for k, v in os.environ.items() if not k.startswith("HUB_TEST_")}
        sep = os.pathsep
        cases = {"HUB_TEST_A": (f"/my/bin{sep}$HUB_TEST_A", "/my/bin"),
                 "HUB_TEST_B": (f"${{HUB_TEST_B}}{sep}/lib", "/lib"),
                 "HUB_TEST_C": (f"/a{sep}$HUB_TEST_C{sep}/b", f"/a{sep}/b"),
                 "HUB_TEST_EMPTY": (f"/opt/lib{sep}$HUB_TEST_EMPTY", "/opt/lib"),
                 "HUB_TEST_D": ("$HUB_TEST_D --flag", " --flag"),
                 "HUB_TEST_E": (f"/opt{sep}$HUB_TEST_E/site", f"/opt{sep}/site"),         # do not glue into /opt/site
                 "HUB_TEST_F": ("::1,localhost,$HUB_TEST_F", "::1,localhost,"),           # leave someone else's "::" alone
                 "HUB_TEST_G": (f"$HUB_TEST_G{sep}", "")}                                 # an empty tail stays — as written
        with mock.patch.dict(os.environ, {**env, "HUB_TEST_EMPTY": ""}, clear=True):
            _, srv = self.written({"command": "x", "env": {**{k: v for k, (v, _) in cases.items()},
                                                          "HUB_TEST_ONLY": "$HUB_TEST_ONLY"}})
        self.assertEqual({k: srv["env"].get(k) for k in cases}, {k: want for k, (_, want) in cases.items()})
        self.assertNotIn("HUB_TEST_ONLY", srv["env"])    # a value made of one reference — the variable is unset, not empty

    def test_a_failed_write_leaves_no_copy_behind(self):
        path = os.path.join(self.dir, "x.json")
        with self.assertRaises(TypeError):
            catalog.write_private(path, {"bad": object()})
        self.assertEqual(os.listdir(self.dir), [])

    def test_only_the_whole_name_and_braces_count(self):
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin"}):
            _, srv = self.written({"command": "x", "env": {"PATH": "/x:${PATH}:$PATH_EXTRA"}})
        self.assertEqual(srv["env"]["PATH"], "/x:/usr/bin:$PATH_EXTRA")

    def test_other_variables_are_not_expanded(self):
        with mock.patch.dict(os.environ, {"HOME": "/home/x"}):
            _, srv = self.written({"command": "x", "env": {"CACHE": "$HOME/c"}})
        self.assertEqual(srv["env"]["CACHE"], "$HOME/c")

    @unittest.skipIf(os.name == "nt", "POSIX file permissions")
    def test_session_mcp_json_is_owner_only(self):
        path = os.path.join(self.dir, "sessions", "s", "mcp.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write("{}")
        os.chmod(path, 0o644)  # left over from an earlier write with ordinary permissions
        self.written({"command": "x", "env": {"TOKEN": "secret"}})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX file permissions")
    def test_mcp_login_folder_copy_is_owner_only_too(self):
        # MCP login (`hub mcp login`) copies the server description from the session's mcp.json — with the same env,
        # which may hold keys
        from core import mcplogin
        self.written({"command": "x", "env": {"TOKEN": "secret"}})
        d = mcplogin.login_dir(self.dir, "s", "office")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, ".mcp.json")).st_mode), 0o600)

    def test_browser_env_drops_a_missing_variable(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("HUB_TEST_NOPE")}
        with mock.patch.dict(os.environ, env, clear=True):
            e = browsers.env({"browser": {"env": {"HUB_TEST_NOPE": f"/my/bin{os.pathsep}$HUB_TEST_NOPE"}}})
        self.assertEqual(e["HUB_TEST_NOPE"], "/my/bin")

    def test_browser_env_expands_self_reference(self):
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin"}):
            e = browsers.env({"browser": {"env": {"PATH": "/opt/node/bin:$PATH", "N": 5}}})
        self.assertEqual((e["PATH"], e["N"]), ("/opt/node/bin:/usr/bin", 5))


if __name__ == "__main__":
    unittest.main()
