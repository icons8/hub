"""An executor on an existing branch (`hub task run … --branch <branch>`) and in a ready copy (`--repo W --here`) —
mini-hubs, slice 3. The daemon on a real git: which copy, which branch, who was switched, what cleanup leaves alone."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import subprocess
import tempfile
import types
import unittest

import platforms
from core.daemon import Hub
from hosts import worktree

OWNER = {"channel": "cli", "owner": True}


def git(cwd, *a):
    return subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True,
                          capture_output=True, text=True).stdout.strip()


def repo(tmp, name="repo"):
    path = os.path.join(tmp, name)
    git(tmp, "init", "-q", "-b", "main", path)
    with open(os.path.join(path, "a.txt"), "w") as f:
        f.write("a\n")
    git(path, "add", "a.txt")
    git(path, "commit", "-q", "-m", "init")
    git(path, "branch", "feat/x")
    return path


class Folders(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="exfold-"))
        self.repo = repo(self.tmp)
        self.h = Hub(os.path.join(self.tmp, "state"), types.SimpleNamespace())
        self.h.execute = lambda a: None
        rec = self.h.record
        rec("session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **OWNER})
        rec("owner.settings", None, {"parallel": 3, "channel": "core"})
        rec("session.requested", "maps", {"cwd": self.repo, "session_id": "s-m", "role": "agent", **OWNER})
        rec("session.started", "maps", {})
        rec("session.folder", "maps", {"channel": "core", "git": "ok", "dirty": 0}, src="core")

    def raise_(self, name, **extra):
        self.h.record("task.run_requested", name, {"task": "do it", "channel": "session", "owner": False,
                                                   "sender": "maps", **extra})
        self.assertIn(name, self.h.state.get("runs") or {}, self.h.state)
        self.h.run_tasks()
        return self.h.state["sessions"].get(name)

    def clean(self):
        """Copy cleanup as two ticks: git runs in the background (core/daemon.clean_worktrees), the next tick writes the result."""
        self.h.clean_worktrees()
        if self.h.cleaner:
            self.h.cleaner.join(30)
        self.h.clean_worktrees()

    def failed(self, name):
        """The reason a start was refused — as a line to whoever started it (task.run_failed)."""
        self.assertNotIn(name, self.h.state["sessions"])
        texts = [m["text"] for m in self.h.state["messages"].values() if m["session"] == "maps"
                 and f"Could not raise executor {name}" in m["text"]]
        self.assertEqual(len(texts), 1, texts)
        return texts[0]

    # --branch
    def test_branch_gives_a_copy_on_that_branch_and_no_new_task_branch(self):
        x = self.raise_("maps-x", branch="feat/x")
        self.assertEqual(git(x["cwd"], "branch", "--show-current"), "feat/x")
        self.assertNotIn("task/maps-x", git(self.repo, "branch", "--list"))
        self.assertTrue(x["worktree"]["keep_branch"])
        (d,) = [d for d in self.h.state["delegations"].values() if d["session"] == "maps-x"]
        self.assertIn("on the existing branch feat/x", d["context"])

    def test_branch_open_in_the_clean_minihub_folder_switches_it_to_the_main_branch(self):
        git(self.repo, "switch", "-q", "feat/x")
        x = self.raise_("maps-x", branch="feat/x")
        self.assertEqual(git(self.repo, "branch", "--show-current"), "main")
        self.assertEqual(git(x["cwd"], "branch", "--show-current"), "feat/x")

    def test_branch_open_in_a_dirty_minihub_folder_is_refused_and_the_folder_stays(self):
        git(self.repo, "switch", "-q", "feat/x")
        with open(os.path.join(self.repo, "a.txt"), "a") as f:
            f.write("started\n")
        self.raise_("maps-x", branch="feat/x")
        self.assertIn("uncommitted changes", self.failed("maps-x"))
        self.assertEqual(git(self.repo, "branch", "--show-current"), "feat/x")

    def test_branch_open_at_another_executor_is_refused_with_its_name(self):
        self.raise_("maps-x", branch="feat/x")
        self.raise_("maps-y", branch="feat/x")
        self.assertIn("maps-x", self.failed("maps-y"))

    def test_a_branch_that_does_not_exist_is_refused(self):
        self.raise_("maps-x", branch="feat/nope")
        self.assertIn("feat/nope", self.failed("maps-x"))

    def test_removing_a_branch_executor_keeps_the_branch_even_when_merged(self):
        x = self.raise_("maps-x", branch="feat/x")
        self.assertEqual(x["worktree"]["branch"], "feat/x")
        ok, why = worktree.remove(x["worktree"])
        self.assertTrue(ok, why)
        self.assertFalse(os.path.exists(x["cwd"]))
        self.assertIn("feat/x", git(self.repo, "branch", "--list"))      # the branch is merged into main — still its own

    def test_branch_in_a_folder_without_git_is_refused_not_ignored(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.raise_("maps-x", repo=plain, branch="feat/x")
        self.assertIn("is not git", self.failed("maps-x"))

    def test_the_reason_reads_in_the_owners_language_in_a_fold_of_the_journal(self):
        # the page and the CLI fold the journal themselves: the reason's marks ride in task.run_failed (reason_l10n), so
        # the Russian owner reads the core's reason in Russian, the session the English
        from core import coretext, i18n, journal, reducer
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.raise_("maps-x", repo=plain, branch="feat/x")
        st = reducer.replay(journal.read(os.path.join(self.h.state_dir, "journal.jsonl")))
        (m,) = [m for m in st["messages"].values() if m["session"] == "maps"
                and "Could not raise executor maps-x" in m["text"]]
        self.assertIn("is not git", m["text"])
        ru = coretext.shown(m, "ru")
        self.assertIn(i18n.tr("ru", "core.daemon.not_git", base=plain, branch="feat/x"), ru)
        self.assertNotIn("is not git", ru)

    def test_the_main_branch_is_refused_even_when_it_is_not_open_anywhere(self):
        git(self.repo, "switch", "-q", "feat/x")
        self.raise_("maps-x", branch="main")
        self.assertIn("main branch", self.failed("maps-x"))

    def test_a_minihub_in_a_linked_copy_is_told_plainly_when_main_is_open_elsewhere(self):
        linked = os.path.join(self.tmp, "linked")
        git(self.repo, "worktree", "add", "-q", linked, "feat/x")          # the main copy is on main
        self.h.record("session.cwd_set", "maps", {"cwd": linked, **OWNER})
        self.h.record("session.folder", "maps", {"channel": "core", "git": "ok", "dirty": 0}, src="core")
        self.raise_("maps-x", branch="feat/x")
        self.assertIn("switch it from feat/x yourself", self.failed("maps-x"))
        self.assertEqual(git(linked, "branch", "--show-current"), "feat/x")   # the folder as it was, not "no branch"

    def test_a_revived_branch_executor_keeps_the_branch_from_origin(self):
        clone = os.path.join(self.tmp, "clone")
        git(self.tmp, "clone", "-q", self.repo, clone)                     # feat/x exists only on origin
        cwd, info, err = worktree.prepare(clone, "maps-x", branch="feat/x")
        self.assertIsNone(err)
        git(clone, "worktree", "remove", info["path"])
        git(clone, "branch", "-D", "feat/x")
        git(clone, "commit", "-q", "--allow-empty", "-m", "onward on main")   # HEAD moved ahead
        ok, why = worktree.restore(info)
        self.assertTrue(ok, why)
        self.assertEqual(git(info["path"], "rev-parse", "HEAD"), git(clone, "rev-parse", "origin/feat/x"))

    def test_a_switched_folder_is_named_when_the_copy_then_fails(self):
        from unittest import mock
        git(self.repo, "switch", "-q", "feat/x")
        with mock.patch("core.daemon.worktree.prepare", return_value=(None, None, "git worktree add: failure")):
            self.raise_("maps-x", branch="feat/x")
        self.assertIn("was switched from feat/x to main", self.failed("maps-x"))

    # --here
    def copy(self, name="copy"):
        """A ready copy not made by the core (like a dispatcher's script): its own branch."""
        w = os.path.join(self.tmp, name)
        git(self.repo, "worktree", "add", "-q", "-b", f"fix/{name}", w)
        return w

    def test_here_works_right_in_the_copy_without_a_nested_one_or_a_new_branch(self):
        w = self.copy()
        x = self.raise_("maps-x", repo=w, here=True)
        self.assertEqual(x["cwd"], w)
        self.assertTrue(x["here"])
        self.assertIsNone(x.get("worktree"))
        self.assertFalse(os.path.exists(os.path.join(w, ".claude")))
        self.assertNotIn("task/maps-x", git(self.repo, "branch", "--list"))
        self.assertEqual(git(w, "branch", "--show-current"), "fix/copy")

    def test_here_refuses_a_folder_that_is_not_the_root_of_a_copy(self):
        w = self.copy()
        os.makedirs(os.path.join(w, "sub"))
        self.raise_("maps-x", repo=os.path.join(w, "sub"), here=True)
        self.assertIn("is not the root", self.failed("maps-x"))
        self.raise_("maps-y", repo=os.path.join(self.tmp, "nope"), here=True)
        self.assertIn("there is no folder", self.failed("maps-y"))

    def test_here_refuses_a_copy_taken_by_a_live_session_under_another_path(self):
        # the same folder by another path (a link): compared by the folder on disk, not by the string
        w = self.copy()
        link = os.path.join(self.tmp, "link")
        platforms.current.link_dir(w, link)
        self.h.record("session.requested", "notes", {"cwd": link, "session_id": "s-n", **OWNER})
        self.raise_("maps-x", repo=w, here=True)
        self.assertIn("taken by notes", self.failed("maps-x"))

    def test_here_refuses_a_copy_where_a_session_works_in_a_subfolder(self):
        w = self.copy()
        os.makedirs(os.path.join(w, "app"))
        link = os.path.join(self.tmp, "link")                              # by the path text — not the same folder
        platforms.current.link_dir(w, link)
        self.h.record("session.requested", "notes", {"cwd": os.path.join(link, "app"), "session_id": "s-n", **OWNER})
        self.raise_("maps-x", repo=w, here=True)
        self.assertIn("taken by notes", self.failed("maps-x"))

    def test_here_refuses_the_raisers_own_folder_under_another_path(self):
        link = os.path.join(self.tmp, "link")
        platforms.current.link_dir(self.repo, link)
        self.raise_("maps-x", repo=link, here=True)
        self.assertIn("is your folder", self.failed("maps-x"))

    def test_cleaning_never_removes_a_here_copy(self):
        w = self.copy()
        self.assertEqual(self.raise_("maps-x", repo=w, here=True)["cwd"], w)
        rec = self.h.record
        rec("session.started", "maps-x", {})
        rec("session.stop_requested", "maps-x", OWNER)
        rec("session.remove_requested", "maps-x", OWNER)
        self.assertEqual(self.h.state["sessions"]["maps-x"]["state"], "removed")
        self.clean()
        self.assertTrue(os.path.isdir(w))
        self.assertIn("fix/copy", git(self.repo, "branch", "--list"))

    def test_an_executors_copy_handed_to_a_here_executor_is_not_removed_with_its_first_owner(self):
        # the copy of a finished executor was handed to a new one (--here): removing the first does not delete it while it is in use
        x = self.raise_("maps-x")
        rec = self.h.record
        rec("session.started", "maps-x", {})
        rec("session.stop_requested", "maps-x", OWNER)
        y = self.raise_("maps-y", repo=x["worktree"]["path"], here=True)
        self.assertEqual(y["cwd"], x["worktree"]["path"])
        rec("session.remove_requested", "maps-x", OWNER)
        self.clean()
        self.assertTrue(os.path.isdir(x["worktree"]["path"]))

    def test_why_a_copy_is_kept_reads_in_the_owners_language_in_a_fold_of_the_journal(self):
        # the hub is told why the removed session's copy stays; the reason is the core's phrase, its marks ride in
        # session.worktree_removed (why_l10n): the hub reads the English, the Russian owner the Russian
        from core import coretext, i18n, journal, reducer
        x = self.raise_("maps-x")
        path = x["worktree"]["path"]
        with open(os.path.join(path, "wip.txt"), "w") as f:
            f.write("wip\n")
        rec = self.h.record
        rec("session.started", "maps-x", {})
        rec("session.stop_requested", "maps-x", OWNER)
        rec("session.remove_requested", "maps-x", OWNER)
        self.clean()
        self.assertTrue(os.path.isdir(path))
        st = reducer.replay(journal.read(os.path.join(self.h.state_dir, "journal.jsonl")))
        (m,) = [m for m in st["messages"].values() if m["session"] == "hub" and "maps-x is kept" in m["text"]]
        self.assertIn("has uncommitted changes", m["text"])
        ru = coretext.shown(m, "ru")
        self.assertIn(i18n.tr("ru", "core.worktree.kept_dirty", path=path), ru)
        self.assertNotIn("has uncommitted changes", ru)


if __name__ == "__main__":
    unittest.main()
