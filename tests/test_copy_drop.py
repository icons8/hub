"""A handed-in executor's finished working copy goes after a term (hub.json executors.drop_copy_after_hours, off by
default): the session, its conversation and its branch stay, and a wake brings the copy back from the branch before
the spawn — otherwise Claude Code would start in the repo's root. On a real git, the daemon with a stub host."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import platforms
from core import executors, journal
from core.daemon import Hub
from hosts import worktree

OWNER = {"channel": "cli", "owner": True}
HOUR = 3600


def git(cwd, *a):
    return subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True,
                          capture_output=True, text=True).stdout.strip()


def repo(tmp, name="repo"):
    path = os.path.join(tmp, name)
    git(tmp, "init", "-q", "-b", "main", path)
    with open(os.path.join(path, "a.txt"), "w") as f:
        f.write("a\n")
    with open(os.path.join(path, ".gitignore"), "w") as f:
        f.write("node_modules/\n.env\n.data/\n")
    git(path, "add", "a.txt", ".gitignore")
    git(path, "commit", "-q", "-m", "init")
    return path


class Drop(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cpdrop-"))
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.repo = repo(self.tmp)
        self.now = 1_000_000.0
        self.h = Hub(os.path.join(self.tmp, "state"), types.SimpleNamespace(), clock=lambda: self.now)
        self.actions = []
        self.h.execute = self.actions.append
        rec = self.h.record
        rec("session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **OWNER})
        rec("owner.settings", None, {"parallel": 10, "channel": "core"})
        rec("session.requested", "maps", {"cwd": self.repo, "session_id": "s-m", "role": "agent", **OWNER})
        rec("session.started", "maps", {})
        rec("session.folder", "maps", {"channel": "core", "git": "ok", "dirty": 0}, src="core")
        self.h.config = {"executors": {"drop_copy_after_hours": 2}}
        # no process stands in the test's copies; lsof of the machine is not the test's business
        patch = mock.patch.object(worktree, "cwd_folders", return_value=set())
        patch.start()
        self.addCleanup(patch.stop)

    def raise_(self, name):
        self.h.record("task.run_requested", name, {"task": "do it", "channel": "session", "owner": False,
                                                   "sender": "maps"})
        self.h.run_tasks()
        for _ in range(100):
            job = self.h.preparing.get(name)
            if not job:
                break
            job["done"].wait(30)
            self.h.run_tasks()
        sess = self.h.state["sessions"][name]
        self.assertTrue(os.path.isdir(sess["worktree"]["path"]), sess)
        return sess

    def retire(self, name, hours_ago):
        """Handed in and asleep: the state the core's retirement leaves (close_requested with retire, the exit)."""
        sess = self.h.state["sessions"][name]
        sess["state"], sess["since"] = "retired", self.now - hours_ago * HOUR

    def clean(self):
        """Two ticks: git in the background (clean_worktrees), the next tick writes the facts."""
        self.h.clean_worktrees()
        if self.h.cleaner:
            self.h.cleaner.join(60)
        self.h.clean_worktrees()

    def facts(self, kind):
        return [e for e in journal.read(os.path.join(self.h.state_dir, "journal.jsonl")) if e["type"] == kind]

    def spawn_of(self, name):
        got = [a for a in self.actions if a.get("do") == "spawn" and a.get("session") == name]
        self.assertTrue(got, self.actions)
        return got[-1]

    def bring_back(self, action):
        """The daemon's spawn gate for a dropped copy, until it lets the spawn go (False) or refuses it."""
        for _ in range(300):
            self.h.delayed.clear()
            if not self.h.wait_copy(action):
                return True
            job = self.h.restoring.get(action["session"])
            if job is None:
                return False   # refused: the fact and host.error are in the journal
            job["done"].wait(30)
        self.fail("the copy did not come back")

    # the drop
    def test_a_finished_copy_of_a_handed_in_executor_goes_after_the_term_and_its_branch_stays(self):
        x = self.raise_("maps-x")
        path, branch = x["worktree"]["path"], x["worktree"]["branch"]
        os.makedirs(os.path.join(path, "node_modules", "pkg"))   # build junk: not a reason to keep the copy
        with open(os.path.join(path, "node_modules", "pkg", "i.js"), "w") as f:
            f.write("x" * 5000)
        self.retire("maps-x", 3)
        self.clean()
        self.assertFalse(os.path.exists(path))
        self.assertIn(branch, git(self.repo, "branch", "--list", branch))
        (f,) = self.facts("session.worktree_dropped")
        self.assertTrue(f["data"]["ok"], f)
        self.assertGreater(f["data"]["bytes"], 5000)
        self.assertEqual((f["data"]["path"], f["data"]["after_hours"]), (path, 2))
        sess = self.h.state["sessions"]["maps-x"]
        self.assertEqual(sess["state"], "retired")   # the session and its conversation stay
        self.assertTrue(sess["worktree"]["dropped"])
        self.clean()   # nothing more to do: no second fact
        self.assertEqual(len(self.facts("session.worktree_dropped")), 1)

    def test_removing_the_session_later_takes_its_merged_branch_as_before(self):
        # the removal of a session takes its branch when nothing in it is unmerged (git branch -d); a dropped copy's
        # session must not leave its branch behind for good
        x = self.raise_("maps-x")
        branch = x["worktree"]["branch"]
        self.retire("maps-x", 3)
        self.clean()
        self.assertIn(branch, git(self.repo, "branch", "--list", branch))
        sess = self.h.state["sessions"]["maps-x"]
        sess["state"], sess["since"] = "removed", self.now
        self.clean()
        self.assertEqual(git(self.repo, "branch", "--list", branch), "")
        self.assertTrue(self.h.state["sessions"]["maps-x"]["worktree"]["cleaned"])

    def test_removing_the_session_after_its_branch_went_by_hand_says_nothing_was_kept(self):
        x = self.raise_("maps-x")
        branch = x["worktree"]["branch"]
        self.retire("maps-x", 3)
        self.clean()
        git(self.repo, "branch", "-D", branch)
        sess = self.h.state["sessions"]["maps-x"]
        sess["state"], sess["since"] = "removed", self.now
        self.clean()
        (f,) = self.facts("session.worktree_removed")
        self.assertTrue(f["data"]["ok"])
        self.assertNotIn(branch, f["data"]["why"])   # not "the branch kept: it has commits merged nowhere"

    def test_off_by_default_and_before_the_term(self):
        x = self.raise_("maps-x")
        self.retire("maps-x", 1)
        self.clean()
        self.assertTrue(os.path.isdir(x["worktree"]["path"]))
        self.h.config = {}
        self.retire("maps-x", 100)
        self.clean()
        self.assertTrue(os.path.isdir(x["worktree"]["path"]))
        self.assertEqual(self.facts("session.worktree_dropped"), [])

    def test_unfinished_copies_stay_with_the_reason_once_per_sleep(self):
        cases = {}
        a = self.raise_("maps-a")   # a commit in no remote and not in main
        with open(os.path.join(a["cwd"], "b.txt"), "w") as f:
            f.write("b\n")
        git(a["cwd"], "add", "b.txt")
        git(a["cwd"], "commit", "-q", "-m", "work")
        cases["maps-a"] = "not in main"
        b = self.raise_("maps-b")   # an edit not committed
        with open(os.path.join(b["cwd"], "a.txt"), "a") as f:
            f.write("more\n")
        cases["maps-b"] = "uncommitted"
        c = self.raise_("maps-c")   # an ignored file that is not build junk and not the repo's own
        os.makedirs(os.path.join(c["cwd"], ".data"))
        with open(os.path.join(c["cwd"], ".data", "db"), "w") as f:
            f.write("rows\n")
        cases["maps-c"] = "ignored file"
        d = self.raise_("maps-d")   # a new file
        with open(os.path.join(d["cwd"], "new.txt"), "w") as f:
            f.write("n\n")
        cases["maps-d"] = "uncommitted"
        for name in cases:
            self.retire(name, 3)
        self.clean()
        got = {f["entity"]: f["data"] for f in self.facts("session.worktree_dropped")}
        self.assertEqual(set(got), set(cases))
        for name, words in cases.items():
            self.assertFalse(got[name]["ok"], got[name])
            self.assertIn(words, got[name]["why"], name)
            self.assertTrue(os.path.isdir(self.h.state["sessions"][name]["worktree"]["path"]), name)
        self.clean()   # judged for this sleep: not again
        self.assertEqual(len(self.facts("session.worktree_dropped")), 4)

    def test_the_repos_own_ignored_file_does_not_hold_the_copy(self):
        with open(os.path.join(self.repo, ".env"), "w") as f:
            f.write("K=1\n")
        x = self.raise_("maps-x")
        with open(os.path.join(x["cwd"], ".env"), "w") as f:   # .worktreeinclude would copy it so
            f.write("K=1\n")
        self.retire("maps-x", 3)
        self.clean()
        self.assertFalse(os.path.exists(x["cwd"]))
        y = self.raise_("maps-y")
        with open(os.path.join(y["cwd"], ".env"), "w") as f:   # its own value: lost with the copy — kept
            f.write("K=2\n")
        self.retire("maps-y", 3)
        self.clean()
        self.assertTrue(os.path.isdir(y["cwd"]))

    def test_the_same_settings_written_anew_do_not_hold_the_copy(self):
        with open(os.path.join(self.repo, ".gitignore"), "a") as f:
            f.write(".claude/settings.local.json\n")
        git(self.repo, "commit", "-q", "-am", "ignore settings")
        os.makedirs(os.path.join(self.repo, ".claude"), exist_ok=True)
        with open(os.path.join(self.repo, ".claude", "settings.local.json"), "w") as f:
            f.write('{"enabledMcpjsonServers": ["a"]}')
        x = self.raise_("maps-x")
        with open(os.path.join(x["cwd"], ".claude", "settings.local.json"), "w") as f:
            f.write('{\n  "enabledMcpjsonServers": [\n    "a"\n  ]\n}\n')
        self.retire("maps-x", 3)
        self.clean()
        self.assertFalse(os.path.exists(x["cwd"]), self.facts("session.worktree_dropped"))

    def test_a_program_in_the_copy_or_an_unknown_answer_keeps_it(self):
        x = self.raise_("maps-x")
        self.retire("maps-x", 3)
        with mock.patch.object(worktree, "cwd_folders", return_value={os.path.join(x["cwd"], "sub")}):
            self.clean()
        self.assertTrue(os.path.isdir(x["cwd"]))
        self.assertIn("a program works", self.facts("session.worktree_dropped")[-1]["data"]["why"])
        y = self.raise_("maps-y")
        self.retire("maps-y", 3)
        with mock.patch.object(worktree, "cwd_folders", return_value=None):
            self.clean()
        self.assertTrue(os.path.isdir(y["cwd"]))

    def test_a_live_session_in_the_copy_holds_it_without_a_fact(self):
        x = self.raise_("maps-x")
        self.retire("maps-x", 3)
        self.h.record("session.requested", "helper", {"cwd": x["cwd"], "session_id": "s-hp", **OWNER})
        self.clean()
        self.assertTrue(os.path.isdir(x["cwd"]))
        self.assertEqual(self.facts("session.worktree_dropped"), [])

    def test_a_git_failure_keeps_the_copy(self):
        x = self.raise_("maps-x")
        self.retire("maps-x", 3)
        real = worktree._git

        def broken(cwd, *args, **kw):
            if args[:1] == ("rev-list",):
                return 128, "", "fatal: bad object"
            return real(cwd, *args, **kw)
        with mock.patch.object(worktree, "_git", broken):
            self.clean()
        self.assertTrue(os.path.isdir(x["cwd"]))
        (f,) = self.facts("session.worktree_dropped")
        self.assertFalse(f["data"]["ok"])
        self.assertIn("bad object", f["data"]["why"])

    def test_a_finished_copy_left_locked_initializing_by_a_killed_git_still_goes(self):
        # a copy brought back by a worktree add that was killed halfway kept git's own lock "initializing";
        # every drop of the finished copy failed on it ("cannot remove a locked working tree") and 4 GB stayed
        x = self.raise_("maps-x")
        path = x["worktree"]["path"]
        git(self.repo, "worktree", "lock", "--reason", "initializing", path)
        self.retire("maps-x", 3)
        self.clean()
        self.assertFalse(os.path.exists(path))
        (f,) = self.facts("session.worktree_dropped")
        self.assertTrue(f["data"]["ok"], f)

    def test_a_lock_someone_put_on_purpose_keeps_the_copy(self):
        x = self.raise_("maps-x")
        path = x["worktree"]["path"]
        git(self.repo, "worktree", "lock", "--reason", "on a stick", path)
        self.retire("maps-x", 3)
        self.clean()
        self.assertTrue(os.path.isdir(path))
        (f,) = self.facts("session.worktree_dropped")
        self.assertFalse(f["data"]["ok"], f)

    # a low disk (executors.drop_copy_below_gb): a burst of big copies handed in within hours can fill the
    # disk before their term
    def low_disk(self, gb):
        patch = mock.patch.object(worktree, "free_space", return_value=int(gb * worktree.GB))
        patch.start()
        self.addCleanup(patch.stop)

    def notices(self):
        return [f["data"] for f in self.facts("owner.notify") if f["data"].get("id") == "copies-disk-low"]

    def test_a_low_disk_drops_finished_copies_before_the_term_and_tells_the_owner_one_line(self):
        a = self.raise_("maps-a")
        b = self.raise_("maps-b")   # unsaved work: stays even on a full disk
        with open(os.path.join(b["cwd"], "a.txt"), "a") as f:
            f.write("more\n")
        c = self.raise_("maps-c")   # not handed in: never
        for name in ("maps-a", "maps-b"):
            self.retire(name, 0.2)
        self.low_disk(40)
        self.clean()
        self.assertTrue(os.path.isdir(a["worktree"]["path"]))   # room enough: the term holds
        self.assertEqual(self.facts("session.worktree_dropped"), [])
        self.low_disk(5)
        self.clean()
        self.assertFalse(os.path.exists(a["worktree"]["path"]))
        self.assertTrue(os.path.isdir(b["worktree"]["path"]))
        self.assertTrue(os.path.isdir(c["worktree"]["path"]))
        got = {f["entity"]: f["data"] for f in self.facts("session.worktree_dropped")}
        self.assertEqual(set(got), {"maps-a", "maps-b"})
        self.assertTrue(got["maps-a"]["ok"] and got["maps-a"]["low_disk"], got["maps-a"])
        self.assertFalse(got["maps-b"]["ok"])
        (n,) = self.notices()
        self.assertIn("1", n["text"])
        self.assertEqual((n["channel"], n["owner"]), ("core", False))
        self.clean()   # nothing new: no second notice
        self.assertEqual(len(self.notices()), 1)
        d = self.raise_("maps-d")   # still low: the next one goes, the same notice counts both
        self.retire("maps-d", 0.1)
        self.clean()
        self.assertFalse(os.path.exists(d["worktree"]["path"]))
        last = self.notices()[-1]
        self.assertIn("2", last["text"])

    def test_a_full_disk_drops_early_too(self):
        a = self.raise_("maps-a")
        self.retire("maps-a", 0.2)
        self.low_disk(0)   # not a byte free: the most urgent case, not "room enough"
        self.clean()
        self.assertFalse(os.path.exists(a["worktree"]["path"]))

    def test_the_threshold_has_a_default_zero_turns_it_off_and_it_needs_the_drop_on(self):
        a = self.raise_("maps-a")
        self.retire("maps-a", 0.2)
        self.low_disk(1)
        self.h.config = {"executors": {"drop_copy_after_hours": 2, "drop_copy_below_gb": 0}}
        self.clean()
        self.h.config = {"executors": {}}
        self.clean()
        self.assertTrue(os.path.isdir(a["worktree"]["path"]))
        self.assertEqual(self.facts("session.worktree_dropped"), [])
        self.h.config = {"executors": {"drop_copy_after_hours": 2}}   # the default threshold
        self.low_disk(executors.DROP_BELOW_GB - 1)
        self.clean()
        self.assertFalse(os.path.exists(a["worktree"]["path"]))

    def test_at_most_a_batch_per_pass(self):
        for i in range(executors.DROP_BATCH + 2):
            self.raise_(f"maps-{i}")
            self.retire(f"maps-{i}", 3)
        self.clean()
        self.assertEqual(len(self.facts("session.worktree_dropped")), executors.DROP_BATCH)
        self.clean()
        self.assertEqual(len(self.facts("session.worktree_dropped")), executors.DROP_BATCH + 2)

    def test_an_own_folder_with_only_claude_settings_goes_with_files_it_stays(self):
        for name in ("solo-a", "solo-b"):
            cwd = os.path.join(self.h.state_dir, "workspaces", name)
            os.makedirs(os.path.join(cwd, ".claude"))
            self.h.record("session.requested", name, {"cwd": cwd, "session_id": f"s-{name}", "executor": True,
                                                      "raised_by": "maps", **OWNER})
            self.retire(name, 3)
        with open(os.path.join(self.h.state_dir, "workspaces", "solo-b", "notes.md"), "w") as f:
            f.write("n\n")
        self.clean()
        self.assertFalse(os.path.exists(os.path.join(self.h.state_dir, "workspaces", "solo-a")))
        self.assertTrue(os.path.isdir(os.path.join(self.h.state_dir, "workspaces", "solo-b")))
        got = {f["entity"]: f["data"]["ok"] for f in self.facts("session.worktree_dropped")}
        self.assertEqual(got, {"solo-a": True, "solo-b": False})

    def test_an_own_folder_with_any_file_inside_claude_stays(self):
        # .claude of a folder without a repo: the owner's choices for it (Claude Code's settings), a command the
        # session wrote, a plain file named .claude — none is junk
        made = {"solo-c": {".claude/settings.local.json": '{"disabledMcpjsonServers": ["maps"]}'},
                "solo-d": {".claude/commands/ship.md": "steps\n"},
                "solo-e": {".claude": "notes\n"}}
        for name, files in made.items():
            cwd = os.path.join(self.h.state_dir, "workspaces", name)
            for rel, text in files.items():
                os.makedirs(os.path.dirname(os.path.join(cwd, rel)), exist_ok=True)
                with open(os.path.join(cwd, rel), "w") as f:
                    f.write(text)
            self.h.record("session.requested", name, {"cwd": cwd, "session_id": f"s-{name}", "executor": True,
                                                      "raised_by": "maps", **OWNER})
            self.retire(name, 3)
        self.clean()
        for name, files in made.items():
            for rel in files:
                self.assertTrue(os.path.isfile(os.path.join(self.h.state_dir, "workspaces", name, rel)), name)
        got = {f["entity"]: f["data"]["ok"] for f in self.facts("session.worktree_dropped")}
        self.assertEqual(got, dict.fromkeys(made, False))

    def test_a_wake_during_a_new_drop_of_the_own_folder_waits_for_it(self):
        cwd = os.path.join(self.h.state_dir, "workspaces", "solo-a")
        os.makedirs(cwd)
        self.h.record("session.requested", "solo-a", {"cwd": cwd, "session_id": "s-solo-a", "executor": True,
                                                      "raised_by": "maps", **OWNER})
        self.retire("solo-a", 3)
        self.clean()   # an earlier sleep: dropped, its mark stays
        self.h.state["sessions"]["solo-a"]["since"] = self.now - 3 * HOUR + 1   # the next sleep
        self.h.cleaning = {"solo-a": {"path": cwd}}   # its drop is going now, the fact not written yet
        self.actions.clear()
        self.h.record("session.wake_requested", "solo-a", {**OWNER})
        self.assertTrue(self.h.wait_copy(self.spawn_of("solo-a")))
        self.assertFalse(os.path.exists(cwd))

    def test_a_wake_brings_back_the_own_folder_it_dropped(self):
        # the host starts a session whose folder is gone in its copy's repo or else the home folder
        # (hosts/common.existing_cwd): a woken executor without a repo would work in the home folder
        cwd = os.path.join(self.h.state_dir, "workspaces", "solo-a")
        os.makedirs(os.path.join(cwd, ".claude"))
        self.h.record("session.requested", "solo-a", {"cwd": cwd, "session_id": "s-solo-a", "executor": True,
                                                      "raised_by": "maps", **OWNER})
        self.retire("solo-a", 3)
        self.clean()
        self.assertFalse(os.path.exists(cwd))
        self.actions.clear()
        self.h.record("session.wake_requested", "solo-a", {**OWNER})
        action = self.spawn_of("solo-a")
        self.assertEqual(action["cwd"], cwd)
        self.assertTrue(self.bring_back(action))
        self.assertTrue(os.path.isdir(cwd))

    # the wake
    def test_a_wake_brings_the_dropped_copy_back_from_its_branch_before_the_spawn(self):
        x = self.raise_("maps-x")
        path, branch = x["worktree"]["path"], x["worktree"]["branch"]
        sha = git(self.repo, "rev-parse", branch)
        self.retire("maps-x", 3)
        self.clean()
        self.assertFalse(os.path.exists(path))
        self.actions.clear()
        self.h.record("session.wake_requested", "maps-x", {**OWNER})
        action = self.spawn_of("maps-x")
        self.assertEqual(action["cwd"], path)
        self.assertTrue(self.bring_back(action))
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(git(path, "rev-parse", "HEAD"), sha)
        self.assertEqual(git(path, "branch", "--show-current"), branch)
        (f,) = self.facts("session.worktree_restored")
        self.assertTrue(f["data"]["ok"], f)
        self.assertNotIn("dropped", self.h.state["sessions"]["maps-x"]["worktree"])

    def test_a_replay_of_the_journal_keeps_the_marks(self):
        from core import reducer
        x = self.raise_("maps-x")
        self.retire("maps-x", 3)
        self.clean()
        st = reducer.replay(journal.read(os.path.join(self.h.state_dir, "journal.jsonl")))
        mark = st["sessions"]["maps-x"]["copy_drop"]
        self.assertEqual(mark, {"since": self.now - 3 * HOUR, "ok": True})
        self.assertTrue(st["sessions"]["maps-x"]["worktree"]["dropped"])
        self.assertEqual(x["worktree"]["path"], st["sessions"]["maps-x"]["worktree"]["path"])

    def test_a_wake_during_the_drop_waits_for_its_fact_then_brings_the_copy_back(self):
        x = self.raise_("maps-x")
        self.retire("maps-x", 3)
        self.h.clean_worktrees()   # the background takes the copy
        self.actions.clear()
        self.h.record("session.wake_requested", "maps-x", {**OWNER})
        action = self.spawn_of("maps-x")
        self.assertTrue(self.h.wait_copy(action))   # waits: no git of its own while the drop is going
        self.assertNotIn("maps-x", self.h.restoring)
        self.h.cleaner.join(60)
        self.h.clean_worktrees()   # the fact of the drop
        self.assertTrue(self.bring_back(action))
        self.assertTrue(os.path.isdir(x["worktree"]["path"]))

    def test_a_copy_that_cannot_come_back_is_not_spawned_in_the_repos_root(self):
        x = self.raise_("maps-x")
        self.retire("maps-x", 3)
        self.clean()
        git(self.repo, "branch", "-D", x["worktree"]["branch"])   # the branch is gone too: restore makes a new one —
        os.makedirs(x["worktree"]["path"])                        # a foreign file in the way makes git refuse
        with open(os.path.join(x["worktree"]["path"], "f"), "w") as f:
            f.write("x")
        with mock.patch.object(worktree, "restore", return_value=(False, "git worktree add: refused")):
            self.actions.clear()
            self.h.record("session.wake_requested", "maps-x", {**OWNER})
            self.assertFalse(self.bring_back(self.spawn_of("maps-x")))
        (f,) = self.facts("session.worktree_restored")
        self.assertFalse(f["data"]["ok"])
        (e,) = [e for e in self.facts("host.error") if e["entity"] == "maps-x"]
        self.assertEqual(e["data"]["do"], "spawn")

    def cut_short(self, path, left=()):
        """A drop that git cut short: the files and the link to the repo went, the folder stayed (a program stands in
        it, Windows; a timeout on a big copy) — remove answers None or False, the session is not marked dropped."""
        def remove(info, owner=None, foreign=None):
            for n in os.listdir(path):
                if n not in left:
                    full = os.path.join(path, n)
                    platforms.current.rmtree(full) if os.path.isdir(full) else os.remove(full)
            return None, "a shell is left"
        return mock.patch.object(worktree, "remove", remove)

    def test_a_wake_after_a_drop_cut_short_does_not_start_in_the_shell(self):
        # git in a folder without .git answers for the repo above it: the executor would work in the main checkout
        x = self.raise_("maps-x")
        path = x["worktree"]["path"]
        self.retire("maps-x", 3)
        with self.cut_short(path):
            self.clean()
        self.assertTrue(os.path.isdir(path))
        self.assertFalse(os.path.exists(os.path.join(path, ".git")))
        self.actions.clear()
        self.h.record("session.wake_requested", "maps-x", {**OWNER})
        self.assertTrue(self.bring_back(self.spawn_of("maps-x")))
        self.assertEqual(git(path, "rev-parse", "--show-toplevel"), path)

    def test_a_shell_with_files_left_is_not_started_in_and_not_erased(self):
        x = self.raise_("maps-x")
        path = x["worktree"]["path"]
        self.retire("maps-x", 3)
        with self.cut_short(path, left=("a.txt",)):
            self.clean()
        self.actions.clear()
        self.h.record("session.wake_requested", "maps-x", {**OWNER})
        self.assertFalse(self.bring_back(self.spawn_of("maps-x")))
        self.assertTrue(os.path.isfile(os.path.join(path, "a.txt")))
        (e,) = [e for e in self.facts("host.error") if e["entity"] == "maps-x"]
        self.assertEqual(e["data"]["do"], "spawn")

    def test_a_copy_deleted_by_hand_comes_back_on_a_wake_too(self):
        x = self.raise_("maps-x")
        path, sha = x["worktree"]["path"], git(self.repo, "rev-parse", x["worktree"]["branch"])
        self.retire("maps-x", 1)
        platforms.current.rmtree(path)   # a disk cleanup took the folder, git still lists the copy
        self.actions.clear()
        self.h.record("session.wake_requested", "maps-x", {**OWNER})
        self.assertTrue(self.bring_back(self.spawn_of("maps-x")))
        self.assertEqual(git(path, "rev-parse", "HEAD"), sha)

    def test_a_copy_never_dropped_is_not_touched_on_a_wake(self):
        self.raise_("maps-x")
        self.retire("maps-x", 1)
        self.actions.clear()
        self.h.record("session.wake_requested", "maps-x", {**OWNER})
        self.assertFalse(self.h.wait_copy(self.spawn_of("maps-x")))
        self.assertEqual(self.facts("session.worktree_restored"), [])


