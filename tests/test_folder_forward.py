"""The core fast-forwards a lead's folder (core/daemon.forward_folders, hosts/worktree.forwardable/forward): a
specialist's or direction's folder stays on the commit it was created from, and "Files" shows the old files (11-07: a
direction's copy 266 commits behind main). Only `merge --ff-only`, only a clean folder on the default branch or on
task/<name> without commits of its own, only while every live session in the folder is free; the target is
origin/<default> when it is ahead of the local branch, otherwise the local one, after a short fetch. While git runs (in the
background) nothing is typed to the sessions of the folder, their spawn and the executors raised from it wait."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import platforms
from core import journal, minihub, reducer
from hosts import worktree

OWNER = {"channel": "cli", "owner": True}


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}).stdout.strip()


def commit(cwd, name, text):
    with open(os.path.join(cwd, name), "w") as f:
        f.write(text)
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", f"{name}: {text}")
    return git(cwd, "rev-parse", "HEAD")


def head(cwd):
    return git(cwd, "rev-parse", "HEAD")


class Repo:
    """A repo with main and a direction's copy .claude/worktrees/lead on task/lead, main one commit ahead of the copy."""

    def __init__(self, case):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="fwd-"))
        self.main = os.path.join(self.root, "proj")
        os.makedirs(self.main)
        git(self.main, "init", "-q", "-b", "main")
        self.v1 = commit(self.main, "PLAN.md", "step 1 (old)")
        self.copy = os.path.join(self.main, ".claude", "worktrees", "lead")
        git(self.main, "worktree", "add", "-q", "-b", "task/lead", self.copy)
        self.v2 = commit(self.main, "PLAN.md", "step 2 (new)")
        case.addCleanup(platforms.current.rmtree, self.root)

    def origin_ahead(self):
        """A bare origin with main one commit ahead of the local main (fetched: refs/remotes/origin/main exists)."""
        bare = os.path.join(self.root, "origin.git")
        git(self.root, "clone", "-q", "--bare", self.main, bare)
        other = os.path.join(self.root, "other")
        git(self.root, "clone", "-q", bare, other)
        self.v3 = commit(other, "NEW.md", "from origin")
        git(other, "push", "-q", "origin", "main")
        git(self.main, "remote", "add", "origin", bare)
        git(self.main, "fetch", "-q", "origin")
        git(self.main, "remote", "set-head", "origin", "main")
        return self.v3


