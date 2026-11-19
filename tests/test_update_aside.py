"""The self-update checks new code aside: the merge and tests run in a separate copy
<state>/update/work-*, the live tree moves only by fast-forward (--ff-only) to the checked commit; release history goes
forward only (the remembered release and the tags seen outside the tree). Real git repos: a shared (bare) one, the
author's copy and a colleague's clone with a personal commit."""
import tests  # noqa: F401 — hide the machine's settings
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Aside(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="update-aside-")
        self.state = os.path.join(self.root, "state")
        os.makedirs(os.path.join(self.state, "spool"))
        self.up = os.path.join(self.root, "up.git")
        self.g(self.root, "init", "-q", "--bare", "-b", "main", self.up)
        self.author = os.path.join(self.root, "author")
        self.g(self.root, "clone", "-q", self.up, self.author)
        self.commit("core.py", "a = 1\n", "start")
        self.mine = os.path.join(self.root, "mine")
        self.g(self.root, "clone", "-q", self.up, self.mine)
        self.write(self.mine, "personal.py", "mine = True\n")
        self.g(self.mine, "add", "-A")
        self.g(self.mine, "commit", "-qm", "my personal change")

    def g(self, cwd, *a):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, repo, name, text):
        with open(os.path.join(repo, name), "w") as f:
            f.write(text)

    def read(self, repo, name):
        with open(os.path.join(repo, name)) as f:
            return f.read()

    def commit(self, name, text, msg, tag=None, force=False):
        """An author commit to the shared main; tag also puts an annotated release tag on it; force goes over a rewrite."""
        self.write(self.author, name, text)
        self.g(self.author, "add", "-A")
        self.g(self.author, "commit", "-qm", msg)
        self.g(self.author, "push", "-q", *(["-f"] if force else []), "origin", "main")
        if tag:
            self.g(self.author, "tag", "-a", "-m", tag, tag)
            self.g(self.author, "push", "-q", "origin", f"refs/tags/{tag}")
        return self.g(self.author, "rev-parse", "HEAD")

    def updater(self, tests=None, app=False, **kw):
        """app: the app's clone (update.app), the one that follows release tags (its delay — tests/test_update_channels)."""
        from modules.update import update
        up = {"enabled": True, "remote": "origin", "branch": "main", "delay_hours": 0,
              **({"app": {"runtime": 1}} if app else {})}
        if app:
            kw = {"claude": lambda: None, "spawn": lambda *a: None, **kw}
        return update.Updater(self.mine, self.state, {"update": up, "dev": {"name": "hub-dev"}},
                              tests=tests or (lambda repo: (True, "OK")), **kw)

    def spooled(self):
        from core import spool
        out = []
        for path, item in spool.take(os.path.join(self.state, "spool")):
            os.unlink(path)
            out.append(item)
        return out

    def tree(self, repo):
        """The whole live tree: HEAD, branch, status and every file byte for byte (without .git)."""
        files = {}
        for d, dirs, names in os.walk(repo):
            dirs[:] = [x for x in dirs if x != ".git"]
            for n in names:
                p = os.path.join(d, n)
                with open(p, "rb") as f:
                    files[os.path.relpath(p, repo)] = f.read()
        git = os.path.join(repo, ".git")
        return (self.g(repo, "rev-parse", "HEAD"), self.g(repo, "symbolic-ref", "-q", "HEAD"),
                self.g(repo, "status", "--porcelain", "--ignored"), files,
                sorted(x for x in os.listdir(git) if x in ("MERGE_HEAD", "ORIG_HEAD", "hub-update.testing")))

    def copies(self):
        """Update copies that are left: update/work-* folders and git's records of them."""
        work = os.path.join(self.state, "update")
        left = sorted(n for n in os.listdir(work) if n.startswith("work-")) if os.path.isdir(work) else []
        listed = [ln for ln in self.g(self.mine, "worktree", "list", "--porcelain").splitlines()
                  if ln.startswith("worktree ")]
        return left, len(listed) - 1

    # --- merge and tests in the copy, fast-forward of the live tree ---

    def test_tests_run_in_a_copy_while_the_live_tree_stays_as_it_was(self):
        before = self.g(self.mine, "rev-parse", "HEAD")
        live = self.tree(self.mine)
        up = self.commit("core.py", "a = 2\n", "new feature")
        seen = []

        def tests(repo):
            seen.append((os.path.realpath(repo) != os.path.realpath(self.mine), self.tree(self.mine) == live,
                         self.read(repo, "core.py"), self.read(repo, "personal.py")))
            return True, "OK"
        self.assertEqual(self.updater(tests).check(), "applied")
        self.assertEqual(seen, [(True, True, "a = 2\n", "mine = True\n")])   # the merge is checked, the live tree is unchanged
        self.assertEqual(self.read(self.mine, "core.py"), "a = 2\n")
        self.assertEqual(self.read(self.mine, "personal.py"), "mine = True\n")   # the personal file is in place
        head = self.g(self.mine, "rev-parse", "HEAD")
        self.assertEqual(self.g(self.mine, "rev-list", "--parents", "-n1", head).split()[1:], [before, up])
        self.assertEqual(self.g(self.mine, "symbolic-ref", "HEAD"), "refs/heads/main")   # the branch is fast-forwarded, not detached
        self.assertEqual(self.copies(), ([], 0))                                         # the copy is removed
        evs = [e["type"] for e in self.spooled()]
        self.assertEqual(evs, ["update.testing", "update.applied", "hub.restart_requested", "hub.restart_requested",
                               "owner.notify", "update.checked"])

    def test_tests_that_passed_only_on_a_retry_go_into_the_applied_fact(self):
        # 2026-11-03 (tests research, P8): a flake is green, but seen — its names in the journal, the facts are the same
        self.commit("core.py", "a = 2\n", "new feature")
        flaky = ["tests.test_b.B.test_x", "tests.test_web_e2e.P.test_y"]
        self.assertEqual(self.updater(lambda repo: (True, "OK", [], flaky)).check(), "applied")
        evs = self.spooled()
        self.assertEqual([e["type"] for e in evs], ["update.testing", "update.applied", "hub.restart_requested",
                                                    "hub.restart_requested", "owner.notify", "update.checked"])
        self.assertEqual(evs[1]["data"]["flaky"], flaky)
        self.commit("core.py", "a = 3\n", "one more")
        self.assertEqual(self.updater().check(), "applied")   # a run without a retry: no field
        self.assertNotIn("flaky", [e for e in self.spooled() if e["type"] == "update.applied"][0]["data"])

    def test_a_hub_without_its_own_commits_moves_straight_to_the_release(self):
        self.g(self.mine, "reset", "-q", "--hard", "origin/main")
        up = self.commit("core.py", "a = 2\n", "new feature")
        self.assertEqual(self.updater().check(), "applied")
        self.assertEqual(self.g(self.mine, "rev-parse", "HEAD"), up)

    def test_a_conflict_leaves_the_live_tree_byte_for_byte(self):
        self.write(self.mine, "core.py", "a = 'mine'\n")
        self.g(self.mine, "commit", "-qam", "personal edit of core")
        live = self.tree(self.mine)
        self.commit("core.py", "a = 2\n", "new feature")
        self.assertEqual(self.updater().check(), "conflict")
        self.assertEqual(self.tree(self.mine), live)
        self.assertEqual(self.copies(), ([], 0))
        (failed,) = [e for e in self.spooled() if e["type"] == "update.failed"]
        self.assertEqual(failed["data"]["files"], ["core.py"])

    def test_red_tests_leave_the_live_tree_byte_for_byte(self):
        live = self.tree(self.mine)
        self.commit("core.py", "a = 2\n", "new feature")
        self.assertEqual(self.updater(lambda repo: (False, "FAILED (failures=1)")).check(), "red")
        self.assertEqual(self.tree(self.mine), live)                          # nothing to roll back: neither revert nor reset
        self.assertEqual(self.copies(), ([], 0))
        self.assertIn("message.received", [e["type"] for e in self.spooled()])

    def test_a_commit_in_the_live_tree_during_the_tests_is_moved_and_nothing_is_applied(self):
        self.commit("core.py", "a = 2\n", "new feature")

        def tests(repo):
            self.write(self.mine, "work.py", "x = 1\n")
            self.g(self.mine, "add", "-A")
            self.g(self.mine, "commit", "-qm", "work merged while tests ran")
            return True, "OK"
        u = self.updater(tests)
        self.assertEqual(u.check(), "moved")
        committed = self.g(self.mine, "rev-parse", "HEAD")
        self.assertEqual(self.g(self.mine, "log", "-1", "--format=%s"), "work merged while tests ran")
        self.assertEqual(self.read(self.mine, "core.py"), "a = 1\n")           # the checked one is not applied
        self.assertEqual(self.copies(), ([], 0))
        self.assertNotIn("update.applied", [e["type"] for e in self.spooled()])
        from modules.update import update
        self.assertIn("moved", update.RETRY_SOON)                              # the next pass is after BUSY_RETRY
        self.assertEqual(self.updater().check(), "applied")                    # and it installs over the new one
        self.assertTrue(subprocess.run(["git", "merge-base", "--is-ancestor", committed, "HEAD"],
                                       cwd=self.mine).returncode == 0)

    def test_edits_left_in_the_live_tree_during_the_tests_are_moved(self):
        self.commit("core.py", "a = 2\n", "new feature")

        def tests(repo):
            self.write(self.mine, "personal.py", "dirty = 1\n")
            return True, "OK"
        self.assertEqual(self.updater(tests).check(), "moved")
        self.assertEqual(self.read(self.mine, "personal.py"), "dirty = 1\n")
        self.assertEqual(self.read(self.mine, "core.py"), "a = 1\n")

    def test_a_fast_forward_refused_for_another_reason_is_blocked_with_what_git_said(self):
        # review: a fast-forward refusal not caused by a file (a leftover index.lock from a crashed git command) is not
        # "a file is in the way" and not
        # a retry of the whole run every 5 minutes: blocked with git's text, said once, like a conflict
        self.commit("core.py", "a = 2\n", "new feature")
        lock = os.path.join(self.mine, ".git", "index.lock")

        def tests(repo):
            open(lock, "w").close()
            return True, "OK"
        self.assertEqual(self.updater(tests).check(), "blocked")
        evs = self.spooled()
        (failed,) = [e for e in evs if e["type"] == "update.failed"]
        self.assertEqual((failed["data"]["why"], failed["data"]["files"]), ("blocked", []))
        (msg,) = [e for e in evs if e["type"] == "message.received"]
        self.assertIn("index.lock", msg["data"]["text"])
        self.assertNotIn("the file is not in git", msg["data"]["text"])
        from modules.update import update
        self.assertNotIn("blocked", update.RETRY_SOON)
        os.unlink(lock)
        self.assertEqual(self.updater().check(), "applied")

    def test_a_finished_check_without_a_result_event_clears_testing_on_the_page(self):
        # review: 'moved' comes after update.testing but without applied and failed: the page showed "tests running" for
        # 30 min
        from core import reducer
        st = reducer.empty()
        for seq, (kind, data) in enumerate([("update.testing", {"from": "a", "upstream": "b"}),
                                            ("update.checked", {"result": "moved", "behind": 1})], 1):
            st, _, why = reducer.reduce(st, {"seq": seq, "ts": seq, "type": kind, "entity": None,
                                             "data": {**data, "channel": "cli", "owner": True}})
            self.assertIsNone(why)
        self.assertNotIn("update_testing", st)
        self.assertEqual(st["update_check"]["result"], "moved")

    def test_a_cleanup_that_fails_after_the_install_does_not_hide_it(self):
        # review: cleaning up the copy in finally was unguarded: the 'applied' outcome was replaced by an exception and
        # "error" on the page
        from unittest import mock
        from modules.update import update
        self.commit("core.py", "a = 2\n", "new feature")
        with mock.patch.object(update.Updater, "drop_copies", side_effect=subprocess.TimeoutExpired("git", 120)):
            self.assertEqual(self.updater().check(), "applied")
        self.assertEqual(self.read(self.mine, "core.py"), "a = 2\n")

    def test_an_untracked_file_named_like_a_new_folder_is_blocked_before_the_tests(self):
        # critic: a release adds dir/new.py, while the hub folder has an untracked file dir
        os.makedirs(os.path.join(self.author, "tools2"))
        self.commit("tools2/new.py", "x = 1\n", "release adds a folder")
        self.write(self.mine, "tools2", "not a folder\n")
        runs = []
        self.assertEqual(self.updater(lambda repo: runs.append(repo) or (True, "OK")).check(), "blocked")
        self.assertEqual(runs, [])
        (failed,) = [e for e in self.spooled() if e["type"] == "update.failed"]
        self.assertEqual(failed["data"]["files"], ["tools2"])

    def test_the_merge_takes_the_checked_commit_not_a_ref_that_moved_meanwhile(self):
        # critic: the "forward only" check goes by commit, but the origin/main ref was merged; a manual fetch in between
        # would slip in something unchecked
        from unittest import mock
        from modules.update import update
        checked = self.commit("core.py", "a = 2\n", "checked")
        later = self.commit("core.py", "a = 3\n", "pushed after the check")
        real = update.Updater._forward

        def forward(u, head_up):
            self.g(self.mine, "update-ref", "refs/remotes/origin/main", later)   # a manual fetch at this minute
            return real(u, head_up)
        self.g(self.mine, "fetch", "-q", "origin")
        self.g(self.mine, "update-ref", "refs/remotes/origin/main", checked)
        u = self.updater()
        u.moved_tag = None
        with mock.patch.object(update.Updater, "_forward", forward), \
                mock.patch.object(u, "_target", lambda remote, branch, **kw: (setattr(u, "ref", "origin/main"),
                                                                        ("origin/main", "origin/main"))[1]):
            self.assertEqual(u.check(), "applied")
        self.assertEqual(self.read(self.mine, "core.py"), "a = 2\n")
        self.assertEqual(u.installed(), checked)

    def test_installed_is_written_before_the_fast_forward(self):
        # critic: interrupted between the fast-forward and the record: HEAD is on the release, but installed is the old
        # one
        from unittest import mock
        from modules.update import update
        up = self.commit("core.py", "a = 2\n", "new feature")
        seen = []
        real = subprocess.run

        def run(cmd, *a, **kw):
            if cmd[:3] == ["git", "merge", "--ff-only"]:
                seen.append(update.Updater(self.mine, self.state, {}).installed())
            return real(cmd, *a, **kw)
        with mock.patch.object(update.subprocess, "run", run):
            self.assertEqual(self.updater().check(), "applied")
        self.assertEqual(seen, [up])

    @unittest.skipIf(os.name == "nt", "umask is POSIX only")
    def test_code_files_get_the_users_umask_not_the_hubs(self):
        # hub processes live with umask 077 (their folders are owner-only); code files written by the update's git get
        # the
        # user's umask, as after his own git pull
        from unittest import mock
        from core import perms
        from modules.update import update
        self.commit("added.py", "x = 1\n", "release adds a file")
        modes = []

        def tests(repo):
            modes.append(os.stat(os.path.join(repo, "added.py")).st_mode & 0o777)
            return True, "OK"
        was = os.umask(0o077)
        try:
            with mock.patch.dict(os.environ, {perms.UMASK_ENV: "022"}):   # the user's umask, recorded by the hub
                self.assertEqual(update.code_umask(), 0o022)
                self.assertEqual(self.updater(tests).check(), "applied")
        finally:
            os.umask(was)
        self.assertEqual(modes, [0o644])                                        # in the copy
        self.assertEqual(os.stat(os.path.join(self.mine, "added.py")).st_mode & 0o777, 0o644)   # in the live tree
        with mock.patch.dict(os.environ):
            os.environ.pop(perms.UMASK_ENV, None)
            self.assertEqual(update.code_umask(), -1)                          # unknown: as the process has it

    def test_an_untracked_file_in_the_way_is_blocked_with_its_path(self):
        self.commit("new.py", "x = 'release'\n", "release adds a file")
        self.write(self.mine, "new.py", "x = 'local, not committed'\n")
        live = self.tree(self.mine)
        runs = []
        self.assertEqual(self.updater(lambda repo: runs.append(repo) or (True, "OK")).check(), "blocked")
        self.assertEqual(runs, [])                                             # visible before the tests: they were not run
        self.assertEqual(self.tree(self.mine), live)
        evs = self.spooled()
        (failed,) = [e for e in evs if e["type"] == "update.failed"]
        self.assertEqual((failed["data"]["why"], failed["data"]["files"]), ("blocked", ["new.py"]))
        (msg,) = [e for e in evs if e["type"] == "message.received"]
        self.assertIn("new.py", msg["data"]["text"])
        self.assertEqual(self.copies(), ([], 0))

    def test_a_check_killed_during_the_tests_leaves_the_live_tree_and_the_next_start_removes_the_copy(self):
        self.commit("core.py", "a = 2\n", "new feature")
        live = self.tree(self.mine)
        started = os.path.join(self.root, "started")
        code = (f"import sys, time; sys.path.insert(0, {REPO!r}); import tests\n"
                "from modules.update import update\n"
                f"def run(repo):\n    open({started!r}, 'w').write(repo)\n    time.sleep(120)\n"
                f"update.Updater({self.mine!r}, {self.state!r}, {{'update': {{'enabled': True, 'channel': 'main'}}}},"
                " tests=run).check()\n")
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        deadline = time.time() + 60
        while not os.path.exists(started) and time.time() < deadline and p.poll() is None:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(started))
        p.kill()
        p.wait()
        self.assertEqual(self.tree(self.mine), live)                           # killed mid-tests: the live tree is unchanged
        self.assertEqual(self.copies()[1], 1)
        self.updater().recover()
        self.assertEqual(self.copies(), ([], 0))
        self.assertEqual(self.tree(self.mine), live)

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "folder permissions: Windows keeps files differently (the rig), root does not notice them")
    def test_a_copy_that_cannot_be_removed_now_is_removed_on_the_next_pass(self):
        # Windows: the tests' Chrome holds the copy's files, cleanup fails; the update installs and the copy is removed
        # later
        self.commit("core.py", "a = 2\n", "new feature")
        held = []

        def tests(repo):
            os.makedirs(os.path.join(repo, "busy"))
            self.write(repo, os.path.join("busy", "file"), "x")
            os.chmod(os.path.join(repo, "busy"), 0o500)
            held.append(os.path.join(repo, "busy"))
            return True, "OK"
        self.addCleanup(lambda: held and os.path.isdir(held[0]) and os.chmod(held[0], 0o700))
        self.assertEqual(self.updater(tests).check(), "applied")
        self.assertEqual(self.read(self.mine, "core.py"), "a = 2\n")
        self.assertEqual(len(self.copies()[0]), 1)                             # not removed: recorded
        os.chmod(held[0], 0o700)
        self.assertEqual(self.updater().check(), "current")
        self.assertEqual(self.copies(), ([], 0))

    def test_only_copies_inside_the_update_folder_are_removed(self):
        from modules.update import update
        keep = os.path.join(self.root, "not-mine")
        os.makedirs(keep)
        os.makedirs(os.path.join(self.state, "update"))
        with open(os.path.join(self.state, update.WORK_LIST), "w") as f:
            json.dump([keep, os.path.join(self.state, "update", "..", "x"), self.mine], f)
        self.updater().recover()
        self.assertTrue(os.path.isdir(keep))
        self.assertTrue(os.path.isdir(self.mine))

    def test_the_shared_code_is_not_marked_as_testing_any_more(self):
        # the live tree stays the same during the tests: a second hub on the same folder has nothing to wait for
        from modules.update import update
        self.commit("core.py", "a = 2\n", "new feature")
        seen = []
        u = self.updater(lambda repo: (seen.append(os.path.exists(update.testing_marker(self.mine))), (True, "OK"))[1])
        self.assertEqual(u.check(), "applied")
        self.assertEqual(seen, [False])
        self.assertFalse(os.path.exists(os.path.join(self.state, update.TESTING_FLAG)))

    # --- a sleeping machine (2026-11-03: a Mac in maintenance wakes ran the suite for 3 hours, timing tests went red) ---

    def test_a_scheduled_install_does_not_start_while_the_mac_is_in_a_dark_wake(self):
        live = self.tree(self.mine)
        self.commit("core.py", "a = 2\n", "new feature")
        ran = []
        u = self.updater(lambda repo: (ran.append(repo), (True, "OK"))[1], dark=lambda: True)
        self.assertEqual(u.check(), "asleep")
        self.assertEqual(ran, [])
        self.assertEqual(self.tree(self.mine), live)
        self.assertEqual(self.copies(), ([], 0))
        evs = self.spooled()
        self.assertEqual([e["type"] for e in evs], ["update.checked"])
        self.assertEqual((evs[0]["data"]["result"], evs[0]["data"]["behind"]), ("asleep", 1))
        from modules.update import update
        self.assertIn("asleep", update.RETRY_SOON)                            # the next pass is after BUSY_RETRY, not in an hour

    def test_the_owner_asking_installs_even_in_a_dark_wake(self):
        # "Install" and `hub update` mean someone is there; the sleep during the run still voids it
        self.commit("core.py", "a = 2\n", "new feature")
        self.assertEqual(self.updater(dark=lambda: True).check(manual=True), "applied")

    def test_looking_for_a_new_version_works_in_a_dark_wake(self):
        self.commit("core.py", "a = 2\n", "new feature")
        self.assertEqual(self.updater(dark=lambda: True).check(apply=False), "available")

    def slept(self, gap):
        """A clock (wall, monotonic): the tests take 4 s, and the machine sleeps gap seconds during them; macOS and
        Linux do not count sleep in time.monotonic."""
        marks = iter([(1000.0, 50.0), (1004.0 + gap, 54.0)])
        return lambda: next(marks)

    def test_red_tests_after_a_sleep_are_not_a_result(self):
        from modules.update import update
        live = self.tree(self.mine)
        self.commit("core.py", "a = 2\n", "new feature")
        red = lambda repo: (False, "FAIL: test_x (tests.test_a.A.test_x)\n898 s not less than 20\nFAILED (failures=1)")
        self.assertEqual(self.updater(red, clock=self.slept(894)).check(), "asleep")
        self.assertEqual(self.tree(self.mine), live)
        self.assertEqual(self.copies(), ([], 0))
        self.assertEqual([e["type"] for e in self.spooled()], ["update.testing", "update.checked"])   # no failed, no message
        self.assertFalse(os.path.exists(os.path.join(self.state, update.RED_FLAG)))   # not counted toward "red twice"
        self.assertEqual(self.updater(clock=self.slept(0)).check(), "applied")   # the next pass checks again

    def test_tests_that_did_not_finish_after_a_sleep_are_not_a_result(self):
        self.commit("core.py", "a = 2\n", "new feature")

        def died(repo):
            raise TimeoutError("tools/test.py")
        self.assertEqual(self.updater(died, clock=self.slept(900)).check(), "asleep")
        self.assertNotIn("message.received", [e["type"] for e in self.spooled()])

    def test_green_tests_after_a_sleep_are_not_applied(self):
        live = self.tree(self.mine)
        self.commit("core.py", "a = 2\n", "new feature")
        self.assertEqual(self.updater(clock=self.slept(900)).check(), "asleep")
        self.assertEqual(self.tree(self.mine), live)

    def test_a_clock_step_under_a_minute_is_not_a_sleep(self):
        # NTP corrects the wall clock by seconds: red stays red
        self.commit("core.py", "a = 2\n", "new feature")
        self.assertEqual(self.updater(lambda repo: (False, "FAILED (failures=1)"), clock=self.slept(30)).check(), "red")

    # --- forward only ---

    def test_a_rewritten_history_is_refused_and_the_tree_is_not_touched(self):
        self.commit("core.py", "a = 2\n", "release one")
        self.assertEqual(self.updater().check(), "applied")
        self.spooled()
        self.g(self.author, "reset", "-q", "--hard", "HEAD~1")
        self.commit("core.py", "a = 'evil'\n", "rewritten", force=True)
        live = self.tree(self.mine)
        self.assertEqual(self.updater().check(), "not-descendant")
        self.assertEqual(self.tree(self.mine), live)
        evs = self.spooled()
        self.assertEqual(sorted(e["type"] for e in evs), ["message.received", "owner.notify", "update.checked"])
        self.assertEqual(self.updater().check(), "not-descendant")
        self.assertEqual([e["type"] for e in self.spooled()], ["update.checked"])   # said once
        self.commit("core.py", "a = 'evil 2'\n", "more on the rewritten history")
        self.assertEqual(self.updater().check(), "not-descendant")
        self.assertEqual([e["type"] for e in self.spooled()], ["update.checked"])   # and not on every new commit
        self.g(self.author, "reset", "-q", "--hard", "HEAD~2")                     # rewritten again, from another point
        self.g(self.author, "commit", "-q", "--amend", "-m", "start, rewritten again")
        self.g(self.author, "push", "-q", "-f", "origin", "main")
        self.assertEqual(self.updater().check(), "not-descendant")
        self.assertEqual(sorted(e["type"] for e in self.spooled()), ["message.received", "owner.notify", "update.checked"])
        self.assertEqual(self.updater().check(apply=False), "not-descendant")       # and a check without installing

    def test_the_first_run_without_the_file_takes_what_is_installed_before_the_fetch(self):
        start = self.g(self.mine, "rev-parse", "origin/main")
        self.commit("core.py", "a = 'evil'\n", "pushed over a rewrite")
        self.g(self.author, "reset", "-q", "--hard", "HEAD~1")
        self.g(self.author, "commit", "-q", "--amend", "-m", "start, rewritten")
        self.g(self.author, "push", "-q", "-f", "origin", "main")   # the shared repo was rewritten before the first check
        self.assertEqual(self.updater().check(), "not-descendant")
        self.assertEqual(self.updater().installed(), start)                # what the hub already installed: merge-base before fetch

    def test_the_first_run_without_the_file_installs_the_next_release(self):
        up = self.commit("core.py", "a = 2\n", "next")
        self.assertEqual(self.updater().check(), "applied")
        self.assertEqual(self.updater().installed(), up)                   # the release commit, not our merge commit

    def test_a_moved_tag_is_refused(self):
        self.commit("core.py", "a = 2\n", "release", tag="v1.0.0")
        self.assertEqual(self.updater(app=True).check(), "applied")
        self.commit("core.py", "a = 3\n", "release two", tag="v1.1.0")
        self.assertEqual(self.updater(app=True).check(apply=False), "available")   # saw v1.1.0
        self.commit("core.py", "a = 'evil'\n", "after release two")
        self.g(self.author, "tag", "-f", "-a", "-m", "moved", "v1.1.0")
        self.g(self.author, "push", "-q", "-f", "origin", "refs/tags/v1.1.0")
        live = self.tree(self.mine)
        self.spooled()
        self.assertEqual(self.updater(app=True).check(), "tag-moved")
        self.assertEqual(self.tree(self.mine), live)
        self.assertIn("owner.notify", [e["type"] for e in self.spooled()])

    def test_the_app_clone_without_a_release_tag_installs_nothing(self):
        self.commit("core.py", "a = 2\n", "new feature")
        live = self.tree(self.mine)
        self.assertEqual(self.updater(app=True).check(), "no-release")   # no tags: it does not take main's head
        self.g(self.author, "checkout", "-q", "-b", "exp")
        self.write(self.author, "core.py", "a = 'experiment'\n")
        self.g(self.author, "commit", "-qam", "experiment")
        self.g(self.author, "tag", "-a", "-m", "off main", "v9.0.0")
        self.g(self.author, "push", "-q", "origin", "refs/tags/v9.0.0")
        self.assertEqual(self.updater(app=True).check(), "no-release")   # the tag is only outside main
        self.g(self.author, "checkout", "-q", "main")
        self.commit("core.py", "a = 3\n", "release", tag="v1.0.0")
        self.assertEqual(self.updater(app=True).check(apply=False), "available")   # fetched the tag
        self.assertTrue(self.g(self.mine, "for-each-ref", "refs/hub-update/tags/v1.0.0"))
        self.g(self.author, "push", "-q", "origin", ":refs/tags/v1.0.0")         # the last tag was removed
        self.assertEqual(self.updater(app=True).check(), "no-release")
        self.assertEqual(self.tree(self.mine), live)
        results = [e["data"]["result"] for e in self.spooled() if e["type"] == "update.checked"]
        self.assertEqual(results, ["no-release", "no-release", "available", "no-release"])


if __name__ == "__main__":
    unittest.main()
