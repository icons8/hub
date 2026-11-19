"""`hub task run … --tools conduit`, when the executor's repo already brings that MCP server in its .mcp.json: the session
gets it from there anyway (launch_flags passes the project's file), so the core does not ask the owner for a "yes" on it
(a session named such a server among the tools of an executor, and the owner got a card for something the executor
had). What the daemon finds in the folder is catalog.repo_tools; the rest of the gate (a risky tool the repo does not
bring) is as before. The reducer side — tests/test_task_run_tools.py."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import subprocess
import tempfile
import unittest

import platforms
from core import catalog
from tests import test_stream_hubd

CONDUIT = {"type": "http", "url": "https://conduit.example.test/mcp"}
CATALOG = {"conduit": {"kind": "mcp", "server": CONDUIT, "risk": ["prod"]},
           "maps": {"kind": "mcp", "server": {"command": "maps-mcp", "args": ["--stdio"]}, "risk": ["prod"],
                    "as": "maps-local"},
           "wiki": {"kind": "mcp", "server": {"type": "http", "url": "https://wiki.example.test/mcp"}, "risk": []},
           "kit": {"kind": "plugin", "risk": []}}


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


class RepoTools(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="repotools-")
        self.addCleanup(platforms.current.rmtree, self.dir)
        self.repo = os.path.join(self.dir, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        self.servers = {"conduit": dict(CONDUIT), "maps-local": {"command": "maps-mcp", "args": ["--stdio"]},
                        "wiki": {"type": "http", "url": "https://other.example.test/mcp"}}
        write(os.path.join(self.repo, ".mcp.json"), {"mcpServers": self.servers})

    def got(self, tools=("conduit", "maps", "wiki", "kit"), cfg=None, project="all", cwd=None):
        return catalog.repo_tools(list(tools), CATALOG, cfg or {}, cwd or self.repo, project)

    def test_the_server_of_the_same_address_is_the_repos(self):
        self.assertEqual(self.got(), ["conduit", "maps"])   # a command, by its `as` name; a plugin has none; wiki is another address

    def test_a_trailing_slash_is_the_same_address(self):
        self.servers["conduit"]["url"] += "/"
        write(os.path.join(self.repo, ".mcp.json"), {"mcpServers": self.servers})
        self.assertEqual(self.got(("conduit",)), ["conduit"])

    def test_the_same_name_at_another_address_is_not_the_catalogs_tool(self):
        self.servers["conduit"]["url"] = "https://elsewhere.example.test/mcp"
        write(os.path.join(self.repo, ".mcp.json"), {"mcpServers": self.servers})
        self.assertEqual(self.got(("conduit",)), [])

    def test_the_same_address_with_other_credentials_is_not_the_catalogs_tool(self):
        # the catalog's entry carries what it is started or called with; the repo's file has other (or no) credentials
        catalog_ = {"conduit": {**CATALOG["conduit"], "server": {**CONDUIT, "headers": {"Authorization": "Bearer k-1"}}},
                    "maps": {**CATALOG["maps"], "server": {**CATALOG["maps"]["server"], "env": {"MAPS_ENV": "prod"}}}}
        self.assertEqual(catalog.repo_tools(["conduit", "maps"], catalog_, {}, self.repo, "all"), [])
        self.servers["conduit"]["headers"] = {"Authorization": "Bearer k-1"}
        self.servers["maps-local"]["env"] = {"MAPS_ENV": "prod"}
        write(os.path.join(self.repo, ".mcp.json"), {"mcpServers": self.servers})
        self.assertEqual(catalog.repo_tools(["conduit", "maps"], catalog_, {}, self.repo, "all"), ["conduit", "maps"])

    def test_another_command_is_not_the_catalogs_tool(self):
        self.servers["maps-local"]["args"] = ["--other"]
        write(os.path.join(self.repo, ".mcp.json"), {"mcpServers": self.servers})
        self.assertEqual(self.got(("maps",)), [])

    def test_a_server_the_project_switched_off_does_not_come_up(self):
        write(os.path.join(self.repo, ".claude", "settings.local.json"), {"disabledMcpjsonServers": ["conduit"]})
        self.assertEqual(self.got(), ["maps"])

    def test_only_where_the_session_gets_the_projects_file(self):
        # launch_flags passes .mcp.json for project "all" and without the owner's connectors
        self.assertEqual(self.got(project="docs"), [])
        self.assertEqual(self.got(cfg={"connectors": True}), [])

    def test_no_file_or_a_broken_one_brings_nothing(self):
        self.assertEqual(self.got(cwd=self.dir), [])
        with open(os.path.join(self.repo, ".mcp.json"), "w") as f:
            f.write("{not json")
        self.assertEqual(self.got(), [])
        write(os.path.join(self.repo, ".mcp.json"), {"mcpServers": ["conduit"]})
        self.assertEqual(self.got(), [])
        self.assertEqual(self.got(cwd=None), [])


class RepoToolsLive(test_stream_hubd.StreamHubdTest):
    """A real hubd, a repo with .mcp.json, the executor of a session that names the tools in --tools."""
    locals().update({n: None for n in dir(test_stream_hubd.StreamHubdTest) if n.startswith("test_")})

    def setUp(self):
        super().setUp()
        self.addCleanup(lambda p=catalog.config_path(self.state): os.path.exists(p) and os.remove(p))
        write(catalog.config_path(self.state), {"mcp": {
            "conduit": {"server": CONDUIT, "risk": ["prod"], "description": "t"},
            "mail": {"server": {"command": "mail-mcp"}, "risk": ["owner-data"], "description": "t"}}})
        self.hub.load_catalog()

    def repo_with_mcp(self, servers):
        top = self.repo()
        write(os.path.join(top, ".mcp.json"), {"mcpServers": servers})
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@x"}
        for args in (("add", ".mcp.json"), ("commit", "-q", "-m", "mcp")):
            subprocess.run(["git", "-C", top, *args], check=True, capture_output=True, env=env)
        return top

    def run_task(self, servers):
        self.new_session("maps", role="agent", about="maps", cwd=self.repo_with_mcp(servers))
        self.assertIn("conduit", self.hub.state["catalog"])
        self.cli("task.run_requested", "maps-job", task="check the release", tools=["conduit", "mail"],
                 channel="session", owner=False, sender="maps")
        self.until(lambda: self.sess("maps-job").get("state") == "idle" or self.events("task.run_failed"), timeout=15)
        self.assertEqual(self.events("task.run_failed"), [])
        return self.sess("maps-job")

    def test_a_tool_the_repo_brings_gets_no_card_and_the_other_risky_one_does(self):
        x = self.run_task({"conduit": CONDUIT})
        self.assertEqual({r["tool"]: r["state"] for r in x["requests"]},
                         {"conduit": "granted", "mail": "needs-owner"})
        (spawn,) = [e for e in self.events("session.requested") if e["entity"] == "maps-job"]
        self.assertEqual(spawn["data"]["repo_tools"], ["conduit"])                     # the fact is in the journal
        (d,) = [d for d in self.hub.state["delegations"].values() if d["session"] == "maps-job"]
        self.assertIn("Already available", d["context"])

    def test_a_repo_without_the_server_asks_for_both(self):
        x = self.run_task({"wiki": {"type": "http", "url": "https://wiki.example.test/mcp"}})
        self.assertEqual({r["tool"]: r["state"] for r in x["requests"]},
                         {"conduit": "needs-owner", "mail": "needs-owner"})
        (spawn,) = [e for e in self.events("session.requested") if e["entity"] == "maps-job"]
        self.assertEqual(spawn["data"].get("repo_tools", []), [])


if __name__ == "__main__":
    unittest.main()