class Plan(unittest.TestCase):
    """hosts/worktree.forwardable: the matrix of conditions."""

    def setUp(self):
        self.r = Repo(self)

    def test_a_copy_on_its_task_branch_goes_to_the_local_main(self):
        plan, why = worktree.forwardable(self.r.copy, ["lead"])
        self.assertIsNone(why)
        self.assertEqual((plan["branch"], plan["head"], plan["target"], plan["ref"], plan["count"]),
                         ("task/lead", self.r.v1, self.r.v2, "main", 1))
        self.assertTrue(worktree.same(plan["top"], self.r.copy))

    def test_origin_ahead_of_the_local_main_is_the_target(self):
        v3 = self.r.origin_ahead()
        plan, why = worktree.forwardable(self.r.copy, ["lead"])
        self.assertEqual((plan["target"], plan["ref"], plan["count"]), (v3, "origin/main", 2))
        plan, why = worktree.forwardable(self.r.main, ["x"])     # the main folder on main: to origin/main
        self.assertEqual((plan["branch"], plan["head"], plan["target"]), ("main", self.r.v2, v3))

    def test_origin_is_fetched_before_the_target_is_chosen_and_an_unreachable_one_is_left_as_it_was(self):
        self.r.origin_ahead()
        other = os.path.join(self.r.root, "other")
        v4 = commit(other, "NEWER.md", "pushed after the last fetch")
        git(other, "push", "-q", "origin", "main")
        worktree._fetched.clear()
        plan, why = worktree.forwardable(self.r.copy, ["lead"])
        self.assertEqual((plan["target"], plan["ref"]), (v4, "origin/main"))
        git(self.r.main, "remote", "set-url", "origin", os.path.join(self.r.root, "gone.git"))
        worktree._fetched.clear()
        plan, why = worktree.forwardable(self.r.copy, ["lead"])   # no network: what was downloaded, nothing thrown
        self.assertEqual((plan["target"], plan["ref"]), (v4, "origin/main"))

    def test_a_fetch_that_hangs_or_fails_leaves_the_plan_as_it_was_and_is_not_tried_again_at_once(self):
        self.r.origin_ahead()
        worktree._fetched.clear()
        with mock.patch.object(worktree.codestore, "git", side_effect=RuntimeError("git: no answer within 20 s")) as g:
            plans = [worktree.forwardable(self.r.copy, ["lead"]) for _ in range(2)]
        self.assertEqual(g.call_count, 1)
        self.assertTrue(all(p["ref"] == "origin/main" for p, _ in plans))   # what was downloaded earlier

    def test_a_lead_folder_does_not_fetch_more_often_than_every_few_minutes(self):
        self.r.origin_ahead()
        worktree._fetched.clear()
        with mock.patch.object(worktree.codestore, "git", wraps=worktree.codestore.git) as g:
            for _ in range(3):
                worktree.forwardable(self.r.copy, ["lead"])
        self.assertEqual(sum(1 for c in g.call_args_list if "fetch" in c.args), 1)

    def test_origin_behind_or_diverged_the_local_main_is_the_target(self):
        self.r.origin_ahead()
        commit(self.r.main, "LOCAL.md", "only here")                  # diverged: origin is not ahead
        local = head(self.r.main)
        plan, _ = worktree.forwardable(self.r.copy, ["lead"])
        self.assertEqual((plan["target"], plan["ref"]), (local, "main"))
        self.assertEqual(worktree.forwardable(self.r.main, ["x"]), (None, "level"))   # main is main

    def test_nothing_to_do_when_level(self):
        git(self.r.copy, "merge", "-q", "--ff-only", "main")
        self.assertEqual(worktree.forwardable(self.r.copy, ["lead"]), (None, "level"))

    def test_own_commits_are_not_touched(self):
        commit(self.r.copy, "MINE.md", "lead's spec")
        self.assertEqual(worktree.forwardable(self.r.copy, ["lead"]), (None, "own"))

    def test_uncommitted_and_untracked_are_not_touched(self):
        with open(os.path.join(self.r.copy, "PLAN.md"), "w") as f:
            f.write("edited")
        self.assertEqual(worktree.forwardable(self.r.copy, ["lead"]), (None, "dirty"))
        git(self.r.copy, "checkout", "--", "PLAN.md")
        with open(os.path.join(self.r.copy, "draft.md"), "w") as f:
            f.write("new")
        self.assertEqual(worktree.forwardable(self.r.copy, ["lead"]), (None, "dirty"))

    def test_executors_copies_are_not_changes_of_the_main_folder(self):
        git(self.r.main, "checkout", "-q", "--detach")
        git(self.r.main, "checkout", "-q", "-b", "task/spec", self.r.v1)   # the main folder behind on task/spec
        plan, why = worktree.forwardable(self.r.main, ["spec"])
        self.assertIsNone(why)                                           # .claude/worktrees/lead is not dirt
        self.assertEqual(plan["target"], self.r.v2)

    def test_detached_head_and_a_foreign_branch_are_not_touched(self):
        self.assertEqual(worktree.forwardable(self.r.copy, ["other"]), (None, "branch"))   # task/lead is not task/other
        git(self.r.copy, "checkout", "-q", "-b", "fix/thing")
        self.assertEqual(worktree.forwardable(self.r.copy, ["lead"]), (None, "branch"))
        git(self.r.copy, "checkout", "-q", "--detach")
        self.assertEqual(worktree.forwardable(self.r.copy, ["lead"]), (None, "detached"))

    def test_not_git_and_gone(self):
        plain = tempfile.mkdtemp(dir=self.r.root)
        self.assertEqual(worktree.forwardable(plain, ["x"]), (None, "not_git"))
        self.assertEqual(worktree.forwardable(os.path.join(plain, "no"), ["x"]), (None, "gone"))


