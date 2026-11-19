"""Codex (experimental): an executor hands over with a merge. The
executor's copy remembers its base — the branch the raiser's folder was on (`base`, `base_sha`); the hand-over rule
names it instead of the word "main"; on an executor's result the daemon checks whether task/<name> is merged into the
base and writes `executor.unmerged`; the fold sends one reminder to the executor, the second time a line to the raiser;
cleaning up the copy checks against the same base. The daemon runs on real git."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from core import executors, journal, reducer
from core.daemon import Hub
from hosts import worktree

OWNER = {"channel": "cli", "owner": True}
CORE = {"channel": "core", "owner": False}


def git(cwd, *a):
    return subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True,
                          capture_output=True, text=True).stdout.strip()


def repo(tmp, branch="main"):
    path = os.path.join(tmp, "repo")
    git(tmp, "init", "-q", "-b", branch, path)
    with open(os.path.join(path, "a.txt"), "w") as f:
        f.write("a\n")
    git(path, "add", "a.txt")
    git(path, "commit", "-q", "-m", "init")
    return path


def commit(path, name="b.txt"):
    with open(os.path.join(path, name), "w") as f:
        f.write("x\n")
    git(path, "add", name)
    git(path, "commit", "-q", "-m", name)


class Base(unittest.TestCase):
    """The copy's base: the branch of the raiser's folder at the moment it was raised."""
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="unmerged-"))

    def test_base_is_the_branch_the_raisers_folder_is_on(self):
        top = repo(self.tmp, "master")
        git(top, "switch", "-q", "-c", "exp/x")
        _, info, err = worktree.prepare(top, "maps-a", with_base=True)
        self.assertIsNone(err)
        self.assertEqual(info["base"], "exp/x")
        self.assertEqual(info["base_sha"], git(top, "rev-parse", "exp/x"))

    def test_detached_head_falls_back_to_the_main_branch(self):
        top = repo(self.tmp, "master")
        git(top, "switch", "-q", "--detach", "HEAD")
        _, info, err = worktree.prepare(top, "maps-a", with_base=True)
        self.assertIsNone(err)
        self.assertEqual(info["base"], "master")

    def test_main_branch_only_on_origin_is_no_base(self):
        top = repo(self.tmp, "trunk")
        git(top, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(top, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        git(top, "switch", "-q", "--detach", "HEAD")
        _, info, err = worktree.prepare(top, "maps-a", with_base=True)
        self.assertIsNone(err)
        self.assertEqual(info.get("base"), "")

    def test_detached_head_without_a_main_branch_has_no_base(self):
        top = repo(self.tmp, "trunk")
        git(top, "switch", "-q", "--detach", "HEAD")
        _, info, err = worktree.prepare(top, "maps-a", with_base=True)
        self.assertIsNone(err)
        self.assertEqual(info.get("base"), "")

    def test_existing_branch_copy_has_no_base(self):
        top = repo(self.tmp)
        git(top, "branch", "feat/x")
        _, info, err = worktree.prepare(top, "maps-a", branch="feat/x", with_base=True)
        self.assertIsNone(err)
        self.assertNotIn("base", info)

    def test_report_rule_names_the_base_instead_of_main(self):
        st = reducer.empty()
        run = {"task": "do it", "by": None}
        (_, _, sess), (_, _, task) = executors.events_for(st, "x", run, "/r/.claude/worktrees/x",
                                                          {"path": "/r/.claude/worktrees/x", "base": "exp/x"})
        self.assertIn("rebase on a fresh branch exp/x", task["context"])
        self.assertIn("`git merge --ff-only` into exp/x", task["context"])
        self.assertNotIn("into main", task["context"])
        # not git or no base — the old text
        (_, _, _), (_, _, task) = executors.events_for(st, "y", run, "/w", None)
        self.assertIn("`git merge --ff-only` into main", task["context"])


class ClaudeHubAsMain(unittest.TestCase):
    """The slice is only for a hub on Codex (hub.json "host": "codex"): on a Claude hub an executor raised from another
    branch behaves as in main — its copy has no base, the hand-over rule says main word for word, no reminder about an
    unmerged branch, cleanup keeps the unmerged branch with `git branch -d`."""
    MAIN_COPY = {"path", "branch", "repo", "checks", "commits"}   # the keys main's prepare writes without --branch

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="unmerged-"))
        self.repo = repo(self.tmp, "master")
        git(self.repo, "switch", "-q", "-c", "exp/x")

    def test_prepare_by_default_has_no_base(self):
        _, info, err = worktree.prepare(self.repo, "maps-a")
        self.assertIsNone(err)
        self.assertEqual(set(info), self.MAIN_COPY)
        _, info, err = worktree.prepare(self.repo, "maps-b", with_base=True)   # what a Codex hub asks for
        self.assertEqual(info["base"], "exp/x")

    def test_daemon_of_a_claude_hub(self):
        from unittest import mock
        conf = os.path.join(self.tmp, "hub.json")
        with open(conf, "w") as f:
            f.write("{}")
        with mock.patch.dict(os.environ, {"HUB_CONFIG": conf}):
            h = Hub(os.path.join(self.tmp, "state"), types.SimpleNamespace())
        h.execute = lambda a: None
        h.PREPARE_WAIT = 60   # the copy is git in the background: a slow machine (Windows, under load) needs more than the tick's 2 s
        rec = h.record
        rec("session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **OWNER})
        rec("session.requested", "maps", {"cwd": self.repo, "session_id": "s-m", "role": "agent", **OWNER})
        rec("session.started", "maps", {})
        rec("task.run_requested", "maps-a", {"task": "do it", "channel": "session", "owner": False, "sender": "maps"})
        h.run_tasks()
        x = h.state["sessions"]["maps-a"]
        (d,) = [d for d in h.state["delegations"].values() if d["session"] == "maps-a"]
        self.assertEqual(set(x["worktree"]), self.MAIN_COPY)
        self.assertTrue(d["context"].startswith("do it\n\n" + executors.REPORT_RULE.format(raiser="maps")))
        commit(x["cwd"])   # the executor committed and did not merge
        rec("session.started", "maps-a", {})
        rec("turn.started", "maps-a", {"marker": d["message"]})
        rec("turn.result", "maps-a", {"text": "did it"})
        rec("turn.stopped", "maps-a", {})
        self.assertEqual(executors.unmerged_due(h.state), [])
        h.check_merged()
        self.assertIsNone(h.merged_worker)                         # no git in the background, no fact
        self.assertEqual([e for e in journal.read(h.journal.path) if e["type"] == "executor.unmerged"], [])
        ok, why = worktree.remove(x["worktree"])
        self.assertEqual((ok, why), (True, "the working copy was removed, the branch task/maps-a kept: it has "
                                           "commits merged nowhere"))


    def test_the_daemon_steps_of_codex_do_nothing_on_a_claude_hub(self):
        # not even a pass over the sessions per tick: a Claude hub's tick is main's (an executor with a base — a copy
        # restored from a hub that was on Codex — is not looked at either)
        conf = os.path.join(self.tmp, "hub.json")
        with open(conf, "w") as f:
            f.write("{}")
        with mock.patch.dict(os.environ, {"HUB_CONFIG": conf}):
            os.environ.pop("HUB_CODEX", None)
            h = Hub(os.path.join(self.tmp, "state"), types.SimpleNamespace())
        boom = mock.Mock(side_effect=AssertionError("looked"))
        with mock.patch.object(executors, "unmerged_due", boom), mock.patch("core.reducer.codex_due", boom):
            h.check_merged()
            h.codex_tick()
        boom.assert_not_called()


class Merged(unittest.TestCase):
    """hosts/worktree.merged: whether the copy's branch is merged into the base."""
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="unmerged-"))
        self.top = repo(self.tmp, "master")
        self.path, self.info, _ = worktree.prepare(self.top, "maps-a", with_base=True)

    def test_no_commits_is_merged(self):
        self.assertEqual(worktree.merged(self.info), (0, False, None))

    def test_commits_ahead_are_not_merged_until_ff(self):
        commit(self.path)
        commit(self.path, "c.txt")
        self.assertEqual(worktree.merged(self.info), (2, False, None))
        git(self.top, "merge", "-q", "--ff-only", "task/maps-a")
        self.assertEqual(worktree.merged(self.info), (0, False, None))

    def test_dirty_copy_without_commits_is_not_handed_over(self):
        with open(os.path.join(self.path, "a.txt"), "w") as f:
            f.write("changed\n")
        self.assertEqual(worktree.merged(self.info), (0, True, None))

    def test_only_compiled_bytecode_is_not_uncommitted(self):
        # the same rule as the cleanup (remove: _bytecode_junk) — a test run's __pycache__ does not make the copy unhanded
        cache = os.path.join(self.path, "tools", "__pycache__")
        os.makedirs(cache)
        with open(os.path.join(cache, "x.cpython-312.pyc"), "w") as f:
            f.write("x")
        self.assertEqual(worktree.merged(self.info), (0, False, None))
        with open(os.path.join(self.path, "notes.txt"), "w") as f:
            f.write("x")
        self.assertEqual(worktree.merged(self.info), (0, True, None))

    def test_git_error_is_not_a_fact(self):
        ahead, dirty, err = worktree.merged({**self.info, "base": "gone"})
        self.assertIsNotNone(err)

    def test_cleanup_keeps_the_branch_unmerged_into_the_base(self):
        # as on main: the copy goes, the branch with its commits stays (review 2026-10-29: keeping the copy saved nothing
        # the branch does not, and copies with node_modules piled up)
        commit(self.path)
        ok, why = worktree.remove(self.info)
        self.assertTrue(ok, why)
        self.assertIn("the branch task/maps-a kept", why)
        self.assertFalse(os.path.isdir(self.path))
        self.assertIn("task/maps-a", git(self.top, "branch", "--list"))

    def test_cleanup_with_the_base_gone_checks_as_before(self):
        # the base was deleted after the merge — the old `git branch -d` check (merged into the folder's HEAD): the branch is removed
        git(self.top, "switch", "-q", "-c", "exp/x")
        _, info, _ = worktree.prepare(self.top, "maps-b", with_base=True)
        self.assertEqual(info["base"], "exp/x")
        commit(info["path"])
        git(self.top, "merge", "-q", "--ff-only", "task/maps-b")
        git(self.top, "switch", "-q", "master")
        git(self.top, "merge", "-q", "--ff-only", "exp/x")
        git(self.top, "branch", "-q", "-D", "exp/x")
        ok, why = worktree.remove(info)
        self.assertTrue(ok, why)
        self.assertNotIn("task/maps-b", git(self.top, "branch", "--list"))

    def test_cleanup_of_a_branch_merged_into_the_base_not_into_head(self):
        # the raiser's folder moved to another branch — `git branch -d` would check against its HEAD; the base is master
        commit(self.path)
        git(self.top, "merge", "-q", "--ff-only", "task/maps-a")
        git(self.top, "switch", "-q", "-c", "other", "HEAD~1")
        ok, why = worktree.remove(self.info)
        self.assertTrue(ok, why)
        self.assertNotIn("task/maps-a", git(self.top, "branch", "--list"))


