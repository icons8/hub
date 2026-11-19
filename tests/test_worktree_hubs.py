"""Several hubs on one machine, slice M2 "copies in a repo" (core/instance.py, hosts/worktree.py, core/daemon.py): an
instance puts its executors' copies in <repo>/.claude/worktrees/@<name>/<executor> on task/@<name>/<executor>, the main
hub as always; a copy another hub owns (its mark in the copy's git folder) or one a live session of another hub works
in (its <state>/folders.json) is neither taken nor removed; `git worktree add/remove` and the fast-forward of a folder
take a file lock of the repo; a folder is not fast-forwarded under another hub's session. On a machine with one hub
nothing of it is written: no lock file in .git, no mark, no folders.json."""
import tests  # noqa: F401 — keep this machine's settings out
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

import platforms
from core import daemon as daemon_mod, i18n, instance, journal, uninstall
from core.daemon import Hub
from hosts import common, worktree
from tests.test_folder_forward import Host, Repo, head

OWNER = {"channel": "cli", "owner": True}
# the logic of the fast-forward on every system: the Windows gate (off until a probe) has its own test in test_folder_forward
FORWARDS = mock.patch.object(platforms.current, "FORWARDS_FOLDERS", True, create=True)


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
    return path


def listed(top):
    return {os.path.realpath(x[9:]) for x in git(top, "worktree", "list", "--porcelain").splitlines()
            if x.startswith("worktree ")}


def marks(top):
    """The owner marks of the repo's copies (in <common>/worktrees/<id>/)."""
    folder = os.path.join(top, ".git", "worktrees")
    return sorted(n for n in (os.listdir(folder) if os.path.isdir(folder) else [])
                  if os.path.exists(os.path.join(folder, n, worktree.OWNER_MARK)))