class Forward(unittest.TestCase):
    """hosts/worktree.forward: checks again right before git, only --ff-only."""

    def setUp(self):
        self.r = Repo(self)
        self.plan, _ = worktree.forwardable(self.r.copy, ["lead"])

    def test_fast_forwards_the_files(self):
        self.assertEqual(worktree.forward(self.plan), (True, None, "done"))
        self.assertEqual(head(self.r.copy), self.r.v2)
        with open(os.path.join(self.r.copy, "PLAN.md")) as f:
            self.assertEqual(f.read(), "step 2 (new)")
        self.assertEqual(git(self.r.copy, "status", "--porcelain"), "")

    def test_a_change_after_the_plan_holds_it(self):
        with open(os.path.join(self.r.copy, "PLAN.md"), "w") as f:
            f.write("edited after the plan")
        ok, why, step = worktree.forward(self.plan)
        self.assertEqual((ok, step), (False, None))
        self.assertIn("uncommitted", why)
        self.assertEqual(head(self.r.copy), self.r.v1)
        with open(os.path.join(self.r.copy, "PLAN.md")) as f:
            self.assertEqual(f.read(), "edited after the plan")

    def test_a_commit_after_the_plan_holds_it(self):
        commit(self.r.copy, "MINE.md", "x")
        mine = head(self.r.copy)
        ok, why, step = worktree.forward(self.plan)
        self.assertEqual((ok, step), (False, None))
        self.assertEqual(head(self.r.copy), mine)

    def test_a_merge_killed_by_its_timeout_says_so(self):
        real = worktree._git

        def slow(cwd, *args, **kw):
            if "merge" in args:
                raise subprocess.TimeoutExpired(["git", "merge"], kw.get("timeout"))
            return real(cwd, *args, **kw)
        with mock.patch.object(worktree, "_git", slow):
            ok, why, step = worktree.forward(self.plan, timeout=7)
        self.assertEqual((ok, step), (False, "timeout"))
        self.assertIn("7 s", why)

    def test_a_refused_merge_is_not_a_half_checkout(self):
        lock = os.path.join(git(self.r.copy, "rev-parse", "--absolute-git-dir"), "index.lock")
        open(lock, "w").close()                                       # the owner's own git holds the index
        ok, why, step = worktree.forward(self.plan)
        self.assertEqual((ok, step, head(self.r.copy)), (False, "refused", self.r.v1))
        os.remove(lock)

    def test_an_ignored_file_the_target_tracks_is_not_overwritten(self):
        # release review 2026-11-09: git overwrites ignored files by default; the owner's ignored local.cfg is not the
        # core's to replace — the merge is refused and the file stays
        commit(self.r.main, "local.cfg", "upstream")
        exclude = os.path.join(git(self.r.main, "rev-parse", "--path-format=absolute", "--git-common-dir"), "info", "exclude")
        os.makedirs(os.path.dirname(exclude), exist_ok=True)
        with open(exclude, "a") as f:
            f.write("local.cfg\n")
        with open(os.path.join(self.r.copy, "local.cfg"), "w") as f:
            f.write("the owner's own")
        plan, why = worktree.forwardable(self.r.copy, ["lead"])
        self.assertIsNone(why)
        ok, why, step = worktree.forward(plan)
        self.assertEqual((ok, step, head(self.r.copy)), (False, "refused", self.r.v1))
        with open(os.path.join(self.r.copy, "local.cfg")) as f:
            self.assertEqual(f.read(), "the owner's own")

    def test_the_repos_hooks_do_not_run(self):
        hooks = git(self.r.main, "rev-parse", "--git-common-dir")
        hook = os.path.join(self.r.main, hooks, "hooks", "post-merge")
        os.makedirs(os.path.dirname(hook), exist_ok=True)
        ran = os.path.join(self.r.root, "hook-ran")
        with open(hook, "w") as f:
            f.write(f"#!/bin/sh\ntouch '{ran}'\n")
        os.chmod(hook, 0o755)
        self.assertTrue(worktree.forward(self.plan)[0])
        self.assertFalse(os.path.exists(ran))
        git(self.r.copy, "reset", "-q", "--hard", self.r.v1)                # a merge of the owner's own runs it
        git(self.r.copy, "merge", "-q", "--ff-only", "main")
        self.assertTrue(os.path.exists(ran))

    def test_a_renamed_session_keeps_its_copys_branch(self):
        self.assertEqual(worktree.forwardable(self.r.copy, ["lead-v2"]), (None, "branch"))
        self.assertIsNone(worktree.forwardable(self.r.copy, ["lead-v2", "lead"])[1])


