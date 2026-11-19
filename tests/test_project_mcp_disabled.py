"""A server the project switched off (`disabledMcpjsonServers` in its .claude/settings*.json) is not passed to claude
with the rest of the project's .mcp.json: --mcp-config carries a file whole, and the choice of a project does not act
on it (checked live with claude 2.1.309: a disabled server came up as "dynamic")."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import subprocess
import tempfile
import unittest

import platforms
from core import catalog


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


class DisabledProjectServers(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pmcp-")
        self.addCleanup(platforms.current.rmtree, self.dir)
        self.repo = os.path.join(self.dir, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        self.state = os.path.join(self.dir, "state")
        os.makedirs(self.state)
        self.servers = {"maps": {"command": "true"}, "docs": {"command": "true", "env": {"KEY": "k-1"}},
                        "wiki": {"type": "http", "url": "https://wiki.example.test/mcp"}}
        write(os.path.join(self.repo, ".mcp.json"), {"mcpServers": self.servers})

    def project_file(self, cwd=None):
        """The file --mcp-config carries for the project's servers: the one that holds `maps` or `docs`."""
        cwd = cwd or self.repo
        cfg = catalog.load_config(self.dir)
        flags = catalog.launch_flags(tools=[], cfg=cfg, catalog=catalog.build(cfg), kind="work", cwd=cwd,
                                     project="all", state_dir=self.state, name="s")
        files = [flags[i + 1] for i, f in enumerate(flags) if f == "--mcp-config"]
        return files[0] if files else None

    def names(self, path):
        with open(path, encoding="utf-8") as f:
            return sorted(json.load(f)["mcpServers"])

    def test_nothing_disabled_passes_the_projects_file_as_it_is(self):
        self.assertEqual(self.project_file(), os.path.join(self.repo, ".mcp.json"))

    def test_a_disabled_server_is_not_passed(self):
        write(os.path.join(self.repo, ".claude", "settings.local.json"), {"disabledMcpjsonServers": ["docs"]})
        path = self.project_file()
        self.assertEqual(self.names(path), ["maps", "wiki"])
        self.assertNotEqual(path, os.path.join(self.repo, ".mcp.json"))
        with open(os.path.join(self.repo, ".mcp.json"), encoding="utf-8") as f:
            self.assertEqual(sorted(json.load(f)["mcpServers"]), ["docs", "maps", "wiki"])   # the project's file is intact
        if os.name == "posix":
            self.assertEqual(os.stat(path).st_mode & 0o077, 0)   # its servers' env may hold keys

    def test_the_copy_lives_in_the_state_folder_and_goes_when_the_switch_off_is_lifted(self):
        settings = os.path.join(self.repo, ".claude", "settings.local.json")
        write(settings, {"disabledMcpjsonServers": ["docs"]})
        path = self.project_file()
        self.assertTrue(path.startswith(os.path.join(self.state, "sessions", "s") + os.sep), path)   # not in the repo
        self.assertEqual(subprocess.run(["git", "-C", self.repo, "status", "--porcelain", "-uall"], capture_output=True,
                                        text=True).stdout.count("project-mcp"), 0)
        write(settings, {"disabledMcpjsonServers": []})
        self.assertEqual(self.project_file(), os.path.join(self.repo, ".mcp.json"))
        self.assertFalse(os.path.exists(path))

    def test_both_settings_files_and_the_repo_root_count(self):
        write(os.path.join(self.repo, ".claude", "settings.json"), {"disabledMcpjsonServers": ["maps"]})
        write(os.path.join(self.repo, ".claude", "settings.local.json"), {"disabledMcpjsonServers": ["wiki"]})
        sub = os.path.join(self.repo, "pkg")
        os.makedirs(sub)
        write(os.path.join(sub, ".mcp.json"), {"mcpServers": self.servers})
        self.assertEqual(self.names(self.project_file(sub)), ["docs"])   # a subfolder reads its repo's choice

    def test_all_disabled_leaves_no_servers_and_odd_settings_are_ignored(self):
        write(os.path.join(self.repo, ".claude", "settings.local.json"),
              {"disabledMcpjsonServers": ["maps", "docs", "wiki", 7, None, ["maps"]]})
        self.assertEqual(self.names(self.project_file()), [])
        write(os.path.join(self.repo, ".claude", "settings.local.json"), {"disabledMcpjsonServers": "docs"})
        self.assertEqual(self.project_file(), os.path.join(self.repo, ".mcp.json"))   # not a list: nothing is disabled
        with open(os.path.join(self.repo, ".claude", "settings.local.json"), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(self.project_file(), os.path.join(self.repo, ".mcp.json"))

    def test_a_file_of_another_shape_goes_as_before(self):
        write(os.path.join(self.repo, ".claude", "settings.local.json"), {"disabledMcpjsonServers": ["docs"]})
        for shape in (["docs"], {"mcpServers": ["docs"]}, {"mcpServers": None}, {"servers": {}}):
            write(os.path.join(self.repo, ".mcp.json"), shape)
            self.assertEqual(self.project_file(), os.path.join(self.repo, ".mcp.json"), shape)

    def test_a_project_file_that_is_not_readable_goes_as_before(self):
        write(os.path.join(self.repo, ".claude", "settings.local.json"), {"disabledMcpjsonServers": ["docs"]})
        with open(os.path.join(self.repo, ".mcp.json"), "w", encoding="utf-8") as f:
            f.write("{broken")
        self.assertEqual(self.project_file(), os.path.join(self.repo, ".mcp.json"))   # claude reports it, as before


if __name__ == "__main__":
    unittest.main()
