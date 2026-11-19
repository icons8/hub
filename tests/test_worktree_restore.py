"""Reviving a fork (PR #7): when a fork leaves, the core removes its working copy, and `hub session revive` failed on
respawn. The working copy is restored in the same place — from the fork's branch if it remains, otherwise new — from
the repo HEAD (with --from-main — from the main branch); the Claude Code conversation is stored by the folder path,
the path is the same — `--resume` will find it."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import subprocess
import tempfile
import unittest

import platforms
from hosts import worktree


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=True).stdout.strip()


class Restore(unittest.TestCase):
    def setUp(self):
        self.repo = os.path.realpath(tempfile.mkdtemp(prefix="wt-restore-"))
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "start")
        self.path = os.path.join(self.repo, ".claude", "worktrees", "w-2")
        self.info = {"path": self.path, "branch": "fork/w-2", "repo": self.repo}

    def test_from_the_kept_branch(self):
        git(self.repo, "worktree", "add", "-q", "-b", "fork/w-2", self.path)
        git(self.path, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "fork work")
        git(self.repo, "worktree", "remove", self.path)                      # removed, the branch with the commit remained
        ok, why = worktree.restore(self.info)
        self.assertTrue(ok, why)
        self.assertEqual(git(self.path, "branch", "--show-current"), "fork/w-2")
        self.assertEqual(git(self.path, "log", "-1", "--format=%s"), "fork work")

    def test_branch_gone_new_one_from_head(self):
        ok, why = worktree.restore(self.info)
        self.assertTrue(ok, why)
        self.assertEqual(git(self.path, "branch", "--show-current"), "fork/w-2")

    def test_branch_gone_new_one_from_head_or_from_main_as_the_copy_was_made(self):
        git(self.repo, "switch", "-q", "-c", "feature/other")
        git(self.repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "other")
        ok, why = worktree.restore(self.info)                               # by default — from the main copy's HEAD
        self.assertTrue(ok, why)
        self.assertEqual(git(self.path, "rev-parse", "HEAD"), git(self.repo, "rev-parse", "feature/other"))
        git(self.repo, "worktree", "remove", self.path)
        git(self.repo, "branch", "-D", "fork/w-2")
        ok, why = worktree.restore({**self.info, "from_main": True})        # the copy was --from-main — from main
        self.assertTrue(ok, why)
        self.assertEqual(git(self.path, "rev-parse", "HEAD"), git(self.repo, "rev-parse", "main"))

    def test_folder_deleted_by_hand_is_restored_from_its_branch(self):
        git(self.repo, "worktree", "add", "-q", "-b", "fork/w-2", self.path)
        platforms.current.rmtree(self.path)                                  # removed past git: the record stays
        ok, why = worktree.restore(self.info)
        self.assertTrue(ok, why)
        self.assertEqual(git(self.path, "branch", "--show-current"), "fork/w-2")

    def test_another_copys_record_is_left_alone(self):
        # a .git shared between machines (or a copy on a disk not mounted now): another copy's folder is missing here.
        # A repo-wide prune would drop its record for the other machine too
        other = os.path.join(self.repo, ".claude", "worktrees", "other")
        git(self.repo, "worktree", "add", "-q", "-b", "fork/other", other)
        platforms.current.rmtree(other)
        ok, why = worktree.restore(self.info)
        self.assertTrue(ok, why)
        self.assertIn(other.replace(os.sep, "/"), git(self.repo, "worktree", "list", "--porcelain"))   # git prints "/"

    def test_a_branch_a_live_copy_holds_is_not_checked_out_twice(self):
        # the kept branch (--branch) may be checked out in another live copy: a second copy on it is refused, not forced
        other = os.path.join(self.repo, ".claude", "worktrees", "live")
        git(self.repo, "worktree", "add", "-q", "-b", "fork/w-2", other)
        ok, _ = worktree.restore({**self.info, "keep_branch": True})
        self.assertFalse(ok)
        self.assertFalse(os.path.isdir(self.path))

    def test_already_there_or_no_repo(self):
        os.makedirs(self.path)
        self.assertEqual(worktree.restore(self.info), (True, "the working copy is in place"))
        self.assertFalse(worktree.restore({"path": "/nonexistent/x", "branch": "fork/x", "repo": "/nonexistent"})[0])


if __name__ == "__main__":
    unittest.main()
