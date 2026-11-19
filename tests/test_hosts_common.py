"""hosts/common.existing_cwd: the session folder is gone (the fork's working copy was removed) — spawn must not fail
(PR #7: a fork, revive after auto-retire — `FileNotFoundError`, the conversation was almost lost)."""
import tests  # noqa: F401 — ignore this machine's settings
import os
import shutil
import tempfile
import unittest

from hosts.common import existing_cwd


class ExistingCwd(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="existing-cwd-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_existing_folder_is_returned_as_is(self):
        self.assertEqual(existing_cwd(self.d), self.d)

    def test_none_or_empty_is_home(self):
        self.assertEqual(existing_cwd(None), os.path.expanduser("~"))
        self.assertEqual(existing_cwd(""), os.path.expanduser("~"))

    def test_missing_worktree_falls_back_to_its_repo(self):
        repo = os.path.join(self.d, "repo")
        os.makedirs(repo)  # the fork's working copy itself (.../.claude/worktrees/<name>) is already removed, the repo is in place
        missing = os.path.join(repo, ".claude", "worktrees", "devops-2")
        self.assertEqual(existing_cwd(missing), repo)

    def test_missing_worktree_whose_repo_is_also_gone_falls_back_to_home(self):
        missing = os.path.join(self.d, "gone-repo", ".claude", "worktrees", "devops-2")
        self.assertEqual(existing_cwd(missing), os.path.expanduser("~"))

    def test_missing_unrelated_folder_falls_back_to_home(self):
        self.assertEqual(existing_cwd(os.path.join(self.d, "nope")), os.path.expanduser("~"))


if __name__ == "__main__":
    unittest.main()
