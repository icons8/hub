"""An executor works in its own working copy of a branch (owner decision 2026-10-01: option 1, but it must verify
that the worktree works correctly). The core creates a git worktree on branch task/<name> and checks it; if that
fails there is no executor, and whoever started it gets the reason. When an executor is removed, its copy is cleaned
up without losing work."""
import tests  # noqa: F401 — ignore the machine's settings
import os
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import platforms
from core import executors, reducer
from hosts import worktree
from tests import winsafe
from tests.fakes import can

OWNER = {"channel": "cli", "owner": True}


def git(cwd, *a):
    return subprocess.run(["git", "-C", cwd, *a], check=True, capture_output=True, text=True).stdout


def listed(repo):
    """The copies of `git worktree list` as OS paths: on Windows git prints C:/..., the core's records hold C:\\..."""
    return {os.path.realpath(x[9:]) for x in git(repo, "worktree", "list", "--porcelain").splitlines()
            if x.startswith("worktree ")}


class Prepare(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wt-"))
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.repo, "apps"))
        git(self.tmp, "init", "-q", self.repo)
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")

    def test_worktree_is_created_and_checked(self):
        cwd, info, err = worktree.prepare(self.repo, "devops-fix")
        self.assertIsNone(err)
        path = os.path.join(self.repo, ".claude", "worktrees", "devops-fix")
        self.assertEqual((cwd, info["path"], info["branch"]), (path, path, "task/devops-fix"))
        self.assertEqual(git(path, "branch", "--show-current").strip(), "task/devops-fix")
        self.assertTrue(all(ok for _, ok in info["checks"]), info["checks"])

    def test_copies_are_hidden_from_git_status_of_the_project_once(self):
        # the project's own `git status` listed "?? .claude/worktrees/" for as long as an executor's copy existed;
        # the line goes to the shared info/exclude (not the project's .gitignore, which is committed), once
        exclude = os.path.join(self.repo, ".git", "info", "exclude")
        self.assertIsNone(worktree.prepare(self.repo, "maps-1")[2])
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertIsNone(worktree.prepare(self.repo, "maps-2")[2])
        with open(exclude, encoding="utf-8") as f:
            self.assertEqual(f.read().splitlines().count("/.claude/worktrees/"), 1)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        # the line lands after the owner's own lines, even when the file does not end with a newline
        os.unlink(exclude)
        with open(exclude, "w", encoding="utf-8", newline="\n") as f:
            f.write("*.log")
        self.assertIsNone(worktree.prepare(self.repo, "maps-3")[2])
        with open(exclude, encoding="utf-8") as f:
            self.assertEqual(f.read().splitlines(), ["*.log", "/.claude/worktrees/"])

    def test_a_big_checkout_gets_minutes_and_a_killed_one_leaves_no_half_copy(self):
        # 21.10: a 0.5 GB copy on a cloud-synced disk made `worktree add` run over 30 s and git was killed midway;
        # a locked "initializing" copy and a branch were left behind, and the executor name stayed taken
        real, seen = worktree._git, []

        def slow(cwd, *args, **kw):
            if args[:2] == ("worktree", "add"):
                seen.append(kw.get("timeout"))
                real(cwd, *args, **kw)
                real(cwd, "worktree", "lock", "--reason", "initializing", args[-1])
                raise subprocess.TimeoutExpired(["git", *args], kw.get("timeout"))
            return real(cwd, *args, **kw)
        with mock.patch.object(worktree, "_git", slow):
            cwd, info, err = worktree.prepare(self.repo, "smm-1")
        self.assertGreaterEqual(seen[0], 300)
        self.assertIsNone(cwd)
        self.assertIn("did not finish within", err)
        self.assertFalse(os.path.exists(info["path"]))
        self.assertNotIn("smm-1", git(self.repo, "worktree", "list"))
        self.assertNotIn("task/smm-1", git(self.repo, "branch", "--list"))
        cwd, _, err = worktree.prepare(self.repo, "smm-1")   # the same name again: from scratch
        self.assertIsNone(err)

    def test_a_copy_left_initializing_by_a_killed_git_is_made_again_and_its_branch_kept(self):
        # PR #236 review: the hub was restarted mid-checkout, git was killed and the copy stayed locked
        # "initializing"; the checks used to reject it, so that name could never be started again
        path = os.path.join(self.repo, ".claude", "worktrees", "smm-1")
        git(self.repo, "worktree", "add", "-q", "-b", "task/smm-1", path)
        git(self.repo, "worktree", "lock", "--reason", "initializing", path)
        with open(os.path.join(path, "half"), "w") as f:            # half checked out
            f.write("x")
        cwd, info, err = worktree.prepare(self.repo, "smm-1")
        self.assertIsNone(err)
        self.assertEqual(cwd, path)
        self.assertTrue(all(ok for _, ok in info["checks"]), info["checks"])
        self.assertNotIn("locked", git(self.repo, "worktree", "list", "--porcelain"))

    def test_a_record_left_initializing_without_its_folder_is_cleared_too(self):
        # git was killed when the entry exists but the folder does not yet: add would refuse with "missing but locked
        # worktree"
        path = os.path.join(self.repo, ".claude", "worktrees", "smm-3")
        git(self.repo, "worktree", "add", "-q", "-b", "task/smm-3", path)
        git(self.repo, "worktree", "lock", "--reason", "initializing", path)
        platforms.current.rmtree(path)
        cwd, info, err = worktree.prepare(self.repo, "smm-3")
        self.assertIsNone(err)
        self.assertTrue(all(ok for _, ok in info["checks"]), info["checks"])

    def test_a_failed_add_does_not_delete_a_branch_with_someone_elses_commits(self):
        # PR #236 review: branch task/<name> was created between the check and `worktree add -b` (and has commits):
        # add refused with "already exists", and cleaning up the half copy does not delete that branch
        real = worktree._git
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "two")

        def racing(cwd, *args, **kw):
            if args[:2] == ("worktree", "add"):
                real(cwd, "branch", "task/smm-2", "HEAD~1")
                return 128, "", "fatal: a branch named 'task/smm-2' already exists"
            return real(cwd, *args, **kw)
        with mock.patch.object(worktree, "_git", racing):
            cwd, _, err = worktree.prepare(self.repo, "smm-2")
        self.assertIsNone(cwd)
        self.assertIn("already exists", err)
        self.assertIn("task/smm-2", git(self.repo, "branch", "--list"))

    def test_agent_in_a_subfolder_keeps_its_subfolder(self):
        apps = os.path.join(self.repo, "apps")
        with open(os.path.join(apps, "main.py"), "w") as f:
            f.write("x = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "apps")
        cwd, info, err = worktree.prepare(apps, "a-2")
        self.assertEqual(cwd, os.path.join(self.repo, ".claude", "worktrees", "a-2", "apps"))

    def test_agent_in_an_ignored_subfolder_gets_the_copy_root_and_the_source_path(self):
        ignored = os.path.join(self.repo, "output")
        os.makedirs(ignored)
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write("output/\n")
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "ignore")
        cwd, info, err = worktree.prepare(ignored, "a-3")
        path = os.path.join(self.repo, ".claude", "worktrees", "a-3")
        self.assertIsNone(err)
        self.assertEqual(cwd, path)   # the folder is not in git, so not in the copy: the root of the copy, not a failure
        self.assertEqual(info["source_cwd"], ignored)
        self.assertTrue(os.path.isdir(path))

    def test_an_ignored_folder_worktreeinclude_brings_into_the_copy_is_the_executors_folder_as_before(self):
        # the repo's .worktreeinclude chose what of out/ a copy gets: the executor works there, as before the change
        out = os.path.join(self.repo, "out")
        os.makedirs(out)
        for rel, text in ((".gitignore", "out/\n"), (".worktreeinclude", "out/**\n"), ("out/.env", "K=1\n"),
                          ("out/report.md", "week 1\n")):
            with open(os.path.join(self.repo, rel), "w") as f:
                f.write(text)
        git(self.repo, "add", ".gitignore", ".worktreeinclude")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "ignore")
        cwd, info, err = worktree.prepare(out, "a-3i")
        path = os.path.join(self.repo, ".claude", "worktrees", "a-3i")
        self.assertIsNone(err)
        self.assertEqual(cwd, os.path.join(path, "out"))
        self.assertNotIn("source_cwd", info)
        self.assertTrue(os.path.isfile(os.path.join(path, "out", "report.md")))

    def test_a_folder_untracked_here_but_on_the_copys_branch_is_the_executors_folder(self):
        # --branch: the branch has apps/ committed, the main copy has it only as a new folder: the copy's apps/ is git's
        main = git(self.repo, "branch", "--show-current").strip()
        git(self.repo, "switch", "-q", "-c", "feat/apps")
        with open(os.path.join(self.repo, "apps", "main.py"), "w") as f:
            f.write("x = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "apps")
        git(self.repo, "switch", "-q", main)
        os.makedirs(os.path.join(self.repo, "apps"), exist_ok=True)
        cwd, info, err = worktree.prepare(os.path.join(self.repo, "apps"), "a-3b", branch="feat/apps")
        self.assertIsNone(err)
        self.assertEqual(cwd, os.path.join(self.repo, ".claude", "worktrees", "a-3b", "apps"))
        self.assertNotIn("source_cwd", info)

    def test_a_folder_in_git_the_copys_start_lacks_is_refused_not_taken_for_one_outside_git(self):
        # the raiser's branch has apps/ committed, the copy starts from the main branch without it: "outside git, do not
        # commit" would be wrong there — the folder's files are the repo's
        git(self.repo, "switch", "-q", "-c", "feature/apps")
        with open(os.path.join(self.repo, "apps", "main.py"), "w") as f:
            f.write("x = 1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "apps")
        cwd, info, err = worktree.prepare(os.path.join(self.repo, "apps"), "a-3t", from_main=True)
        self.assertIsNone(cwd)
        self.assertNotIn("source_cwd", info or {})
        self.assertIn("has no folder apps", err)
        self.assertNotIn("a-3t", git(self.repo, "worktree", "list"))   # the half copy is dropped, as before

    def test_removed_session_takes_its_worktree_but_never_loses_work(self):
        # 01.10: missing feature at the time: worktree cleanup on removal, done by hand until then
        _, info, _ = worktree.prepare(self.repo, "a-2")
        self.assertEqual(worktree.remove(info), (True, "the working copy and the branch were removed"))
        self.assertFalse(os.path.exists(info["path"]))
        self.assertNotIn("task/a-2", git(self.repo, "branch"))
        # edits are not committed: leave the copy alone
        _, info, _ = worktree.prepare(self.repo, "a-3")
        with open(os.path.join(info["path"], "draft.txt"), "w") as f:
            f.write("uncommitted")
        ok, why = worktree.remove(info)
        self.assertFalse(ok)
        self.assertIn("uncommitted", why)
        self.assertTrue(os.path.exists(os.path.join(info["path"], "draft.txt")))
        # commits on the branch not merged anywhere: remove the copy, keep the branch
        _, info, _ = worktree.prepare(self.repo, "a-4")
        git(info["path"], "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "work")
        ok, why = worktree.remove(info)
        self.assertTrue(ok)
        self.assertIn("the branch task/a-4 kept", why)
        self.assertIn("task/a-4", git(self.repo, "branch"))

    def test_only_compiled_bytecode_untracked_does_not_hold_the_copy(self):
        # 05.11: a copy with an untracked tools/__pycache__/ (a test run, no .gitignore entry) was never removed
        def untracked(name, files):
            _, info, _ = worktree.prepare(self.repo, name)
            for rel in files:
                full = os.path.join(info["path"], rel)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w") as f:
                    f.write("x")
            return info

        info = untracked("py-1", ["tools/__pycache__/x.cpython-312.pyc", "__pycache__/y.pyc"])
        self.assertEqual(worktree.remove(info), (True, "the working copy and the branch were removed"))
        self.assertFalse(os.path.exists(info["path"]))
        for keep in (["tools/__pycache__/x.py"], ["notes.txt"], ["tools/__pycache__/x.pyc", "notes.txt"],
                     ["tools/__pycache__/sub/x.pyc"], ["tools/x.pyc"]):
            info = untracked("py-2", keep)
            ok, why = worktree.remove(info)
            self.assertFalse(ok, keep)
            self.assertIn("uncommitted", why)
            for rel in keep:
                self.assertTrue(os.path.exists(os.path.join(info["path"], rel)), keep)
            platforms.current.rmtree(info["path"])
            git(self.repo, "worktree", "prune")
            git(self.repo, "branch", "-D", "task/py-2")

    def test_a_link_among_the_bytecode_keeps_the_copy(self):
        # review of PR #458: os.walk does not enter a linked folder, so a link next to the .pyc files was counted as nothing
        _, info, _ = worktree.prepare(self.repo, "py-3")
        cache = os.path.join(info["path"], "tools", "__pycache__")
        os.makedirs(cache)
        with open(os.path.join(cache, "x.cpython-312.pyc"), "w") as f:
            f.write("x")
        if not winsafe.try_symlink(self.repo, os.path.join(info["path"], "tools", "outside"), target_is_directory=True):
            self.skipTest(winsafe.NO_LINKS)
        ok, why = worktree.remove(info)
        self.assertFalse(ok)
        self.assertIn("uncommitted", why)
        self.assertTrue(os.path.exists(os.path.join(cache, "x.cpython-312.pyc")))   # nothing was deleted
        platforms.current.rmtree(info["path"])
        git(self.repo, "worktree", "prune")
        git(self.repo, "branch", "-D", "task/py-3")

    def removed_copy_with_link(self, junction):
        # 2026-11-09: node_modules of a copy was a junction to the main checkout's; `git worktree remove` (2.33, Windows)
        # went through it and emptied the target, the owner's folder outside the copy
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write("ln" + chr(10))
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "ignore")
        _, info, _ = worktree.prepare(self.repo, "a-30")
        target = self.outside_folder()
        link = self.link_into(info["path"], target, junction)
        return info, target, link

    def assert_target_whole(self, target):
        self.assertTrue(os.path.isfile(os.path.join(target, "keep.txt")))
        self.assertTrue(os.path.isdir(os.path.join(target, "empty")))

    def test_a_junction_in_a_copy_is_cut_and_its_target_is_not_emptied(self):
        info, target, _ = self.removed_copy_with_link(junction=True)
        self.assertEqual(worktree.remove(info), (True, "the working copy and the branch were removed"))
        self.assertFalse(os.path.exists(info["path"]))
        self.assert_target_whole(target)

    def test_a_symlink_in_a_copy_is_left_alone_and_its_target_is_not_emptied(self):
        # git and rmtree do not walk through a symlink: nothing to cut, the copy goes, the target stays
        info, target, _ = self.removed_copy_with_link(junction=False)
        self.assertEqual(worktree.remove(info), (True, "the working copy and the branch were removed"))
        self.assertFalse(os.path.exists(info["path"]))
        self.assert_target_whole(target)

    def test_a_symlink_that_git_tracks_does_not_hold_the_copy(self):
        # review of the fix: cutting every link also cut a tracked symlink (mode 120000), and then `git worktree remove`
        # refused (" D ln") and the copy stayed for good
        if not can.symlink():
            self.skipTest(winsafe.NO_LINKS)
        git(self.repo, "config", "core.symlinks", "true")
        blob = subprocess.run(["git", "-C", self.repo, "hash-object", "-w", "--stdin"], input="f.txt",
                              check=True, capture_output=True, text=True).stdout.strip()
        git(self.repo, "update-index", "--add", "--cacheinfo", f"120000,{blob},ln")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "a tracked symlink")
        _, info, _ = worktree.prepare(self.repo, "a-31")
        if not os.path.islink(os.path.join(info["path"], "ln")):
            self.skipTest("git here does not check a symlink out as a link")
        self.assertEqual(worktree.remove(info), (True, "the working copy and the branch were removed"))
        self.assertFalse(os.path.exists(info["path"]))

    def test_a_junction_that_cannot_be_cut_stops_the_removal_before_git(self):
        info, target, link = self.removed_copy_with_link(junction=True)
        with mock.patch.object(platforms.current, "cut_junctions", side_effect=OSError("denied")):
            ok, why = worktree.remove(info)
        self.assertFalse(ok)
        self.assertIn("junction", why)
        self.assertTrue(os.path.lexists(link))
        self.assert_target_whole(target)

    def test_a_mount_point_in_a_copy_is_not_cut(self):
        # a volume mounted into a folder is a junction to the API too: it is not ours to unmount
        info, target, link = self.removed_copy_with_link(junction=True)
        # like ntpath.ismount on Windows: a path with the \\?\ prefix is never a mount point except a drive root
        with mock.patch("os.path.ismount", side_effect=lambda p: not p.startswith("\\\\?\\") and os.path.basename(p) == "ln"):
            ok, why = worktree.remove(info)
        self.assertFalse(ok)
        self.assertIn("mount point", why)
        self.assertTrue(os.path.lexists(link))
        self.assert_target_whole(target)

    def test_a_half_made_copy_with_a_junction_is_dropped_without_emptying_its_target(self):
        info, target, _ = self.removed_copy_with_link(junction=True)
        self.assertTrue(worktree._drop_half(self.repo, info["path"]))
        self.assertFalse(os.path.exists(info["path"]))
        self.assert_target_whole(target)

    def test_a_worktree_with_paths_over_260_chars_is_removed_too(self):
        deep = os.path.join(self.repo, *["node_modules_package_folder"] * 10)
        os.makedirs(platforms.current.long_path(deep))
        with open(platforms.current.long_path(os.path.join(deep, "index.js")), "w") as f:
            f.write("x\n")
        git(self.repo, "-c", "core.longpaths=true", "add", "-A")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "deep")
        _, info, err = worktree.prepare(self.repo, "a-5")
        self.assertIsNone(err)
        self.assertGreater(len(os.path.join(info["path"], os.path.relpath(deep, self.repo), "index.js")), 260)
        self.assertEqual(worktree.remove(info), (True, "the working copy and the branch were removed"))
        self.assertFalse(os.path.exists(info["path"]))
        self.assertNotIn("a-5", git(self.repo, "worktree", "list"))

    def test_a_copy_git_says_it_removed_but_left_is_not_reported_removed(self):
        # git answers 0 and leaves the folder; if the core can't finish it either, the report says so
        _, info, _ = worktree.prepare(self.repo, "a-32")
        real = worktree._git

        def leaves_a_file(cwd, *a, **kw):
            if a[:2] == ("worktree", "remove") and "-f" not in a:   # -f -f: drop the entry of an already removed copy
                code = real(cwd, "worktree", "remove", "-f", "-f", info["path"], **kw)[0]
                os.makedirs(info["path"])
                with open(os.path.join(info["path"], "locked.bin"), "w") as f:
                    f.write("x")
                return code, "", ""
            return real(cwd, *a, **kw)
        with mock.patch.object(worktree, "_git", leaves_a_file), \
                mock.patch.object(platforms.current, "rmtree", return_value=True):
            ok, why = worktree.remove(info)
        self.assertFalse(ok)
        self.assertIn(f"did not remove {info['path']} completely", why)
        self.assertTrue(os.path.exists(os.path.join(info["path"], "locked.bin")))

    def test_a_copy_git_says_it_removed_but_left_as_an_empty_shell_is_finished(self):
        # git answers 0 and leaves folders without files: the core erases the shell, the copy is reported removed
        _, info, _ = worktree.prepare(self.repo, "a-33")
        real = worktree._git

        def leaves_a_shell(cwd, *a, **kw):
            if a[:2] == ("worktree", "remove") and "-f" not in a:
                code = real(cwd, "worktree", "remove", "-f", "-f", info["path"], **kw)[0]
                os.makedirs(os.path.join(info["path"], "sub"))
                return code, "", ""
            return real(cwd, *a, **kw)
        with mock.patch.object(worktree, "_git", leaves_a_shell):
            ok, why = worktree.remove(info)
        self.assertTrue(ok, why)
        self.assertFalse(os.path.lexists(info["path"]))
        self.assertNotIn("a-33", git(self.repo, "worktree", "list"))

    def test_only_the_remove_call_runs_git_in_english(self):
        # LC_ALL=C only where the core parses a git refusal by its text: git hooks (worktree add) would inherit it too
        envs = []
        real = platforms.current.run
        env = {k: v for k, v in os.environ.items() if k != "LC_ALL"}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(platforms.current, "run",
                               side_effect=lambda a, t, **k: envs.append((a, k["env"].get("LC_ALL"))) or real(a, t, **k)):
            _, info, _ = worktree.prepare(self.repo, "a-8")
            worktree.remove(info)
        self.assertEqual({a[a.index("-C") + 2:a.index("-C") + 4] == ["worktree", "remove"] for a, lc in envs
                          if lc == "C"}, {True})
        self.assertTrue(any(a[a.index("-C") + 2:a.index("-C") + 4] == ["worktree", "add"] and lc is None
                            for a, lc in envs))

    def test_a_locked_copy_is_left_to_git(self):
        _, info, _ = worktree.prepare(self.repo, "a-7")
        git(self.repo, "worktree", "lock", info["path"])
        ok, why = worktree.remove(info)
        self.assertFalse(ok)
        self.assertIn("locked", why)
        self.assertTrue(os.path.isdir(info["path"]))

    def test_when_git_cannot_remove_the_copy_the_core_does(self):
        _, info, _ = worktree.prepare(self.repo, "a-6")
        real = worktree._git

        def fail_remove(cwd, *a, **kw):
            if a[:2] == ("worktree", "remove") and "-f" not in a:   # -f -f: drop the entry of an already removed copy
                return 1, "", "error: cannot remove: Filename too long"
            return real(cwd, *a, **kw)
        with mock.patch.object(worktree, "_git", fail_remove):
            self.assertEqual(worktree.remove(info), (True, "the working copy and the branch were removed"))
        self.assertFalse(os.path.exists(info["path"]))
        self.assertNotIn("a-6", git(self.repo, "worktree", "list"))

    def test_a_big_copy_that_git_cannot_remove_in_time_is_cleaned_by_the_core(self):
        # git worktree remove of a big copy (node_modules, Windows) ran out of time: the process was killed, the
        # copy is half-deleted and has no .git (prunable), so the next cleanup would not recognise it; the core
        # finishes the job itself
        _, info, _ = worktree.prepare(self.repo, "a-9")
        real = worktree._git
        seen = {}

        def dies_midway(cwd, *a, **kw):
            if a[:2] == ("worktree", "remove") and "-f" not in a:   # -f -f: drop the entry of an already removed copy
                seen["timeout"] = kw.get("timeout", 30)
                os.remove(os.path.join(info["path"], ".git"))
                raise subprocess.TimeoutExpired(["git", *a], seen["timeout"])
            return real(cwd, *a, **kw)
        with mock.patch.object(worktree, "_git", dies_midway):
            self.assertEqual(worktree.remove(info), (True, "the working copy and the branch were removed"))
        self.assertGreaterEqual(seen["timeout"], 300)
        self.assertFalse(os.path.exists(info["path"]))
        self.assertNotIn("a-9", git(self.repo, "worktree", "list"))
        self.assertNotIn("task/a-9", git(self.repo, "branch"))

    def test_a_failed_remove_that_already_took_the_git_link_is_cleaned_too(self):
        _, info, _ = worktree.prepare(self.repo, "a-10")
        real = worktree._git

        def fails_midway(cwd, *a, **kw):
            if a[:2] == ("worktree", "remove") and "-f" not in a:   # -f -f: drop the entry of an already removed copy
                os.remove(os.path.join(info["path"], ".git"))
                return 1, "", "error: failed to delete: Directory not empty"
            return real(cwd, *a, **kw)
        with mock.patch.object(worktree, "_git", fails_midway):
            self.assertEqual(worktree.remove(info), (True, "the working copy and the branch were removed"))
        self.assertFalse(os.path.exists(info["path"]))
        self.assertNotIn("a-10", git(self.repo, "worktree", "list"))

    def test_a_timeout_before_git_touched_anything_still_ends_with_the_copy_removed(self):
        # git was killed before doing anything; the copy was checked clean above, so the core removes it rather than
        # leaving it hanging
        _, info, _ = worktree.prepare(self.repo, "a-11")
        real = worktree._git

        def times_out(cwd, *a, **kw):
            if a[:2] == ("worktree", "remove") and "-f" not in a:   # -f -f: drop the entry of an already removed copy
                raise subprocess.TimeoutExpired(["git", *a], 600)
            return real(cwd, *a, **kw)
        with mock.patch.object(worktree, "_git", times_out):
            ok, why = worktree.remove(info)
        self.assertTrue(ok, why)
        self.assertFalse(os.path.exists(info["path"]))

    def test_a_copy_without_its_git_link_is_left_with_the_reason(self):
        # .git is gone (the cleanup was killed mid worktree remove, or the session removed it): the folder does not
        # show whose work it is, so do not erase it; tell the hub where it is
        self.commit_file()
        _, info, _ = worktree.prepare(self.repo, "a-12")
        os.remove(os.path.join(info["path"], ".git"))
        ok, why = worktree.remove(info)
        self.assertFalse(ok)
        self.assertIn(f"{info['path']} has no .git", why)
        self.assertTrue(os.path.isdir(info["path"]))
        self.assertIn(os.path.realpath(info["path"]), listed(self.repo))
        self.assertIn("task/a-12", git(self.repo, "branch"))

    def test_an_empty_leftover_of_a_removed_copy_is_erased_without_a_report(self):
        # the folder stayed with no .git and no file in it: completely empty, or with empty subfolders only (git took
        # the files); every one of them cost the hub a "copy left" message and a manual delete. Nothing to lose: erase
        # it and its record, the branch goes by the usual rule
        for name, subdirs in (("a-17", ()), ("a-18", ("node_modules/x/y", "dist"))):
            _, info, _ = worktree.prepare(self.repo, name)
            os.remove(os.path.join(info["path"], ".git"))
            for d in subdirs:
                os.makedirs(os.path.join(info["path"], *d.split("/")))
            self.assertEqual(worktree.remove(info), (True, "the working copy and the branch were removed"), name)
            self.assertFalse(os.path.exists(info["path"]), name)
            self.assertNotIn(os.path.realpath(info["path"]), listed(self.repo), name)
            self.assertNotIn(f"task/{name}", git(self.repo, "branch"), name)

    def test_a_leftover_with_a_single_file_is_still_kept(self):
        self.commit_file()
        _, info, _ = worktree.prepare(self.repo, "a-19")
        os.remove(os.path.join(info["path"], ".git"))
        os.remove(os.path.join(info["path"], "a.txt"))
        os.makedirs(os.path.join(info["path"], "sub", "deeper"))
        with open(os.path.join(info["path"], "sub", "deeper", "note.txt"), "w") as f:
            f.write("x")
        ok, why = worktree.remove(info)
        self.assertFalse(ok)
        self.assertIn("has no .git", why)
        self.assertTrue(os.path.exists(os.path.join(info["path"], "sub", "deeper", "note.txt")))

    def locked_root(self, root):
        """The folder `root` refuses to go (WinError 5: a program stands in it) while everything else is erased as usual."""
        real = platforms.current.rmdir

        def rmdir(path):
            if os.path.realpath(path) == os.path.realpath(root):
                raise PermissionError(13, "Permission denied", path)
            return real(path)
        return mock.patch.object(platforms.current, "rmdir", rmdir)

    def test_an_empty_shell_whose_root_cannot_go_yet_is_pending_not_kept_for_good(self):
        # the owner's colleague, Windows: a program with its cwd in the root of a deleted executor's copy; git took the files
        # and the root answers "Permission denied". The shell holds nothing: not "check the work yourselves", but "later"
        info = self.leftover("a-21")
        with self.locked_root(info["path"]):
            ok, why = worktree.remove(info)
        self.assertIsNone(ok, why)
        self.assertIn("Permission denied", why)
        self.assertNotIn("has no .git", why)
        self.assertTrue(os.path.isdir(info["path"]))
        self.assertEqual(worktree.remove(info), (True, "the working copy and the branch were removed"))   # the program is gone
        self.assertFalse(os.path.exists(info["path"]))
        self.assertNotIn(os.path.realpath(info["path"]), listed(self.repo))
        self.assertNotIn("task/a-21", git(self.repo, "branch"))

    def test_a_shell_with_a_file_is_not_pending_even_when_the_root_is_locked(self):
        info = self.leftover("a-22")
        with open(os.path.join(info["path"], "note.txt"), "w") as f:
            f.write("x")
        with self.locked_root(info["path"]):
            ok, why = worktree.remove(info)
        self.assertIs(ok, False)
        self.assertIn("has no .git", why)

    def test_git_that_took_the_files_but_not_the_root_is_pending_with_the_whole_reason(self):
        # the same on the first pass: `git worktree remove` erased the files, then died on the root. The reason was cut to 200
        # characters (the path twice), and nobody ever tried again
        _, info, _ = worktree.prepare(self.repo, "a-23")
        real, path = worktree._git, info["path"]
        said = f"fatal: failed to delete '{path}': Permission denied " + "x" * 300

        def dies_on_the_root(cwd, *a, **kw):
            if a[:2] == ("worktree", "remove") and "-f" not in a:
                for name in os.listdir(path):
                    full = os.path.join(path, name)
                    platforms.current.rmtree(full) if os.path.isdir(full) else os.remove(full)
                return 128, "", said
            return real(cwd, *a, **kw)
        with mock.patch.object(worktree, "_git", dies_on_the_root), self.locked_root(path), \
                mock.patch.object(platforms.current, "rmtree", lambda p: True):
            ok, why = worktree.remove(info)
        self.assertIsNone(ok, why)
        self.assertIn(said, why)
        self.assertIn("Permission denied", why.replace(said, ""))   # and what the folder itself answered
        self.assertEqual(worktree.remove(info), (True, "the working copy and the branch were removed"))
        self.assertFalse(os.path.exists(path))
        self.assertNotIn(os.path.realpath(path), listed(self.repo))

    def link_into(self, folder, target, junction=False):
        """A link (or a Windows junction) `folder`/ln to `target`; skips the test where the OS or the user can't make one."""
        link = os.path.join(folder, "ln")
        if junction:
            if os.name != "nt":
                self.skipTest("junctions are Windows only")
            made = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True).returncode == 0
            if not made:
                self.skipTest("can't make a junction here")
            return link
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):   # Windows without the right to symlinks (WinError 1314)
            self.skipTest("can't make a symlink here")
        return link

    def leftover(self, name):
        _, info, _ = worktree.prepare(self.repo, name)
        os.remove(os.path.join(info["path"], ".git"))
        return info

    def outside_folder(self):
        """A folder outside the copy with an empty subfolder and a file: what a link in a leftover points at."""
        target = os.path.join(self.tmp, "elsewhere")
        os.makedirs(os.path.join(target, "empty"))
        with open(os.path.join(target, "keep.txt"), "w") as f:
            f.write("x")
        return target

    def link_in_leftover(self, junction):
        info = self.leftover("a-20")
        target = self.outside_folder()
        os.makedirs(os.path.join(info["path"], "dist"))
        link = self.link_into(os.path.join(info["path"], "dist"), target, junction)
        ok, why = worktree.remove(info)
        self.assertFalse(ok)
        self.assertIn("has no .git", why)
        self.assertTrue(os.path.lexists(link))
        self.assertTrue(os.path.exists(os.path.join(target, "keep.txt")))
        self.assertTrue(os.path.isdir(os.path.join(target, "empty")))   # a walk that follows the link would erase it

    def test_a_symlink_in_an_empty_leftover_is_content_and_its_target_is_not_touched(self):
        self.link_in_leftover(junction=False)

    def test_a_junction_in_an_empty_leftover_is_content_and_its_target_is_not_touched(self):
        self.link_in_leftover(junction=True)

    def leftover_that_is_a_link(self, junction):
        info = self.leftover("a-22")
        target = self.outside_folder()
        platforms.current.rmtree(info["path"])
        holder = os.path.join(self.tmp, "holder")
        os.makedirs(holder)
        link = self.link_into(holder, target, junction)
        ok, why = worktree.remove(dict(info, path=link))
        self.assertFalse(ok)
        self.assertTrue(os.path.lexists(link))
        self.assertTrue(os.path.isdir(os.path.join(target, "empty")))

    def test_a_leftover_that_is_a_symlink_is_not_touched(self):
        self.leftover_that_is_a_link(junction=False)

    def test_a_leftover_that_is_a_junction_is_not_touched(self):
        self.leftover_that_is_a_link(junction=True)

    def test_a_mount_point_in_an_empty_leftover_is_content(self):
        info = self.leftover("a-24")
        mount = os.path.join(info["path"], "mnt")
        os.makedirs(mount)
        # like ntpath.ismount on Windows: a path with the \\?\ prefix is never a mount point except a drive root
        with mock.patch("os.path.ismount", side_effect=lambda p: not p.startswith("\\\\?\\") and os.path.basename(p) == "mnt"):
            ok, why = worktree.remove(info)
        self.assertFalse(ok)
        self.assertTrue(os.path.isdir(mount))

    def test_empty_folders_next_to_a_file_may_go_but_the_file_stays(self):
        # the walk erases what it can before it meets the file: the empty `dist` goes, `src` with the file stays whole
        info = self.leftover("a-26")
        os.makedirs(os.path.join(info["path"], "dist"))
        os.makedirs(os.path.join(info["path"], "src", "empty"))
        keep = os.path.join(info["path"], "src", "keep.txt")
        with open(keep, "w") as f:
            f.write("x")
        ok, why = worktree.remove(info)
        self.assertFalse(ok)
        self.assertIn("has no .git", why)
        self.assertTrue(os.path.exists(keep))

    def test_an_empty_read_only_folder_is_erased(self):
        # Windows: rmdir refuses (WinError 5) on a folder with the read-only attribute; the old path cleared it and retried
        if os.name != "nt":
            self.skipTest("the read-only attribute blocks rmdir on Windows only")
        info = self.leftover("a-27")
        ro = os.path.join(info["path"], "dist", "ro")
        os.makedirs(ro)
        os.chmod(ro, stat.S_IREAD)
        self.assertEqual(worktree.remove(info), (True, "the working copy and the branch were removed"))
        self.assertFalse(os.path.exists(info["path"]))

    def test_a_file_that_appears_while_erasing_is_not_lost(self):
        # no look-through first and rmtree after: every folder is removed with rmdir, which refuses by itself on a
        # folder that got a file in the meantime
        info = self.leftover("a-25")
        os.makedirs(os.path.join(info["path"], "sub"))
        late = os.path.join(info["path"], "late.txt")
        real = os.rmdir

        def rmdir(p, *a, **kw):
            if not os.path.exists(late):
                with open(late, "w") as f:
                    f.write("x")
            return real(p, *a, **kw)
        with mock.patch("os.rmdir", rmdir):
            ok, why = worktree.remove(info)
        self.assertFalse(ok)
        self.assertTrue(os.path.exists(late))

    def commit_file(self):
        with open(os.path.join(self.repo, "a.txt"), "w") as f:
            f.write("a\n")
        git(self.repo, "add", "a.txt")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "a")

    def test_a_copy_without_its_git_link_keeps_deletions_and_ignored_files(self):
        # only deleted files: that is how both a half-removed copy and the work of a session that removed .git look;
        # do not guess
        self.commit_file()
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write(".env\n")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "ignore")
        _, info, _ = worktree.prepare(self.repo, "a-13")
        os.remove(os.path.join(info["path"], "a.txt"))
        with open(os.path.join(info["path"], ".env"), "w") as f:
            f.write("KEY=x\n")
        os.remove(os.path.join(info["path"], ".git"))
        self.assertFalse(worktree.remove(info)[0])
        self.assertTrue(os.path.exists(os.path.join(info["path"], ".env")))

    def test_a_copy_whose_git_link_was_taken_otherwise_keeps_its_edits(self):
        self.commit_file()
        _, info, _ = worktree.prepare(self.repo, "a-14")
        with open(os.path.join(info["path"], "a.txt"), "w") as f:
            f.write("edit\n")
        os.remove(os.path.join(info["path"], ".git"))
        ok, why = worktree.remove(info)
        self.assertFalse(ok)
        self.assertIn("has no .git", why)
        with open(os.path.join(info["path"], "a.txt")) as f:
            self.assertEqual(f.read(), "edit\n")

    def test_finishing_one_copy_does_not_drop_the_record_of_another_leftover(self):
        # finishing one copy drops only its own entry: `worktree prune` would drop the entry of a neighbouring
        # abandoned one too
        _, first, _ = worktree.prepare(self.repo, "a-15")
        _, second, _ = worktree.prepare(self.repo, "a-16")
        os.remove(os.path.join(second["path"], ".git"))
        real = worktree._git

        def dies_midway(cwd, *a, **kw):
            if a[:2] == ("worktree", "remove") and "-f" not in a:   # -f -f: drop the entry of an already removed copy
                os.remove(os.path.join(first["path"], ".git"))
                raise subprocess.TimeoutExpired(["git", *a], 600)
            return real(cwd, *a, **kw)
        with mock.patch.object(worktree, "_git", dies_midway):
            self.assertEqual(worktree.remove(first), (True, "the working copy and the branch were removed"))
        copies = listed(self.repo)
        self.assertNotIn(os.path.realpath(first["path"]), copies)
        self.assertIn(os.path.realpath(second["path"]), copies)
        self.assertTrue(os.path.isdir(second["path"]))

    def test_a_copy_whose_folder_was_deleted_by_hand_loses_its_record_and_its_branch_too(self):
        # the folder is gone, git still lists the copy and holds its branch: the same cleanup as for a copy that is there
        _, info, _ = worktree.prepare(self.repo, "gone-1")
        _, keep, _ = worktree.prepare(self.repo, "gone-2")
        platforms.current.rmtree(info["path"])
        self.assertEqual(worktree.remove(info), (True, "the working copy and the branch were removed"))
        copies = listed(self.repo)   # on Windows git prints C:/..., the records hold C:\...
        self.assertNotIn(os.path.realpath(info["path"]), copies)
        self.assertNotIn("task/gone-1", git(self.repo, "branch"))
        self.assertIn(os.path.realpath(keep["path"]), copies)   # only this record: the other copy stays
        # commits merged nowhere: the record goes, the branch stays
        _, info, _ = worktree.prepare(self.repo, "gone-3")
        self.commit(info["path"], "work")
        platforms.current.rmtree(info["path"])
        ok, why = worktree.remove(info)
        self.assertTrue(ok)
        self.assertIn("the branch task/gone-3 kept", why)
        self.assertNotIn(os.path.realpath(info["path"]), listed(self.repo))
        self.assertIn("task/gone-3", git(self.repo, "branch"))
        # a repeated call: the record is gone already, nothing to clean and no false "kept"
        self.assertEqual(worktree.remove(info), (True, "the working copy is already gone"))
        # nothing to clean: no repo or no path in the details
        self.assertEqual(worktree.remove({"path": info["path"], "repo": None, "branch": "task/x"})[0], True)

    def test_a_record_git_cannot_drop_is_not_reported_as_already_gone(self):
        _, info, _ = worktree.prepare(self.repo, "gone-4")
        platforms.current.rmtree(info["path"])
        real = worktree._git

        def refuse(cwd, *args, **kw):
            if args[:2] == ("worktree", "remove"):
                return 128, "", "fatal: Unable to create index.lock"
            return real(cwd, *args, **kw)
        with mock.patch.object(worktree, "_git", refuse):
            ok, why = worktree.remove(info)
        self.assertFalse(ok)
        self.assertIn("index.lock", why)
        self.assertIn(os.path.realpath(info["path"]), listed(self.repo))   # the next run tries again
        self.assertEqual(worktree.remove(info), (True, "the working copy and the branch were removed"))

    def test_a_missing_folder_that_a_live_session_of_another_hub_holds_is_left_alone(self):
        _, info, _ = worktree.prepare(self.repo, "gone-5")
        platforms.current.rmtree(info["path"])
        ok, why = worktree.remove(info, owner={"state": self.tmp}, foreign=lambda path: "hub-b")
        self.assertFalse(ok)
        self.assertIn("hub-b", why)
        self.assertIn(os.path.realpath(info["path"]), listed(self.repo))

    def test_a_folder_that_is_not_a_listed_worktree_is_never_taken(self):
        # no .git, and git does not know it: a foreign folder in .claude/worktrees, leave it alone
        stray = os.path.join(self.repo, ".claude", "worktrees", "stray")
        os.makedirs(stray)
        with open(os.path.join(stray, "keep.txt"), "w") as f:
            f.write("x")
        ok, _ = worktree.remove({"path": stray, "repo": self.repo, "branch": "task/stray"})
        self.assertFalse(ok)
        self.assertTrue(os.path.exists(os.path.join(stray, "keep.txt")))

    def commit(self, cwd, msg):
        git(cwd, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", msg)

    def on_other_branch(self):
        """The main copy is on its own branch feature/other (+1 commit) -> the name of the main branch."""
        main = git(self.repo, "branch", "--show-current").strip()
        git(self.repo, "switch", "-q", "-c", "feature/other")
        self.commit(self.repo, "other")
        return main

    def with_origin(self, main):
        """origin is a bare clone of the repo; a second clone pushes one more commit to its main branch -> its sha."""
        origin, other = os.path.join(self.tmp, "origin.git"), os.path.join(self.tmp, "other")
        git(self.tmp, "clone", "-q", "--bare", self.repo, origin)
        git(origin, "symbolic-ref", "HEAD", f"refs/heads/{main}")   # like a real origin: HEAD is the main branch
        git(self.repo, "remote", "add", "origin", origin)
        git(self.tmp, "clone", "-q", "-b", main, origin, other)
        self.commit(other, "pushed")
        git(other, "push", "-q", "origin", main)
        git(self.repo, "fetch", "-q", "origin")
        return git(other, "rev-parse", "HEAD")

    def test_by_default_the_executor_branches_off_the_head_of_the_main_copy(self):
        # the session is deliberately on its own branch (an epic, a long migration): executors branch from it (PR #283, review)
        self.on_other_branch()
        cwd, info, err = worktree.prepare(self.repo, "a-18")
        self.assertIsNone(err)
        self.assertEqual(git(cwd, "rev-parse", "HEAD"), git(self.repo, "rev-parse", "feature/other"))
        self.assertNotIn("from_main", info)

    def test_from_main_starts_from_main_even_when_the_main_copy_is_on_another_branch(self):
        # 25.10: the main copy stood on a foreign branch, the executor branched from it, and "ff into main" bypassed main
        main = self.on_other_branch()
        cwd, info, err = worktree.prepare(self.repo, "a-19", from_main=True)
        self.assertIsNone(err)
        self.assertEqual(git(cwd, "rev-parse", "HEAD"), git(self.repo, "rev-parse", main))
        self.assertTrue(info["from_main"])
        self.assertEqual(git(self.repo, "branch", "--show-current").strip(), "feature/other")   # the main copy is left alone

    def test_from_main_takes_origin_when_the_local_main_is_strictly_behind(self):
        main = self.on_other_branch()
        pushed = self.with_origin(main)
        cwd, _, err = worktree.prepare(self.repo, "a-21", from_main=True)
        self.assertIsNone(err)
        self.assertEqual(git(cwd, "rev-parse", "HEAD"), pushed)
        self.assertNotEqual(git(self.repo, "rev-parse", main), pushed)   # the local main is untouched

    def test_from_main_fetches_origin_first_and_says_when_it_could_not(self):
        # 11-11: origin was read as git saw it at the last fetch, and briefs carried a manual `git fetch`
        main = self.on_other_branch()
        origin, other = os.path.join(self.tmp, "origin.git"), os.path.join(self.tmp, "other")
        git(self.tmp, "clone", "-q", "--bare", self.repo, origin)
        git(origin, "symbolic-ref", "HEAD", f"refs/heads/{main}")
        git(self.repo, "remote", "add", "origin", origin)
        git(self.repo, "fetch", "-q", "origin")
        git(self.repo, "remote", "set-head", "origin", main)
        git(self.tmp, "clone", "-q", "-b", main, origin, other)
        self.commit(other, "pushed after the last fetch")
        git(other, "push", "-q", "origin", main)
        pushed = git(other, "rev-parse", "HEAD")
        cwd, info, err = worktree.prepare(self.repo, "a-24", from_main=True)
        self.assertIsNone(err)
        self.assertEqual(git(cwd, "rev-parse", "HEAD"), pushed)
        self.assertNotIn("fetch", info)   # a note for the answer only; it is not kept in the record
        self.assertEqual([c for c in info["checks"] if not c[1]], [])
        git(self.repo, "remote", "set-url", "origin", os.path.join(self.tmp, "gone.git"))   # no network: as before, with a note
        cwd, info, err = worktree.prepare(self.repo, "a-25", from_main=True)
        self.assertIsNone(err)
        self.assertEqual(git(cwd, "rev-parse", "HEAD"), pushed)
        self.assertEqual([c[0][:22] for c in info["checks"] if not c[1]], ["origin was not fetched"])

    def test_from_main_keeps_unpushed_commits_of_the_local_main(self):
        main = self.on_other_branch()
        self.with_origin(main)
        git(self.repo, "switch", "-q", main)
        self.commit(self.repo, "unpushed")                    # local main and origin diverged: our commits are kept
        local = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "-q", "feature/other")
        cwd, _, err = worktree.prepare(self.repo, "a-22", from_main=True)
        self.assertIsNone(err)
        self.assertEqual(git(cwd, "rev-parse", "HEAD"), local)
        git(self.repo, "switch", "-q", main)                  # and merely ahead of origin: also ours
        git(self.repo, "reset", "-q", "--hard", f"origin/{main}")
        self.commit(self.repo, "ahead")
        ahead = git(self.repo, "rev-parse", "HEAD")
        git(self.repo, "switch", "-q", "feature/other")
        cwd, _, err = worktree.prepare(self.repo, "a-23", from_main=True)
        self.assertIsNone(err)
        self.assertEqual(git(cwd, "rev-parse", "HEAD"), ahead)

    def test_without_a_main_branch_from_main_starts_from_head_as_before(self):
        git(self.repo, "branch", "-m", "trunk")
        self.commit(self.repo, "more")
        cwd, _, err = worktree.prepare(self.repo, "a-20", from_main=True)
        self.assertIsNone(err)
        self.assertEqual(git(cwd, "rev-parse", "HEAD"), git(self.repo, "rev-parse", "HEAD"))

    def test_not_a_git_repo_means_same_folder(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.assertEqual(worktree.prepare(plain, "x-2"), (None, None, None))

    def test_broken_worktree_is_an_error_not_a_shared_copy(self):
        os.makedirs(os.path.join(self.repo, ".claude", "worktrees", "devops-2"))  # a foreign folder is in the way
        with open(os.path.join(self.repo, ".claude", "worktrees", "devops-2", "x"), "w") as f:
            f.write("x")
        cwd, info, err = worktree.prepare(self.repo, "devops-2")
        self.assertIsNone(cwd)
        self.assertTrue(err)


class Core(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "devops", cwd="/k8s", session_id="s-dev", role="agent", about="DevOps", **OWNER)
        for n in ("hub", "devops"):
            self.ev("session.started", n)
        self.ev("turn.started", "devops", marker=None, prompt_head="x")
        self.ev("message.received", None, to="devops", text="task", channel="session", owner=False, sender="hub")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def test_removed_executor_asks_hubd_to_clean_its_worktree(self):
        wt = {"path": "/k8s/.claude/worktrees/devops-fix", "branch": "task/devops-fix", "repo": "/k8s", "checks": []}
        self.ev("session.requested", "devops-fix", session_id="s-f", executor=True, raised_by="devops", cwd=wt["path"],
                worktree=wt, **OWNER)
        self.st["sessions"]["devops-fix"]["state"] = "asleep"
        self.ev("session.remove_requested", "devops-fix", channel="cli", owner=True)
        self.assertEqual([s["name"] for s in executors.worktrees_to_clean(self.st)], ["devops-fix"])
        self.assertIsNone(self.ev("session.worktree_removed", "devops-fix", ok=True, why="removed"))
        self.assertEqual(executors.worktrees_to_clean(self.st), [])


if __name__ == "__main__":
    unittest.main()