class Fold(unittest.TestCase):
    """The facts session.folder_forwarding / session.folder_forwarded in the fold."""

    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.started", "hub")
        for name in ("lead", "nb"):
            self.ev("session.requested", name, cwd="/p", session_id=f"s-{name}", role="agent", about=name, **OWNER)
            self.ev("session.started", name)

    def ev(self, type, entity=None, src=None, ts=None, **data):
        self.seq += 1
        e = {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type, "entity": entity, "data": data,
             **({"src": src} if src else {})}
        self.st, acts, why = reducer.reduce(self.st, e)
        self.actions += acts
        return why

    def fact(self, kind, sessions=("lead", "nb"), **extra):
        return self.ev(kind, "lead", src="core", channel="core", path="/p", branch="task/lead", **{"from": "a" * 40},
                       to="b" * 40, ref="main", count=3, sessions=list(sessions), **extra)

    def typed(self, name):
        return [a for a in self.actions if a["do"] == "type" and a["session"] == name]

    def test_only_the_core_and_only_free_sessions(self):
        self.assertIsNotNone(self.ev("session.folder_forwarding", "lead", channel="cli", sessions=["lead"], path="/p"))
        self.ev("message.received", None, to="nb", text="work", **OWNER)        # nb has a turn now
        self.assertIsNotNone(self.fact("session.folder_forwarding"))
        self.assertNotIn("forwarding", self.st["sessions"]["lead"])
        self.assertIsNone(self.fact("session.folder_forwarding", sessions=["lead"]))
        self.assertEqual(self.st["sessions"]["lead"]["forwarding"]["path"], "/p")

    def test_typing_waits_for_the_result(self):
        self.assertIsNone(self.fact("session.folder_forwarding"))
        self.ev("message.received", None, to="lead", text="what is new?", **OWNER)
        self.assertEqual(self.typed("lead"), [])                                  # git is moving its files
        self.assertIsNone(self.fact("session.folder_forwarded", ok=True))
        (typed,) = self.typed("lead")
        self.assertIn("what is new?", typed["text"])
        self.assertIn("fast-forwarded", typed["text"])                            # the quiet line rides with it
        self.assertNotIn("forwarding", self.st["sessions"]["lead"])

    def test_the_hold_ends_by_itself(self):
        self.assertIsNone(self.fact("session.folder_forwarding"))
        self.ev("message.received", None, to="lead", text="later", **OWNER)
        self.ev("core.slept", None, ts=1000 + self.seq + reducer.FORWARD_HOLD + 1, seconds=1)
        self.assertEqual(len(self.typed("lead")), 1)

    def test_a_sleep_of_the_machine_does_not_end_the_hold(self):
        self.assertIsNone(self.fact("session.folder_forwarding"))
        self.ev("message.received", None, to="lead", text="after the sleep", **OWNER)
        self.ev("core.slept", None, ts=1000 + self.seq + reducer.FORWARD_HOLD + 1, seconds=reducer.FORWARD_HOLD)
        self.assertEqual(self.typed("lead"), [])                                  # git slept with the machine

    def test_a_quiet_line_to_each_and_no_wake(self):
        self.ev("session.parked", "nb", src="core", channel="core", why="idle")
        self.assertIsNone(self.fact("session.folder_forwarding"))
        n = len(self.actions)
        self.assertIsNone(self.fact("session.folder_forwarded", ok=True))
        self.assertEqual(self.actions[n:], [])                                    # nobody spawned, nothing typed
        self.assertEqual(self.st["sessions"]["nb"]["state"], "parked")
        lines = {m["session"]: m for m in self.st["messages"].values() if m.get("forwarded")}
        self.assertEqual(set(lines), {"lead", "nb"})
        self.assertEqual(lines["lead"]["state"], "aside")
        self.assertIn("aaaaaaaa..bbbbbbbb", lines["lead"]["text"])

    def test_two_moves_before_the_next_message_are_one_line(self):
        for a, b, n in (("a", "b", 3), ("b", "c", 2)):
            self.assertIsNone(self.ev("session.folder_forwarding", "lead", src="core", channel="core", path="/p",
                                      sessions=["lead"]))
            self.assertIsNone(self.ev("session.folder_forwarded", "lead", src="core", channel="core", path="/p",
                                      branch="task/lead", ref="main", **{"from": a * 40}, to=b * 40, count=n,
                                      sessions=["lead"], ok=True))
        (line,) = [m for m in self.st["messages"].values() if m.get("forwarded")]
        self.assertIn("aaaaaaaa..cccccccc", line["text"])
        self.assertIn("5", line["text"])

    def test_a_compacting_session_holds_the_move(self):
        self.st["sessions"]["lead"]["compacting"] = 1000 + self.seq
        self.assertIsNotNone(self.fact("session.folder_forwarding", sessions=["lead"]))
        self.st["sessions"]["lead"].pop("compacting")                            # the compaction ended
        self.assertIsNone(self.fact("session.folder_forwarding", sessions=["lead"]))

    def test_git_stopped_halfway_is_said(self):
        self.assertIsNone(self.fact("session.folder_forwarding"))
        self.assertIsNone(self.fact("session.folder_forwarded", ok=False, touched=True, why="TimeoutExpired"))
        lines = [m for m in self.st["messages"].values() if "git status" in m["text"]]
        self.assertEqual({m["session"] for m in lines}, {"lead", "nb"})
        self.assertEqual({m["state"] for m in lines}, {"aside"})
        self.assertIsNone(self.fact("session.folder_forwarding"))
        self.assertIsNone(self.fact("session.folder_forwarded", ok=False, touched=True, why="TimeoutExpired"))
        self.assertEqual(len([m for m in self.st["messages"].values() if "git status" in m["text"]]), 2)   # unread: one

    def test_a_failure_says_nothing_to_the_sessions(self):
        self.assertIsNone(self.fact("session.folder_forwarding"))
        self.assertIsNone(self.fact("session.folder_forwarded", ok=False, why="git: refused"))
        self.assertFalse([m for m in self.st["messages"].values() if m.get("forwarded")])
        self.assertNotIn("forwarding", self.st["sessions"]["lead"])


