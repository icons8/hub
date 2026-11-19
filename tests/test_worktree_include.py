"""An executor's working copy gets the files from .worktreeinclude, as with `claude --worktree` (06.10, an executor's
finding: the copy lacked the git-ignored .env listed in .worktreeinclude — the project's tests failed at start). Only
files that are both in .worktreeinclude and ignored by git are copied; other working copies in .claude/worktrees are not."""
import tests  # noqa: F401 — keep the machine's settings out
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from hosts import worktree


def git(cwd, *a):
    return subprocess.run(["git", "-C", cwd, *a], check=True, capture_output=True, text=True).stdout


def put(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class WorktreeInclude(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wti-"))
        self.repo = os.path.join(self.tmp, "repo")
        git(self.tmp, "init", "-q", self.repo)
        put(os.path.join(self.repo, ".gitignore"), ".env\nlocal.json\n.claude/worktrees/\nbuild/\n")
        put(os.path.join(self.repo, ".worktreeinclude"), ".env\napps/*/config/local.json\n")
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
        put(os.path.join(self.repo, ".env"), "KEY=1")
        put(os.path.join(self.repo, "apps", "search", "config", "local.json"), "{}")
        put(os.path.join(self.repo, "build", "out.bin"))                         # ignored, but not in the list
        put(os.path.join(self.repo, ".claude", "worktrees", "old", ".env"), "someone else's copy")

    def test_executor_copy_gets_the_listed_ignored_files(self):
        cwd, info, err = worktree.prepare(self.repo, "it-1")
        self.assertIsNone(err)
        with open(os.path.join(cwd, ".env")) as f:
            self.assertEqual(f.read(), "KEY=1")
        self.assertTrue(os.path.isfile(os.path.join(cwd, "apps", "search", "config", "local.json")))
        self.assertFalse(os.path.exists(os.path.join(cwd, "build")))
        self.assertFalse(os.path.exists(os.path.join(cwd, ".claude", "worktrees", "old")))
        self.assertIn(["files from .worktreeinclude: 2", True], info["checks"])

    def test_ignored_folders_not_named_by_the_list_are_not_walked(self):
        # 23.10: in a monorepo with gigabytes of ignored files (node_modules, logs, .pnpm-store) two walks over all
        # files took ~30 s — a timeout, and the executor did not start. As in `claude --worktree`: an ignored
        # directory is one line (`--directory`), and we descend into it only if a list pattern can match inside
        put(os.path.join(self.repo, "build", "deep", ".env"), "from a build")             # an .env, but in a directory not on the list
        put(os.path.join(self.repo, "secrets", "a.key"), "k")                          # the directory is named in the list
        put(os.path.join(self.repo, "apps", "search", "config", "cache", "x.bin"))    # does not match a pattern
        with open(os.path.join(self.repo, ".gitignore"), "a") as f:
            f.write("secrets/\ncache/\n")
        with open(os.path.join(self.repo, ".worktreeinclude"), "a") as f:
            f.write("/secrets/\n")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "more")
        calls, real = [], worktree._git

        def spy(cwd, *args, **kw):
            calls.append(args)
            return real(cwd, *args, **kw)
        with mock.patch.object(worktree, "_git", spy):
            cwd, info, err = worktree.prepare(self.repo, "it-3")
        self.assertIsNone(err)
        self.assertTrue(os.path.isfile(os.path.join(cwd, ".env")))
        self.assertTrue(os.path.isfile(os.path.join(cwd, "secrets", "a.key")))
        self.assertFalse(os.path.exists(os.path.join(cwd, "build")))
        self.assertFalse(os.path.exists(os.path.join(cwd, "apps", "search", "config", "cache")))
        walks = [a for a in calls if "ls-files" in a and "--ignored" in a]
        self.assertTrue(walks)
        for a in walks:   # each walk either does not enter ignored directories or only the named ones
            self.assertTrue("--directory" in a or ("--" in a and a[a.index("--") + 1:] == ("secrets/",)), a)

    def test_list_patterns_follow_git_rules(self):
        # git itself matches the list patterns: "!" inside a directory under a pattern, "**" on a named directory,
        # character classes; a pattern git does not understand is no reason to abort the preparation
        with open(os.path.join(self.repo, ".gitignore"), "a") as f:
            f.write("foo/\na/b/\nsecret*.env\n")
        with open(os.path.join(self.repo, ".worktreeinclude"), "a") as f:
            f.write("foo/**\n!foo/keep\n[z-a]\na/**/z\nsecret[[:digit:]].env\n")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "more")
        put(os.path.join(self.repo, "foo", "keep"))
        put(os.path.join(self.repo, "foo", "take"))
        put(os.path.join(self.repo, "a", "b", "c", "z"))
        put(os.path.join(self.repo, "secret1.env"))
        put(os.path.join(self.repo, "secretx.env"))
        cwd, info, err = worktree.prepare(self.repo, "it-5")
        self.assertIsNone(err)
        got = {os.path.relpath(os.path.join(d, f), cwd) for d, _, fs in os.walk(cwd) for f in fs
               if ".git" not in os.path.relpath(d, cwd).split(os.sep)} - set(git(cwd, "ls-files").split()) - {".git"}
        self.assertEqual(got, {".env", os.path.join("apps", "search", "config", "local.json"),
                               os.path.join("foo", "take"), os.path.join("a", "b", "c", "z"), "secret1.env"})

    def test_failed_copy_is_removed_with_its_branch(self):
        # 23.10: the preparation failed after `worktree add` (a timeout) — the copy and the task/<name> branch stayed
        # without a session, cleanup did not see them; a retry with the same name went into someone else's half-copy
        with mock.patch.object(worktree, "_mcp_settings", side_effect=subprocess.TimeoutExpired("git", 1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                worktree.prepare(self.repo, "it-4")
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".claude", "worktrees", "it-4")))
        self.assertNotIn("it-4", git(self.repo, "worktree", "list"))
        self.assertEqual(git(self.repo, "branch", "--list", "task/it-4"), "")

    def test_include_failure_does_not_cancel_the_copy(self):
        # the list was not copied (git refused, a timeout) — the executor works without it, the reason is visible in the checks
        with mock.patch.object(worktree, "wanted", side_effect=subprocess.TimeoutExpired("git", 600)):
            cwd, info, err = worktree.prepare(self.repo, "it-6")
        self.assertIsNone(err)
        self.assertTrue(os.path.isdir(cwd))
        self.assertTrue(any(not ok and "were not copied" in what for what, ok in info["checks"]))

    def test_without_the_list_nothing_is_copied(self):
        os.remove(os.path.join(self.repo, ".worktreeinclude"))
        cwd, info, err = worktree.prepare(self.repo, "it-2")
        self.assertIsNone(err)
        self.assertFalse(os.path.exists(os.path.join(cwd, ".env")))


if __name__ == "__main__":
    unittest.main()