class Unfinished(unittest.TestCase):
    """hosts/worktree.unfinished by itself: what keeps a copy."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cpfin-"))
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.repo = repo(self.tmp)
        self.cwd, self.info, err = worktree.prepare(self.repo, "w-1")
        self.assertIsNone(err)

    @unittest.skipUnless(os.path.isdir("/proc/self"), "Linux: the processes' folders from /proc")
    def test_a_program_in_the_copy_is_seen_without_lsof(self):
        # a Linux without the lsof package (a slim server image): the guard must not go blind
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=self.cwd)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        real = os.path.exists
        with mock.patch.object(worktree.shutil, "which", return_value=None), \
                mock.patch.object(worktree.os.path, "exists", lambda p: not p.endswith("/lsof") and real(p)):
            found = worktree.cwd_folders()
        self.assertIsNotNone(found)
        self.assertIn(os.path.realpath(self.cwd), {os.path.realpath(f) for f in found})

    def test_a_clean_copy_on_main_is_finished(self):
        self.assertIsNone(worktree.unfinished(self.info))

    def test_a_commit_pushed_to_a_remote_is_finished(self):
        remote = os.path.join(self.tmp, "remote.git")
        git(self.tmp, "init", "-q", "--bare", remote)
        git(self.repo, "remote", "add", "origin", remote)
        with open(os.path.join(self.cwd, "b.txt"), "w") as f:
            f.write("b\n")
        git(self.cwd, "add", "b.txt")
        git(self.cwd, "commit", "-q", "-m", "w")
        self.assertIn("not in main", worktree.unfinished(self.info))
        git(self.cwd, "push", "-q", "origin", "HEAD:refs/heads/pr")
        self.assertIsNone(worktree.unfinished(self.info))

    def test_the_branch_ahead_while_head_moved_keeps_it(self):
        with open(os.path.join(self.cwd, "b.txt"), "w") as f:
            f.write("b\n")
        git(self.cwd, "add", "b.txt")
        git(self.cwd, "commit", "-q", "-m", "w")
        git(self.cwd, "switch", "-q", "--detach", "main")
        self.assertIn("not in main", worktree.unfinished(self.info))

    def test_pycache_and_ds_store_are_junk(self):
        os.makedirs(os.path.join(self.cwd, "pkg", "__pycache__"))
        with open(os.path.join(self.cwd, "pkg", "__pycache__", "m.pyc"), "wb") as f:
            f.write(b"x")
        with open(os.path.join(self.cwd, ".DS_Store"), "wb") as f:
            f.write(b"x")
        self.assertIsNone(worktree.unfinished(self.info))

    def test_an_untracked_source_in_a_build_folder_keeps_the_copy(self):
        # build and dist are junk when ignored; untracked, they may be a file the session wrote and did not commit
        os.makedirs(os.path.join(self.cwd, "src", "build"))
        with open(os.path.join(self.cwd, "src", "build", "plan.py"), "w") as f:
            f.write("x\n")
        self.assertIsNotNone(worktree.unfinished(self.info))
        with open(os.path.join(self.cwd, ".gitignore"), "a") as f:
            f.write("build/\n")
        git(self.cwd, "commit", "-qam", "ignore build")
        git(self.repo, "merge", "-q", "--ff-only", self.info["branch"])
        self.assertIsNone(worktree.unfinished(self.info))

    def test_a_missing_folder_is_not_a_copy(self):
        self.assertIsNotNone(worktree.unfinished({**self.info, "path": os.path.join(self.tmp, "nope")}))


if __name__ == "__main__":
    unittest.main()