class Daemon(unittest.TestCase):
    """The daemon checks the executor's result, the fold reminds it and then tells the raiser (a hub on Codex)."""
    def setUp(self):
        from unittest import mock
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="unmerged-"))
        self.repo = repo(self.tmp, "master")
        conf = os.path.join(self.tmp, "hub.json")
        with open(conf, "w") as f:
            f.write('{"host": "codex"}')
        env = mock.patch.dict(os.environ, {"HUB_CONFIG": conf})
        env.start()
        self.addCleanup(env.stop)
        self.h = Hub(os.path.join(self.tmp, "state"), types.SimpleNamespace())
        self.h.execute = lambda a: None
        self.h.PREPARE_WAIT = 60   # as above: the session of the executor exists only once git made its copy
        rec = self.h.record
        rec("session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **OWNER})
        rec("session.requested", "maps", {"cwd": self.repo, "session_id": "s-m", "role": "agent", **OWNER})
        rec("session.started", "maps", {})
        rec("task.run_requested", "maps-a", {"task": "do it", "channel": "session", "owner": False, "sender": "maps"})
        self.h.run_tasks()
        self.x = self.h.state["sessions"]["maps-a"]
        (self.d,) = [d for d in self.h.state["delegations"].values() if d["session"] == "maps-a"]
        rec("session.started", "maps-a", {})

    def turn(self, marker, text="did it, merged"):
        rec = self.h.record
        rec("turn.started", "maps-a", {"marker": marker})
        rec("turn.result", "maps-a", {"text": text})
        rec("turn.stopped", "maps-a", {})

    def check(self, h=None):
        """Like two ticks: git runs in the background (core/daemon.check_merged), the next one writes the result."""
        h = h or self.h
        h.check_merged()
        if h.merged_worker:
            h.merged_worker.join(30)
        h.check_merged()

    def to(self, who, words):
        return [m for m in self.h.state["messages"].values() if m["session"] == who and words in m["text"]]

    def test_base_reaches_the_rule(self):
        self.assertEqual(self.x["worktree"]["base"], "master")
        self.assertIn("`git merge --ff-only` into master", self.d["context"])

    def test_a_task_quoting_the_main_rule_keeps_its_words(self):
        # only the rule is rewritten for the base: a brief that quotes the main rule (copied from an earlier task) stays
        # as written, the rule below it names the base
        quote = "A previous brief: " + executors.MERGE_MAIN + "."
        run = {"task": quote, "by": "maps", "ts": 1}
        (_, _, task), = [x for x in executors.events_for(self.h.state, "maps-b", run, cwd="/x",
                                                          worktree={"base": "exp/y", "path": "/x"}) if x[0] == "delegation.created"]
        words = task["context"]
        self.assertTrue(words.startswith(quote), words[:200])
        self.assertIn("`git merge --ff-only` into exp/y", words)

    def test_merged_result_writes_nothing(self):
        commit(self.x["cwd"])
        git(self.repo, "merge", "-q", "--ff-only", "task/maps-a")
        self.turn(self.d["message"])
        self.check()
        self.assertEqual([e for e in journal.read(self.h.journal.path) if e["type"] == "executor.unmerged"], [])

    def test_unmerged_reminds_once_then_tells_the_raiser(self):
        commit(self.x["cwd"])
        self.turn(self.d["message"])
        self.check()
        self.check()   # the same result — no second fact
        facts = [e for e in journal.read(self.h.journal.path) if e["type"] == "executor.unmerged"]
        self.assertEqual(len(facts), 1)
        self.assertEqual({k: facts[0]["data"][k] for k in ("branch", "base", "ahead", "dirty")},
                         {"branch": "task/maps-a", "base": "master", "ahead": 1, "dirty": False})
        (note,) = self.to("maps-a", "is not merged into master")
        self.assertEqual(note.get("for_delegation"), self.d["id"])
        self.assertEqual(self.to("maps", "is not merged into master"), [])
        # the reply to the reminder continues the task; still not merged — a line to the raiser, nothing to the executor
        self.turn(note["id"], "cannot merge: conflict")
        self.check()
        self.assertEqual(len(self.to("maps-a", "is not merged into master")), 1)
        (line,) = self.to("maps", "is not merged into master")
        self.assertIn("(1 commit ahead)", line["text"])

    def test_merged_after_the_reminder_is_quiet(self):
        commit(self.x["cwd"])
        self.turn(self.d["message"])
        self.check()
        (note,) = self.to("maps-a", "is not merged into master")
        git(self.repo, "merge", "-q", "--ff-only", "task/maps-a")
        self.turn(note["id"], "merged")
        self.check()
        self.assertEqual(self.to("maps", "is not merged into master"), [])

    def test_state_counter_survives_a_restart(self):
        commit(self.x["cwd"])
        self.turn(self.d["message"])
        self.check()
        h2 = Hub(os.path.join(self.tmp, "state"), types.SimpleNamespace())   # the core restarts: the daemon's memory is empty
        h2.execute = lambda a: None
        self.check(h2)   # the same result — the fold does not remind a second time
        self.assertEqual(len([m for m in h2.state["messages"].values() if m["session"] == "maps-a"
                              and "is not merged into" in m["text"]]), 1)
        self.assertEqual([m for m in h2.state["messages"].values() if m["session"] == "maps"
                          and "is not merged into" in m["text"]], [])

    def test_git_error_is_no_reminder_and_is_retried(self):
        commit(self.x["cwd"])
        self.turn(self.d["message"])
        git(self.repo, "branch", "-m", "master", "trunk")   # no base (a lock, renamed) — not a fact
        self.check()
        facts = lambda: [e for e in journal.read(self.h.journal.path) if e["type"] == "executor.unmerged"]  # noqa
        self.assertEqual(facts(), [])
        git(self.repo, "branch", "-m", "trunk", "master")
        self.check()
        self.assertEqual(facts(), [])                       # before the retry time — leave it alone
        now = self.h.clock()
        self.h.clock = lambda: now + 61
        self.check()
        self.assertEqual(len(facts()), 1)

    def test_next_task_gets_its_own_reminder(self):
        commit(self.x["cwd"])
        self.turn(self.d["message"])
        self.check()
        (note,) = self.to("maps-a", "is not merged into master")
        git(self.repo, "merge", "-q", "--ff-only", "task/maps-a")
        self.turn(note["id"], "merged")
        self.check()
        # woken for a new task — again not merged: again to it first, not to the raiser
        self.h.record("delegation.created", None, {"session": "maps-a", "for_session": "maps", "context": "more",
                                                   "channel": "session", "owner": False, "sender": "maps"})
        (d2,) = [d for d in self.h.state["delegations"].values() if d["session"] == "maps-a" and d["id"] != self.d["id"]]
        commit(self.x["cwd"], "c.txt")
        self.turn(d2["message"])
        self.check()
        self.assertEqual(len(self.to("maps-a", "is not merged into master")), 2)
        self.assertEqual(self.to("maps", "is not merged into master"), [])

    def test_branch_pushed_for_a_merge_request_is_handed_over(self):
        remote = os.path.join(self.tmp, "remote.git")
        git(self.tmp, "init", "-q", "--bare", remote)
        git(self.repo, "remote", "add", "origin", remote)
        commit(self.x["cwd"])
        git(self.x["cwd"], "push", "-q", "origin", "task/maps-a")
        self.turn(self.d["message"])
        self.check()
        self.assertEqual([e for e in journal.read(self.h.journal.path) if e["type"] == "executor.unmerged"], [])

    def test_only_the_core_writes_the_fact(self):
        self.h.record("executor.unmerged", "maps-a", {"branch": "task/maps-a", "base": "master", "ahead": 1,
                                                      "seq": 1, "channel": "session", "owner": False,
                                                      "sender": "maps-a"}, src="spool")
        self.assertEqual(self.to("maps-a", "is not merged into"), [])


class Fold(unittest.TestCase):
    """The fold of executor.unmerged without the disk."""
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.requested", "maps", cwd="/r", session_id="s-m", role="agent", **OWNER)
        self.ev("session.started", "maps")
        self.ev("task.run_requested", "maps-a", task="do it", channel="session", owner=False, sender="maps")
        run = self.st["runs"]["maps-a"]
        for type, entity, data in executors.events_for(self.st, "maps-a", run, "/r/.claude/worktrees/maps-a",
                                                       {"path": "/r/.claude/worktrees/maps-a", "base": "master",
                                                        "branch": "task/maps-a"}):
            self.ev(type, entity, **data)
        (self.d,) = [d for d in self.st["delegations"].values() if d["session"] == "maps-a"]
        self.ev("session.started", "maps-a")
        self.ev("turn.started", "maps-a", marker=self.d["message"])
        self.ev("turn.result", "maps-a", text="ready")
        self.ev("turn.stopped", "maps-a")

    def ev(self, type, entity=None, src="core", **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                   "entity": entity, "data": data, "src": src})
        return why

    def fact(self, seq, **extra):
        return self.ev("executor.unmerged", "maps-a", **{"branch": "task/maps-a", "base": "master", "ahead": 2,
                                                        "dirty": False, "seq": seq, **CORE, **extra})

    def test_dirty_without_commits_says_so(self):
        self.assertIsNone(self.fact(self.d.get("replied_seq") or self.seq, ahead=0, dirty=True))
        (note,) = [m for m in self.st["messages"].values() if m["session"] == "maps-a" and "is not merged into" in m["text"]]
        self.assertIn("(uncommitted changes in the copy)", note["text"])

    def test_sleeping_executor_gets_no_reminder_raiser_does(self):
        self.ev("session.close_requested", "maps-a", retire=True, **CORE)
        self.ev("session.exited", "maps-a")
        self.assertTrue(executors.dormant(self.st["sessions"]["maps-a"]), self.st["sessions"]["maps-a"]["state"])
        self.assertIsNone(self.fact(self.seq))
        texts = [m["text"] for m in self.st["messages"].values() if "is not merged into" in m["text"]]
        self.assertEqual(len(texts), 1)
        (to_maps,) = [m for m in self.st["messages"].values() if "is not merged into" in m["text"]]
        self.assertEqual(to_maps["session"], "maps")
        self.assertNotIn("For executor", to_maps["text"])

    def test_a_parked_executor_gets_its_reminder(self):
        # review 2026-10-29: parked after its report before the background check came back — the reminder still goes to
        # it (the message raises it), not straight to the raiser
        self.st["sessions"]["maps-a"]["state"] = "parked"
        self.assertIsNone(self.fact(self.seq))
        (note,) = [m for m in self.st["messages"].values() if "is not merged into" in m["text"]]
        self.assertEqual(note["session"], "maps-a")

    def test_owner_raised_executor_tells_the_owner(self):
        self.ev("task.run_requested", "solo", task="do it", **OWNER)
        run = self.st["runs"]["solo"]
        for type, entity, data in executors.events_for(self.st, "solo", run, "/r/.claude/worktrees/solo",
                                                       {"path": "/r/.claude/worktrees/solo", "base": "master",
                                                        "branch": "task/solo"}):
            self.ev(type, entity, **data)
        self.ev("session.started", "solo")
        for n in (1, 2):
            self.ev("executor.unmerged", "solo", branch="task/solo", base="master", ahead=1, dirty=False,
                    seq=100 + n, **CORE)
        hub = [m for m in self.st["messages"].values() if m["session"] == "hub" and "is not merged into" in m["text"]]
        self.assertEqual(len(hub), 1)


if __name__ == "__main__":
    unittest.main()
