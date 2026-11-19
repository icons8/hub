"""An executor's copy gets from .claude/settings.local.json of the folder it was made from only the project's MCP
choice (disabledMcpjsonServers, enabledMcpjsonServers, enableAllProjectMcpServers): git ignores the file, and without
it the .mcp.json servers disabled in the main folder were started again in the copy and failed ("Connection closed").
Everything else (permissions etc.) is not carried over; the core does not run the repo's scripts."""
import tests  # noqa: F401 — keeps the machine's settings out
import json
import os
import subprocess
import tempfile
import unittest

from hosts import worktree


def git(cwd, *a):
    return subprocess.run(["git", "-C", cwd, *a], check=True, capture_output=True, text=True).stdout


def put(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))


def read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


MCP = {"disabledMcpjsonServers": ["grafana"], "enabledMcpjsonServers": ["docs"], "enableAllProjectMcpServers": False}


class WorktreeMcpSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wtm-"))
        self.repo = os.path.join(self.tmp, "repo")
        git(self.tmp, "init", "-q", self.repo)
        put(os.path.join(self.repo, ".gitignore"), ".claude/settings.local.json\n.claude/worktrees/\n")
        put(os.path.join(self.repo, ".mcp.json"), {"mcpServers": {"grafana": {"command": "x"}}})
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
        self.local = os.path.join(self.repo, ".claude", "settings.local.json")

    def test_copy_gets_only_the_project_mcp_keys(self):
        put(self.local, {**MCP, "permissions": {"allow": ["Bash(ls:*)"]}, "env": {"A": "1"}})
        cwd, info, err = worktree.prepare(self.repo, "it-1")
        self.assertIsNone(err)
        self.assertEqual(read(os.path.join(cwd, ".claude", "settings.local.json")), MCP)

    def test_without_the_file_the_copy_has_none(self):
        cwd, info, err = worktree.prepare(self.repo, "it-2")
        self.assertIsNone(err)
        self.assertFalse(os.path.exists(os.path.join(cwd, ".claude", "settings.local.json")))

    def test_file_without_mcp_keys_gives_nothing(self):
        put(self.local, {"permissions": {"allow": ["Bash(ls:*)"]}})
        cwd, info, err = worktree.prepare(self.repo, "it-3")
        self.assertIsNone(err)
        self.assertFalse(os.path.exists(os.path.join(cwd, ".claude", "settings.local.json")))

    def test_existing_file_in_the_copy_keeps_its_other_keys(self):
        # .worktreeinclude or an earlier copy already put its own file there — the MCP keys are added, the rest stays
        put(self.local, {"disabledMcpjsonServers": ["grafana"]})
        dst = os.path.join(self.repo, ".claude", "worktrees", "it-4", ".claude", "settings.local.json")
        git(self.repo, "worktree", "add", "-q", "-b", "task/it-4", os.path.dirname(os.path.dirname(dst)))
        put(dst, {"permissions": {"allow": ["Read"]}, "disabledMcpjsonServers": ["old"]})
        cwd, info, err = worktree.prepare(self.repo, "it-4")
        self.assertIsNone(err)
        self.assertEqual(read(dst), {"permissions": {"allow": ["Read"]}, "disabledMcpjsonServers": ["grafana"]})

    def test_broken_source_is_ignored(self):
        put(self.local, "{not json")
        cwd, info, err = worktree.prepare(self.repo, "it-5")
        self.assertIsNone(err)
        self.assertFalse(os.path.exists(os.path.join(cwd, ".claude", "settings.local.json")))

    def test_not_ignored_by_git_is_not_written(self):
        # the file is not in .gitignore — writing it would make the copy dirty (the "clean" check); we do not write it
        put(os.path.join(self.repo, ".gitignore"), ".claude/worktrees/\n")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "gi")
        put(self.local, MCP)
        cwd, info, err = worktree.prepare(self.repo, "it-6")
        self.assertIsNone(err)
        self.assertFalse(os.path.exists(os.path.join(cwd, ".claude", "settings.local.json")))

    def test_restored_copy_gets_them_too(self):
        put(self.local, MCP)
        cwd, info, err = worktree.prepare(self.repo, "it-7")
        git(self.repo, "worktree", "remove", "--force", info["path"])
        ok, _ = worktree.restore(info)
        self.assertTrue(ok)
        self.assertEqual(read(os.path.join(info["path"], ".claude", "settings.local.json")), MCP)

    def test_other_mcp_json_gets_no_approvals(self):
        # a branch with a different .mcp.json: the disabled ones are carried over, the approvals are not (they were
        # for another file)
        git(self.repo, "checkout", "-q", "-b", "feat/x")
        put(os.path.join(self.repo, ".mcp.json"), {"mcpServers": {"docs": {"command": "other"}}})
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "x")
        git(self.repo, "checkout", "-q", "-")
        put(self.local, {**MCP, "enableAllProjectMcpServers": True})
        cwd, info, err = worktree.prepare(self.repo, "it-8", branch="feat/x")
        self.assertIsNone(err)
        self.assertEqual(read(os.path.join(cwd, ".claude", "settings.local.json")),
                         {"disabledMcpjsonServers": ["grafana"]})

    def test_session_folder_below_the_root(self):
        # a session in a subfolder of the repo: its .claude/settings.local.json goes to the same subfolder of the copy
        put(os.path.join(self.repo, "apps", "web", "a.txt"), "x")
        put(os.path.join(self.repo, ".gitignore"), "settings.local.json\n.claude/worktrees/\n")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "web")
        put(os.path.join(self.repo, "apps", "web", ".claude", "settings.local.json"), {"disabledMcpjsonServers": ["x"]})
        cwd, info, err = worktree.prepare(os.path.join(self.repo, "apps", "web"), "it-9")
        self.assertIsNone(err)
        dst = os.path.join(cwd, ".claude", "settings.local.json")
        self.assertEqual(read(dst), {"disabledMcpjsonServers": ["x"]})


if __name__ == "__main__":
    unittest.main()
