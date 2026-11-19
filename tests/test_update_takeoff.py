"""Take the hub's own edits of the core off and put them back (dev-guard spec §5.7; owner decision q1793545: "see, hold,
roll back"): the core does it itself, with no agent. "Take off": everything in the code folder, uncommitted and new files
too, goes into a backup branch backup/local-edits-<date>; the files the band names as edits of the core go back to the
release with a new commit; the hub's own modules stay; the hub restarts the way an update restarts it; a line goes to the
hub's feed. "Put back": a merge of the last such branch; a conflict leaves the code as it was and hands the merge to
Hub Developer. Real git repos of their own (not the history of this repository)."""
import tests  # noqa: F401 — hide the machine's settings
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

import platforms


class Repos:
    """A shared repo with a release, the hub's clone of it, the updater on the clone (the tests are substituted)."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="takeoff-")
        self.addCleanup(platforms.current.rmtree, self.root)
        self.state = os.path.join(self.root, "state")
        os.makedirs(os.path.join(self.state, "spool"))
        self.up = os.path.join(self.root, "up.git")
        self.g(self.root, "init", "-q", "--bare", "-b", "main", self.up)
        self.author = os.path.join(self.root, "author")
        self.g(self.root, "clone", "-q", self.up, self.author)
        self.write(self.author, "core.py", "a = 1\n")
        self.write(self.author, "core/daemon.py", "d = 1\n")
        self.write(self.author, "core/tools.py", "t = 1\n")
        self.write(self.author, "modules/stats/stats.py", "x = 1\n")
        self.write(self.author, "tests/test_a.py", "t = 1\n")
        self.write(self.author, ".gitignore", "*.log\n__pycache__/\n")
        self.release("start")
        self.mine = os.path.join(self.root, "mine")
        self.g(self.root, "clone", "-q", self.up, self.mine)
        self.updater().check(apply=False)                                   # the first pass puts the install mark
        self.taken()

    def g(self, cwd, *a):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, repo, name, text):
        path = os.path.join(repo, *name.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="\n") as f:
            f.write(text)

    def read(self, repo, name):
        path = os.path.join(repo, *name.split("/"))
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return f.read()

    def release(self, msg):
        self.g(self.author, "add", "-A")
        self.g(self.author, "commit", "-qm", msg)
        self.g(self.author, "push", "-q", "origin", "main")
        return self.g(self.author, "rev-parse", "HEAD")

    def commit(self, repo, msg="mine"):
        self.g(repo, "add", "-A")
        self.g(repo, "commit", "-qm", msg)

    def updater(self, turn=None, tests=None):
        from modules.update import update
        up = {"enabled": True, "remote": "origin", "branch": "main", "channel": "main", "delay_hours": 0}
        return update.Updater(self.mine, self.state, {"update": up, "dev": {"name": "hub-dev"}},
                              tests=tests or (lambda repo: (True, "OK")), turn=turn)

    def taken(self):
        from core import spool
        out = []
        for path, item in spool.take(os.path.join(self.state, "spool")):
            os.unlink(path)
            out.append(item)
        return out

    def edits(self):
        from core import codeedits
        return codeedits.classify(self.mine, self.g(self.mine, "rev-parse", "origin/main"))

    def branches(self):
        out = self.g(self.mine, "for-each-ref", "--format=%(refname:short)", "refs/heads/backup/")
        return out.split() if out else []

    def at(self, ref, name):
        """A file's text in a commit, or None."""
        r = subprocess.run(["git", "show", f"{ref}:{name}"], cwd=self.mine, capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else None

    def own_module(self):
        self.write(self.mine, "modules/mine/mine.py", "own = 1\n")
        self.write(self.mine, "modules/mine/module.json", "{}\n")
        self.write(self.mine, "bin/hub-mine", "#!/usr/bin/env python3\n")

    def mixed(self):
        """Edits of every kind: committed, uncommitted, a deleted file of the release, new files, an own module, an
        ignored file, a path with a space."""
        self.write(self.mine, "core.py", "a = 2  # committed\n")
        self.write(self.mine, "core/x y.py", "new = 1\n")                   # committed new file of the core, a space
        self.commit(self.mine)
        self.write(self.mine, "core/daemon.py", "d = 2  # not committed\n")
        os.unlink(os.path.join(self.mine, "modules", "stats", "stats.py"))  # a file of the release deleted
        self.write(self.mine, "core/sub/new.py", "n = 1\n")                 # a new file outside git, deep down
        self.own_module()
        self.write(self.mine, "run.log", "kept\n")                          # ignored: not the hub's edit, not touched

    def kinds(self, items):
        return [i["type"] for i in items]

    def notes(self, items):
        return [i["data"].get("text", "") for i in items if i["type"] == "owner.notify"]


class TakeOff(Repos, unittest.TestCase):
    def test_everything_goes_into_the_backup_branch_and_the_edits_of_the_core_back_to_the_release(self):
        self.mixed()
        head = self.g(self.mine, "rev-parse", "HEAD")
        self.assertEqual(self.updater().local_edits_switch("off"), "off")
        today = time.strftime("%Y-%m-%d")
        self.assertEqual(self.branches(), [f"backup/local-edits-{today}"])
        backup = f"backup/local-edits-{today}"
        # the backup holds the folder as it was, the uncommitted and the new files too; what git ignores is not in it
        self.assertEqual(self.at(backup, "core.py"), "a = 2  # committed\n")
        self.assertEqual(self.at(backup, "core/daemon.py"), "d = 2  # not committed\n")
        self.assertEqual(self.at(backup, "core/sub/new.py"), "n = 1\n")
        self.assertEqual(self.at(backup, "core/x y.py"), "new = 1\n")
        self.assertIsNone(self.at(backup, "modules/stats/stats.py"))
        self.assertEqual(self.at(backup, "modules/mine/mine.py"), "own = 1\n")
        self.assertIsNone(self.at(backup, "run.log"))
        # the main branch: one new commit on top of what was there, the edits of the core are the release's again
        self.assertEqual(self.g(self.mine, "rev-parse", "HEAD~1"), head)
        for name in ("core.py", "core/daemon.py", "modules/stats/stats.py"):
            self.assertEqual(self.read(self.mine, name), self.at("origin/main", name), name)
        self.assertIsNone(self.read(self.mine, "core/sub/new.py"))
        self.assertIsNone(self.read(self.mine, "core/x y.py"))
        self.assertIsNone(self.at("HEAD", "core/x y.py"))
        # the own module stays where it was, outside git as before; the ignored file too
        self.assertEqual(self.read(self.mine, "modules/mine/mine.py"), "own = 1\n")
        self.assertEqual(self.read(self.mine, "bin/hub-mine"), "#!/usr/bin/env python3\n")
        self.assertEqual(self.read(self.mine, "run.log"), "kept\n")
        self.assertEqual(self.edits(), {"core": [], "modules": ["mine"]})
        # nothing is left half done: what git sees in the tree is only the own module
        status = self.g(self.mine, "status", "--porcelain", "--untracked-files=all").splitlines()
        self.assertEqual(sorted(s[3:] for s in status), ["bin/hub-mine", "modules/mine/mine.py", "modules/mine/module.json"])

    def test_the_hub_restarts_as_after_an_update_and_the_feed_names_the_branch(self):
        self.mixed()
        self.updater().local_edits_switch("off")
        items = self.taken()
        restarts = [i["data"] for i in items if i["type"] == "hub.restart_requested"]
        self.assertEqual([r.get("parts") for r in restarts], [["web", "files"], []])
        self.assertIs(restarts[1].get("now"), False)                         # waits for free sessions, as an update
        (note,) = self.notes(items)
        self.assertIn(f"backup/local-edits-{time.strftime('%Y-%m-%d')}", note)
        self.assertIn("5", note)                                             # core.py, x y.py, daemon.py, new.py, stats
        self.assertTrue(all(i["data"].get("owner") is True and i["data"].get("source") == "update" for i in items))

    def test_with_nothing_to_take_off_nothing_moves_and_the_feed_says_so(self):
        self.own_module()                                                    # the own module alone is not an edit
        head = self.g(self.mine, "rev-parse", "HEAD")
        self.assertEqual(self.updater().local_edits_switch("off"), "none")
        self.assertEqual((self.branches(), self.g(self.mine, "rev-parse", "HEAD")), ([], head))
        items = self.taken()
        self.assertEqual(self.kinds(items), ["owner.notify"])

    def test_a_second_press_finds_nothing_left(self):
        self.mixed()
        self.updater().local_edits_switch("off")
        head = self.g(self.mine, "rev-parse", "HEAD")
        self.assertEqual(self.updater().local_edits_switch("off"), "none")
        self.assertEqual((len(self.branches()), self.g(self.mine, "rev-parse", "HEAD")), (1, head))

    def test_only_uncommitted_edits_make_no_commit_on_main(self):
        self.write(self.mine, "core.py", "a = 9\n")
        head = self.g(self.mine, "rev-parse", "HEAD")
        self.assertEqual(self.updater().local_edits_switch("off"), "off")
        self.assertEqual(self.g(self.mine, "rev-parse", "HEAD"), head)
        self.assertEqual(self.read(self.mine, "core.py"), "a = 1\n")
        self.assertEqual(self.g(self.mine, "status", "--porcelain"), "")
        (backup,) = self.branches()
        self.assertEqual(self.at(backup, "core.py"), "a = 9\n")

    def test_a_branch_of_the_same_name_is_not_overwritten(self):
        # Hub Developer's own backup (the "Fix" task names the same pattern): ours takes the next free name
        today = time.strftime("%Y-%m-%d")
        self.g(self.mine, "branch", f"backup/local-edits-{today}")
        mark = self.g(self.mine, "rev-parse", f"backup/local-edits-{today}")
        self.write(self.mine, "core.py", "a = 2\n")
        self.updater().local_edits_switch("off")
        self.assertEqual(sorted(self.branches()), [f"backup/local-edits-{today}", f"backup/local-edits-{today}-2"])
        self.assertEqual(self.g(self.mine, "rev-parse", f"backup/local-edits-{today}"), mark)

    def test_it_runs_under_the_machine_lock(self):
        seen = []

        @contextlib.contextmanager
        def turn(patience):
            seen.append(("in", self.g(self.mine, "rev-parse", "HEAD")))
            yield
            seen.append(("out", self.g(self.mine, "rev-parse", "HEAD")))

        self.write(self.mine, "core.py", "a = 2\n")
        self.commit(self.mine)
        head = self.g(self.mine, "rev-parse", "HEAD")
        self.updater(turn=turn).local_edits_switch("off")
        self.assertEqual(seen[0], ("in", head))                              # nothing moved before the lock
        self.assertNotEqual(seen[1][1], head)

    def test_a_busy_lock_moves_nothing_and_says_so(self):
        from core import testlock

        @contextlib.contextmanager
        def turn(patience):
            raise testlock.Busy("someone else's run")
            yield  # noqa — a generator

        self.write(self.mine, "core.py", "a = 2\n")
        self.assertEqual(self.updater(turn=turn).local_edits_switch("off"), "busy")
        self.assertEqual((self.branches(), self.read(self.mine, "core.py")), ([], "a = 2\n"))
        self.assertEqual(self.kinds(self.taken()), ["owner.notify"])

    def test_the_app_has_no_edits_to_take_off(self):
        from modules.update import update
        u = update.Updater(self.mine, self.state, {"update": {"enabled": True, "app": {}}}, tests=lambda repo: (True, ""))
        self.write(self.mine, "core.py", "a = 2\n")
        self.assertEqual(u.local_edits_switch("off"), "app")
        self.assertEqual(self.read(self.mine, "core.py"), "a = 2\n")
        self.assertEqual(self.kinds(self.taken()), ["owner.notify"])           # the command said "the result is in the feed"

    def test_a_new_folder_where_the_release_has_a_file_gives_the_file_back(self):
        os.unlink(os.path.join(self.mine, "core.py"))
        self.write(self.mine, "core.py/inner.py", "i = 1\n")                 # a folder of new files at a release file
        self.assertEqual(self.updater().local_edits_switch("off"), "off")
        self.assertEqual(self.read(self.mine, "core.py"), "a = 1\n")
        (backup,) = self.branches()
        self.assertEqual(self.at(backup, "core.py/inner.py"), "i = 1\n")
        self.assertEqual(self.edits(), {"core": [], "modules": []})

    def test_a_held_index_lock_moves_nothing(self):
        # a session's git command holds the index: HEAD goes back, no backup branch is left, the edits stay as they were
        self.write(self.mine, "core.py", "a = 2\n")
        self.commit(self.mine)
        self.write(self.mine, "core/daemon.py", "d = 2\n")
        head = self.g(self.mine, "rev-parse", "HEAD")
        open(os.path.join(self.mine, ".git", "index.lock"), "w").close()
        self.assertEqual(self.updater().local_edits_switch("off"), "failed")
        os.unlink(os.path.join(self.mine, ".git", "index.lock"))
        self.assertEqual((self.g(self.mine, "rev-parse", "HEAD"), self.branches()), (head, []))
        self.assertEqual((self.read(self.mine, "core.py"), self.read(self.mine, "core/daemon.py")), ("a = 2\n", "d = 2\n"))
        self.assertEqual(self.g(self.mine, "status", "--porcelain").split(), ["M", "core/daemon.py"])

    def test_a_write_into_the_folder_while_the_trees_are_built_moves_nothing(self):
        from modules.update import takeoff
        self.write(self.mine, "core.py", "a = 2\n")
        real, calls = takeoff._folder_tree, []

        def folder(g):
            calls.append(1)
            if len(calls) == 2:                                                # a session saves a file in between
                self.write(self.mine, "core.py", "a = 3  # saved meanwhile\n")
            return real(g)

        with unittest.mock.patch.object(takeoff, "_folder_tree", folder):
            self.assertEqual(self.updater().local_edits_switch("off"), "moved")
        self.assertEqual((self.branches(), self.read(self.mine, "core.py")), ([], "a = 3  # saved meanwhile\n"))

    def test_a_hub_with_no_install_mark_does_not_guess_the_release(self):
        from modules.update import update
        os.unlink(os.path.join(self.state, update.INSTALLED))
        self.write(self.mine, "core.py", "a = 2\n")
        self.assertEqual(self.updater().local_edits_switch("off"), "no-base")
        self.assertEqual((self.branches(), self.read(self.mine, "core.py")), ([], "a = 2\n"))

    def test_the_check_names_the_branch_to_put_back_until_it_is_put_back(self):
        self.mixed()
        self.updater().local_edits_switch("off")
        self.taken()
        self.updater().check(apply=False)
        (got,) = [i["data"] for i in self.taken() if i["type"] == "update.checked"]
        backup = got["local_edits"]["backup"]
        self.assertEqual(backup["name"], f"backup/local-edits-{time.strftime('%Y-%m-%d')}")
        self.assertEqual(backup["count"], 5)
        self.assertLess(abs(backup["at"] - time.time()), 120)
        self.assertEqual(got["local_edits"]["count"], 0)
        self.updater().local_edits_switch("on")
        self.taken()
        self.updater().check(apply=False)
        (got,) = [i["data"] for i in self.taken() if i["type"] == "update.checked"]
        self.assertNotIn("backup", got["local_edits"])


class PutBack(Repos, unittest.TestCase):
    def off(self):
        self.mixed()
        self.assertEqual(self.updater().local_edits_switch("off"), "off")
        self.taken()

    def test_the_edits_of_the_core_come_back_and_the_own_module_is_not_touched(self):
        self.off()
        self.write(self.mine, "modules/mine/mine.py", "own = 2  # edited after the take-off\n")
        self.assertEqual(self.updater().local_edits_switch("on"), "on")
        self.assertEqual(self.read(self.mine, "core.py"), "a = 2  # committed\n")
        self.assertEqual(self.read(self.mine, "core/daemon.py"), "d = 2  # not committed\n")
        self.assertEqual(self.read(self.mine, "core/sub/new.py"), "n = 1\n")
        self.assertEqual(self.read(self.mine, "core/x y.py"), "new = 1\n")
        self.assertIsNone(self.read(self.mine, "modules/stats/stats.py"))
        self.assertEqual(self.read(self.mine, "modules/mine/mine.py"), "own = 2  # edited after the take-off\n")
        # the edits are back, committed now: the tree has nothing of the core outside git
        got = self.edits()
        self.assertEqual(sorted(f["path"] for f in got["core"]),
                         ["core.py", "core/daemon.py", "core/sub/new.py", "core/x y.py", "modules/stats/stats.py"])
        self.assertEqual(got["modules"], ["mine"])
        self.assertEqual(self.g(self.mine, "status", "--porcelain", "--untracked-files=no"), "")
        self.assertNotIn("core", self.g(self.mine, "status", "--porcelain", "--untracked-files=all"))
        items = self.taken()
        self.assertEqual([i["data"].get("parts") for i in items if i["type"] == "hub.restart_requested"], [["web", "files"], []])
        self.assertEqual(len(self.notes(items)), 1)

    def test_a_release_merged_after_the_take_off_stays_and_the_edits_come_back_on_top(self):
        self.off()
        self.write(self.author, "core/tools.py", "t = 2  # the release\n")
        self.release("next")
        self.assertEqual(self.updater().check(), "applied")                 # the update merges the release
        self.taken()
        self.assertEqual(self.updater().local_edits_switch("on"), "on")
        self.assertEqual(self.read(self.mine, "core/tools.py"), "t = 2  # the release\n")
        self.assertEqual(self.read(self.mine, "core.py"), "a = 2  # committed\n")
        self.assertEqual(self.read(self.mine, "core/daemon.py"), "d = 2  # not committed\n")

    def test_a_conflict_leaves_the_code_as_it_was_and_hands_the_merge_to_hub_developer(self):
        self.off()
        self.write(self.author, "core.py", "a = 3  # the release changed it too\n")
        self.release("next")
        self.assertEqual(self.updater().check(), "applied")
        self.taken()
        head = self.g(self.mine, "rev-parse", "HEAD")
        self.assertEqual(self.updater().local_edits_switch("on"), "conflict")
        self.assertEqual(self.g(self.mine, "rev-parse", "HEAD"), head)
        self.assertEqual(self.read(self.mine, "core.py"), "a = 3  # the release changed it too\n")
        self.assertNotIn("core", self.g(self.mine, "status", "--porcelain", "--untracked-files=all"))
        self.assertFalse(os.path.exists(os.path.join(self.mine, ".git", "MERGE_HEAD")))
        items = self.taken()
        (task,) = [i["data"] for i in items if i["type"] == "message.received"]
        self.assertEqual(task["to"], "hub-dev")
        self.assertIn("core.py", task["text"])
        self.assertIn(f"backup/local-edits-{time.strftime('%Y-%m-%d')}", task["text"])
        self.assertNotIn("hub.restart_requested", self.kinds(items))
        self.assertEqual(len(self.notes(items)), 1)
        # no copy of the merge is left behind
        self.assertEqual(self.g(self.mine, "worktree", "list", "--porcelain").count("worktree "), 1)

    def test_on_top_of_a_newer_release_the_core_tests_run_first_and_red_moves_nothing(self):
        self.off()
        self.write(self.author, "core/tools.py", "t = 2  # the release\n")
        self.release("next")
        self.assertEqual(self.updater().check(), "applied")
        self.taken()
        head, ran = self.g(self.mine, "rev-parse", "HEAD"), []

        def tests(repo):
            ran.append(self.read(repo, "core.py"))                             # the copy has the edits merged in
            return False, "FAIL: test_x (tests.test_a.T.test_x)", ["tests.test_a.T.test_x"]

        self.assertEqual(self.updater(tests=tests).local_edits_switch("on"), "red")
        self.assertEqual(ran, ["a = 2  # committed\n"])
        self.assertEqual((self.g(self.mine, "rev-parse", "HEAD"), self.read(self.mine, "core.py")), (head, "a = 1\n"))
        items = self.taken()
        (task,) = [i["data"] for i in items if i["type"] == "message.received"]
        self.assertIn("tests.test_a.T.test_x", task["text"])
        self.assertNotIn("hub.restart_requested", self.kinds(items))

    def newer(self):
        """Take off, then a release merged by the update: put back is a merge with the tests."""
        self.off()
        self.write(self.author, "core/tools.py", "t = 2  # the release\n")
        self.release("next")
        self.assertEqual(self.updater().check(), "applied")
        self.taken()
        return self.g(self.mine, "rev-parse", "HEAD")

    def test_an_edit_of_the_live_tree_during_the_tests_is_not_carried_over_untested(self):
        head = self.newer()

        def tests(repo):
            self.write(self.mine, "core/tools.py", "t = 3  # a session, meanwhile\n")
            return True, "OK"

        self.assertEqual(self.updater(tests=tests).local_edits_switch("on"), "moved")
        self.assertEqual((self.g(self.mine, "rev-parse", "HEAD"), self.read(self.mine, "core.py")), (head, "a = 1\n"))

    def test_a_machine_that_slept_during_the_tests_says_nothing_about_them(self):
        from modules.update import update
        head = self.newer()
        times = iter([(0, 0), (1000, 10)])                                    # 990 s of wall time the monotonic missed
        u = self.updater(tests=lambda repo: (False, "FAIL: x (tests.test_a.T.x)"))
        u.clock = lambda: next(times)
        self.assertEqual(u.local_edits_switch("on"), "asleep")
        self.assertEqual(self.g(self.mine, "rev-parse", "HEAD"), head)
        self.assertNotIn("message.received", self.kinds(self.taken()))      # no red task for Hub Developer
        self.assertGreater(1000 - 10, update.SLEPT)

    def test_a_file_in_the_way_is_seen_before_the_tests(self):
        self.newer()
        self.write(self.mine, "core/x y.py", "in the way\n")                # where the edits add a file of theirs
        ran = []
        self.assertEqual(self.updater(tests=lambda repo: ran.append(1) or (True, "OK")).local_edits_switch("on"), "blocked")
        self.assertEqual((ran, self.read(self.mine, "core/x y.py")), ([], "in the way\n"))
        (task,) = [i["data"] for i in self.taken() if i["type"] == "message.received"]
        self.assertEqual(task["to"], "hub-dev")

    def test_without_new_code_since_the_take_off_no_tests_run(self):
        # the edits come back to the very code they ran with before: nothing new to check
        self.off()
        ran = []
        self.assertEqual(self.updater(tests=lambda repo: ran.append(repo) or (False, "")).local_edits_switch("on"), "on")
        self.assertEqual(ran, [])

    def test_edits_the_release_took_in_are_not_offered_again(self):
        self.write(self.mine, "core.py", "a = 2\n")
        self.updater().local_edits_switch("off")
        self.write(self.author, "core.py", "a = 2\n")                       # the same change came with a release
        self.release("next")
        self.assertEqual(self.updater().check(), "applied")
        self.taken()
        self.updater().check(apply=False)
        (got,) = [i["data"] for i in self.taken() if i["type"] == "update.checked"]
        self.assertNotIn("backup", got["local_edits"])
        self.assertEqual(self.updater().local_edits_switch("on"), "returned")

    def test_without_a_backup_branch_there_is_nothing_to_put_back(self):
        self.write(self.mine, "core.py", "a = 2\n")
        head = self.g(self.mine, "rev-parse", "HEAD")
        self.assertEqual(self.updater().local_edits_switch("on"), "none")
        self.assertEqual((self.g(self.mine, "rev-parse", "HEAD"), self.read(self.mine, "core.py")), (head, "a = 2\n"))
        self.assertEqual(self.kinds(self.taken()), ["owner.notify"])

    def test_a_backup_branch_that_is_not_the_cores_is_not_merged(self):
        # Hub Developer's own branch under the same pattern (the "Fix" task) is not ours to merge
        self.write(self.mine, "core.py", "a = 2\n")
        self.g(self.mine, "checkout", "-q", "-b", f"backup/local-edits-{time.strftime('%Y-%m-%d')}")
        self.commit(self.mine, "by hand")
        self.g(self.mine, "checkout", "-q", "main")
        self.assertEqual(self.updater().local_edits_switch("on"), "none")
        self.assertEqual(self.read(self.mine, "core.py"), "a = 1\n")

    def test_a_second_press_finds_them_already_back(self):
        self.off()
        self.updater().local_edits_switch("on")
        head = self.g(self.mine, "rev-parse", "HEAD")
        self.assertEqual(self.updater().local_edits_switch("on"), "returned")
        self.assertEqual(self.g(self.mine, "rev-parse", "HEAD"), head)

    def test_the_last_backup_is_the_one_put_back(self):
        self.write(self.mine, "core.py", "a = 2\n")
        self.updater().local_edits_switch("off")
        self.updater().local_edits_switch("on")
        self.write(self.mine, "core/daemon.py", "d = 7\n")
        self.updater().local_edits_switch("off")                           # the second take-off: core.py and daemon.py
        self.assertEqual(self.read(self.mine, "core.py"), "a = 1\n")
        self.assertEqual(self.updater().local_edits_switch("on"), "on")
        self.assertEqual((self.read(self.mine, "core.py"), self.read(self.mine, "core/daemon.py")), ("a = 2\n", "d = 7\n"))

    def test_an_edit_of_the_same_file_after_the_take_off_is_not_overwritten(self):
        self.off()
        self.write(self.mine, "core.py", "a = 5  # edited again, not committed\n")
        head = self.g(self.mine, "rev-parse", "HEAD")
        self.assertEqual(self.updater().local_edits_switch("on"), "blocked")
        self.assertEqual((self.g(self.mine, "rev-parse", "HEAD"), self.read(self.mine, "core.py")),
                         (head, "a = 5  # edited again, not committed\n"))
        (task,) = [i["data"] for i in self.taken() if i["type"] == "message.received"]
        self.assertEqual(task["to"], "hub-dev")


class Flag(Repos, unittest.TestCase):
    """The page and `hub update local-edits off|on` leave a flag in the state folder; the update process acts on it."""

    def test_the_flag_is_taken_once_and_a_fresh_check_is_asked_for(self):
        from modules.update import update
        self.write(self.mine, "core.py", "a = 2\n")
        flag = os.path.join(self.state, update.EDITS_FLAG)
        with open(flag, "w") as f:
            f.write("off")
        cfg = {"update": {"enabled": True, "remote": "origin", "branch": "main", "channel": "main"},
               "dev": {"name": "hub-dev"}}
        self.assertEqual(update.edits_flag(self.state, cfg, repo=self.mine, tests=lambda repo: (True, "OK")), "off")
        self.assertFalse(os.path.exists(flag))
        self.assertTrue(os.path.exists(os.path.join(self.state, update.NOW_FLAG)))
        self.assertEqual(self.read(self.mine, "core.py"), "a = 1\n")
        self.assertIsNone(update.edits_flag(self.state, cfg, repo=self.mine, tests=lambda repo: (True, "OK")))

    def test_a_strange_flag_is_dropped_and_does_nothing(self):
        from modules.update import update
        self.write(self.mine, "core.py", "a = 2\n")
        flag = os.path.join(self.state, update.EDITS_FLAG)
        with open(flag, "w") as f:
            f.write("rm -rf")
        self.assertIsNone(update.edits_flag(self.state, {"update": {"enabled": True}}, repo=self.mine))
        self.assertFalse(os.path.exists(flag))
        self.assertEqual(self.read(self.mine, "core.py"), "a = 2\n")


class Page(unittest.TestCase):
    """POST /api/update/local-edits {to}: the page leaves the flag for the update process, nothing moves in the server."""

    def setUp(self):
        from tests.test_web import WebTest
        self.helper = WebTest("test_page_and_snapshot")
        self.helper.setUp()
        self.addCleanup(self.helper.tearDown)

    def checked(self, **edits):
        self.helper.j.append("update.checked", None, {"result": "current", "behind": 0, "channel": "cli", "owner": True,
                                                       "local_edits": {"count": 0, "files": [], **edits}})
        self.helper.settle()

    def press(self, to):
        code, out = self.helper.req("POST", "/api/update/local-edits", {"to": to})
        return code, json.loads(out)

    def flag(self):
        from modules.update import update
        path = os.path.join(self.helper.state, update.EDITS_FLAG)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return f.read()

    def test_take_off_leaves_the_flag_and_a_second_press_waits_for_it(self):
        self.checked(count=1, files=[{"path": "core.py", "st": "M"}])
        code, out = self.press("off")
        self.assertEqual((code, self.flag()), (202, "off"))
        self.assertTrue(out["text"])
        self.assertEqual(self.press("off")[0], 409)                          # another tab: the first is on its way
        self.assertEqual(self.flag(), "off")

    def test_nothing_to_take_off_or_to_put_back_is_refused_and_a_fresh_check_is_asked_for(self):
        from modules.update import update
        self.checked()
        self.assertEqual([self.press("off")[0], self.press("on")[0]], [409, 409])
        self.assertIsNone(self.flag())
        self.assertTrue(os.path.exists(os.path.join(self.helper.state, update.NOW_FLAG)))   # the band redraws in 15 s

    def test_put_back_needs_a_backup_named_by_the_check(self):
        self.checked(backup={"name": "backup/local-edits-2026-11-06", "count": 2, "at": 1762430000})
        self.assertEqual(self.press("on")[0], 202)
        self.assertEqual(self.flag(), "on")

    def test_a_strange_word_is_refused(self):
        self.checked(count=1, files=[{"path": "core.py", "st": "M"}])
        for bad in ("", "rm", "OFF", None):
            self.assertEqual(self.press(bad)[0], 400, bad)
        self.assertIsNone(self.flag())

    def test_the_path_is_the_cores(self):
        from core import modclaims
        self.assertIn("/api/update/local-edits", modclaims.CORE_PATHS)


class Command(unittest.TestCase):
    """`hub update local-edits off|on`: the owner's word, as module add; the flag for the update process."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="takeoff-cli-")
        self.addCleanup(platforms.current.rmtree, self.state)
        os.makedirs(os.path.join(self.state, "spool"))

    def hub(self, *args, session=None):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE")}
        env.update(HUB_STATE_DIR=self.state, PYTHONUTF8="1", **({"HUB_SESSION": session} if session else {}))
        return subprocess.run([sys.executable, os.path.join(repo, "bin", "hub"), "update", "local-edits", *args],
                              env=env, capture_output=True, text=True, encoding="utf-8", timeout=60)

    def flag(self):
        from modules.update import update
        path = os.path.join(self.state, update.EDITS_FLAG)
        return open(path).read() if os.path.exists(path) else None

    def test_the_owner_leaves_the_flag(self):
        r = self.hub("off")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.flag(), "off")
        self.assertNotEqual(self.hub("on").returncode, 0)                    # the first is still on its way
        self.assertEqual(self.flag(), "off")

    def test_a_wrong_word_is_refused(self):
        for args in ((), ("maybe",), ("off", "on")):
            self.assertNotEqual(self.hub(*args).returncode, 0, args)
        self.assertIsNone(self.flag())

    def test_a_session_that_is_not_the_hub_is_refused(self):
        r = self.hub("off", session="maps")
        self.assertNotEqual(r.returncode, 0)
        self.assertIsNone(self.flag())


class Shape(unittest.TestCase):
    """The fold keeps only a backup the page can trust: our branch name, whole numbers."""

    def shape(self, backup):
        from core import reducer
        return reducer.local_edits_shape({"count": 0, "files": [], "backup": backup}).get("backup")

    def test_a_good_backup_is_kept(self):
        b = {"name": "backup/local-edits-2026-11-06-2", "count": 3, "at": 1762430000}
        self.assertEqual(self.shape(b), b)

    def test_anything_else_is_dropped(self):
        for bad in ("x", None, {"name": "main", "count": 1, "at": 1}, {"name": "backup/local-edits-2026-11-06\n", "count": 1, "at": 1},
                    {"name": "backup/local-edits-2026-11-06", "count": True, "at": 1},
                    {"name": "backup/local-edits-2026-11-06", "count": -1, "at": 1},
                    {"name": "backup/local-edits-2026-11-06", "count": 1, "at": "now"},
                    {"name": "backup/local-edits-2026-11-06", "count": 1, "at": float("nan")}):
            self.assertIsNone(self.shape(bad), bad)


if __name__ == "__main__":
    unittest.main()