class Space(unittest.TestCase):
    """hosts/worktree.prepare: the instance's namespace, the main hub's paths as always."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wth-"))
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.repo = repo(self.tmp)

    def test_an_instance_puts_its_copy_and_branch_under_its_name(self):
        cwd, info, err = worktree.prepare(self.repo, "maps-fix", space="work2")
        self.assertIsNone(err)
        path = os.path.join(self.repo, ".claude", "worktrees", "@work2", "maps-fix")
        self.assertEqual((cwd, info["path"], info["branch"]), (path, path, "task/@work2/maps-fix"))
        self.assertEqual(git(path, "branch", "--show-current"), "task/@work2/maps-fix")
        self.assertTrue(all(ok for _, ok in info["checks"]), info["checks"])

    def test_the_main_hub_and_an_instance_with_one_executor_name_get_two_copies(self):
        main = worktree.prepare(self.repo, "maps-fix")
        inst = worktree.prepare(self.repo, "maps-fix", space="work2")
        self.assertNotEqual(main[0], inst[0])
        self.assertEqual({main[1]["branch"], inst[1]["branch"]}, {"task/maps-fix", "task/@work2/maps-fix"})
        self.assertEqual(listed(self.repo), {self.repo, main[0], inst[0]})
        # the main folder is clean: both copies are inside .claude/worktrees
        self.assertEqual(worktree.changes(self.repo)[1], [])

    def test_a_removed_copy_of_an_instance_goes_up_to_its_repo(self):
        gone = os.path.join(self.repo, ".claude", "worktrees", "@work2", "maps-fix")
        self.assertEqual(common.existing_cwd(gone), self.repo)
        self.assertEqual(common.existing_cwd(os.path.join(self.repo, ".claude", "worktrees", "maps-fix")), self.repo)


class Owner(unittest.TestCase):
    """The mark of the hub that made a copy: another hub does not take it, its cleanup does not remove it."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wth-o-"))
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.repo = repo(self.tmp)
        self.a, self.b = os.path.join(self.tmp, "hub-a"), os.path.join(self.tmp, "hub-b")
        os.makedirs(self.a)
        os.makedirs(self.b)

    def test_a_copy_of_another_hub_is_not_taken_and_not_removed(self):
        cwd, info, err = worktree.prepare(self.repo, "maps-fix", owner={"state": self.a, "hub": "hub"})
        self.assertIsNone(err)
        self.assertEqual(worktree.owner_of(cwd)["state"], os.path.realpath(self.a))
        with open(os.path.join(cwd, "work.txt"), "w") as f:
            f.write("a's work\n")
        got, _, err = worktree.prepare(self.repo, "maps-fix", owner={"state": self.b, "hub": "hub-work2"})
        self.assertIsNone(got)
        self.assertIn("hub", str(err))
        self.assertIn("another hub", str(err))
        self.assertTrue(os.path.exists(os.path.join(cwd, "work.txt")))   # untouched
        git(cwd, "add", "work.txt")
        git(cwd, "commit", "-q", "-m", "a's work")
        ok, why = worktree.remove({**info, "path": cwd}, owner={"state": self.b, "hub": "hub-work2"})
        self.assertFalse(ok)
        self.assertIn("another hub", str(why))
        self.assertTrue(os.path.isdir(cwd))
        self.assertIn("task/maps-fix", git(self.repo, "branch", "--list"))
        # its own hub takes it and removes it
        got, _, err = worktree.prepare(self.repo, "maps-fix", owner={"state": self.a, "hub": "hub"})
        self.assertEqual((got, err), (cwd, None))
        ok, _ = worktree.remove(info, owner={"state": self.a, "hub": "hub"})
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(cwd))

    def test_a_copy_a_live_session_of_another_hub_works_in_is_not_taken_and_not_removed(self):
        # an unmarked copy at this path (an instance on an earlier core made it under the plain name)
        path = os.path.join(self.repo, ".claude", "worktrees", "maps-fix")
        git(self.repo, "worktree", "add", "-q", "-b", "task/maps-fix", path)
        inside = lambda p: "hub-work2" if worktree.same(p, path) else None   # noqa: E731
        got, _, err = worktree.prepare(self.repo, "maps-fix", owner={"state": self.a, "hub": "hub"}, foreign=inside)
        self.assertIsNone(got)
        self.assertIn("hub-work2", str(err))
        self.assertEqual(marks(self.repo), [])   # not claimed
        info = {"path": path, "branch": "task/maps-fix", "repo": self.repo}
        ok, why = worktree.remove(info, owner={"state": self.a, "hub": "hub"}, foreign=inside)
        self.assertFalse(ok)
        self.assertIn("hub-work2", str(why))
        self.assertTrue(os.path.isdir(path))

    def test_an_unmarked_copy_nobody_else_works_in_is_taken_as_before_and_marked(self):
        path = os.path.join(self.repo, ".claude", "worktrees", "maps-fix")
        git(self.repo, "worktree", "add", "-q", "-b", "task/maps-fix", path)
        got, _, err = worktree.prepare(self.repo, "maps-fix", owner={"state": self.a, "hub": "hub"},
                                       foreign=lambda p: None)
        self.assertEqual((got, err), (path, None))
        self.assertEqual(worktree.owner_of(path)["state"], os.path.realpath(self.a))

    def test_without_an_owner_nothing_is_written_into_git(self):
        cwd, info, err = worktree.prepare(self.repo, "maps-fix")
        self.assertIsNone(err)
        self.assertEqual(marks(self.repo), [])
        self.assertIsNone(worktree.owner_of(cwd))
        self.assertTrue(worktree.remove(info)[0])
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".git", worktree.LOCK)))

    def test_a_mark_of_a_hub_removed_since_is_no_ones(self):
        cwd, _, _ = worktree.prepare(self.repo, "maps-fix", owner={"state": self.a, "hub": "hub-old"})
        platforms.current.rmtree(self.a)
        got, _, err = worktree.prepare(self.repo, "maps-fix", owner={"state": self.b, "hub": "hub"})
        self.assertEqual((got, err), (cwd, None))

    def test_a_revive_does_not_resume_in_a_copy_another_hub_holds_now(self):
        cwd, info, _ = worktree.prepare(self.repo, "maps-fix", owner={"state": self.a, "hub": "hub"})
        ok, why = worktree.restore(info, owner={"state": self.b, "hub": "hub-work2"})
        self.assertFalse(ok)
        self.assertIn("another hub", str(why))
        inside = lambda p: "hub-work2" if worktree.same(p, cwd) else None   # noqa: E731
        ok, why = worktree.restore(info, owner={"state": self.a, "hub": "hub"}, foreign=inside)
        self.assertFalse(ok)
        self.assertIn("hub-work2", str(why))
        self.assertEqual(worktree.restore(info, owner={"state": self.a, "hub": "hub"})[0], True)

    def test_the_mark_is_read_without_git(self):
        # a slow git must not make another hub's copy look unmarked: the mark is found from the files git keeps
        cwd, _, _ = worktree.prepare(self.repo, "maps-fix", owner={"state": self.a, "hub": "hub"})

        def slow(*a, **kw):
            raise subprocess.TimeoutExpired(["git"], 30)
        with mock.patch.object(worktree, "_git", slow):
            self.assertEqual(worktree.owner_of(cwd)["state"], os.path.realpath(self.a))

    def test_a_repo_in_a_folder_named_worktrees_is_not_a_copy(self):
        top = repo(self.tmp, "worktrees")
        self.assertIsNone(worktree.owner_of(top))
        worktree._mark(top, {"state": self.a, "hub": "hub"})
        self.assertFalse(os.path.exists(os.path.join(top, ".git", worktree.OWNER_MARK)))

    def test_a_copy_taken_after_a_removed_hub_gets_the_new_owners_mark(self):
        cwd, _, _ = worktree.prepare(self.repo, "maps-fix", owner={"state": self.a, "hub": "hub-old"})
        platforms.current.rmtree(self.a)
        worktree.prepare(self.repo, "maps-fix", owner={"state": self.b, "hub": "hub"})
        self.assertEqual(worktree.owner_of(cwd)["state"], os.path.realpath(self.b))
        os.makedirs(self.a)   # the removed hub is added again under the same name: the copy stays the taker's
        self.assertEqual(worktree.prepare(self.repo, "maps-fix", owner={"state": self.b, "hub": "hub"})[2], None)

    def test_a_revived_copy_in_place_from_before_marks_gets_a_mark(self):
        path = os.path.join(self.repo, ".claude", "worktrees", "maps-fix")
        git(self.repo, "worktree", "add", "-q", "-b", "task/maps-fix", path)
        info = {"path": path, "branch": "task/maps-fix", "repo": self.repo}
        self.assertEqual(worktree.restore(info, owner={"state": self.a, "hub": "hub"})[0], True)
        self.assertEqual(worktree.owner_of(path)["state"], os.path.realpath(self.a))

    def test_a_restored_copy_is_marked(self):
        cwd, info, _ = worktree.prepare(self.repo, "maps-fix", owner={"state": self.a, "hub": "hub"})
        worktree.remove(info, owner={"state": self.a, "hub": "hub"})
        ok, _ = worktree.restore(info, owner={"state": self.a, "hub": "hub"})
        self.assertTrue(ok)
        self.assertEqual(worktree.owner_of(cwd)["state"], os.path.realpath(self.a))


