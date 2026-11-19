"""Session commands are not lost and are not refused with a raw code (friction roundup 2026-10-16): an automaton
refusal is a phrase saying what to do next, not "session.remove_requested in idle"; `hub session remove` removes an
idle executor at once; whoever started an executor stops and removes it themselves; `--for mN` on a session that starts
its own executor is the owner's word to that session itself; cleaning up a working copy does not hold the core tick
(17–20 s on git, the CLI did not wait — "in the mailbox")."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import re
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from core import coretext, i18n, machines, reducer

WEB = {"channel": "web", "owner": True}
CLI = {"channel": "cli", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}
MAPS = {"channel": "session", "owner": False, "sender": "maps"}
NOTES = {"channel": "session", "owner": False, "sender": "notes"}
PLUGIN = {"channel": "plugin", "owner": False, "sender": "hello"}
RAW = re.compile(r"[a-z_.]+ in [a-z-]+")
CYRILLIC = re.compile("[" + chr(0x400) + "-" + chr(0x4FF) + "]")
RUSSIAN = re.compile("[" + chr(0x430) + "-" + chr(0x44F) + "]")   # lowercase Russian letters


class Base(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.acts = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **CLI)
        self.ev("session.requested", "maps", cwd="/w/maps", session_id="s-maps", role="agent", **CLI)
        self.ev("session.requested", "notes", cwd="/w/notes", session_id="s-notes", **CLI)
        for n in ("hub", "maps", "notes"):
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.acts += acts
        return why

    def executor(self, name="maps-1", raiser="maps"):
        self.ev("session.requested", name, cwd=f"/w/{raiser}", session_id=f"s-{name}", executor=True,
                raised_by=raiser)
        self.ev("session.started", name)
        return self.st["sessions"][name]

    def state(self, name):
        return self.st["sessions"][name]["state"]


class Phrases(unittest.TestCase):
    def test_no_machine_refuses_with_a_raw_code(self):
        # every automaton refusal is in words: an event that has no row for this state — English for the session, the
        # owner reads it in Russian (the core's phrases, core/coretext.py)
        for name in ("SESSION", "CLOSING", "DELEGATION", "OWNER", "SCHEDULE", "MESSAGE"):
            machine = getattr(machines, name)
            states = {machine["initial"]} | {s for row in machine["transitions"].values() for s in row}
            for kind in machine["transitions"]:
                for state in sorted(states):
                    to, _, why = machines.decide(machine, state, kind)
                    if to is not None or why is None:
                        continue
                    with self.subTest(machine=name, kind=kind, state=state):
                        self.assertFalse(RAW.fullmatch(why), why)
                        self.assertNotRegex(why, CYRILLIC)
                        self.assertRegex(coretext.human(why, "ru"), RUSSIAN)
                        self.assertTrue(coretext.human(why, "ru").endswith(f" ({kind} in {state})"))

    def test_the_common_ones_say_what_to_do_next(self):
        S, C = machines.SESSION, machines.CLOSING
        cases = [(S, "busy", "session.remove_requested", "hub session stop"),
                 (S, "removed", "session.remove_requested", "already removed"),
                 (S, "idle", "session.interrupt_requested", "nothing to interrupt"),
                 (S, "idle", "permission.answered", "already answered"),
                 (C, "handoff", "session.close_requested", "already handing over"),
                 (machines.SCHEDULE, "removed", "schedule.remove_requested", "already removed"),
                 (machines.SCHEDULE, "done", "schedule.remove_requested", "already fired")]
        for machine, state, kind, said in cases:
            with self.subTest(kind=kind, state=state):
                self.assertIn(said, machines.decide(machine, state, kind)[2])

    def test_the_next_command_names_the_session(self):
        # the name goes in as a substitution: the owner reads the same refusal in his language with the name in it
        why = machines.decide(machines.SESSION, "asleep", "session.take_requested", name="maps")[2]
        self.assertEqual(why, "the session is asleep — wake it first: hub session wake maps "
                              "(session.take_requested in asleep)")
        self.assertIn("hub session wake maps", coretext.human(why, "ru"))
        self.assertNotEqual(coretext.human(why, "ru"), why)
        holder = machines.decide(machines.SESSION, "asleep", "session.take_requested")[2]
        self.assertIn("hub session wake <name>", holder)
        self.assertIn("hub session wake " + i18n.tr("ru", "core.machines.name_holder"), coretext.human(holder, "ru"))


class Remove(Base):
    def test_a_free_executor_is_removed_at_once(self):
        # 16 refusals "remove_requested in idle": an executor handed in its result and is idle — the hub removes it, the core used to refuse
        self.executor()
        for who in (HUB, MAPS):
            with self.subTest(who=who["sender"]):
                name = f"x-{who['sender']}"
                self.executor(name)
                self.acts = []
                self.assertIsNone(self.ev("session.remove_requested", name, **who))
                self.assertEqual(self.state(name), "removed")
                self.assertTrue([a for a in self.acts if a["do"] == "release" and a.get("now") and a["session"] == name])   # as stop does

    def test_a_busy_one_is_refused_with_the_next_step(self):
        self.executor()
        self.ev("turn.started", "maps-1", prompt_head="work")
        why = self.ev("session.remove_requested", "maps-1", **HUB)
        self.assertIn("hub session stop maps-1", why or "")
        self.assertEqual(self.state("maps-1"), "busy")

    def test_a_free_worker_or_agent_is_refused_with_the_next_step(self):
        # a live non-executor — put to sleep first (it hands over its work; owner decision 2026-09-29)
        why = self.ev("session.remove_requested", "notes", **HUB)
        self.assertIn("hub session close notes", why or "")
        self.assertEqual(self.state("notes"), "idle")
        self.assertIsNotNone(self.ev("session.remove_requested", "maps", **HUB))
        self.assertEqual(self.state("maps"), "idle")

    def test_a_free_executor_with_an_open_task_or_question_is_not_cut_silently(self):
        # review: stop would cut off a task waiting for the owner's answer — that is decided explicitly
        self.executor()
        self.ev("delegation.created", None, session="maps-1", context="", words="do it", **MAPS)
        why = self.ev("session.remove_requested", "maps-1", **HUB)
        self.assertIn("is not closed yet", why or "")
        self.assertIn("hub session stop maps-1", why or "")
        self.assertEqual(self.state("maps-1"), "idle")
        self.ev("delegation.done", f"d{self.seq - 1}", **MAPS)
        self.ev("session.background", "maps-1", tasks=[{"id": "b1", "title": "test server"}])
        self.assertIn("test server", self.ev("session.remove_requested", "maps-1", **HUB) or "")   # background — too

    def test_the_removed_one_says_it_is_gone(self):
        self.st["sessions"]["notes"]["state"] = "asleep"
        self.assertIsNone(self.ev("session.remove_requested", "notes", **HUB))
        why = self.ev("session.remove_requested", "notes", **HUB)
        self.assertIn("already removed", why or "")
        self.assertIn("no such session", self.ev("session.remove_requested", "nope", **HUB) or "")


class Raiser(Base):
    def test_the_raiser_stops_interrupts_and_removes_its_executor(self):
        # 2026-10-11/12: 17 refusals "the hub or the owner does this" to the starter for their own executors
        self.executor()
        self.ev("turn.started", "maps-1", prompt_head="work")
        self.acts = []
        self.assertIsNone(self.ev("session.interrupt_requested", "maps-1", **MAPS))
        self.assertTrue([a for a in self.acts if a["do"] == "interrupt"])
        self.assertIsNone(self.ev("session.stop_requested", "maps-1", **MAPS))
        self.assertEqual(self.state("maps-1"), "retired")
        self.assertIsNone(self.ev("session.remove_requested", "maps-1", **MAPS))
        self.assertEqual(self.state("maps-1"), "removed")

    def test_not_someone_elses(self):
        self.executor()
        for who in (NOTES, PLUGIN):
            with self.subTest(who=who["sender"]):
                why = self.ev("session.stop_requested", "maps-1", **who)
                self.assertIn("whoever raised it", why or "")
                self.assertEqual(self.state("maps-1"), "idle")
                self.assertIsNotNone(self.ev("session.remove_requested", "maps-1", **who))
        # a working one — also "who is entitled", not a hint about stop, which would be refused
        self.ev("turn.started", "maps-1", prompt_head="work")
        self.assertIn("whoever raised it", self.ev("session.remove_requested", "maps-1", **NOTES) or "")
        # nor an ordinary session: it has no starter
        self.st["sessions"]["notes"]["state"] = "asleep"
        self.assertIsNotNone(self.ev("session.remove_requested", "notes", **MAPS))
        self.assertEqual(self.state("notes"), "asleep")

    def test_not_a_direction(self):
        # a direction (an executor with a backlog) is removed by the hub or the owner, not by the starter
        self.executor("maps-dir")
        self.st["sessions"]["maps-dir"].update(backlog="maps-dir", state="asleep")
        self.assertIsNotNone(self.ev("session.remove_requested", "maps-dir", **MAPS))
        self.assertEqual(self.state("maps-dir"), "asleep")
        self.assertIsNone(self.ev("session.remove_requested", "maps-dir", **HUB))

    def test_cancelling_a_running_executor_names_stop(self):
        self.executor()
        why = self.ev("task.run_cancelled", "maps-1", **MAPS)
        self.assertIn("hub session stop maps-1", why or "")


class OwnerWordToTheSender(Base):
    def setUp(self):
        super().setUp()
        self.ev("account.limits", None, windows={"seven_day": {"used": 0.9}})   # above the threshold — waits for the owner's word
        self.ev("catalog.loaded", tools={"mail": {"kind": "mcp", "risk": ["owner-data"]}})

    def said(self, to, **who):
        self.ev("message.received", None, to=to, text="yes, go ahead", **(who or WEB))
        return f"m{self.seq}"

    def test_the_owners_word_to_the_raiser_itself_counts(self):
        # 15 refusals of task.run "--for: an owner message to the hub is needed": a mini-hub passes on the owner's word said to it
        self.assertIsNone(self.ev("task.run_requested", "maps-ci", task="fix it", **{"for": self.said("maps")},
                                  **MAPS))
        self.assertEqual(self.st["runs"]["maps-ci"]["state"], "ready")

    def test_not_words_to_another_session_or_from_a_session(self):
        for ref in (self.said("notes"), self.said("maps", **NOTES)):
            with self.subTest(ref=ref):
                why = self.ev("task.run_requested", f"maps-{ref}", task="fix it", **{"for": ref}, **MAPS)
                self.assertIn("--for", why or "")

    def test_risky_tools_still_wait_for_the_owner(self):
        # the owner's word to a session is not a "yes" to a risky tool for its executor: the owner or the hub decides that
        self.assertIsNone(self.ev("task.run_requested", "maps-ml", task="mail", tools=["mail"],
                                  **{"for": self.said("maps")}, **MAPS))
        self.assertEqual(self.st["runs"]["maps-ml"]["ask"], ["mail"])

    def test_a_granted_tool_by_a_session_still_needs_the_hub(self):
        why = self.ev("tools.granted", "notes", tool="mail", **{"for": self.said("maps")}, **MAPS)
        self.assertIn("--for", why or "")
        self.assertNotIn("their message to you too", why)   # here one's own word does not count — and there is no such hint


class CleaningDoesNotHoldTheTick(unittest.TestCase):
    def test_git_runs_aside_and_the_result_is_recorded_on_a_later_tick(self):
        # 2026-10-08 and 2026-10-14: git worktree remove of a large copy — 17–20 s in the tick; session commands waited longer than the CLI's 5 s
        from core.daemon import Hub
        tmp = tempfile.mkdtemp(prefix="cleanbg-")
        h = Hub(os.path.join(tmp, "state"), types.SimpleNamespace())
        h.execute = lambda a: None
        wt = {"path": os.path.join(tmp, "copy"), "branch": "task/x", "repo": tmp}
        h.record("session.requested", "x", {"cwd": wt["path"], "session_id": "s-x", "worktree": wt, "executor": True,
                                            **CLI})
        h.record("session.started", "x", {})
        h.record("session.remove_requested", "x", CLI)
        self.assertEqual(h.state["sessions"]["x"]["state"], "removed")
        go, calls = threading.Event(), []

        def slow(info):
            calls.append(info["path"])
            go.wait(10)
            return True, "copy and branch removed"
        with mock.patch("hosts.worktree.remove", slow):
            t = time.monotonic()
            h.clean_worktrees()
            self.assertLess(time.monotonic() - t, 2)
            h.clean_worktrees()                                   # while it runs — does not start a second time
            self.assertFalse(h.state["sessions"]["x"]["worktree"].get("cleaned"))
            go.set()
            h.cleaner.join(5)
            h.clean_worktrees()
        self.assertEqual(calls, [wt["path"]])
        self.assertEqual(h.state["sessions"]["x"]["worktree"].get("cleanup"), "copy and branch removed")

    def locked_shell_hub(self):
        """A hub with a removed executor whose copy is an empty shell that does not go now; the clock is ours."""
        from core.daemon import Hub
        tmp = tempfile.mkdtemp(prefix="cleanlock-")
        self.clock = [1000.0]
        h = Hub(os.path.join(tmp, "state"), types.SimpleNamespace(), mono=lambda: self.clock[0])
        h.execute = lambda a: None
        wt = {"path": os.path.join(tmp, "copy"), "branch": "task/x", "repo": tmp}
        h.record("session.requested", "x", {"cwd": wt["path"], "session_id": "s-x", "worktree": wt, "executor": True,
                                            **CLI})
        h.record("session.started", "x", {})
        h.record("session.remove_requested", "x", CLI)
        return h

    def tick(self, h):
        h.clean_worktrees()
        if h.cleaner:
            h.cleaner.join(5)
        h.clean_worktrees()

    def test_a_shell_that_does_not_go_now_is_asked_again_in_a_minute_and_the_result_waits(self):
        # a program stands in the root of the copy (Windows): git took the files, the folder says "Permission denied"; the
        # result was recorded once and nobody came back
        h = self.locked_shell_hub()
        answers = [(None, "locked")] * 2 + [(True, "removed")]
        calls = []
        with mock.patch("hosts.worktree.remove", lambda info: (calls.append(self.clock[0]), answers.pop(0))[1]):
            self.tick(h)
            self.assertEqual(len(calls), 1)
            self.assertFalse(h.state["sessions"]["x"]["worktree"].get("cleaned"))   # not a result yet
            self.clock[0] += 30
            self.tick(h)
            self.assertEqual(len(calls), 1)   # not before a minute
            self.clock[0] += 31
            self.tick(h)
            self.assertEqual(len(calls), 2)
            self.assertFalse(h.state["sessions"]["x"]["worktree"].get("cleaned"))
            self.clock[0] += 60
            self.tick(h)
        self.assertEqual(len(calls), 3)
        self.assertEqual(h.state["sessions"]["x"]["worktree"].get("cleanup"), "removed")
        self.assertEqual(h.clean_retry, {})

    def test_a_shell_that_never_goes_ends_in_a_result_with_the_reason_after_30_tries(self):
        h = self.locked_shell_hub()
        calls = []
        with mock.patch("hosts.worktree.remove", lambda info: (calls.append(1), (None, "the root is locked"))[1]):
            for _ in range(60):
                self.tick(h)
                self.clock[0] += 61
                if h.state["sessions"]["x"]["worktree"].get("cleaned"):
                    break
        self.assertEqual(len(calls), 30)
        wt = h.state["sessions"]["x"]["worktree"]
        self.assertTrue(wt.get("cleaned"))
        self.assertIn("the root is locked", wt.get("cleanup"))

    def test_a_slow_worktree_add_runs_aside_and_the_executor_is_raised_on_a_later_tick(self):
        # 2026-10-21: `git worktree add` of a large copy — over 30 s right in the tick: the core stalled, session commands
        # went "to the mailbox", and git was killed on a timeout so the executor did not start
        from core.daemon import Hub
        tmp = tempfile.mkdtemp(prefix="prepbg-")
        h = Hub(os.path.join(tmp, "state"), types.SimpleNamespace())
        h.execute = lambda a: None
        h.PREPARE_WAIT = 0.2
        h.record("session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **CLI})
        h.record("session.requested", "maps", {"cwd": tmp, "session_id": "s-m", "role": "agent", **CLI})
        h.record("session.started", "maps", {})
        h.record("task.run_requested", "maps-1", {"task": "do it", "channel": "session", "owner": False,
                                                  "sender": "maps"})
        go, calls = threading.Event(), []
        copy = os.path.join(tmp, "copy")

        def slow(base, name, branch=None):
            calls.append(name)
            go.wait(10)
            return copy, {"path": copy, "branch": "task/maps-1", "repo": tmp, "checks": []}, None
        with mock.patch("hosts.worktree.prepare", slow):
            t = time.monotonic()
            h.run_tasks()
            self.assertLess(time.monotonic() - t, 2)
            h.run_tasks()                                          # while it runs — a second git is not started
            self.assertNotIn("maps-1", h.state["sessions"])
            go.set()
            h.preparing["maps-1"]["thread"].join(5)
            h.run_tasks()
        self.assertEqual(calls, ["maps-1"])
        self.assertEqual(h.state["sessions"]["maps-1"]["cwd"], copy)
        self.assertNotIn("maps-1", h.preparing)

    def test_a_fast_worktree_add_raises_the_executor_in_the_same_tick(self):
        from core.daemon import Hub
        tmp = tempfile.mkdtemp(prefix="prepfast-")
        h = Hub(os.path.join(tmp, "state"), types.SimpleNamespace())
        h.execute = lambda a: None
        h.record("session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **CLI})
        h.record("session.requested", "maps", {"cwd": tmp, "session_id": "s-m", "role": "agent", **CLI})
        h.record("session.started", "maps", {})
        h.record("task.run_requested", "maps-2", {"task": "do it", "channel": "session", "owner": False,
                                                  "sender": "maps"})
        with mock.patch("hosts.worktree.prepare", lambda *a, **k: (_ for _ in ()).throw(OSError("disk"))):
            h.run_tasks()
        self.assertNotIn("maps-2", h.state.get("runs") or {})
        self.assertFalse(h.preparing)

    def _raiser(self, prefix):
        from core.daemon import Hub
        tmp = tempfile.mkdtemp(prefix=prefix)
        h = Hub(os.path.join(tmp, "state"), types.SimpleNamespace())
        h.execute = lambda a: None
        h.PREPARE_WAIT = 0.05
        h.record("session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **CLI})
        h.record("session.requested", "maps", {"cwd": tmp, "session_id": "s-m", "role": "agent", **CLI})
        h.record("session.started", "maps", {})
        return h, tmp

    def test_the_copy_being_prepared_is_not_cleaned_under_a_removed_session_of_the_same_name(self):
        # PR #236 review: a removed executor with an uncleaned copy, a task again under its name — prepare takes the same
        # folder; while it runs in the background the session is still removed, and cleanup would delete the copy from under the new executor
        h, tmp = self._raiser("prepclean-")
        wt = {"path": os.path.join(tmp, "copy"), "branch": "task/maps-1", "repo": tmp}
        h.record("session.requested", "maps-1", {"cwd": wt["path"], "session_id": "s-1", "worktree": wt,
                                                 "executor": True, **CLI})
        h.record("session.started", "maps-1", {})
        h.record("session.remove_requested", "maps-1", CLI)
        self.assertEqual(h.state["sessions"]["maps-1"]["state"], "removed")
        h.record("task.run_requested", "maps-1", {"task": "more", "channel": "session", "owner": False,
                                                  "sender": "maps"})
        go, removed = threading.Event(), []
        with mock.patch("hosts.worktree.prepare", lambda *a, **k: (go.wait(10), (wt["path"], dict(wt, checks=[]),
                                                                                  None))[1]), \
                mock.patch("hosts.worktree.remove", lambda info: (removed.append(info["path"]), (True, "removed"))[1]):
            h.run_tasks()
            h.clean_worktrees()
            go.set()
            h.preparing["maps-1"]["thread"].join(5)
            h.run_tasks()
            h.clean_worktrees()
            if h.cleaner:
                h.cleaner.join(5)
        self.assertEqual(removed, [])
        self.assertEqual(h.state["sessions"]["maps-1"]["cwd"], wt["path"])

    def test_copies_of_one_repo_are_prepared_one_at_a_time(self):
        # PR #236 review: two `git worktree add` in one repo at once — git reads a neighbour's half-written entry
        # ("failed to read .git/worktrees/…/commondir", 1 in 90 on a probe); in the tick they run one at a time
        h, tmp = self._raiser("prepone-")
        for n in ("maps-1", "maps-2"):
            h.record("task.run_requested", n, {"task": "do it", "channel": "session", "owner": False,
                                               "sender": "maps"})
        go, now, most = threading.Event(), [], []

        def prep(base, name, branch=None):
            now.append(name)
            most.append(len(now))
            go.wait(0.3)
            now.remove(name)
            path = os.path.join(base, name)
            return path, {"path": path, "branch": f"task/{name}", "repo": base, "checks": []}, None
        with mock.patch("hosts.worktree.prepare", prep):
            h.run_tasks()
            for job in list(h.preparing.values()):
                job["thread"].join(5)
            h.run_tasks()
        self.assertEqual(max(most), 1)
        self.assertEqual({h.state["sessions"][n]["cwd"] for n in ("maps-1", "maps-2")},
                         {os.path.join(tmp, "maps-1"), os.path.join(tmp, "maps-2")})

    def test_from_main_reaches_the_copy(self):
        # hub task run --from-main: the copy is checked out from the repo's main branch (PR #283, review)
        h, _ = self._raiser("prepmain-")
        h.record("task.run_requested", "maps-m", {"task": "do it", "from_main": True, "channel": "session",
                                                  "owner": False, "sender": "maps"})
        seen = []

        def prep(base, name, branch=None, **kw):
            seen.append(kw)
            return None, None, "stop"
        with mock.patch("hosts.worktree.prepare", prep):
            h.run_tasks()
            for job in list(h.preparing.values()):
                job["thread"].join(5)
        self.assertEqual(seen, [{"from_main": True}])

    def test_the_tick_waits_for_all_new_copies_once_not_once_per_copy(self):
        # PR #236 review: several executors of a large repo at once — the tick waited 2 s for each
        h, tmp = self._raiser("prepwait-")
        h.PREPARE_WAIT = 0.3
        for n in ("maps-1", "maps-2", "maps-3"):   # three — that many slots for executors by default
            h.record("task.run_requested", n, {"task": "do it", "channel": "session", "owner": False,
                                               "sender": "maps", "repo": tempfile.mkdtemp(prefix=n)})
        go = threading.Event()
        with mock.patch("hosts.worktree.prepare", lambda *a, **k: (go.wait(10), (None, None, "no"))[1]):
            t = time.monotonic()
            h.run_tasks()
            took = time.monotonic() - t
            go.set()
            for job in list(h.preparing.values()):
                job["thread"].join(5)
        self.assertEqual(len(h.preparing), 3)
        self.assertLess(took, 0.6)

    def test_a_copy_prepared_for_a_cancelled_run_is_not_taken_by_a_new_run_elsewhere(self):
        # PR #236 review: the task was removed while git was checking out the copy, and a new one for another repo was
        # put under the same name — the result of the old checkout is not its
        h, tmp = self._raiser("prepstale-")
        other = tempfile.mkdtemp(prefix="prepother-")
        h.record("task.run_requested", "maps-1", {"task": "do it", "channel": "session", "owner": False,
                                                  "sender": "maps"})
        go, calls = threading.Event(), []

        def prep(base, name, branch=None):
            calls.append(base)
            if len(calls) == 1:
                go.wait(10)
            path = os.path.join(base, "copy")
            return path, {"path": path, "branch": "task/maps-1", "repo": base, "checks": []}, None
        with mock.patch("hosts.worktree.prepare", prep):
            h.run_tasks()
            h.record("task.run_cancelled", "maps-1", {"channel": "session", "owner": False, "sender": "maps"})
            h.record("task.run_requested", "maps-1", {"task": "other", "repo": other, "channel": "session",
                                                      "owner": False, "sender": "maps"})
            h.run_tasks()                                          # the old one is still running — do not start the new one
            for _ in range(100):   # the copy is prepared in a thread: on a loaded machine it has not started yet
                if calls:
                    break
                time.sleep(0.05)
            self.assertEqual(calls, [tmp])
            go.set()
            h.preparing["maps-1"]["thread"].join(5)
            for _ in range(3):
                h.run_tasks()
                if "maps-1" in h.state["sessions"]:
                    break
                time.sleep(0.1)
        self.assertEqual(calls, [tmp, other])
        self.assertEqual(h.state["sessions"]["maps-1"]["cwd"], os.path.join(other, "copy"))

    def test_a_late_result_does_not_land_on_a_session_back_under_the_name_and_its_copy_waits(self):
        # review: while git runs, the name could be restored or created anew — the old copy's result is not its; an
        # executor with the same name (the same copy folder) waits a tick instead of starting in a folder git is deleting
        from core.daemon import Hub
        tmp = tempfile.mkdtemp(prefix="cleanbg-")
        h = Hub(os.path.join(tmp, "state"), types.SimpleNamespace())
        h.execute = lambda a: None
        wt = {"path": os.path.join(tmp, "copy"), "branch": "task/x", "repo": tmp}
        h.record("session.requested", "x", {"cwd": wt["path"], "session_id": "s-x", "worktree": wt, "executor": True,
                                            **CLI})
        h.record("session.started", "x", {})
        h.record("session.remove_requested", "x", CLI)
        os.makedirs(os.path.join(wt["path"], "app"))                # folders are compared on disk (worktree.in_tree)
        go = threading.Event()
        with mock.patch("hosts.worktree.remove", lambda info: (go.wait(10), (True, "removed"))[1]):
            h.clean_worktrees()
            self.assertTrue(h.busy_copy("x", {}, "/elsewhere"))
            self.assertTrue(h.busy_copy("y", {"here": True}, os.path.join(wt["path"], "app")))
            self.assertFalse(h.busy_copy("y", {}, wt["path"]))
            h.state["sessions"]["x"]["state"] = "idle"            # restored (revive) — the copy is its again
            go.set()
            h.cleaner.join(5)
            self.assertFalse(h.busy_copy("x", {}, "/elsewhere"))
            h.clean_worktrees()
        self.assertNotIn("cleaned", h.state["sessions"]["x"]["worktree"])


if __name__ == "__main__":
    unittest.main()