class Host:
    kind, screen_dialogs = "stream", False

    def __init__(self):
        self.calls = []
        self.tool_flags = lambda **kw: []

    def __getattr__(self, do):
        if do.startswith("_"):
            raise AttributeError(do)
        return lambda name, **kw: self.calls.append((do, name, kw)) or "sent"

    def alive(self, name):
        return True

    def poll(self):
        return []


class Daemon(unittest.TestCase):
    """core/daemon.forward_folders on real git: the background, the hold, the facts in the journal."""

    def setUp(self):
        from core.daemon import Hub
        # the platform gate (Windows: off until a probe) has its own test below; the rest is the logic, on every system
        patch = mock.patch.object(platforms.current, "FORWARDS_FOLDERS", True, create=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.r = Repo(self)
        self.host = Host()
        self.h = Hub(tempfile.mkdtemp(prefix="fwd-d-"), self.host, registry_dir=tempfile.mkdtemp())
        self.h.record("session.requested", "hub", {"cwd": self.r.root, "session_id": "s-hub", "kind": "hub", **OWNER})
        self.lead("lead", self.r.copy)

    def lead(self, name, cwd, role="agent"):
        self.h.record("session.requested", name, {"cwd": cwd, "session_id": f"s-{name}", "role": role, "about": name,
                                                  **OWNER})
        self.h.record("session.started", name, {})

    def facts(self, kind):
        return [e for e in journal.read(os.path.join(self.h.state_dir, "journal.jsonl")) if e["type"] == kind]

    def settle(self, rounds=20):
        """Steps until the background is done: a scan, then git, then the result."""
        for _ in range(rounds):
            self.h.folders_fwd_at.clear()
            self.h.forward_folders()
            for t in (self.h.fwd_scan, (self.h.fwd_job or {}).get("thread")):
                if t is not None:
                    t.join(30)
            if self.h.fwd_job is None and self.facts("session.folder_forwarded"):
                break
        else:
            self.h.forward_folders()
        if self.h.fwd_scan is not None:
            self.h.fwd_scan.join(30)

    def test_a_free_lead_gets_its_folder_forwarded_and_a_quiet_line(self):
        self.settle()
        self.assertEqual(head(self.r.copy), self.r.v2)
        (start,), (done,) = self.facts("session.folder_forwarding"), self.facts("session.folder_forwarded")
        self.assertEqual((done["entity"], done["data"]["ok"], done["data"]["from"], done["data"]["to"]),
                         ("lead", True, self.r.v1, self.r.v2))
        self.assertEqual(done["data"]["sessions"], ["lead"])
        (line,) = [m for m in self.h.state["messages"].values() if m.get("forwarded")]
        self.assertEqual((line["session"], line["state"]), ("lead", "aside"))
        self.assertEqual([c for c in self.host.calls if c[0] == "type"], [])   # no turn for it
        self.settle(rounds=2)
        self.assertEqual(len(self.facts("session.folder_forwarding")), 1)   # level now — no more facts

    def test_a_folder_that_is_not_git_is_skipped_quietly(self):   # a clean install: agents' folders are no repositories
        import contextlib
        import io
        plain = tempfile.mkdtemp(prefix="fwd-plain-")
        self.addCleanup(platforms.current.rmtree, plain)
        self.lead("plain", plain)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.settle(rounds=3)
        self.assertEqual(self.h.fwd_said.get("plain"), "not_git")   # remembered, not said
        self.assertNotIn("not fast-forwarded", out.getvalue())

    def test_a_busy_neighbour_in_the_folder_holds_it(self):
        self.lead("nb", os.path.join(self.r.copy), role="executor")
        self.h.record("message.received", None, {"to": "nb", "text": "work", **OWNER})
        self.settle(rounds=3)
        self.assertEqual(head(self.r.copy), self.r.v1)
        self.assertEqual(self.facts("session.folder_forwarding"), [])

    def test_a_busy_lead_is_not_forwarded(self):
        self.h.record("message.received", None, {"to": "lead", "text": "work", **OWNER})
        self.settle(rounds=3)
        self.assertEqual(head(self.r.copy), self.r.v1)

    def test_a_dirty_folder_is_untouched(self):
        with open(os.path.join(self.r.copy, "PLAN.md"), "w") as f:
            f.write("my edit")
        self.settle(rounds=3)
        self.assertEqual(head(self.r.copy), self.r.v1)
        with open(os.path.join(self.r.copy, "PLAN.md")) as f:
            self.assertEqual(f.read(), "my edit")
        self.assertEqual(self.facts("session.folder_forwarding"), [])

    def test_a_turn_taken_mid_forward_waits_for_git(self):
        gate, inside = threading.Event(), threading.Event()
        real = worktree.forward

        def slow(plan, **kw):
            inside.set()
            gate.wait(30)
            return real(plan, **kw)
        with mock.patch.object(worktree, "forward", slow):
            for _ in range(5):
                self.h.folders_fwd_at.clear()
                self.h.forward_folders()
                if self.h.fwd_scan is not None:
                    self.h.fwd_scan.join(30)
                if inside.wait(0.2):
                    break
            self.assertTrue(inside.is_set())
            self.h.record("message.received", None, {"to": "lead", "text": "go on", **OWNER})
            self.assertEqual([c for c in self.host.calls if c[0] == "type"], [])   # held while git runs
            self.h.record("task.run_requested", "lead-x", {"task": "fix", "channel": "session", "owner": False,
                                                           "sender": "lead"})
            self.h.run_tasks()
            self.assertNotIn("lead-x", self.h.preparing)                    # its executor waits too
            gate.set()
            self.h.fwd_job["thread"].join(30)
            self.h.forward_folders()
        (typed,) = [c for c in self.host.calls if c[0] == "type"]
        self.assertIn("go on", typed[2]["text"])
        self.assertEqual(head(self.r.copy), self.r.v2)

    def test_a_spawn_waits_for_git(self):
        self.h.record("session.parked", "lead", {"channel": "core", "why": "idle"})
        self.h.record("session.folder_forwarding", "lead", {"channel": "core", "path": self.r.copy, "sessions": ["lead"]})
        spawns = len([c for c in self.host.calls if c[0] == "spawn"])
        self.h.record("message.received", None, {"to": "lead", "text": "wake", **OWNER})
        self.assertEqual(len([c for c in self.host.calls if c[0] == "spawn"]), spawns)
        self.assertTrue(self.h.delayed)
        self.h.record("session.folder_forwarded", "lead", {"channel": "core", "path": self.r.copy, "sessions": ["lead"],
                                                           "ok": False})
        self.h.delayed = [(0, a) for _, a in self.h.delayed]
        self.h.run_delayed()
        self.assertEqual(len([c for c in self.host.calls if c[0] == "spawn"]), spawns + 1)

    def test_a_renamed_lead_is_forwarded_on_its_copys_branch(self):
        self.h.state["sessions"]["lead"]["worktree"] = {"path": self.r.copy, "branch": "task/lead"}   # as a direction's
        self.h.spare_of = None   # a change outside the fold: the spare state is copied anew
        self.h.record("session.renamed", "lead", {"to": "lead-v2", **OWNER})
        self.h.record("session.started", "lead-v2", {})
        self.assertIn("lead-v2", self.h.state["sessions"])
        self.settle()
        self.assertEqual(head(self.r.copy), self.r.v2)

    def test_a_git_timeout_is_a_failure_with_a_line(self):
        def stuck(plan, **kw):
            return False, "git was stopped", "timeout"
        with mock.patch.object(worktree, "forward", stuck):
            self.settle()
        (done,) = self.facts("session.folder_forwarded")
        self.assertEqual((done["data"]["ok"], done["data"]["touched"]), (False, True))
        self.h.forward_folders()                                        # the next try — not in a minute
        self.assertIsNone(self.h.fwd_scan)
        self.assertTrue([m for m in self.h.state["messages"].values() if "git status" in m["text"]])
        self.assertNotIn("forwarding", self.h.state["sessions"]["lead"])

    def test_a_failure_before_the_merge_is_not_said_and_is_retried_soon(self):
        def moved(plan, **kw):
            return False, "the folder changed", None
        with mock.patch.object(worktree, "forward", moved):
            self.settle()
        (done,) = self.facts("session.folder_forwarded")
        self.assertNotIn("touched", done["data"])
        self.assertFalse([m for m in self.h.state["messages"].values() if "git status" in m["text"]])
        self.assertLess(self.h.folders_fwd_at["lead"], self.h.clock())          # the next pass, not in an hour

    def test_an_interrupted_forward_is_closed_after_a_restart(self):
        self.h.record("session.folder_forwarding", "lead", {"channel": "core", "path": self.r.copy, "sessions": ["lead"]})
        self.h.forward_folders()                                       # no job of this process holds the mark
        (done,) = self.facts("session.folder_forwarded")
        self.assertFalse(done["data"]["ok"])
        self.assertNotIn("forwarding", self.h.state["sessions"]["lead"])

    def test_after_a_restart_a_running_git_of_the_old_process_keeps_the_hold(self):
        self.h.record("session.folder_forwarding", "lead", {"channel": "core", "path": self.r.copy, "sessions": ["lead"]})
        lock = os.path.join(git(self.r.copy, "rev-parse", "--absolute-git-dir"), "index.lock")
        open(lock, "w").close()
        self.h.forward_folders()
        self.assertEqual(self.facts("session.folder_forwarded"), [])
        self.assertIn("forwarding", self.h.state["sessions"]["lead"])
        os.remove(lock)
        self.h.forward_folders()
        self.assertEqual(len(self.facts("session.folder_forwarded")), 1)

    def test_not_on_windows_not_switched_off_not_the_hubs_own_code(self):
        from core import catalog
        for patch in (mock.patch.object(platforms.current, "FORWARDS_FOLDERS", False, create=True),
                      mock.patch.dict(self.h.config, {"forward_folders": False}),
                      mock.patch.object(catalog, "REPO", self.r.copy)):
            with patch, mock.patch.object(worktree, "forward", side_effect=AssertionError("git on a closed path")):
                self.settle(rounds=3)
            self.assertEqual(head(self.r.copy), self.r.v1)
            self.assertEqual(self.facts("session.folder_forwarding"), [])


class Platforms(unittest.TestCase):
    """The gate by system: no patch of FORWARDS_FOLDERS here (on Windows `platforms.current` is the module read below)."""

    def test_the_windows_platform_says_no(self):
        from platforms import posix, windows
        self.assertTrue(posix.FORWARDS_FOLDERS)
        self.assertFalse(windows.FORWARDS_FOLDERS)


if __name__ == "__main__":
    unittest.main()