HOLD = """
import sys, time
sys.path.insert(0, sys.argv[1])
import platforms
with platforms.current.exclusive(sys.argv[2]):
    print("held", flush=True)
    while not __import__("os").path.exists(sys.argv[3]):
        time.sleep(0.05)
"""


class RepoLock(unittest.TestCase):
    """The file lock of a repo: <git-common-dir>/agents-hub.lock, one git of the hubs at a time across processes."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wth-l-"))
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.repo = repo(self.tmp)
        self.common = worktree.common_dir(self.repo)

    def hold(self):
        """Another process holds the lock until the returned release() is called."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        done = os.path.join(self.tmp, "release")
        p = subprocess.Popen([sys.executable, "-c", HOLD, root, os.path.join(self.common, worktree.LOCK), done],
                             stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "held")

        def release():
            with open(done, "w"):
                pass
            p.wait(30)
            p.stdout.close()
        self.addCleanup(lambda: p.poll() is not None or release())
        return release

    def test_a_held_lock_is_waited_for_then_given_up(self):
        release = self.hold()
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            with worktree.repo_lock(self.common, timeout=0.3):
                pass
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        release()
        with worktree.repo_lock(self.common, timeout=5):
            pass

    def test_no_folder_no_lock(self):
        with worktree.repo_lock(None, timeout=0.1):
            pass

    @FORWARDS
    def test_a_forward_waits_for_the_lock_another_hub_holds(self):
        r = Repo(self)
        h = Hub(tempfile.mkdtemp(prefix="wth-fl-"), Host(), registry_dir=tempfile.mkdtemp())
        h.machine_hubs = lambda: {"space": None, "owner": {"state": h.state_dir, "hub": "hub"}, "others": []}
        h.record("session.requested", "hub", {"cwd": r.root, "session_id": "s-hub", "kind": "hub", **OWNER})
        h.record("session.requested", "lead", {"cwd": r.copy, "session_id": "s-lead", "role": "agent", "about": "x",
                                               **OWNER})
        h.record("session.started", "lead", {})
        self.common = worktree.common_dir(r.main)
        release = self.hold()
        with mock.patch.object(daemon_mod, "FORWARD_LOCK_WAIT", 0.3):
            for _ in range(10):
                h.folders_fwd_at.clear()
                h.forward_folders()
                for t in (h.fwd_scan, (h.fwd_job or {}).get("thread")):
                    if t is not None:
                        t.join(30)
                done = [e for e in journal.read(os.path.join(h.state_dir, "journal.jsonl"))
                        if e["type"] == "session.folder_forwarded"]
                if done and h.fwd_job is None:
                    break
        release()
        self.assertFalse(done[0]["data"]["ok"])
        self.assertIn("another git", done[0]["data"]["why"])
        self.assertEqual(head(r.copy), r.v1)   # not moved


class Folders(unittest.TestCase):
    """<state>/folders.json: the folders of a hub's live sessions, read by the other hubs of the machine."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wth-f-"))
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.reg = os.path.join(self.tmp, "reg")
        self.main = os.path.join(self.tmp, "main")
        self.inst = os.path.join(self.tmp, ".hub-work2")
        os.makedirs(self.main)
        os.makedirs(self.inst)

    def test_the_other_hubs_of_the_machine(self):
        self.assertEqual(instance.others(self.main, main_state=self.main, registry=self.reg), [])
        instance.register({"name": "work2", "state": self.inst}, self.reg)
        self.assertEqual(instance.others(self.main, main_state=self.main, registry=self.reg),
                         [{"name": "work2", "state": self.inst}])
        self.assertEqual(instance.others(self.inst, main_state=self.main, registry=self.reg),
                         [{"name": None, "state": self.main}])
        platforms.current.rmtree(self.inst)   # removed by hand: not another hub any more
        self.assertEqual(instance.others(self.main, main_state=self.main, registry=self.reg), [])

    def test_live_folders_and_a_stale_file(self):
        instance.write_folders(self.inst, ["/home/x/repo", "/home/x/repo/.claude/worktrees/@work2/maps-fix"])
        hubs = [{"name": "work2", "state": self.inst}, {"name": None, "state": self.main}]
        self.assertEqual(instance.live_folders(hubs), [("hub-work2", "/home/x/repo"),
                                                       ("hub-work2", "/home/x/repo/.claude/worktrees/@work2/maps-fix")])
        old = time.time() - instance.FOLDERS_STALE - 60
        os.utime(os.path.join(self.inst, instance.FOLDERS), (old, old))
        self.assertEqual(instance.live_folders(hubs), [])   # its hub stopped long ago

    def test_garbage_is_no_folders(self):
        with open(os.path.join(self.inst, instance.FOLDERS), "w") as f:
            f.write('{"cwds": [1, "", null, "/home/x/a"]}')
        self.assertEqual(instance.live_folders([{"name": "work2", "state": self.inst}]), [("hub-work2", "/home/x/a")])
        with open(os.path.join(self.inst, instance.FOLDERS), "w") as f:
            f.write("not json")
        self.assertEqual(instance.live_folders([{"name": "work2", "state": self.inst}]), [])


class TwoHubs(unittest.TestCase):
    """The stand: the main hub and the instance work2 on temporary folders, one shared repo, executors of one name."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="wth-2-"))
        self.addCleanup(platforms.current.rmtree, self.tmp)
        self.repo = repo(self.tmp)
        self.reg = os.path.join(self.tmp, "reg")
        main_state, inst_state = os.path.join(self.tmp, "main"), os.path.join(self.tmp, ".hub-work2")
        os.makedirs(inst_state)
        instance.write(inst_state, {"name": "work2", "title": "work2", "config_dir": os.path.join(self.tmp, "cfg2")})
        instance.register({"name": "work2", "state": inst_state}, self.reg)
        self.main, self.inst = self.hub(main_state, main_state), self.hub(inst_state, main_state)

    def hub(self, state, main_state):
        h = Hub(state, types.SimpleNamespace())
        h.hubs_registry, h.main_state = self.reg, main_state
        h.execute = lambda a: None
        rec = h.record
        rec("session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **OWNER})
        rec("owner.settings", None, {"parallel": 3, "channel": "core"})
        rec("session.requested", "maps", {"cwd": self.repo, "session_id": "s-m", "role": "agent", **OWNER})
        rec("session.started", "maps", {})
        rec("session.folder", "maps", {"channel": "core", "git": "ok", "dirty": 0}, src="core")
        return h

    def raise_(self, h, name="maps-fix"):
        h.record("task.run_requested", name, {"task": "do it", "channel": "session", "owner": False, "sender": "maps"})
        for _ in range(50):
            h.run_tasks()
            if name in h.state["sessions"] or not h.preparing:
                break
            time.sleep(0.1)
        return h.state["sessions"].get(name)

    def remove(self, h, name="maps-fix"):
        h.record("session.started", name, {})
        h.record("session.stop_requested", name, OWNER)
        h.record("session.remove_requested", name, OWNER)
        self.assertEqual(h.state["sessions"][name]["state"], "removed")
        h.clean_worktrees()
        if h.cleaner:
            h.cleaner.join(30)
        h.clean_worktrees()

    def test_one_executor_name_two_copies_each_hub_cleans_only_its_own(self):
        a, b = self.raise_(self.main), self.raise_(self.inst)
        wt = os.path.join(self.repo, ".claude", "worktrees")
        self.assertEqual((a["cwd"], a["worktree"]["branch"]), (os.path.join(wt, "maps-fix"), "task/maps-fix"))
        self.assertEqual((b["cwd"], b["worktree"]["branch"]),
                         (os.path.join(wt, "@work2", "maps-fix"), "task/@work2/maps-fix"))
        with open(os.path.join(b["cwd"], "b.txt"), "w") as f:
            f.write("work of the instance\n")
        self.remove(self.main)
        self.assertFalse(os.path.exists(a["cwd"]))
        self.assertTrue(os.path.exists(os.path.join(b["cwd"], "b.txt")))
        self.assertEqual(git(b["cwd"], "branch", "--show-current"), "task/@work2/maps-fix")
        os.remove(os.path.join(b["cwd"], "b.txt"))
        self.remove(self.inst)
        self.assertFalse(os.path.exists(b["cwd"]))
        self.assertEqual(listed(self.repo), {self.repo})

    def test_the_folders_of_live_sessions_are_published(self):
        self.raise_(self.inst)
        self.inst.publish_folders()
        self.main.publish_folders()
        cwds = [c for _, c in instance.live_folders([{"name": "work2", "state": self.inst.state_dir}])]
        self.assertIn(self.repo, cwds)
        self.assertIn(os.path.join(self.repo, ".claude", "worktrees", "@work2", "maps-fix"), cwds)
        self.assertTrue(os.path.exists(os.path.join(self.main.state_dir, instance.FOLDERS)))

    def test_a_main_copy_an_instance_session_works_in_is_neither_taken_nor_removed(self):
        a = self.raise_(self.main)
        # the instance (rolled back to an earlier core: no namespace) runs a session right in the main hub's copy
        self.inst.record("session.requested", "old", {"cwd": a["cwd"], "session_id": "s-o", "role": "agent", **OWNER})
        self.inst.publish_folders()
        self.main.machine = None   # the cache of the machine's hubs: read again
        self.remove(self.main)
        self.assertTrue(os.path.isdir(a["cwd"]))
        kept = [m["text"] for m in self.main.state["messages"].values() if "maps-fix is kept" in m["text"]]
        self.assertEqual(len(kept), 1, kept)
        self.assertIn("hub-work2", kept[0])
        # raising one of that name again does not take the copy from under it
        got = self.raise_(self.main, "maps-fix")
        self.assertEqual(got["state"], "removed")   # still the removed one: no new executor
        texts = [m["text"] for m in self.main.state["messages"].values() if m["session"] == "maps"
                 and "Could not raise executor maps-fix" in m["text"]]
        self.assertTrue(texts and "hub-work2" in texts[-1], texts)

    def test_the_published_folders_are_rewritten_by_the_wall_clock(self):
        # the readers judge folders.json by its mtime: a hub that slept (the awake clock stood still, the wall clock
        # went on) with its sessions as they were still rewrites it at once on waking
        self.main.mono = lambda: 1000.0
        self.main.publish_folders()
        path = os.path.join(self.main.state_dir, instance.FOLDERS)
        self.main.publish_folders()   # nothing changed, just written: not rewritten
        old = time.time() - instance.FOLDERS_EVERY - 60
        os.utime(path, (old, old))
        woke = time.time() + instance.FOLDERS_EVERY + 60
        with mock.patch.object(daemon_mod.time, "time", lambda: woke):
            self.main.publish_folders()
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["at"], int(woke))   # written anew (the wall clock is the one patched)

    def test_a_branch_is_not_switched_under_a_session_of_another_hub(self):
        git(self.repo, "branch", "feat/x")
        git(self.repo, "switch", "-q", "feat/x")
        instance.write_folders(self.inst.state_dir, [self.repo])   # a session of work2 in the same folder
        self.main.record("task.run_requested", "maps-x", {"task": "do it", "channel": "session", "owner": False,
                                                          "sender": "maps", "branch": "feat/x"})
        self.main.run_tasks()
        self.assertEqual(git(self.repo, "branch", "--show-current"), "feat/x")   # not switched
        texts = [m["text"] for m in self.main.state["messages"].values() if m["session"] == "maps"
                 and "Could not raise executor maps-x" in m["text"]]
        self.assertTrue(texts and "hub-work2" in texts[-1], texts)

    def test_the_cleaner_waits_once_per_repo_another_hub_holds(self):
        a, b = self.raise_(self.main, "maps-a"), self.raise_(self.main, "maps-b")
        for name in ("maps-a", "maps-b"):
            self.main.record("session.started", name, {})
            self.main.record("session.stop_requested", name, OWNER)
            self.main.record("session.remove_requested", name, OWNER)
        waits = []

        def held(target, timeout):
            waits.append(target)
            raise TimeoutError(target)
        with mock.patch.object(worktree, "repo_lock", held):
            self.main.clean_worktrees()
            self.main.cleaner.join(30)
            self.main.clean_worktrees()
        self.assertEqual(len(waits), 1)
        self.assertTrue(os.path.isdir(a["cwd"]) and os.path.isdir(b["cwd"]))
        self.assertFalse(any(s["worktree"].get("cleaned") for s in (self.main.state["sessions"]["maps-a"],
                                                                     self.main.state["sessions"]["maps-b"])))
        self.remove_all()

    def stand_for_uninstall(self):
        """The main hub's executor and three of the instance's in one repo: merged, with unmerged commits, with
        uncommitted work."""
        main = self.raise_(self.main, "maps-fix")
        merged, ahead, dirty = (self.raise_(self.inst, n) for n in ("maps-ok", "maps-ahead", "maps-wip"))
        with open(os.path.join(ahead["cwd"], "c.txt"), "w") as f:
            f.write("commit not merged anywhere\n")
        git(ahead["cwd"], "add", "c.txt")
        git(ahead["cwd"], "commit", "-q", "-m", "work")
        with open(os.path.join(dirty["cwd"], "w.txt"), "w") as f:
            f.write("not committed\n")
        for s in (main, merged, ahead, dirty):
            self.assertTrue(os.path.isdir(s["cwd"]), s)
        return main, merged, ahead, dirty

    def test_removing_an_instance_takes_its_copies_and_merged_branches_not_the_main_hubs(self):
        main, merged, ahead, dirty = self.stand_for_uninstall()
        summary, kept = uninstall.drop_copies(self.inst.state_dir, "work2")
        self.assertFalse(os.path.exists(merged["cwd"]))
        self.assertFalse(os.path.exists(ahead["cwd"]))   # clean: the copy goes, its commits stay on the branch
        self.assertTrue(os.path.isfile(os.path.join(dirty["cwd"], "w.txt")))   # uncommitted work is not erased
        self.assertTrue(os.path.isdir(main["cwd"]))   # the main hub's copy and branch are not its business
        branches = git(self.repo, "branch", "--format=%(refname:short)").split()
        self.assertEqual(sorted(branches), ["main", "task/@work2/maps-ahead", "task/@work2/maps-wip", "task/maps-fix"])
        self.assertIn("2", summary)
        self.assertEqual(len(kept), 2, kept)
        self.assertTrue(any(dirty["cwd"] in x and "uncommitted" in x for x in kept), kept)
        self.assertTrue(any("task/@work2/maps-ahead" in x for x in kept), kept)
        self.assertEqual(listed(self.repo), {self.repo, main["cwd"], dirty["cwd"]})
        # nothing else of the copies is left: the second run has nothing new to remove
        self.assertEqual(uninstall.drop_copies(self.inst.state_dir, "work2")[0], i18n.t("cli.uninstall.copies_none"))

    def test_the_step_runs_in_the_plan_of_an_instance_and_says_what_stays(self):
        _, _, _, dirty = self.stand_for_uninstall()
        steps = uninstall.plan(self.inst.state_dir, os.path.join(self.tmp, "cfg2"), home=self.tmp)
        copies = [s for s in steps if s["do"] == "copies"]
        self.assertEqual(len(copies), 1)
        self.assertLess(steps.index(copies[0]), min(i for i, s in enumerate(steps) if s["do"] in ("remove", "unregister")))
        said = []
        self.assertEqual(uninstall.execute(copies, say=said.append), 0)   # a copy that stays is no failure
        self.assertTrue(said[0].startswith("  ✓ "), said)
        self.assertTrue(any(dirty["cwd"] in x for x in said[1:]), said)

    def test_the_plan_of_a_hub_that_is_not_an_instance_has_no_such_step(self):
        steps = uninstall.plan(self.main.state_dir, os.path.join(self.tmp, "cfg"), home=self.tmp)
        self.assertFalse([s for s in steps if s["do"] == "copies"])

    def test_a_repo_another_hubs_git_holds_keeps_its_copies_and_says_so(self):
        _, merged, _, _ = self.stand_for_uninstall()

        def held(target, timeout):
            raise TimeoutError(target)
        with mock.patch.object(worktree, "repo_lock", held):
            _, kept = uninstall.drop_copies(self.inst.state_dir, "work2")
        self.assertTrue(os.path.isdir(merged["cwd"]))
        self.assertEqual(len(kept), 1, kept)
        self.assertIn(self.repo, kept[0])

    def test_a_copy_outside_the_instances_folder_and_one_of_another_hub_are_never_touched(self):
        main, _, _, _ = self.stand_for_uninstall()
        plain = worktree.prepare(self.repo, "plain")   # no owner mark, not in the instance's folder
        info = dict(plain[1])
        # details of a main-hub copy (not under @work2) and a copy marked by the main hub inside @work2
        other = os.path.join(self.repo, ".claude", "worktrees", "@work2", "from-main")
        worktree.prepare(self.repo, "from-main", space="work2", owner={"state": self.main.state_dir, "hub": "hub"})
        got = worktree.drop_space("work2", [info], {"state": self.inst.state_dir, "hub": "hub-work2"})
        self.assertTrue(os.path.isdir(main["cwd"]) and os.path.isdir(plain[0]))
        self.assertTrue(os.path.isdir(other))
        # a record of the repo names the copy as git does (C:/… on Windows)
        self.assertIn(os.path.normcase(other), [os.path.normcase(os.path.normpath(p)) for p, _ in got["kept"]])

    def test_a_flag_keeps_the_copies(self):
        self.stand_for_uninstall()
        steps = uninstall.plan(self.inst.state_dir, os.path.join(self.tmp, "cfg2"), home=self.tmp, copies=False)
        self.assertFalse([s for s in steps if s["do"] == "copies"])

    def test_hub_uninstall_takes_the_flag_and_the_dry_run_names_the_step(self):
        import contextlib
        import io
        for argv, shown in (([], True), (["--keep-copies"], False)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch.object(sys, "stdin", io.StringIO("")):
                code = uninstall.main(["--dry-run", *argv], self.inst.state_dir, os.path.join(self.tmp, "cfg2"), None,
                                      tty=False)
            self.assertEqual(code, 0, out.getvalue())
            self.assertEqual("working copies" in out.getvalue(), shown, out.getvalue())

    def test_a_copy_known_only_to_the_repo_keeps_a_branch_that_is_not_the_instances(self):
        # the session is gone from the state; its copy (a user's branch checked out by --branch) is merged into HEAD
        self.raise_(self.inst, "maps-ok")   # the state knows the repo
        git(self.repo, "branch", "feature-x")
        path = os.path.join(self.repo, ".claude", "worktrees", "@work2", "foo")
        got = worktree.prepare(self.repo, "foo", branch="feature-x", space="work2",
                               owner={"state": self.inst.state_dir, "hub": "hub-work2"})
        self.assertIsNone(got[2])
        self.assertEqual(uninstall.drop_copies(self.inst.state_dir, "work2")[1], [])
        self.assertFalse(os.path.exists(path))
        self.assertIn("feature-x", git(self.repo, "branch", "--format=%(refname:short)").split())

    def test_the_repo_lock_is_taken_for_one_git_step_not_held_over_all_the_copies(self):
        self.stand_for_uninstall()
        depth, most, taken = [0], [0], []
        real = worktree.repo_lock

        import contextlib

        @contextlib.contextmanager
        def counting(target, timeout):
            depth[0] += 1
            most[0] = max(most[0], depth[0])
            taken.append(target)
            try:
                with real(target, timeout):
                    yield
            finally:
                depth[0] -= 1
        with mock.patch.object(worktree, "repo_lock", counting):
            uninstall.drop_copies(self.inst.state_dir, "work2")
        self.assertEqual(most[0], 1)
        self.assertGreaterEqual(len(taken), 5)   # the records, three copies, the branches

    def test_a_tag_named_like_a_kept_copys_branch_does_not_make_it_look_unmerged(self):
        _, _, _, dirty = self.stand_for_uninstall()
        git(self.repo, "tag", "task/@work2/maps-wip")   # `refname:short` would print heads/task/@work2/maps-wip
        _, kept = uninstall.drop_copies(self.inst.state_dir, "work2")
        self.assertEqual([x for x in kept if "maps-wip" in x and "merged nowhere" in x], [])
        self.assertTrue(os.path.isdir(dirty["cwd"]))

    def test_a_record_of_a_copy_whose_folder_is_gone_is_dropped(self):
        _, merged, _, _ = self.stand_for_uninstall()
        platforms.current.rmtree(merged["cwd"])
        uninstall.drop_copies(self.inst.state_dir, "work2")
        self.assertNotIn(merged["cwd"], listed(self.repo))

    def remove_all(self):
        self.main.clean_worktrees()
        if self.main.cleaner:
            self.main.cleaner.join(30)
        self.main.clean_worktrees()

    @FORWARDS
    def test_a_folder_is_not_forwarded_under_a_session_of_another_hub(self):
        r = Repo(self)
        h = Hub(tempfile.mkdtemp(prefix="wth-fw-"), Host(), registry_dir=tempfile.mkdtemp())
        h.hubs_registry, h.main_state = self.reg, h.state_dir
        h.record("session.requested", "hub", {"cwd": r.root, "session_id": "s-hub", "kind": "hub", **OWNER})
        h.record("session.requested", "lead", {"cwd": r.copy, "session_id": "s-lead", "role": "agent", "about": "x",
                                               **OWNER})
        h.record("session.started", "lead", {})
        instance.write_folders(self.inst.state_dir, [r.copy])   # a session of work2 lives in the lead's folder

        def settle():
            for _ in range(6):
                h.folders_fwd_at.clear()
                h.forward_folders()
                for t in (h.fwd_scan, (h.fwd_job or {}).get("thread")):
                    if t is not None:
                        t.join(30)
        settle()
        self.assertEqual(head(r.copy), r.v1)
        self.assertEqual([e for e in journal.read(os.path.join(h.state_dir, "journal.jsonl"))
                          if e["type"] == "session.folder_forwarding"], [])
        instance.write_folders(self.inst.state_dir, [r.main])   # it moved to another folder of the repo
        h.machine = None
        settle()
        self.assertEqual(head(r.copy), r.v2)

    @FORWARDS
    def test_an_instance_lead_forwards_only_its_own_task_branch(self):
        r = Repo(self)   # the copy is on task/lead: the main hub's namespace
        h = Hub(os.path.join(self.tmp, ".hub-work3"), Host(), registry_dir=tempfile.mkdtemp())
        instance.write(h.state_dir, {"name": "work3", "title": "work3", "config_dir": os.path.join(self.tmp, "c3")})
        h.hubs_registry, h.main_state = self.reg, os.path.join(self.tmp, "nomain")
        h.record("session.requested", "hub", {"cwd": r.root, "session_id": "s-hub", "kind": "hub", **OWNER})
        h.record("session.requested", "lead", {"cwd": r.copy, "session_id": "s-lead", "role": "agent", "about": "x",
                                               **OWNER})
        h.record("session.started", "lead", {})
        for _ in range(4):
            h.folders_fwd_at.clear()
            h.forward_folders()
            if h.fwd_scan is not None:
                h.fwd_scan.join(30)
        self.assertEqual(head(r.copy), r.v1)
        self.assertEqual(h.fwd_said.get("lead"), "branch")
        git(r.copy, "branch", "-m", "task/@work3/lead")
        h.fwd_said.clear()
        for _ in range(6):
            h.folders_fwd_at.clear()
            h.forward_folders()
            for t in (h.fwd_scan, (h.fwd_job or {}).get("thread")):
                if t is not None:
                    t.join(30)
        self.assertEqual(head(r.copy), r.v2)


class OneHub(unittest.TestCase):
    """A machine with one hub: the copy, the branch and the disk as before — no mark, no lock file, no folders.json."""

    def test_nothing_new_is_written(self):
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="wth-1-"))
        self.addCleanup(platforms.current.rmtree, tmp)
        path = repo(tmp)
        state = os.path.join(tmp, "state")
        h = Hub(state, types.SimpleNamespace())
        h.hubs_registry, h.main_state = os.path.join(tmp, "reg"), state
        h.execute = lambda a: None
        rec = h.record
        rec("session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **OWNER})
        rec("owner.settings", None, {"parallel": 3, "channel": "core"})
        rec("session.requested", "maps", {"cwd": path, "session_id": "s-m", "role": "agent", **OWNER})
        rec("session.started", "maps", {})
        rec("session.folder", "maps", {"channel": "core", "git": "ok", "dirty": 0}, src="core")
        rec("task.run_requested", "maps-fix", {"task": "do it", "channel": "session", "owner": False, "sender": "maps"})
        for _ in range(50):
            h.run_tasks()
            if "maps-fix" in h.state["sessions"]:
                break
            time.sleep(0.1)
        x = h.state["sessions"]["maps-fix"]
        self.assertEqual((x["cwd"], x["worktree"]["branch"]),
                         (os.path.join(path, ".claude", "worktrees", "maps-fix"), "task/maps-fix"))
        h.publish_folders()
        self.assertEqual(marks(path), [])
        self.assertFalse(os.path.exists(os.path.join(path, ".git", worktree.LOCK)))
        self.assertFalse(os.path.exists(os.path.join(state, instance.FOLDERS)))
        self.assertFalse(os.path.exists(instance.registry_dir()) and os.listdir(instance.registry_dir()))


if __name__ == "__main__":
    unittest.main()
