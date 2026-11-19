"""Mini-hubs (the "mini-hub" spec): one knob — the hub's parallelism N (hub.json executors.parallel, 3 by default, carried
by the owner.settings fact — the journal fold does not depend on hub.json). N >= 1 — every specialist and direction is a
mini-hub with up to N executors; N = 0 and the "solo" tick — no executors. A journal before the first owner.settings with
parallel keeps the old rules: no mini-hubs, caps of 3 for a specialist and 6 for the rest (and max_per_agent /
max_per_other from the old settings)."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from core import delegations, executors, i18n, minihub, reducer, restart
from tests import winsafe
from tests.fakes import lang

PENDING_TASK = i18n.t("core.minihub.pending_task")   # what the owner's page and terminal say (English in tests)

OWNER = {"channel": "cli", "owner": True}
# a background service: runs past restart.SERVICE_AFTER and prints nothing — a relaunch does not wait for it
SERVICE_BG = {"title": "bundle-run", "kind": "bash", "since": -10 ** 6}


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


class Core(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions, self.events = reducer.empty(), 0, [], []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.started", "hub")
        for name in ("notes", "api-x"):
            self.ev("session.requested", name, cwd=f"/{name}", session_id=f"s-{name}", role="agent", about=name,
                    **OWNER)
            self.ev("session.started", name)

    def ev(self, type, entity=None, ts=None, src=None, **data):
        self.seq += 1
        e = {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type, "entity": entity, "data": data,
             **({"src": src} if src else {})}
        self.events.append(e)
        self.st, acts, why = reducer.reduce(self.st, e)
        self.actions += acts
        return why

    def run_task(self, name, text="fix CI", who="notes", **extra):
        return self.ev("task.run_requested", name, task=text, **by(who), **extra)

    def start(self, name, cwd="/x/.claude/worktrees/x"):
        """As the daemon does: the executor is started and has received the task (core/executors.events_for)."""
        run = self.st["runs"][name]
        for type, entity, data in executors.events_for(self.st, name, run, cwd=cwd, worktree=None):
            self.assertIsNone(self.ev(type, entity, **data), (type, data))
        self.ev("session.started", name)

    def state_of(self, name):
        return self.st["runs"][name]["state"]

    def parallel(self, n, clean=True, **extra):
        """The owner set parallelism (the daemon, via the owner.settings fact); clean — the daemon found the folders of
        free specialists clean (session.folder): they become mini-hubs; respawned and parked ones are alive again."""
        self.actions.clear()
        live = self.live()
        why = self.ev("owner.settings", None, timezone="UTC", parallel=n, channel="core", **extra)
        if clean:
            for name, sess in list(self.st["sessions"].items()):
                if minihub.want(self.st, sess) and not minihub.on(self.st, sess) and minihub.free(self.st, sess):
                    self.folder(name)
        self.respawned()
        self.unpark(live)
        self.actions.clear()
        return why

    def live(self):
        return {n for n, s in self.st["sessions"].items() if s["state"] in ("idle", "busy", "dialog", "starting")}

    def unpark(self, live):
        """A mode change parks a free session with nothing to do (new rules apply with the first message): wake it as the
        next turn would — the helpers below work with a live session."""
        for name in sorted(live):
            if self.st["sessions"][name]["state"] == "parked":
                self.ev("session.wake_requested", name, **OWNER)
                self.ev("session.started", name)

    def folder(self, name, git="ok", dirty=0, clean=None):
        """The daemon's fact about a session's folder (core/daemon.check_folders): dirty — lines in git status (since
        16.10); clean — a field of the old core (journals before the change)."""
        extra = {"clean": clean} if clean is not None else {"dirty": dirty} if dirty is not None else {}
        return self.ev("session.folder", name, src="core", channel="core", git=git, **extra)

    def solo(self, name, on=True, who=None):
        self.actions.clear()
        live = self.live()
        why = self.ev("session.solo_set", name, on=on, **(by(who) if who else OWNER))
        self.respawned()
        self.unpark(live)
        return why

    def respawned(self):
        out = [a["session"] for a in self.actions if a.get("do") == "spawn"]
        for name in out:
            self.ev("session.started", name)
        return out


class Caps(Core):
    """A journal before parallelism: the old caps (also from the old settings)."""

    def test_caps_by_default_are_as_before(self):
        for i in range(4):
            self.assertIsNone(self.run_task(f"notes-{i}"))
        self.assertEqual([self.state_of(f"notes-{i}") for i in range(4)], ["ready"] * 3 + ["queued"])

    def test_cap_per_specialist_comes_from_settings(self):
        self.ev("owner.settings", None, timezone="UTC", executors={"max_per_agent": 1}, channel="core")
        self.assertIsNone(self.run_task("notes-a"))
        self.assertIsNone(self.run_task("notes-b"))
        self.assertEqual((self.state_of("notes-a"), self.state_of("notes-b")), ("ready", "queued"))
        self.assertIn("1 working executors", self.st["runs"]["notes-b"]["why"])

    def test_cap_for_others_comes_from_settings(self):
        # an executor (not a specialist) raises subtasks — the "others" cap
        self.ev("owner.settings", None, timezone="UTC", executors={"max_per_other": 1}, channel="core")
        self.assertIsNone(self.run_task("hub-x", who="hub"))
        self.start("hub-x")
        self.assertIsNone(self.run_task("hub-x-a", who="hub-x"))
        self.assertIsNone(self.run_task("hub-x-b", who="hub-x"))
        self.assertEqual((self.state_of("hub-x-a"), self.state_of("hub-x-b")), ("ready", "queued"))

    def test_hub_wide_cap_counts_executors_of_all_sessions_but_not_the_hubs(self):
        self.ev("owner.settings", None, timezone="UTC", executors={"max_live": 2}, channel="core")
        self.assertIsNone(self.run_task("notes-a"))
        self.start("notes-a")
        self.assertIsNone(self.run_task("api-x-a", who="api-x"))
        self.assertIsNone(self.run_task("notes-b"))                    # the third is over the shared cap
        self.assertEqual((self.state_of("api-x-a"), self.state_of("notes-b")), ("ready", "queued"))
        self.assertIn("in all (the hub's ceiling)", self.st["runs"]["notes-b"]["why"])
        for i in range(3):                                                # the hub has no cap and does not count
            self.assertIsNone(self.run_task(f"hub-{i}", who="hub"))
            self.assertEqual(self.state_of(f"hub-{i}"), "ready")
        self.ev("task.run_requested", "mine", task="x", **OWNER)        # the owner is outside the cap too
        self.assertEqual(self.state_of("mine"), "ready")

    def test_queue_under_the_hub_wide_cap_rises_one_by_one(self):
        self.ev("owner.settings", None, timezone="UTC", executors={"max_live": 1}, channel="core")
        self.assertIsNone(self.run_task("notes-a"))
        self.start("notes-a")
        self.assertIsNone(self.run_task("notes-b"))
        self.assertIsNone(self.run_task("api-x-a", who="api-x"))
        self.assertEqual(executors.promotable(self.st), [])
        self.st["sessions"]["notes-a"]["state"] = "asleep"            # handed in and fell asleep — the slot is free
        self.assertEqual(executors.promotable(self.st), ["notes-b"])  # one slot — one executor, in request order

    def test_owner_settings_without_caps_keep_the_caps_set_before(self):
        # old owner.settings events (timezone, compaction) carry no caps — and do not reset them
        self.ev("owner.settings", None, timezone="UTC", executors={"max_per_agent": 1}, channel="core")
        self.ev("owner.settings", None, timezone="Europe/Paris", compact_wake=True, channel="core")
        self.assertEqual(self.st["owner"]["executors"], {"max_per_agent": 1})


class Number(Core):
    """Case 2: the number N — the path hub.json -> owner.settings -> fold; zero does not turn into the old cap."""

    def test_values(self):
        for value, n in ((None, 3), (0, 0), (0.0, 0), (1, 1), (4, 4), (2.7, 2), (-1, 3), (True, 3), (False, 3),
                         ("2", 3), (float("nan"), 3), (float("inf"), 3)):
            self.assertEqual(minihub.parallel_of(value), n, value)

    def test_before_the_setting_the_old_rules(self):
        self.assertIsNone(minihub.parallel(self.st))
        self.assertFalse(minihub.on(self.st, self.st["sessions"]["notes"]))
        self.assertIsNone(minihub.alone(self.st, self.st["sessions"]["notes"]))

    def test_zero_is_zero_not_the_old_cap(self):
        self.parallel(0)
        self.assertEqual(self.st["owner"]["parallel"], 0)
        why = self.run_task("notes-a")
        self.assertIn("parallelism 0", why)
        self.assertNotIn("notes-a", self.st.get("runs") or {})

    def test_a_bad_value_in_the_fact_folds_as_the_default(self):
        self.parallel("many")
        self.assertEqual(minihub.parallel(self.st), 3)

    def test_n_is_the_cap_of_each_specialist(self):
        self.parallel(2)
        for i in range(3):
            self.assertIsNone(self.run_task(f"notes-{i}"))
        self.assertEqual([self.state_of(f"notes-{i}") for i in range(3)], ["ready", "ready", "queued"])
        self.assertIn("2 working executors", self.st["runs"]["notes-2"]["why"])


class Matrix(Core):
    """Case 10: one N for a specialist and a direction, whatever the role; an executor without a backlog has its own
    cap."""

    def setUp(self):
        super().setUp()
        self.ev("session.requested", "dir-a", cwd="/dir-a", session_id="s-dir-a", role="agent", about="a", **OWNER)
        self.ev("session.started", "dir-a")
        self.ev("task.backlog", None, slug="dir-a", session="dir-a", **OWNER)
        self.ev("session.requested", "dir-b", cwd="/dir-b", session_id="s-dir-b", **OWNER)
        self.ev("session.started", "dir-b")
        self.ev("task.backlog", None, slug="dir-b", session="dir-b", **OWNER)
        self.assertIsNone(self.run_task("hub-x", who="hub"))
        self.start("hub-x")

    def test_one_n_for_role_times_backlog(self):
        for who in ("notes", "dir-a", "dir-b"):
            self.assertTrue(minihub.lead(self.st["sessions"][who]), who)
        self.assertFalse(minihub.lead(self.st["sessions"]["hub-x"]))
        self.parallel(1)
        for who in ("notes", "dir-a", "dir-b"):
            self.assertTrue(minihub.on(self.st, self.st["sessions"][who]), who)
            self.assertIsNone(self.run_task(f"{who}-1", who=who))
            self.assertIsNone(self.run_task(f"{who}-2", who=who))
            self.assertEqual((self.state_of(f"{who}-1"), self.state_of(f"{who}-2")), ("ready", "queued"), who)
        for i in range(executors.LIMIT_OTHER + 1):   # a hub executor raises subtasks — the old cap
            self.assertIsNone(self.run_task(f"hub-x-{i}", who="hub-x"))
        self.assertEqual(self.state_of(f"hub-x-{executors.LIMIT_OTHER}"), "queued")
        self.assertEqual(self.state_of("hub-x-0"), "ready")


class Queue(Core):
    """A task or message from another session to a busy mini-hub goes to its queue, not to the hub to decide: its turns
    are short."""

    def setUp(self):
        super().setUp()
        self.ev("session.requested", "docs", cwd="/docs", session_id="s-docs", **OWNER)
        self.ev("session.started", "docs")

    def to_hub(self):
        return [m for m in self.st["messages"].values() if m["session"] == "hub" and "busy" in m["text"]]

    def test_a_busy_specialist_gets_it_after_its_turn_too(self):
        # owner decision 2026-10-16: an ordinary specialist too — queue it without a hub turn
        self.ev("turn.started", "notes", marker=None)
        self.ev("delegation.created", None, session="notes", for_session="docs", context="T", **by("docs"))
        self.assertEqual((self.st["messages"][f"m{self.seq}"].get("held"), self.st["messages"][f"m{self.seq}"]["state"]),
                         (None, "queued"))
        self.assertEqual(self.to_hub(), [])

    def test_a_busy_minihub_gets_it_after_its_turn(self):
        self.parallel(3)                                                         # respawned with the addendum
        self.ev("turn.started", "notes", marker=None)
        self.ev("delegation.created", None, session="notes", for_session="docs", context="T", **by("docs"))
        task = self.st["messages"][f"m{self.seq}"]
        self.ev("message.received", None, to="notes", text="a question", **by("docs"))
        note = self.st["messages"][f"m{self.seq}"]
        self.assertEqual((task.get("held"), note.get("held"), task["state"], note["state"]),
                         (None, None, "queued", "queued"))
        self.assertEqual(self.to_hub(), [])
        self.ev("turn.result", "notes", text="ok")
        self.ev("turn.stopped", "notes")
        typed = [a for a in self.actions if a.get("do") == "type" and a.get("session") == "notes"]
        self.assertEqual([a["message"] for a in typed][:1], [task["id"]])            # after the turn — to the session itself


class Delegating(Core):
    """Helpers: a mini-hub, a task from another session, a turn with executor requests (no tests — subclasses do not
    inherit them)."""

    def setUp(self):
        super().setUp()
        self.ev("session.requested", "docs", cwd="/docs", session_id="s-docs", **OWNER)
        self.ev("session.started", "docs")

    def minihub(self, n=3):
        self.parallel(n)

    def task(self):
        self.ev("delegation.created", None, session="notes", for_session="docs", context="T", **by("docs"))
        return f"d{self.seq}", f"m{self.seq}"

    def turn(self, name, marker, text, runs=()):
        self.ev("turn.started", name, marker=marker)
        for run in runs:
            self.assertIsNone(self.run_task(run))
        self.ev("turn.result", name, text=text)
        self.ev("turn.stopped", name)
        return f"m{self.seq - 1}x1"

    def to(self, who, since=0):
        return [m for m in self.st["messages"].values() if m["session"] == who and int(m["id"][1:].split("x")[0]) > since]

    def executor_done(self, name, text="done, MR !1"):
        """The executor is started and has done the task; return the id of its "reply to the task" to whoever started it."""
        (sub,) = [d for d in self.st["delegations"].values() if d["session"] == name]
        self.ev("turn.started", name, marker=sub["message"])
        self.ev("turn.result", name, text=text)
        report = f"m{self.seq}x1"
        self.ev("turn.stopped", name)
        return sub, report


class Alone(Delegating):
    """Case 1: N = 0 or "solo" — the session gets no new executors: no hub task run, no queue, no ready ones, no waking
    a sleeping one, not even by force; waiting requests are dropped with one line. Hub and owner executors are outside
    the rule."""

    def queued_and_ready(self):
        self.minihub(1)
        self.assertIsNone(self.run_task("notes-a"))
        self.assertIsNone(self.run_task("notes-b"))
        self.assertEqual((self.state_of("notes-a"), self.state_of("notes-b")), ("ready", "queued"))

    def dropped(self, why):
        self.assertEqual(self.st.get("runs") or {}, {})
        lines = [m for m in self.to("notes") if "Executor requests are dropped" in m["text"]]
        self.assertEqual(len(lines), 1)
        self.assertIn("notes-a, notes-b", lines[0]["text"])
        self.assertIn(why, lines[0]["text"])

    def test_zero_drops_the_queue_and_closes_task_run(self):
        self.queued_and_ready()
        self.parallel(0)
        self.dropped("parallelism 0")
        self.assertIn("parallelism 0", self.run_task("notes-c"))
        self.assertIn("do it yourself", executors.check(self.st, "notes-d", "notes"))

    def test_solo_drops_the_queue_and_closes_task_run(self):
        self.queued_and_ready()
        self.solo("notes")
        self.dropped("\"solo\" is chosen")
        self.assertIn("\"solo\" is chosen", self.run_task("notes-c"))
        self.assertIsNone(self.run_task("api-x-a", who="api-x"))     # a neighbour's — as before
        self.assertEqual(self.state_of("api-x-a"), "ready")

    def test_hub_and_owner_executors_are_outside(self):
        self.parallel(0)
        self.assertIsNone(self.run_task("hub-a", who="hub"))
        self.ev("task.run_requested", "mine", task="x", **OWNER)
        self.assertEqual((self.state_of("hub-a"), self.state_of("mine")), ("ready", "ready"))

    def test_no_wake_of_a_sleeping_executor(self):
        self.minihub()
        did, mid = self.task()
        self.turn("notes", mid, "delegated", runs=["notes-ci"])
        self.start("notes-ci")
        sub, _ = self.executor_done("notes-ci")
        self.ev("session.stop_requested", "notes-ci", **OWNER)
        self.assertTrue(executors.dormant(self.st["sessions"]["notes-ci"]))
        self.solo("notes")
        for who in (by("notes"), OWNER, by("hub")):
            why = self.ev("message.received", None, to="notes-ci", text="finish", wake=True, about=sub["id"], **who)
            self.assertIn("cannot be woken", why or "", who)
        for who in (OWNER, by("hub")):
            why = self.ev("session.wake_requested", "notes-ci", **who)
            self.assertIn("cannot be woken", why or "", who)
        self.assertEqual(self.st["sessions"]["notes-ci"]["state"], "retired")

    def test_solo_is_set_by_the_owner_the_hub_and_the_trainer_only(self):
        self.ev("agents.system", None, names=["dev", "trainer"], trainer="trainer", channel="core")
        self.assertIn("the owner, the hub or agent-dev", self.solo("notes", who="api-x"))
        self.assertIsNone(self.solo("notes", who="hub"))
        self.assertIsNone(self.solo("notes", on=False, who="trainer"))
        self.assertIsNone(self.solo("notes"))
        self.assertIn("is for a specialist or a direction", self.solo("docs"))
        self.assertIn("is for a specialist or a direction", self.solo("hub"))


class Children(Delegating):
    """Case 3: switched off (N -> 0, "solo") or N lowered while children are working — the children finish, the task
    waits for them, the child's result arrives in the original task; live ones are not stopped, no new ones beyond N
    are raised."""

    def two_working(self):
        self.minihub()
        did, mid = self.task()
        self.turn("notes", mid, "delegated to two", runs=["notes-a", "notes-b"])
        self.start("notes-a")
        self.start("notes-b")
        self.assertEqual(minihub.waiting_on(self.st, self.st["delegations"][did]), ["notes-a", "notes-b"])
        return did

    def test_switched_off_the_task_still_waits_for_its_children(self):
        for off in (lambda: self.parallel(0), lambda: self.solo("notes")):
            with self.subTest():
                self.setUp()
                did = self.two_working()
                self.actions.clear()
                off()
                killed = [a for a in self.actions if a.get("do") in ("release", "kill", "stop") and
                          a.get("session") in ("notes-a", "notes-b")]
                self.assertEqual(killed, [])
                d = self.st["delegations"][did]
                self.assertEqual(minihub.waiting_on(self.st, d), ["notes-a", "notes-b"])
                self.assertEqual(d["state"], "working")
                _, first = self.executor_done("notes-a")
                self.turn("notes", first, "the first is in, waiting for the second")
                self.assertEqual(self.st["delegations"][did]["state"], "working")   # waiting for the second
                sub, last = self.executor_done("notes-b")
                self.assertEqual(sub["parent"], did)
                self.assertEqual(minihub.waiting_on(self.st, self.st["delegations"][did]), [])
                # the child's result goes into the original task: a turn on it is the task's turn, its result is the reply to the requester
                self.turn("notes", last, "both are in, checked")
                self.assertEqual(self.st["delegations"][did]["state"], "replied")

    def test_lower_n_keeps_the_live_and_raises_no_new(self):
        self.two_working()
        self.actions.clear()
        self.parallel(1)
        self.assertEqual([a for a in self.actions if a.get("session") in ("notes-a", "notes-b")
                          and a.get("do") in ("release", "kill")], [])
        self.assertIsNone(self.run_task("notes-c"))
        self.assertEqual(self.state_of("notes-c"), "queued")
        self.executor_done("notes-a")
        self.ev("session.stop_requested", "notes-a", **OWNER)
        self.assertEqual(executors.promotable(self.st), [])          # live ones are still 1 = N
        self.executor_done("notes-b")
        self.ev("session.stop_requested", "notes-b", **OWNER)
        self.assertEqual(executors.promotable(self.st), ["notes-c"])


class After(Delegating):
    """The switch after the current task (owner decision 2026-10-15): a session becomes a mini-hub when it has no task
    in progress and its folder is clean; until then — as before. It stops being one immediately. A folder that is not
    git or has no commits — it goes solo by itself."""

    def test_busy_with_a_task_it_stays_as_before_until_the_task_ends(self):
        did, mid = self.task()
        self.ev("turn.started", "notes", marker=mid)
        self.parallel(3)
        notes = lambda: self.st["sessions"]["notes"]   # noqa: E731 — the fold returns the new state
        self.assertFalse(minihub.on(self.st, notes()))
        self.assertEqual(minihub.pending(self.st, notes()), PENDING_TASK)
        self.folder("notes")                                          # the folder is clean but a task is in progress
        self.assertFalse(minihub.on(self.st, notes()))
        self.assertIsNone(self.run_task("notes-a"))                   # as before: it may raise executors, up to N
        self.ev("turn.result", "notes", text="did it myself")
        self.ev("turn.stopped", "notes")
        self.assertEqual(self.st["delegations"][did]["state"], "replied")   # a reply — as before, not "with the executor"
        (lessons,) = [m for m in self.to("notes") if m["state"] == "typing"]   # "task closed — write up the lessons"
        self.folder("notes")
        self.assertFalse(minihub.on(self.st, notes()))                # a message is waiting — there is work to do
        self.turn("notes", lessons["id"], "noted")
        self.actions.clear()
        self.folder("notes")
        self.assertTrue(minihub.on(self.st, notes()))
        self.assertEqual(notes()["state"], "parked")                  # nothing to do — parking, not a spawn
        self.ev("message.received", None, to="notes", text="more", **by("docs"))
        self.assertTrue([a for a in self.actions if a.get("do") == "spawn" and a.get("minihub")])

    def test_old_journal_a_dirty_folder_waits_as_it_did(self):
        self.parallel(3, clean=False)
        self.folder("notes", clean=False)                              # a field of the old core — the fold works the old way
        self.assertFalse(minihub.on(self.st, self.st["sessions"]["notes"]))
        self.assertIs(self.st["sessions"]["notes"]["folder_dirty"], True)
        self.folder("notes", clean=True)
        self.assertTrue(minihub.on(self.st, self.st["sessions"]["notes"]))
        self.assertNotIn("folder_dirty", self.st["sessions"]["notes"])

    def test_not_git_or_no_commits_is_solo_by_itself(self):
        self.parallel(3)
        self.assertTrue(minihub.on(self.st, self.st["sessions"]["notes"]))
        self.assertIsNone(self.run_task("notes-a"))
        for git, why in (("none", "the folder is not git"), ("empty", "the repo has no commits")):
            self.folder("notes", git=git, dirty=None)
            self.respawned()                                           # respawned without the mini-hub rules
            self.assertFalse(minihub.on(self.st, self.st["sessions"]["notes"]))
            self.assertIn(why, minihub.alone(self.st, self.st["sessions"]["notes"]))
            self.assertIn(why, self.solo("notes", on=False))           # cannot be turned off — executors would get in each other's way
            if git == "empty":   # work in another repo: the first commit in its own folder, then --repo
                self.assertIn("--repo", minihub.alone(self.st, self.st["sessions"]["notes"]))
        self.assertEqual(self.st.get("runs") or {}, {})                # the request was dropped at the first switch
        self.folder("notes", git="ok", dirty=None)                     # became git — waits for the switch again
        self.assertIsNone(minihub.alone(self.st, self.st["sessions"]["notes"]))
        self.assertFalse(minihub.on(self.st, self.st["sessions"]["notes"]))
        (line,) = [m for m in self.to("notes") if m["state"] == "typing"]   # "requests dropped" counts as work: the turn comes first
        self.turn("notes", line["id"], "understood")
        self.folder("notes")
        self.assertTrue(minihub.on(self.st, self.st["sessions"]["notes"]))

    def test_only_the_core_reports_the_folder(self):
        self.parallel(3, clean=False)
        why = self.ev("session.folder", "notes", channel="cli", owner=True, git="ok", clean=True, src="spool")
        self.assertIn("the core checks", why)
        self.assertFalse(minihub.on(self.st, self.st["sessions"]["notes"]))

    def test_back_on_after_zero_waits_for_the_folder_again(self):
        self.parallel(3)
        self.parallel(0)
        self.assertNotIn("minihub_since", self.st["sessions"]["notes"])
        self.parallel(2, clean=False)
        self.assertFalse(minihub.on(self.st, self.st["sessions"]["notes"]))

    def test_n_from_three_to_two_keeps_the_minihub(self):
        self.parallel(3)
        self.actions.clear()
        self.parallel(2, clean=False)
        self.assertTrue(minihub.on(self.st, self.st["sessions"]["notes"]))
        self.assertEqual([a for a in self.actions if a.get("do") == "spawn"], [])


class OldEvents(Core):
    """Case 9: events of the old tick (session.minihub_set, minihub_default) fold and affect nothing; the minihub flag
    in a state left by the old core — likewise."""

    def test_old_events_fold_and_change_nothing(self):
        self.assertIsNone(self.ev("owner.settings", None, timezone="UTC", minihub_default=True, channel="core"))
        self.assertIsNone(self.ev("session.minihub_set", "notes", on=True, **OWNER))
        self.ev("session.requested", "fresh", cwd="/fresh", session_id="s-fresh", role="agent", about="x", **OWNER)
        self.st["sessions"]["api-x"]["minihub"] = True                 # a state written by the old core
        for name in ("notes", "fresh", "api-x"):
            self.assertFalse(minihub.on(self.st, self.st["sessions"][name]), name)
        self.assertNotIn("minihub_default", self.st["owner"])
        for i in range(4):
            self.assertIsNone(self.run_task(f"notes-{i}"))
        self.assertEqual(self.state_of("notes-3"), "queued")            # the old cap of 3


class Folders(unittest.TestCase):
    """The daemon checks a specialist's folder: not git, a repo without commits, dirty, clean — the session.folder fact
    (with no extras)."""

    def git(self, cwd, *args):
        import subprocess
        subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True,
                       env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                            "GIT_COMMITTER_EMAIL": "t@x", "PATH": "/usr/bin:/bin:/usr/local/bin",
                            # a fresh environment — git isolation from tests/__init__.py explicitly, else the machine's system gitconfig
                            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.environ["GIT_CONFIG_GLOBAL"]})

    def repo(self, commit=True):
        d = tempfile.mkdtemp(prefix="minihub-f-")
        self.git(d, "init", "-q")
        if commit:
            self.git(d, "commit", "-q", "--allow-empty", "-m", "x")
        return d

    def test_folder_kinds(self):
        from hosts import worktree
        plain, empty, ok = tempfile.mkdtemp(), self.repo(commit=False), self.repo()
        self.assertEqual(worktree.folder(plain, dirty=True), ("none", None))
        self.assertEqual(worktree.folder(empty, dirty=True), ("empty", None))
        self.assertEqual(worktree.folder(ok), ("ok", None))
        self.assertEqual(worktree.folder(ok, dirty=True), ("ok", 0))
        import os
        os.makedirs(os.path.join(ok, ".claude", "worktrees", "x"))     # executors' working copies are not dirt
        with open(os.path.join(ok, ".claude", "worktrees", "x", "f"), "w") as f:
            f.write("1")
        self.assertEqual(worktree.folder(ok, dirty=True), ("ok", 0))
        for n in ("new.txt", "two.txt"):
            with open(os.path.join(ok, n), "w") as f:
                f.write("1")
        self.assertEqual(worktree.folder(ok, dirty=True), ("ok", 2))
        self.assertEqual(worktree.folder(os.path.join(plain, "gone")), ("gone", None))
        self.assertEqual(worktree.folder(None), ("gone", None))

    def test_the_daemon_reports_and_the_session_becomes_a_minihub(self):
        import os
        h = DaemonSettings.hub(self, {})
        ok, plain = self.repo(), tempfile.mkdtemp()
        for name, cwd in (("spec", ok), ("sec", plain)):
            h.record("session.requested", name, {"cwd": cwd, "session_id": f"s-{name}", "role": "agent",
                                                 "about": name, "channel": "cli", "owner": True})
            h.record("session.started", name)
        with open(os.path.join(ok, "wip.txt"), "w") as f:
            f.write("1")
        h.check_folders()
        spec, sec = h.state["sessions"]["spec"], h.state["sessions"]["sec"]
        self.assertTrue(minihub.on(h.state, spec))                     # uncommitted changes do not hold the switch
        self.assertEqual(sec.get("solo_auto"), "the folder is not git")
        seq = h.state["seq"]
        h.check_folders()                                              # too early — nothing
        self.assertEqual(h.state["seq"], seq)
        h.record("session.wake_requested", "spec", {"channel": "cli", "owner": True})   # parked — wake it
        h.record("session.started", "spec")
        h.check_folders()                                              # a live mini-hub — a number for the mark
        spec = h.state["sessions"]["spec"]
        self.assertEqual(spec.get("folder_dirty"), 1)
        self.assertIn("git status: 1", minihub.dirty(h.state, spec))
        os.remove(os.path.join(ok, "wip.txt"))
        h.folders_at.clear()
        h.check_folders()                                              # a mini-hub: the number is kept fresh
        spec = h.state["sessions"]["spec"]
        self.assertTrue(minihub.on(h.state, spec))
        self.assertNotIn("folder_dirty", spec)
        self.assertIsNone(minihub.dirty(h.state, spec))
        seq = h.state["seq"]
        h.folders_at.clear()
        h.check_folders()                                              # nothing changed — no fact at all
        self.assertEqual(h.state["seq"], seq)


class With(Delegating):
    """A mini-hub task that it delegated to an executor is "with the executor", not replied: the interim result goes into
    the task record, the requester gets one "delegated X" line; the reply goes out when the executors have handed in."""

    def test_delegating_turn_is_not_an_answer_and_the_executors_result_is(self):
        self.minihub()
        did, mid = self.task()
        mark = self.seq
        self.turn("notes", mid, "delegated notes-ci, waiting for the result", runs=["notes-ci"])
        d = self.st["delegations"][did]
        self.assertEqual(d["state"], "working")
        (line,) = self.to("docs", mark)
        self.assertIn("notes-ci", line["text"])
        self.assertNotIn("reply to task", line.get("origin") or "")
        self.assertFalse([m for m in self.to("hub", mark) if f"REPLIED {did}" in m["text"]])
        self.start("notes-ci")
        mark = self.seq
        sub, report = self.executor_done("notes-ci")
        self.assertEqual(sub.get("parent"), did)
        self.assertEqual(self.to("docs", mark), [])
        mark = self.seq
        self.turn("notes", report, "on T: the executor is done, MR checked")
        self.assertEqual(self.st["delegations"][did]["state"], "replied")
        (answer,) = self.to("docs", mark)
        self.assertIn(f"reply to task {did}", answer["origin"])
        self.assertTrue([m for m in self.to("hub", mark) if f"REPLIED {did}" in m["text"]])

    def test_before_the_parallel_setting_the_delegating_turn_answers_as_before(self):
        did, mid = self.task()
        mark = self.seq
        self.turn("notes", mid, "delegated notes-ci, waiting for the result", runs=["notes-ci"])
        self.assertEqual(self.st["delegations"][did]["state"], "replied")
        (answer,) = self.to("docs", mark)
        self.assertIn(f"reply to task {did}", answer["origin"])

    def test_an_executors_dash_still_reaches_the_mini_hub_that_waits_for_it(self):
        # review of PR #406: a bare "—" result is not sent to the assigning session, but a mini-hub gets no REPLIED for
        # its executor (the bus): without the "—" it never learned the executor was done, its task stayed with it
        self.minihub()
        did, mid = self.task()
        self.turn("notes", mid, "delegated", runs=["notes-ci"])
        self.start("notes-ci")
        mark = self.seq
        sub, _ = self.executor_done("notes-ci", "—")
        (got,) = self.to("notes", mark)
        self.assertEqual((got["text"], got.get("task")), ("—", sub["id"]))
        mark = self.seq
        self.turn("notes", got["id"], "on T: the executor checked, nothing new")
        self.assertEqual(self.st["delegations"][did]["state"], "replied")
        (answer,) = self.to("docs", mark)
        self.assertIn(f"reply to task {did}", answer["origin"])

    def test_waiting_for_executors_is_not_stuck_nor_timed_out(self):
        from core import delegations
        self.minihub()
        did, mid = self.task()
        self.turn("notes", mid, "delegated", runs=["notes-ci"])
        d = self.st["delegations"][did]
        self.assertIsNone(delegations.nudge_at(self.st, d))
        mark = self.seq
        self.ev("timer.fired", did, ts=d["deadline"] + 1)
        self.assertFalse([m for m in self.to("hub", mark) if "TIMEOUT" in m["text"]])
        self.assertFalse(self.st["delegations"][did]["timed_out"])
        self.assertGreater(self.st["delegations"][did]["deadline"], d["deadline"])   # deadline — restarted
        self.start("notes-ci")
        _, report = self.executor_done("notes-ci")
        self.turn("notes", report, "")          # a turn without a result: the task may get "stuck" again — the nudge ladder is back
        self.assertIsNotNone(delegations.nudge_at(self.st, self.st["delegations"][did]))

    def test_two_executors_the_answer_goes_after_both(self):
        self.minihub()
        did, mid = self.task()
        self.turn("notes", mid, "delegated to two", runs=["notes-a", "notes-b"])
        self.start("notes-a")
        self.start("notes-b")
        _, report = self.executor_done("notes-a")
        mark = self.seq
        self.turn("notes", report, "the first is done, waiting for the second")
        self.assertEqual(self.to("docs", mark), [])                     # both are already reported — no second line
        self.assertEqual(self.st["delegations"][did]["state"], "working")
        _, report = self.executor_done("notes-b")
        mark = self.seq
        self.turn("notes", report, "both are done")
        self.assertEqual(len(self.to("docs", mark)), 1)
        self.assertEqual(self.st["delegations"][did]["state"], "replied")

    def test_an_executor_asking_the_owner_still_holds_the_task(self):
        self.minihub()
        did, mid = self.task()
        self.turn("notes", mid, "delegated", runs=["notes-ci"])
        self.start("notes-ci")
        (sub,) = [d for d in self.st["delegations"].values() if d["session"] == "notes-ci"]
        self.ev("turn.started", "notes-ci", marker=sub["message"])
        self.ev("dialog.opened", "notes-ci", request_id="r1", tool="AskUserQuestion",
                input={"questions": [{"question": "Which?", "options": [{"label": "A"}, {"label": "B"}]}]})
        from core import minihub
        self.assertEqual(minihub.waiting_on(self.st, self.st["delegations"][did]), ["notes-ci"])

    def test_a_cancelled_request_is_off_the_list_and_the_minihubs_turn_continues_the_task(self):
        self.minihub(1)
        did, mid = self.task()
        self.turn("notes", mid, "delegated", runs=["notes-a", "notes-b"])
        self.assertEqual(self.state_of("notes-b"), "queued")
        self.start("notes-a")
        _, report = self.executor_done("notes-a")
        self.turn("notes", report, "the first is done")
        mark = self.seq
        self.ev("task.run_cancelled", "notes-b", **by("hub"))
        (note,) = [m for m in self.to("notes", mark)]
        self.assertEqual(note.get("for_delegation"), did)
        mark = self.seq
        self.turn("notes", note["id"], "the second is cancelled, on T — the result of the first")
        self.assertEqual(len(self.to("docs", mark)), 1)
        self.assertEqual(self.st["delegations"][did]["state"], "replied")

    def test_queue_wait_of_a_minihub_is_not_the_hubs_and_budget_is_one_card(self):
        self.minihub(1)
        did, mid = self.task()
        mark = self.seq
        self.turn("notes", mid, "delegated", runs=["notes-a", "notes-b"])
        self.assertEqual([m for m in self.to("hub", mark) if "is in the queue" in m["text"]], [])
        self.assertEqual([m for m in self.to("notes", mark) if "notes-b" in m["text"]], [])   # the number is in the output
        self.assertEqual(self.state_of("notes-b"), "queued")
        # with mini-hubs the budget is economy mode (slice 4, tests/test_economy.py): the hub gets one card on entering
        # the mode, not a line for every waiting request
        mark = self.seq
        self.ev("budget.mode", None, mode="economy", why="window 5 h 90%", until=10 ** 10, channel="core")
        self.ev("turn.started", "notes", marker=None)
        self.assertIsNone(self.run_task("notes-c"))
        self.assertIn("limit economy", self.st["runs"]["notes-c"]["why"])
        self.assertEqual([m for m in self.to("hub", mark) if "notes-c" in m["text"]], [])
        self.assertEqual(len([m for m in self.to("hub", mark) if "Limit economy" in m["text"]]), 1)

    def test_a_specialists_queue_note_still_goes_to_the_hub(self):
        self.ev("owner.settings", None, timezone="UTC", executors={"max_per_agent": 1}, channel="core")
        did, mid = self.task()
        mark = self.seq
        self.turn("notes", mid, "delegated", runs=["notes-a", "notes-b"])
        self.assertEqual(len([m for m in self.to("hub", mark) if "notes-b" in m["text"]]), 1)

    def test_a_failed_worktree_continues_the_task_too(self):
        self.minihub()
        did, mid = self.task()
        self.turn("notes", mid, "delegated", runs=["notes-ci"])
        mark = self.seq
        self.ev("task.run_failed", "notes-ci", reason="git worktree add: the branch is busy")
        (note,) = self.to("notes", mark)
        self.assertEqual(note.get("for_delegation"), did)

    def test_a_fallen_executor_leaves_the_list(self):
        self.minihub()
        did, mid = self.task()
        self.turn("notes", mid, "delegated", runs=["notes-ci"])
        self.start("notes-ci")
        (sub,) = [d for d in self.st["delegations"].values() if d["session"] == "notes-ci"]
        self.ev("turn.started", "notes-ci", marker=sub["message"])
        self.ev("turn.result", "notes-ci", text="", is_error=True, subtype="error_during_execution")
        (stop,) = [m for m in self.to("notes") if m["text"].startswith(f"STOPPED {sub['id']}")]
        mark = self.seq
        self.turn("notes", stop["id"], "the executor crashed — will start a new one later, for now the answer: not done")
        self.assertEqual(len(self.to("docs", mark)), 1)

    def test_hubs_own_task_tells_the_hub_once(self):
        self.minihub()
        self.ev("delegation.created", None, session="notes", context="T", **by("hub"))
        did, mid = f"d{self.seq}", f"m{self.seq}"
        mark = self.seq
        self.turn("notes", mid, "delegated", runs=["notes-ci"])
        notes = [m for m in self.to("hub", mark) if did in m["text"]]
        self.assertEqual(len(notes), 1)
        self.assertNotIn("REPLIED", notes[0]["text"])
        self.assertIn("notes-ci", notes[0]["text"])


class Wake(Delegating):
    """A mini-hub wakes its executor that has handed in, to rework its own task (until now only the hub and the owner
    could): through the caps; its result is again a reply to the mini-hub, and the mini-hub's task is again "with the
    executor"."""

    def done_and_asleep(self, did, mid):
        self.turn("notes", mid, "delegated", runs=["notes-ci"])
        self.start("notes-ci")
        sub, report = self.executor_done("notes-ci")
        self.turn("notes", report, "not everything is done — will wake it for rework")
        x = self.st["sessions"]["notes-ci"]
        x.update(state="retired")                    # handed in and fell asleep (core/forks: 2 min after the report)
        return sub

    def wake(self, about, who="notes", text="finish the test"):
        return self.ev("message.received", None, to="notes-ci", text=text, wake=True,
                       **({"about": about} if about else {}), **by(who))

    def test_minihub_wakes_its_executor_on_its_task(self):
        self.minihub()
        did, mid = self.task()
        sub = self.done_and_asleep(did, mid)
        self.assertEqual(self.st["delegations"][did]["state"], "replied")   # replied, even though "not everything"
        self.ev("delegation.created", None, session="notes", for_session="docs", context="T2", **by("docs"))
        did2, mid2 = f"d{self.seq}", f"m{self.seq}"
        self.ev("turn.started", "notes", marker=mid2)
        self.assertIsNone(self.wake(sub["id"]))
        woke = f"m{self.seq}"
        self.assertNotEqual(self.st["sessions"]["notes-ci"]["state"], "retired")
        self.assertEqual(self.st["delegations"][sub["id"]]["state"], "working")
        from core import minihub
        self.assertEqual(minihub.waiting_on(self.st, self.st["delegations"][did]), ["notes-ci"])
        self.ev("turn.result", "notes", text="woke notes-ci")
        self.ev("turn.stopped", "notes")
        self.ev("session.started", "notes-ci")
        mark = self.seq
        self.ev("turn.started", "notes-ci", marker=woke)
        self.ev("turn.result", "notes-ci", text="test finished")
        self.ev("turn.stopped", "notes-ci")
        (report,) = [m for m in self.to("notes", mark) if sub["id"] in (m.get("origin") or "")]
        self.assertIn("reply to task", report["origin"])

    def test_only_its_own_executor_and_only_its_task(self):
        self.minihub()
        did, mid = self.task()
        sub = self.done_and_asleep(did, mid)
        self.assertIn("--about dN is needed", self.wake(None))                     # name the task: not "not allowed"
        self.assertIn("--about dN is needed", self.ev("session.wake_requested", "notes-ci", **by("notes")) or "")
        self.assertEqual(self.st["sessions"]["notes-ci"]["state"], "retired")
        self.assertIn("--about", self.wake(did))                               # not its task
        self.assertIn("the owner, the hub or whoever raised it", self.wake(sub["id"], who="api-x"))  # someone else's
        self.solo("notes")                                                       # "solo": no executors
        self.assertIn("works alone", self.wake(sub["id"]))
        self.assertIn("works alone", self.wake(sub["id"], who="hub"))                   # and the hub cannot wake it either (case 1)
        self.solo("notes", on=False)
        self.assertIsNone(self.wake(sub["id"], who="hub"))

    def test_waking_goes_through_the_caps(self):
        self.minihub(1)
        did, mid = self.task()
        sub = self.done_and_asleep(did, mid)
        self.assertIsNone(self.run_task("notes-b"))
        self.start("notes-b")
        self.assertIsNone(self.wake(sub["id"]))   # slots are taken — not a refusal but a queue (slice 2, tests/test_minihub_bus)
        self.assertIn("1 working executors", self.st["runs"]["notes-ci"]["why"])
        self.assertEqual(self.st["sessions"]["notes-ci"]["state"], "retired")

    def test_a_term_keeps_an_executor_queued_for_rework(self):
        # 27.10: a mini-hub woke a handed-in executor for rework, places were busy — the request went to the core queue; the term
        # remove_after_hours removed the sleeper, and then task go answered "no such request in the queue"
        from core import executors
        self.minihub(1)
        did, mid = self.task()
        sub = self.done_and_asleep(did, mid)
        self.assertIsNone(self.run_task("notes-b"))
        self.start("notes-b")
        self.assertIsNone(self.wake(sub["id"]))
        x = self.st["sessions"]["notes-ci"]
        self.assertEqual(self.st["runs"]["notes-ci"]["state"], "queued")
        later = x["since"] + 10 ** 6
        self.assertNotIn(("remove", "notes-ci"), executors.retiring(self.st, later, keep=3600))   # in the queue — it holds
        self.ev("task.run_cancelled", "notes-ci", **by("notes"))
        self.assertIn(("remove", "notes-ci"), executors.retiring(self.st, later, keep=3600))      # cancelled — it goes

    def test_cli_passes_about(self):
        import os
        import subprocess
        import sys
        from core import spool
        tmp = tempfile.mkdtemp(prefix="minihub-cli-")
        hub = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
        env = {**os.environ, "HUB_STATE_DIR": tmp, "HUB_CLI_WAIT": "0"}
        env.pop("HUB_SESSION", None)
        r = subprocess.run([sys.executable, hub, "session", "wake", "x", "--about", "d7", "finish", "test"], env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        (ev,) = [e for _, e in spool.take(os.path.join(tmp, "spool"))]
        self.assertEqual((ev["data"]["to"], ev["data"]["about"], ev["data"]["text"], ev["data"]["wake"]),
                         ("x", "d7", "finish test", True))


class Backbone(Core):
    """The repo backbone (a human-only skill: /ship, /...-work) opens a working copy — the core rejects the mini-hub's
    edits in it. The backbone is run by an executor; the core does not let anyone enter the backbone into a mini-hub —
    not the mini-hub itself, nor the owner, nor the hub (2026-10-16: it was held only by rule text)."""

    def setUp(self):
        super().setUp()
        for name in ("notes", "api-x"):   # Claude Code at startup: which repo skills are human-only — a host fact
            self.ev("session.info", name, model="m", cwd=f"/{name}", builtins=["compact"], backbone=["repo-work"])

    def test_a_plain_session_still_gets_its_backbone(self):
        self.assertIsNone(self.ev("message.received", None, to="notes", text="/repo-work TICK-1", **by("notes"),
                                  slash=True))
        self.assertTrue(any(a.get("do") == "type" and a["text"].startswith("/repo-work TICK-1") for a in self.actions))

    def test_a_minihub_gets_no_backbone_from_anyone(self):
        self.parallel(3)
        self.assertTrue(minihub.on(self.st, self.st["sessions"]["notes"]))
        for who in (dict(**by("notes"), slash=True), OWNER, by("hub")):
            self.actions.clear()
            why = self.ev("message.received", None, to="notes", text="/repo-work TICK-1", **who)
            self.assertIn("an executor drives the scaffold", why or "", who)
            self.assertIn("hub task run", why)
            self.assertIn("via /repo-work", why)
            self.assertFalse([a for a in self.actions if a.get("do") == "type"], who)
        # an executor name `<mini-hub>-<slug>` over the limit — the refusal talks about length, not "Latin letters, digits"
        why = self.run_task("notes-" + "x" * 40)
        self.assertIn("at most 40 characters", why)
        self.assertNotIn("Latin letters", why)
        self.assertIsNone(self.run_task("notes-" + "x" * 34))
        self.assertIn("Latin letters", self.run_task("Notes-" + "x" * 40))   # wrong characters — the old refusal, one round
        # other commands and plain text — as before
        self.assertIsNone(self.ev("message.received", None, to="notes", text="/compact", **OWNER))
        self.assertIsNone(self.ev("message.received", None, to="notes", text="what is repo-work?", **OWNER))


class Host(unittest.TestCase):
    """A mini-hub at launch: the addendum and a ban on edits in its own folder via a Claude Code rule (--disallowedTools)."""

    def spawn(self, cwd, **kw):
        import os
        from core import catalog
        from hosts.streamjson import StreamHost
        state = tempfile.mkdtemp(prefix="minihub-host-")
        h = StreamHost(state, claude=("claude",))
        h.fresh_prompt = False
        h.tool_flags = lambda **k: catalog.launch_flags(catalog={}, cfg=catalog.load_config(state), state_dir=state,
                                                        **k)
        got = {}

        class P:
            stdout, stdin, pid = iter(()), None, 1

            def poll(self):
                return None

        def popen(cmd, **k):
            got["cmd"] = cmd
            return P()
        os.makedirs(cwd, exist_ok=True)
        with mock.patch("subprocess.Popen", popen), mock.patch("platforms.current.session_tree", lambda p: None), \
                mock.patch("threading.Thread"):
            self.assertEqual(h.spawn("maps", "sid", cwd=cwd, **kw), "spawned")
        cmd = got["cmd"]
        i = cmd.index("--disallowedTools")
        deny = []
        for a in cmd[i + 1:]:
            if a.startswith("--"):
                break
            deny.append(a)
        with open(cmd[cmd.index("--append-system-prompt-file") + 1], encoding="utf-8") as f:
            prompt = f.read()
        return deny, prompt

    def test_a_minihub_may_not_edit_its_folder_nor_commit(self):
        cwd = tempfile.mkdtemp(prefix="minihub-repo-")
        deny, prompt = self.spawn(cwd, minihub=True)
        from core import minihub
        self.assertIn(f"Edit(/{minihub._rule_path(cwd)}/**)", deny)   # Windows: C:\x → /c/x
        # Claude Code does not match Write(path) and warns on every launch — writes are held by Edit(path) (2026-10-21)
        self.assertFalse([r for r in deny if r.startswith("Write")], deny)
        # EnterWorktree: its own working copy — edits bypassing the executor (2026-10-16, the backbone was held only by rule text)
        for rule in ("NotebookEdit", "EnterWorktree", "Bash(git commit:*)", "Bash(git push:*)", "SendMessage"):
            self.assertIn(rule, deny)
        self.assertIn("You are a mini-hub", prompt)

    def test_without_the_flag_nothing_changes(self):
        deny, prompt = self.spawn(tempfile.mkdtemp(prefix="minihub-repo-"))
        self.assertEqual(deny, ["SendMessage", "ListAgents"])
        self.assertNotIn("mini-hub", prompt)

    def test_rules_for_windows_paths_and_a_folder_holding_its_own_files(self):
        from core import minihub
        self.assertIn("Edit(//c/Users/x/repo/**)", minihub.deny("C:\\Users\\x\\repo"))
        # its own folder holds its CLAUDE.md or the hub folder (a session without a folder lives in the home one): do not block the path
        rules = minihub.deny("/home/x", keep=("/home/x/.hub/agents/a/CLAUDE.md",))
        self.assertEqual([r for r in rules if r.startswith(("Edit", "Write"))], [])
        self.assertIn("Bash(git commit:*)", rules)
        rules = minihub.deny("/home/x/repo", keep=("/home/x/.hub/agents/a/CLAUDE.md", "/home/x/repo-other"))
        # on Windows /home/x/repo is D:\home\x\repo: the rule has //d/home/x/repo, without ":"
        home = os.path.splitdrive(os.path.abspath("/home"))[0].lower().rstrip(":")
        self.assertIn(f"Edit(/{'/' + home if home else ''}/home/x/repo/**)", rules)


class DaemonSettings(unittest.TestCase):
    def hub(self, cfg):
        from core import catalog
        from core.daemon import Hub
        h = Hub(tempfile.mkdtemp(prefix="minihub-d-"), type("H", (), {"alive": lambda s, n: True})(),
                registry_dir=tempfile.mkdtemp())
        base = catalog.load_config(h.state_dir)
        with mock.patch.object(catalog, "load_config", lambda _d: {**base, **cfg}):
            h.load_catalog()
        return h

    def test_caps_from_settings_reach_the_core_as_a_fact(self):
        h = self.hub({"executors": {"max_per_agent": 5, "max_live": 8, "normal": {"model": "sonnet"}}})
        self.assertEqual(h.state["owner"]["executors"], {"max_live": 8})   # the old caps are no longer a setting

    def test_default_settings_add_no_caps_and_parallel_three(self):
        h = self.hub({"minihub": {"default": True}})
        self.assertNotIn("executors", h.state["owner"])
        self.assertNotIn("minihub_default", h.state["owner"])
        self.assertEqual(h.state["owner"]["parallel"], 3)   # no value — 3 (owner decision 2026-10-15)

    def test_parallel_reaches_the_core_as_a_fact_once(self):
        from core import catalog
        h = self.hub({"executors": {"parallel": 0}})
        self.assertEqual(h.state["owner"]["parallel"], 0)
        seq = h.state["seq"]
        base = catalog.load_config(h.state_dir)
        with mock.patch.object(catalog, "load_config", lambda _d: {**base, "executors": {"parallel": 0}}):
            h.load_catalog()
        self.assertEqual(h.state["seq"], seq)               # unchanged — no new event

    def test_parallel_is_a_field_of_the_executors_settings(self):
        from modules.web import settings
        self.assertEqual(settings.validate("executors.max_live", 8), (8, None))
        self.assertEqual({settings.KEYS[k][4] for k in ("executors.parallel", "executors.max_live")}, {"executors"})
        self.assertNotIn("minihub.default", settings.KEYS)
        self.assertNotIn("executors.max_per_agent", settings.KEYS)
        self.assertNotIn("executors.max_per_other", settings.KEYS)
        self.assertEqual(settings.KEYS["executors.parallel"][3], 3)
        self.assertEqual(settings.validate("executors.parallel", 0), (0, None))
        self.assertEqual(settings.validate("executors.parallel", 4.0), (4, None))
        for bad in (2.5, -1, True, "3", 21):
            self.assertIsNone(settings.validate("executors.parallel", bad)[0], bad)
        self.assertEqual(executors.caps_from({"executors": {"max_live": 0, "max_per_agent": 2}}), {})


class ReviewFlag(Core):
    """The mini-hub mode over a session's life: the process is the one that parallelism, role, backlog and "solo"
    dictate."""

    def restarted(self, name):
        """With which mode its next process will start (spawn with the mode or without) — since the last call. The core
        parks a free session with nothing to do (slice 1 review: the first launch does not start everyone at once) —
        the next message wakes it."""
        sess = self.st["sessions"][name]
        if sess["state"] == "parked" and \
                not [a for a in self.actions if a.get("session") == name and a.get("do") == "spawn"]:
            self.ev("session.wake_requested", name, **OWNER)
        out = [a.get("minihub") for a in self.actions if a.get("session") == name and a.get("do") == "spawn"]
        self.actions.clear()
        if out:
            self.ev("session.started", name)
        return out

    def test_becoming_a_specialist_or_a_direction_restarts_a_live_session(self):
        self.parallel(3)
        self.ev("session.requested", "w", cwd="/w", session_id="s-w", **OWNER)
        self.ev("session.started", "w")
        self.actions.clear()
        self.ev("session.role_set", "w", role="agent", about="W", **OWNER)
        self.assertEqual(self.restarted("w"), [])                    # as a mini-hub — after the current task
        self.folder("w")
        self.assertEqual(self.restarted("w"), [True])
        self.ev("session.requested", "maps", cwd="/maps", session_id="s-maps", **OWNER)
        self.ev("session.started", "maps")
        self.actions.clear()
        self.ev("task.backlog", None, slug="maps", session="maps", **OWNER)
        self.folder("maps")
        self.assertEqual(self.restarted("maps"), [True])

    def test_losing_the_role_or_the_backlog_restarts_it_without_the_rules(self):
        self.parallel(3)
        self.assertIsNone(self.ev("session.role_set", "notes", role="worker", **OWNER))
        self.assertEqual(self.restarted("notes"), [None])
        self.ev("session.requested", "maps", cwd="/maps", session_id="s-maps", **OWNER)
        self.ev("session.started", "maps")
        self.actions.clear()
        self.ev("task.backlog", None, slug="maps", session="maps", **OWNER)
        self.folder("maps")
        self.assertEqual(self.restarted("maps"), [True])
        self.ev("task.backlog_closed", None, slug="maps", **OWNER)
        self.assertEqual(self.restarted("maps"), [None])

    def test_nothing_restarts_before_the_parallel_setting(self):
        self.ev("session.requested", "w", cwd="/w", session_id="s-w", **OWNER)
        self.ev("session.started", "w")
        self.actions.clear()
        self.ev("session.role_set", "w", role="agent", about="W", **OWNER)
        self.assertEqual(self.restarted("w"), [])

    def test_solo_survives_a_new_raise_of_a_system_agent_and_revive(self):
        from core import layout, minihub
        self.parallel(3)
        self.ev("session.requested", "docs", cwd="/docs", session_id="s-docs", role="agent", about="Docs", **OWNER)
        self.ev("session.started", "docs")
        self.folder("docs")
        self.assertTrue(minihub.on(self.st, self.st["sessions"]["docs"]))
        self.solo("docs")
        self.ev("session.remove_requested", "docs", **OWNER)
        with mock.patch.object(layout, "keeps_place", lambda st, s: s["name"] == "docs"):
            self.ev("session.requested", "docs", cwd="/docs", session_id="s-docs2", role="agent", about="Docs", **OWNER)
        self.assertFalse(minihub.on(self.st, self.st["sessions"]["docs"]))
        self.solo("notes")
        self.ev("session.remove_requested", "notes", **OWNER)
        self.ev("session.requested", "notes", cwd="/notes", session_id="s-notes", revive=True, **OWNER)
        self.assertTrue(self.st["sessions"]["notes"].get("solo"))


class ReviewWith(Delegating):
    """A task is "with executors" only while the link is alive; an interim result is not "news" (slice 2 review)."""

    def test_an_executor_whose_task_never_came_does_not_hold_the_task(self):
        from core import minihub
        self.minihub()
        did, mid = self.task()
        self.turn("notes", mid, "delegated", runs=["notes-ci"])
        run = self.st["runs"]["notes-ci"]
        (req, _task) = executors.events_for(self.st, "notes-ci", run, cwd="/x", worktree=None)
        self.assertIsNone(self.ev(req[0], req[1], **req[2]))       # it was started but the task (delegation.created) never arrived
        self.assertEqual(minihub.waiting_on(self.st, self.st["delegations"][did]), ["notes-ci"])
        self.ev("session.started", "notes-ci")
        self.assertEqual(minihub.waiting_on(self.st, self.st["delegations"][did]), [])

    def test_an_intermediate_result_is_not_news_for_the_owner(self):
        self.minihub()
        did, mid = self.task()
        self.turn("notes", mid, "delegated", runs=["notes-ci"])
        self.assertFalse(self.st["sessions"]["notes"].get("owner_out"))   # another session's task — not the owner's

    def test_free_does_not_walk_the_executors_of_a_busy_minihub(self):
        from core import delegations, minihub
        self.minihub()
        did, mid = self.task()
        self.turn("notes", mid, "delegated", runs=["notes-ci"])
        self.st["sessions"]["notes"]["background"] = {"t1": {}}
        with mock.patch.object(minihub, "waiting_on", side_effect=AssertionError("full pass")):
            self.assertIsNone(delegations.free(self.st, self.st["delegations"][did]))


class RulePath(unittest.TestCase):
    def test_a_folder_behind_a_link_is_expanded_to_its_real_path_with_a_drive_too(self):
        # PR #180, review: on Windows a path with a drive (D:\x) was not expanded by realpath — a junction in the deny
        # rules stayed itself, while Claude Code compares the real path (on POSIX /tmp -> /private/tmp was always expanded)
        import platforms
        real = os.path.realpath(tempfile.mkdtemp(prefix="minihub-real-"))
        self.addCleanup(shutil.rmtree, real, ignore_errors=True)
        link = os.path.join(os.path.realpath(tempfile.mkdtemp(prefix="minihub-link-")), "via")
        self.addCleanup(shutil.rmtree, os.path.dirname(link), ignore_errors=True)
        try:
            platforms.current.link_dir(real, link)
        except OSError as e:
            self.skipTest(f"cannot make a link: {e}")
        self.assertEqual(minihub._rule_path(link, real=True), minihub._rule_path(real))
        self.assertNotEqual(minihub._rule_path(link), minihub._rule_path(real))   # without real — as written
        rules = minihub.deny(link)
        for p in (minihub._rule_path(link), minihub._rule_path(real)):
            self.assertIn(f"Edit(/{p}/**)", rules)

    def test_realpath_failing_on_a_network_error_falls_back_to_abspath(self):
        # PR #180 review: ntpath.realpath raises OSError on network errors (a disconnected drive) — the mini-hub did not
        # start; the rule is built from the written path
        cwd = os.path.abspath(tempfile.mkdtemp(prefix="minihub-net-"))
        self.addCleanup(shutil.rmtree, cwd, ignore_errors=True)
        with mock.patch.object(minihub.os.path, "realpath", side_effect=OSError(1231, "network unavailable")):
            self.assertEqual(minihub._rule_path(cwd, real=True), minihub._rule_path(cwd))
            self.assertIn(f"Edit(/{minihub._rule_path(cwd)}/**)", minihub.deny(cwd))


class ReviewHost(unittest.TestCase):
    """The mini-hub ban also covers a nested `claude -p` and the folder's real path (slice 2 review)."""

    def test_nested_claude_gets_the_deny_rules_too(self):
        import json
        import os
        import runpy
        from hosts import streamjson
        cwd = tempfile.mkdtemp(prefix="minihub-repo-")
        state = tempfile.mkdtemp(prefix="minihub-host-")
        h = streamjson.StreamHost(state, claude=("claude",))
        h.fresh_prompt = False
        popen = mock.MagicMock()
        popen.poll.return_value = None
        with mock.patch.object(streamjson.subprocess, "Popen", return_value=popen) as p, \
                mock.patch.object(streamjson.threading, "Thread"):
            h.spawn("maps", "sid", cwd=cwd, minihub=True)
        env = p.call_args.kwargs["env"]
        deny = json.loads(env["HUB_NESTED_DENY"])
        from core import minihub
        self.assertIn(f"Edit(/{minihub._rule_path(cwd, real=True)}/**)", deny)   # Windows: C:\x → /c/x
        compose = runpy.run_path(os.path.join(os.path.dirname(streamjson.__file__), "nested", "claude"))["compose"]
        for argv in (["-p", "x"], ["-p", "--setting-sources", "", "--strict-mcp-config", "x"]):
            args = compose(argv, [], deny)
            i = args.index("--disallowedTools")
            self.assertEqual(args[i + 1:i + 1 + len(deny)], deny)
            self.assertEqual([a for a in args if a not in deny and a != "--disallowedTools"][-len(argv):], argv)
        self.assertNotIn("--disallowedTools", compose(["-p", "x"], [], []))
        self.assertNotIn("--disallowedTools", compose(["mcp", "list"], [], deny))
        self.assertEqual(compose(["-p", "--", "x"], [], ["A"])[-2:], ["--", "x"])        # after "--" — text only
        with mock.patch.object(streamjson.subprocess, "Popen", return_value=popen) as p, \
                mock.patch.object(streamjson.threading, "Thread"):
            h.spawn("other", "sid2", cwd=cwd)
        self.assertNotIn("HUB_NESTED_DENY", sorted(p.call_args.kwargs["env"]))

    def test_rules_cover_the_real_path_of_a_linked_folder(self):
        import os
        from core import minihub
        real = os.path.realpath(tempfile.mkdtemp(prefix="minihub-real-"))
        link = os.path.join(tempfile.mkdtemp(prefix="minihub-link-"), "repo")
        winsafe.symlink(self, real, link)
        rules = minihub.deny(link)
        self.assertIn(f"Edit(/{minihub._rule_path(link)}/**)", rules)   # Windows: C:\x → /c/x
        self.assertIn(f"Edit(/{minihub._rule_path(real)}/**)", rules)
        # the hub's own folder inside the real one — do not block the path via the link either
        self.assertEqual([r for r in minihub.deny(link, keep=(os.path.join(real, ".hub"),)) if r.startswith("Edit")], [])


class Review2With(Delegating):
    """Slice 2 review, round 2: an owner's task — the interim result is "news" to the owner (it is their "delegated"
    line), nothing to the hub; session tasks — one "delegated X" line, as before; an interim result does not go into
    the "Done" summary."""

    def owners_task(self):
        self.ev("message.received", None, to="hub", text="fix CI", **OWNER)
        self.ev("delegation.created", None, session="notes", **{"for": f"m{self.seq}"}, **by("hub"))
        return f"d{self.seq}", f"m{self.seq}"

    def test_an_intermediate_result_of_the_owners_task_is_news_and_the_hub_hears_nothing(self):
        self.minihub()
        did, mid = self.owners_task()
        mark = self.seq
        self.turn("notes", mid, "delegated notes-ci", runs=["notes-ci"])
        self.assertEqual(self.st["delegations"][did]["state"], "working")
        self.assertEqual(self.st["sessions"]["notes"].get("owner_out"), mark + 3)   # the turn's result is "news" for the owner
        self.assertEqual([m["text"] for m in self.to("hub", mark) if did in m["text"]], [])
        self.start("notes-ci")
        _, report = self.executor_done("notes-ci")
        mark = self.seq
        self.turn("notes", report, "done, checked")
        self.assertEqual(self.st["delegations"][did]["state"], "replied")
        self.assertEqual(self.st["sessions"]["notes"]["owner_out"], mark + 2)

    def test_an_intermediate_result_is_not_done_in_the_away_summary(self):
        self.minihub()
        self.ev("owner.away", None, **OWNER)
        did, mid = self.owners_task()
        self.turn("notes", mid, "delegated notes-ci", runs=["notes-ci"])
        self.assertEqual([d for d in self.st["owner"]["done"] if d["session"] == "notes"], [])
        self.start("notes-ci")
        _, report = self.executor_done("notes-ci")
        self.turn("notes", report, "done, checked")
        (done,) = [d for d in self.st["owner"]["done"] if d["session"] == "notes"]
        self.assertEqual((done["text"], done.get("delegation")), ("done, checked", did))


class Review2Restart(Core):
    """A respawn caused by a change of the mini-hub flag has its own reason in the background-work cut text (slice 2
    review)."""

    def cut_text(self, on):
        # a service in the background (runs past restart.SERVICE_AFTER, silent): the mode switch does not wait for it
        # and cuts it; working background holds the switch (test_the_switch_waits_for_working_background)
        self.st["sessions"]["notes"]["background"] = {"t1": dict(SERVICE_BG)}
        mark = self.seq
        self.parallel(3) if on else self.solo("notes")
        (m,) = [m for m in self.st["messages"].values() if m["session"] == "notes" and "bundle-run" in m["text"]
                and int(m["id"][1:].split("x")[0]) > mark]
        return m["text"]

    def test_the_cut_names_the_minihub_switch(self):
        self.st["sessions"]["notes"]["background"] = {"t1": {"title": "bundle-run"}}
        self.parallel(3)                                  # do not cut background work: becomes a mini-hub once there is none (slice 1 review)
        self.assertFalse(minihub.on(self.st, self.st["sessions"]["notes"]))
        self.assertEqual([m for m in self.st["messages"].values() if "bundle-run" in m["text"]], [])
        self.st["sessions"]["notes"]["background"] = {}
        self.folder("notes")
        self.unpark({"notes"})
        text = self.cut_text(False)
        self.assertIn("mini-hub mode is off", text)
        self.assertNotIn("a new tool", text)

    def test_other_restarts_keep_their_text(self):
        self.parallel(3)
        self.st["sessions"]["notes"]["background"] = {"t1": {"title": "bundle-run"}}
        self.ev("session.model_set", "notes", model="sonnet", **OWNER)
        self.ev("session.started", "notes")
        (m,) = [m for m in self.st["messages"].values() if m["session"] == "notes" and "bundle-run" in m["text"]]
        self.assertIn("a new tool, model or folder", m["text"])


class Review2Cli(Delegating):
    """`hub session wake X --about dN` without text: for the owner and the hub — an ordinary wake, as before; for a
    mini-hub — a refusal with a hint (the core would not let it through without text anyway). --about — only right
    after the name."""

    def hub_cli(self, *args, me=None):
        import json
        import os
        import subprocess
        import sys
        from core import spool
        tmp = tempfile.mkdtemp(prefix="minihub-cli-")
        with open(os.path.join(tmp, "journal.jsonl"), "w", encoding="utf-8") as f:
            for e in self.events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        hub = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
        env = {**os.environ, "HUB_STATE_DIR": tmp, "HUB_CLI_WAIT": "0"}
        env.pop("HUB_SESSION", None)
        if me:
            env["HUB_SESSION"] = me
        r = subprocess.run([sys.executable, hub, *args], env=env, capture_output=True, text=True)
        return r, [e for _, e in spool.take(os.path.join(tmp, "spool"))]

    def asleep(self):
        self.minihub()
        did, mid = self.task()
        self.turn("notes", mid, "delegated", runs=["notes-ci"])
        self.start("notes-ci")
        sub, report = self.executor_done("notes-ci")
        self.ev("session.stop_requested", "notes-ci", **OWNER)
        return sub["id"]

    def test_owner_and_hub_wake_as_before(self):
        sub = self.asleep()
        for me, about in ((None, [sub]), ("hub", [sub])):
            r, got = self.hub_cli("session", "wake", "notes-ci", "--about", *about, me=me)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual([(e["type"], e["entity"]) for e in got], [("session.wake_requested", "notes-ci")], me)

    def test_the_minihub_without_the_text_gets_the_hint(self):
        sub = self.asleep()
        from core import executors as ex
        self.assertTrue(ex.dormant(self.st["sessions"]["notes-ci"]))
        r, got = self.hub_cli("session", "wake", "notes-ci", "--about", sub, me="notes")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--about", r.stderr)
        self.assertEqual(got, [])

    def test_about_only_right_after_the_name(self):
        r, got = self.hub_cli("session", "wake", "x", "finish", "--about", "d7")
        self.assertEqual(r.returncode, 0, r.stderr)
        (e,) = got
        self.assertEqual((e["data"]["text"], e["data"].get("about")), ("finish --about d7", None))


class Review3With(Delegating):
    """Slice 2 review, round 3: an interim result is only a turn busy with nothing else (neither an owner's turn nor a
    reply to another task); the core marks it interim_out, and "Done" reads that mark."""

    def card_answer_turn(self):
        """A mini-hub asked the owner with a card on a session's task, the owner answered from Telegram and went away;
        the turn on the answer is a delegation."""
        did, mid = self.task()
        self.ev("turn.started", "notes", marker=mid)
        self.ev("permission.requested", "notes", request_id="r1", tool="AskUserQuestion", interactive=True,
                input={"questions": [{"question": "Which CI?"}]})
        self.ev("turn.result", "notes", text="asked")
        self.ev("turn.stopped", "notes")
        self.assertEqual(self.st["delegations"][did]["state"], "waiting-owner")
        self.ev("permission.answered", "notes", request_id="r1", behavior="allow", answers={"Which CI?": "shared"},
                channel="telegram", owner=True)
        (ans,) = [m for m in self.st["messages"].values() if m.get("for_delegation") == did]
        self.ev("owner.away", None, **OWNER)
        self.turn("notes", ans["id"], "delegated notes-ci", runs=["notes-ci"])
        return did, self.seq - 1

    def now_turn(self):
        """In the middle of a delegation on a session's task the hub urgently (--now) inserted an owner's task — the turn's result answers it."""
        did, mid = self.task()
        self.ev("turn.started", "notes", marker=mid)
        self.ev("message.received", None, to="hub", text="and collect the log", **OWNER)
        self.ev("delegation.created", None, session="notes", **{"for": f"m{self.seq}"}, context="log", now=True,
                **by("hub"))
        now = f"d{self.seq}"
        self.assertTrue(self.st["messages"][f"m{self.seq}"].get("injected"))
        self.ev("turn.started", "notes", marker=f"m{self.seq}")
        self.assertIsNone(self.run_task("notes-ci"))
        self.ev("turn.result", "notes", text="delegated notes-ci; log collected")
        self.ev("turn.stopped", "notes")
        return did, now, self.seq - 1

    def test_a_pure_intermediate_result_gets_the_mark(self):
        self.minihub()
        did, mid = self.task()
        self.turn("notes", mid, "delegated notes-ci", runs=["notes-ci"])
        self.assertEqual(self.st["delegations"][did]["passed_seq"], self.seq - 1)
        self.assertEqual(self.st["sessions"]["notes"].get("interim_out"), self.seq - 1)

    def test_the_owners_own_turn_is_not_intermediate(self):
        self.minihub()
        did, res = self.card_answer_turn()
        self.assertEqual(self.st["delegations"][did]["passed_seq"], res)
        self.assertNotEqual(self.st["sessions"]["notes"].get("interim_out"), res)
        (done,) = [d for d in self.st["owner"]["done"] if d["session"] == "notes"]
        self.assertEqual(done["text"], "delegated notes-ci")

    def test_a_turn_answering_another_task_is_not_intermediate_and_is_done(self):
        self.minihub()
        self.ev("owner.away", None, **OWNER)
        did, now, res = self.now_turn()
        self.assertEqual(self.st["delegations"][did]["passed_seq"], res)
        self.assertEqual((self.st["delegations"][now]["state"], self.st["delegations"][now]["replied_seq"]),
                         ("replied", res))
        self.assertNotEqual(self.st["sessions"]["notes"].get("interim_out"), res)
        (done,) = [d for d in self.st["owner"]["done"] if d["session"] == "notes"]
        self.assertEqual((done["text"], done.get("delegation")), ("delegated notes-ci; log collected", now))


class Review3Restart(Core):
    """The "mini-hub mode" cut text appears only when the process is respawned because of the flag, and only because of it."""

    def busy_with_background(self, bg=None):
        self.ev("turn.started", "notes", marker=None)
        self.st["sessions"]["notes"]["background"] = {"t1": dict(bg or SERVICE_BG)}
        return self.seq

    def end_turn(self, mark):
        self.ev("turn.result", "notes", text="ok")
        self.ev("turn.stopped", "notes")
        if any(a.get("session") == "notes" and a.get("do") == "spawn" for a in self.actions):
            self.ev("session.started", "notes")
        return [m["text"] for m in self.st["messages"].values() if m["session"] == "notes" and "bundle-run" in m["text"]
                and int(m["id"][1:].split("x")[0]) > mark]

    def test_switched_on_and_back_off_during_a_turn_does_not_name_the_switch(self):
        mark = self.busy_with_background()
        self.ev("owner.settings", None, timezone="UTC", parallel=3, channel="core")
        self.ev("owner.settings", None, timezone="UTC", parallel=0, channel="core")
        self.assertEqual([t for t in self.end_turn(mark) if "mini-hub mode" in t], [])

    def test_with_another_reason_before_or_after_the_text_is_the_old_one(self):
        model = lambda m: ("session.model_set", "notes", {"model": m, **OWNER})   # noqa: E731
        par = lambda n: ("owner.settings", None, {"timezone": "UTC", "parallel": n, "channel": "core"})   # noqa: E731
        for first, second in ((model("sonnet"), par(3)), (par(0), model("haiku"))):
            mark = self.busy_with_background()
            self.ev(first[0], first[1], **first[2])
            self.ev(second[0], second[1], **second[2])
            (text,) = self.end_turn(mark)
            self.assertIn("a new tool, model or folder", text)
            self.assertNotIn("mini-hub mode", text)

    def test_the_switch_waits_for_working_background(self):
        # 11.11: a relaunch the core decides on itself waits for the session's background work (reducer._relaunch_waits)
        self.parallel(3)
        mark = self.busy_with_background({"title": "bundle-run", "kind": "bash", "since": self.seq + 1000})
        self.ev("owner.settings", None, timezone="UTC", parallel=0, channel="core")
        self.actions = []
        self.assertEqual(self.end_turn(mark), [])
        self.assertEqual([a["do"] for a in self.actions if a.get("session") == "notes" and a["do"] in ("release", "spawn")], [])
        self.st["sessions"]["notes"]["background"] = {}
        self.ev("session.background", "notes", tasks=[])
        # the background ended: the switch goes — with no task it is parked, the new rules come with the next message
        self.assertEqual([a["do"] for a in self.actions if a.get("session") == "notes" and a["do"] in ("release", "spawn")],
                         ["release"])
        self.assertEqual(self.st["sessions"]["notes"]["state"], "parked")

    def test_the_switch_alone_during_a_turn_names_it(self):
        self.parallel(3)                                  # switches on only for a free session — but switches off mid-turn too
        mark = self.busy_with_background()
        self.ev("owner.settings", None, timezone="UTC", parallel=0, channel="core")
        (text,) = self.end_turn(mark)
        self.assertIn("mini-hub mode is off", text)


class Review3Cli(Delegating):
    """`hub session wake`: --about only with dN (otherwise the words are text, as before); the "text needed" hint only
    for the mini-hub that raised this executor, everyone else gets the core's ordinary refusal."""

    hub_cli = Review2Cli.hub_cli
    asleep = Review2Cli.asleep

    def test_about_without_a_task_id_is_text_as_before(self):
        for args, text in ((["--about", "finish", "tests"], "--about finish tests"), (["--about"], "--about")):
            r, got = self.hub_cli("session", "wake", "x", *args)
            self.assertEqual(r.returncode, 0, r.stderr)
            (e,) = got
            self.assertEqual((e["type"], e["data"]["text"], e["data"].get("about")), ("message.received", text, None))

    def test_the_hint_only_for_the_minihub_that_raised_it(self):
        sub = self.asleep()
        r, got = self.hub_cli("session", "wake", "notes-ci", "--about", sub, me="api-x")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([(e["type"], e["entity"]) for e in got], [("session.wake_requested", "notes-ci")])
        self.ev("session.solo_set", "notes", on=True, **OWNER)
        r, got = self.hub_cli("session", "wake", "notes-ci", "--about", sub, me="notes")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([(e["type"], e["entity"]) for e in got], [("session.wake_requested", "notes-ci")])


class Review2Host(unittest.TestCase):
    """Slice 2 review, round 2: the hub's own folder is outside the ban in any path form; the old environment's bans do
    not leak into the environment of a session without them."""

    def test_keep_by_its_own_path_or_its_real_one_inside_any_form_of_the_folder(self):
        import os
        from core import minihub
        real = os.path.realpath(tempfile.mkdtemp(prefix="minihub-real-"))
        away = os.path.realpath(tempfile.mkdtemp(prefix="minihub-away-"))
        winsafe.symlink(self, away, os.path.join(real, ".hub"))   # the hub folder inside the session folder — a link pointing outside
        edits = [r for r in minihub.deny(real, keep=(os.path.join(real, ".hub", "agents", "a", "CLAUDE.md"),))
                 if r.startswith(("Edit", "Write"))]
        self.assertEqual(edits, [])
        link = os.path.join(tempfile.mkdtemp(prefix="minihub-link-"), "repo")
        winsafe.symlink(self, real, link)                         # and the session folder — via a link
        edits = [r for r in minihub.deny(link, keep=(os.path.join(link, ".hub"),)) if r.startswith(("Edit", "Write"))]
        self.assertEqual(edits, [])

    def test_a_session_without_the_rules_does_not_inherit_them(self):
        import os
        from hosts import streamjson
        cwd = tempfile.mkdtemp(prefix="minihub-repo-")
        h = streamjson.StreamHost(tempfile.mkdtemp(prefix="minihub-host-"), claude=("claude",))
        h.fresh_prompt = False
        popen = mock.MagicMock()
        popen.poll.return_value = None
        with mock.patch.dict(os.environ, {"HUB_NESTED_DENY": '["Edit(//x/**)"]'}), \
                mock.patch.object(streamjson.subprocess, "Popen", return_value=popen) as p, \
                mock.patch.object(streamjson.threading, "Thread"):
            h.spawn("other", "sid", cwd=cwd)
        self.assertNotIn("HUB_NESTED_DENY", sorted(p.call_args.kwargs["env"]))



class Review1(Delegating):
    """Slice 1 review, round 1: dropped requests go into the task; free means no background work and no queue; moving
    the folder restarts the switch; a closed session does not "become" one; a fraction below 1 is invalid; the switch
    parks a free session with nothing to do; the gate is in one place."""

    def test_dropped_requests_belong_to_their_task(self):
        self.minihub(1)
        did, mid = self.task()
        self.turn("notes", mid, "delegated", runs=["notes-a", "notes-b"])
        mark = self.seq
        self.parallel(0)
        (line,) = [m for m in self.to("notes", mark) if "Executor requests are dropped" in m["text"]]
        self.assertEqual(line.get("for_delegation"), did)              # the "do it yourself" line belongs to that task
        self.assertEqual(minihub.waiting_on(self.st, self.st["delegations"][did]), [])
        mark = self.seq
        self.turn("notes", line["id"], "did it myself")
        self.assertEqual(self.st["delegations"][did]["state"], "replied")
        self.assertEqual(len(self.to("docs", mark)), 1)

    def test_free_means_no_background_and_no_waiting_messages(self):
        self.parallel(3, clean=False)
        notes = lambda: self.st["sessions"]["notes"]   # noqa: E731 — the fold returns the new state
        notes()["background"] = {"t1": {"title": "bundle-run"}}
        self.assertFalse(minihub.free(self.st, notes()))
        self.folder("notes")
        self.assertFalse(minihub.on(self.st, notes()))                 # do not cut background work
        notes()["background"] = {}
        self.ev("message.received", None, to="notes", text="take a look", **by("docs"))
        self.assertFalse(minihub.free(self.st, notes()))                # a message is waiting — there is work to do
        self.folder("notes")
        self.assertFalse(minihub.on(self.st, notes()))

    def test_moved_folder_goes_through_the_transition_again(self):
        self.parallel(3)
        self.assertTrue(minihub.on(self.st, self.st["sessions"]["notes"]))
        self.ev("session.cwd_set", "notes", cwd="/elsewhere", **OWNER)
        self.respawned()
        notes = self.st["sessions"]["notes"]
        self.assertFalse(minihub.on(self.st, notes))
        self.assertEqual(minihub.pending(self.st, notes), PENDING_TASK)
        self.folder("notes")
        self.assertTrue(minihub.on(self.st, self.st["sessions"]["notes"]))

    def test_a_fraction_below_one_is_bad_not_zero(self):
        self.assertEqual([minihub.parallel_of(v) for v in (0, 0.0, 0.5, 0.99, 1.7)], [0, 0, 3, 3, 1])

    def test_a_gone_session_is_not_pending(self):
        self.parallel(3, clean=False)
        for gone in ("closed", "failed", "removed"):
            sess = {**self.st["sessions"]["notes"], "state": gone}
            self.assertIsNone(minihub.pending(self.st, sess), gone)

    def test_a_free_session_without_work_is_parked_not_restarted(self):
        self.actions.clear()
        self.ev("owner.settings", None, timezone="UTC", parallel=3, channel="core")
        self.folder("notes")
        notes = self.st["sessions"]["notes"]
        self.assertTrue(minihub.on(self.st, notes))
        self.assertEqual([a for a in self.actions if a.get("do") == "spawn"], [])   # do not start everyone at once
        self.assertEqual(notes["state"], "parked")
        self.actions.clear()
        self.ev("message.received", None, to="notes", text="take a look", **by("docs"))
        (spawn,) = [a for a in self.actions if a.get("do") == "spawn"]
        self.assertTrue(spawn.get("minihub"))                          # the new rules apply from the next message

    def test_the_gate_asks_once(self):
        self.parallel(0)
        with mock.patch.object(minihub, "alone", wraps=minihub.alone) as alone:
            self.assertIn("parallelism 0", self.run_task("notes-a"))
        self.assertEqual(alone.call_count, 1)


class Review1Daemon(unittest.TestCase):
    """Slice 1 review, round 1: the daemon and folders — a git error does not crash a tick; the expensive check runs
    only for those whose time has come."""

    git, repo = Folders.git, Folders.repo   # helpers without inheriting (a subclass would pull in the Folders tests)

    def hub_with(self, cwd):
        h = DaemonSettings.hub(self, {})
        h.record("session.requested", "spec", {"cwd": cwd, "session_id": "s-spec", "role": "agent", "about": "spec",
                                               "channel": "cli", "owner": True})
        h.record("session.started", "spec")
        return h

    def test_a_git_failure_is_a_fact_not_a_crash(self):
        import subprocess
        from hosts import worktree
        h = self.hub_with(self.repo())
        boom = subprocess.TimeoutExpired(["git", "status"], 30)
        with mock.patch.object(worktree, "folder", side_effect=boom):
            h.check_folders()
            spec = h.state["sessions"]["spec"]
            self.assertIn("did not answer in time", spec.get("folder_error") or "")
            self.assertFalse(minihub.on(h.state, spec))                 # not checked — not clean
            self.assertEqual(i18n.t("core.minihub.pending_folder", error=spec["folder_error"]),
                             minihub.pending(h.state, spec))
            seq = h.state["seq"]
            h.folders_at.clear()
            h.check_folders()                                           # the same error — no new fact
            self.assertEqual(h.state["seq"], seq)
        h.folders_at.clear()
        h.check_folders()
        self.assertNotIn("folder_error", h.state["sessions"]["spec"])
        self.assertTrue(minihub.on(h.state, h.state["sessions"]["spec"]))

    def test_free_is_asked_only_when_due(self):
        h = self.hub_with(self.repo())
        h.check_folders()
        with mock.patch.object(minihub, "free", wraps=minihub.free) as free:
            for _ in range(5):
                h.check_folders()
        self.assertEqual(free.call_count, 0)



class Review1Round2(Delegating):
    """Slice 1 review, round 2: a background service and a message taken by an executor are not work; the same folder
    resets nothing; "will become a mini-hub" takes one pass per snapshot; the first message wakes a parked mini-hub."""

    def test_any_background_holds_the_transition(self):
        self.parallel(3, clean=False)
        self.ev("session.background", "notes", id="srv1", kind="bash", title="dev-server", ts=2000)
        late = 2000 + restart.SERVICE_AFTER + 60   # runs past the service term and stays silent — still not cut
        self.actions.clear()
        self.ev("session.folder", "notes", ts=late, src="core", channel="core", git="ok", clean=True)
        notes = self.st["sessions"]["notes"]
        self.assertFalse(minihub.on(self.st, notes))
        self.assertEqual([a for a in self.actions if a.get("do") in ("release", "spawn")], [])   # the dev server is alive
        self.assertEqual(minihub.pending(self.st, notes), i18n.t("core.minihub.pending_background"))

    def test_a_message_taken_by_an_executor_still_holds_the_transition(self):
        self.parallel(3, clean=False)
        self.ev("turn.started", "notes", marker=None, prompt_head="own")
        self.ev("message.received", None, to="notes", text="take a look", **by("docs"))
        mid = f"m{self.seq}"
        self.st["messages"][mid]["handover"] = "notes-a"   # taken by an executor (hub route ... executor), not started yet
        self.st.setdefault("runs", {})["notes-a"] = {"by": "notes", "task": "take a look", "message": mid, "repo": None,
                                                     "ts": 1, "state": "queued", "why": "queue", "auto": True}
        self.ev("turn.result", "notes", text="ok")
        self.ev("turn.stopped", "notes")
        self.assertEqual(self.st["messages"][mid]["state"], "queued")
        self.folder("notes")
        notes = self.st["sessions"]["notes"]
        self.assertFalse(minihub.on(self.st, notes))                    # queued — it holds until the executor starts
        self.assertEqual(minihub.pending(self.st, notes), PENDING_TASK)

    def test_background_with_other_work_names_the_current_task(self):
        self.parallel(3, clean=False)
        self.ev("session.background", "notes", id="b1", kind="agent", title="triage", ts=2000)
        self.ev("message.received", None, to="notes", text="take a look", **by("docs"))   # plus a task in the queue
        notes = self.st["sessions"]["notes"]
        self.assertEqual(minihub.pending(self.st, notes), PENDING_TASK)

    def test_the_same_folder_is_a_no_op(self):
        self.parallel(3)
        self.assertTrue(minihub.on(self.st, self.st["sessions"]["notes"]))
        self.actions.clear()
        self.assertIsNone(self.ev("session.cwd_set", "notes", cwd="/notes/", **OWNER))   # a trailing slash — the same folder
        self.assertEqual(self.actions, [])                              # no restart
        notes = self.st["sessions"]["notes"]
        self.assertTrue(minihub.on(self.st, notes))                     # no new switch
        self.assertEqual((notes["cwd"], bool(notes.get("dirty"))), ("/notes", False))
        self.ev("session.cwd_set", "notes", cwd="/notes/./", **OWNER)  # different text — a different folder (symlinks)
        self.assertFalse(minihub.on(self.st, self.st["sessions"]["notes"]))

    def test_the_folders_solo_reason_reads_in_the_owners_language(self):
        # solo_auto in the state is the English phrase; the page (the snapshot, in the page server's language) and the
        # refusal to unset "solo" show it to the Russian owner in Russian, a session reads the English
        from core import coretext, views
        self.parallel(3, clean=False)
        self.folder("notes", git="none")
        self.assertEqual(self.st["sessions"]["notes"]["solo_auto"], "the folder is not git")
        r = views.Reader()
        r.state = self.st
        rows = {s["name"]: s for s in r.snapshot()["sessions"]}
        self.assertEqual(rows["notes"]["solo_auto"], i18n.t("core.minihub.auto_none"))
        self.assertIsNone(rows["api-x"]["solo_auto"])
        why = self.solo("notes", on=False)
        self.assertIn("works solo not by the checkbox: the folder is not git", why)
        self.assertIn(i18n.tr("ru", "core.reducer.solo_auto", name="notes", why=i18n.tr("ru", "core.minihub.auto_none")),
                      coretext.human(why, "ru"))

    def test_the_snapshot_skips_the_scan_when_nobody_waits(self):
        from core import views
        r = views.Reader()
        for n in (0, 3):                                                # N = 0; N = 3 and all are already mini-hubs
            self.parallel(n)
            r.state = self.st
            with mock.patch.object(minihub, "busy", wraps=minihub.busy) as busy:
                r.snapshot()
            self.assertEqual(busy.call_count, 0, n)

    def test_pending_scans_once_per_snapshot(self):
        from core import views
        self.parallel(3, clean=False)
        self.ev("session.background", "notes", id="b1", kind="agent", title="triage", ts=2000)
        with mock.patch.object(minihub, "free", wraps=minihub.free) as free:
            self.assertEqual(i18n.t("core.minihub.pending_background"),
                             minihub.pending(self.st, self.st["sessions"]["notes"]))
        self.assertEqual(free.call_count, 1)
        r = views.Reader()
        r.state = self.st
        with mock.patch.object(minihub, "busy", wraps=minihub.busy) as busy:
            rows = {s["name"]: s["minihub_pending"] for s in r.snapshot()["sessions"]}
        self.assertEqual(busy.call_count, 1)                            # not once per session
        self.assertEqual(i18n.t("core.minihub.pending_background"), rows["notes"])
        self.assertEqual(rows["api-x"], PENDING_TASK)

    def test_a_parked_minihub_wakes_with_its_first_message(self):
        self.actions.clear()
        self.ev("owner.settings", None, timezone="UTC", parallel=3, channel="core")
        self.folder("notes")
        self.assertEqual(self.st["sessions"]["notes"]["state"], "parked")   # parked, not started
        self.actions.clear()
        self.ev("message.received", None, to="notes", text="look into the bug", **by("docs"))   # not an owner wake
        mid = f"m{self.seq}"
        (spawn,) = [a for a in self.actions if a.get("do") == "spawn"]
        self.assertTrue(spawn.get("minihub"))                           # started as a mini-hub — with the addendum
        self.actions.clear()
        self.ev("session.started", "notes")
        self.assertTrue(minihub.on(self.st, self.st["sessions"]["notes"]))
        (typed,) = [a for a in self.actions if a.get("do") == "type"]
        self.assertEqual(typed["message"], mid)                         # and this message is delivered to it
        self.ev("turn.started", "notes", marker=mid)
        self.assertEqual(self.st["messages"][mid]["state"], "delivered")


class Review1Round2Daemon(unittest.TestCase):
    """Slice 1 review, round 2: non-UTF-8 git output does not crash a tick; the error reason is short, without paths; a
    repeat of the same kind produces no new fact."""

    git, repo = Folders.git, Folders.repo

    def test_undecodable_git_output_is_a_fact_not_a_crash(self):
        from hosts import worktree
        h = Review1Daemon.hub_with(self, self.repo())
        bad = UnicodeDecodeError("utf-8", b"\xc8", 0, 1, "invalid start byte")
        with mock.patch.object(worktree, "folder", side_effect=bad):
            h.check_folders()
        self.assertEqual(h.state["sessions"]["spec"].get("folder_error"), "git: UnicodeDecodeError")

    def test_git_output_is_read_as_utf8_whatever_the_locale(self):
        import subprocess
        from hosts import worktree
        from platforms import current
        base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, base, True)
        name = lang.word("words", "topic_label", 1).capitalize()   # a Russian word of the language data
        self.assertIn(b"\x98", name[0].encode("utf-8"))   # the first letter in UTF-8 contains byte 0x98, which cp1251 lacks
        top = os.path.join(base, name)
        os.mkdir(top)
        self.git(top, "init", "-q")
        self.git(top, "commit", "-q", "--allow-empty", "-m", "x")

        def locale_run(args, timeout, **kw):   # like Windows with cp1251: text without encoding uses the system encoding
            enc, errors = kw.pop("encoding", None) or "cp1251", kw.pop("errors", None) or "strict"
            kw.pop("text", None)
            r = subprocess.run(args, timeout=timeout, **kw)
            return subprocess.CompletedProcess(args, r.returncode, r.stdout.decode(enc, errors),
                                               r.stderr.decode(enc, errors))
        with mock.patch.object(current, "run", side_effect=locale_run):
            self.assertEqual(worktree.folder(top, dirty=True), ("ok", 0))

    def test_the_reason_is_short_and_the_same_kind_is_not_repeated(self):
        import subprocess
        from hosts import worktree
        h = Review1Daemon.hub_with(self, self.repo())
        where = "/home/x/very/long/folder"
        boom = subprocess.TimeoutExpired(["git", "-C", where, "status", "--porcelain"], 30)
        with mock.patch.object(worktree, "folder", side_effect=boom):
            h.check_folders()
        spec = h.state["sessions"]["spec"]
        self.assertEqual(spec["folder_error"], "git status: did not answer in time")
        self.assertIn("(git status: did not answer in time)", minihub.pending(h.state, spec))
        seq = h.state["seq"]
        again = subprocess.TimeoutExpired(["git", "-C", where + "-2", "status", "--porcelain"], 31, output=b"x")
        h.folders_at.clear()
        with mock.patch.object(worktree, "folder", side_effect=again):
            h.check_folders()
        self.assertEqual(h.state["seq"], seq)                           # the same kind — no new fact


class Dirty(Delegating):
    """Uncommitted changes in the folder do not hold the switch (owner decision 2026-10-16: an executor sorts them out):
    the mini-hub sees them as a number (page, hub status) and delegates the sorting to an executor. The daemon's fact
    is the dirty field (git status lines); the old core's clean field folds the old way."""

    hub_cli = Review2Cli.hub_cli

    def test_a_free_session_with_a_dirty_folder_becomes_a_minihub(self):
        self.parallel(3, clean=False)
        self.actions.clear()
        self.folder("notes", dirty=3)
        notes = self.st["sessions"]["notes"]
        self.assertTrue(minihub.on(self.st, notes))
        self.assertEqual(notes["folder_dirty"], 3)
        self.assertIsNone(minihub.pending(self.st, notes))
        self.assertEqual(minihub.dirty(self.st, notes), i18n.t("core.minihub.dirty", n=3))
        self.assertEqual([a for a in self.actions if a.get("do") == "spawn"], [])   # parking, not a spawn
        self.assertEqual(notes["state"], "parked")

    def test_busy_it_waits_for_the_task_only(self):
        did, mid = self.task()
        self.ev("turn.started", "notes", marker=mid)
        self.parallel(3, clean=False)
        self.folder("notes", dirty=2)
        notes = self.st["sessions"]["notes"]
        self.assertFalse(minihub.on(self.st, notes))
        self.assertEqual(minihub.pending(self.st, notes), PENDING_TASK)
        self.assertIsNone(minihub.dirty(self.st, notes))               # the mark exists only for a mini-hub

    def test_the_number_follows_the_folder_and_zero_clears_it(self):
        self.parallel(3, clean=False)
        self.folder("notes", dirty=2)
        self.folder("notes", dirty=5)
        notes = self.st["sessions"]["notes"]
        self.assertEqual((notes["folder_dirty"], minihub.on(self.st, notes)), (5, True))
        since = notes["minihub_since"]
        self.folder("notes", dirty=0)
        notes = self.st["sessions"]["notes"]
        self.assertNotIn("folder_dirty", notes)
        self.assertEqual(notes["minihub_since"], since)                # the switch is not restarted

    def test_known_dirty_from_the_old_core_moves_on_the_new_fact(self):
        self.parallel(3, clean=False)
        self.folder("notes", clean=False)                              # the old core: dirty — waits
        self.assertFalse(minihub.on(self.st, self.st["sessions"]["notes"]))
        self.folder("notes", dirty=1)                                  # the new one: switch
        notes = self.st["sessions"]["notes"]
        self.assertTrue(minihub.on(self.st, notes))
        self.assertEqual(notes["folder_dirty"], 1)

    def test_snapshot_and_hub_status_show_the_mark(self):
        from core import views
        self.parallel(3, clean=False)
        self.folder("notes", dirty=4)
        r = views.Reader()
        r.state = self.st
        rows = {s["name"]: s for s in r.snapshot()["sessions"]}
        self.assertEqual(rows["notes"]["minihub_dirty"], i18n.t("core.minihub.dirty", n=4))   # the owner's page
        self.assertIsNone(rows["notes"]["minihub_pending"])
        self.assertIsNone(rows["api-x"]["minihub_dirty"])
        out, _ = self.hub_cli("status")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(i18n.t("core.minihub.dirty", n=4), out.stdout)   # the owner's terminal
        self.assertNotIn("no uncommitted", out.stdout)


class DirtyDaemon(unittest.TestCase):
    """The daemon: uncommitted changes are the number of git status lines, kept fresh for a mini-hub too; a session
    dirty by the old core's state (folder_dirty True) gets a new fact and switches."""

    git, repo = Folders.git, Folders.repo

    def test_old_dirty_state_gets_a_new_fact(self):
        h = Review1Daemon.hub_with(self, self.repo())
        cwd = h.state["sessions"]["spec"]["cwd"]
        with open(os.path.join(cwd, "wip.txt"), "w") as f:
            f.write("1")
        h.state["sessions"]["spec"]["folder_dirty"] = True             # a snapshot from the old core: "dirty, waiting"
        h.check_folders()
        spec = h.state["sessions"]["spec"]
        self.assertTrue(minihub.on(h.state, spec))
        self.assertNotIn("folder_dirty", spec)                         # the old core's mark is cleared; no number —
        # git status is not asked for a session waiting to switch: the number exists for a live mini-hub and at spawn

    def test_a_busy_session_is_not_asked_and_a_minihub_reports_only_changes(self):
        from hosts import worktree
        h = Review1Daemon.hub_with(self, self.repo())
        cwd = h.state["sessions"]["spec"]["cwd"]
        h.record("turn.started", "spec", {"marker": None, "prompt_head": "own"})
        with mock.patch.object(worktree, "folder", wraps=worktree.folder) as folder:
            h.check_folders()                                          # busy: nobody shows its number
        self.assertEqual(folder.call_args.kwargs, {"dirty": False})
        self.assertNotIn("folder_dirty", h.state["sessions"]["spec"])
        h.record("turn.result", "spec", {"text": "ok"})
        h.record("turn.stopped", "spec")
        with mock.patch.object(worktree, "folder", wraps=worktree.folder) as folder:
            h.check_folders()                                          # became free — a mini-hub right away
        self.assertEqual(folder.call_args.kwargs, {"dirty": False})    # the switch does not wait for git status
        spec = h.state["sessions"]["spec"]
        self.assertTrue(minihub.on(h.state, spec))
        self.assertEqual(spec["state"], "parked")                      # parked
        with open(os.path.join(cwd, "wip.txt"), "w") as f:
            f.write("1")
        h.folders_at.clear()
        with mock.patch.object(worktree, "folder", wraps=worktree.folder) as folder:
            h.check_folders()                                          # asleep — no git status
        self.assertEqual(folder.call_args.kwargs, {"dirty": False})
        self.assertNotIn("folder_dirty", h.state["sessions"]["spec"])
        h.record("session.wake_requested", "spec", {"channel": "cli", "owner": True})   # woken
        h.record("session.started", "spec")
        h.folders_at.clear()
        h.check_folders()
        self.assertEqual(h.state["sessions"]["spec"]["folder_dirty"], 1)
        seq = h.state["seq"]
        h.folders_at.clear()
        h.check_folders()                                              # the same number — no fact
        self.assertEqual(h.state["seq"], seq)


class DirtyNote(unittest.TestCase):
    """The addendum to a mini-hub at spawn: uncommitted changes in the folder — a list (up to 10 lines) and the sorting-out
    procedure."""

    git, repo = Folders.git, Folders.repo
    spawn = Host.spawn

    def test_a_dirty_folder_is_listed_with_the_procedure(self):
        cwd = self.repo()
        for i in range(12):
            with open(os.path.join(cwd, f"f{i:02}.txt"), "w") as f:
                f.write("1")
        _, prompt = self.spawn(cwd, minihub=True, uncommitted=self.uncommitted(cwd))
        self.assertIn("Your folder has uncommitted changes", prompt)
        self.assertIn(minihub.STASH.format(name="maps"), prompt)
        self.assertIn("?? f00.txt", prompt)
        self.assertIn("?? f09.txt", prompt)
        self.assertNotIn("f10.txt", prompt)
        self.assertIn("… 2 more", prompt)

    def hub(self, cwd, **sessions):
        h = DaemonSettings.hub(self, {})
        for n, where in {"maps": cwd, **sessions}.items():
            os.makedirs(where, exist_ok=True)
            h.record("session.requested", n, {"cwd": where, "session_id": f"s-{n}", "channel": "cli", "owner": True})
        return h

    def uncommitted(self, cwd, h=None):
        """What the daemon hands the host before spawning a mini-hub (core/daemon.execute -> uncommitted)."""
        got = {}
        h = h or self.hub(cwd)
        h.host.spawn = lambda name, **a: got.update(a) or "spawned"
        h.execute({"do": "spawn", "session": "maps", "session_id": "sid", "cwd": cwd, "minihub": True})
        return got.get("uncommitted", "no field")

    def test_a_folder_shared_with_another_session_names_it(self):
        cwd = self.repo()
        with open(os.path.join(cwd, "wip.txt"), "w") as f:
            f.write("1")
        os.makedirs(os.path.join(cwd, "sub", ".claude", "worktrees", "x"), exist_ok=True)
        h = self.hub(cwd, ops=os.path.join(cwd, "sub"), own=os.path.join(cwd, ".claude", "worktrees", "maps-a"),
                     far=tempfile.mkdtemp())
        got = self.uncommitted(cwd, h)
        self.assertEqual((got["lines"], got["shared"]), (["?? wip.txt"], ["ops"]))   # an executor's copy is not a neighbour
        _, prompt = self.spawn(cwd, minihub=True, uncommitted=got)
        self.assertIn("This folder is also shared by: ops", prompt)
        self.assertIn(minihub.STASH.format(name="maps") + " -- ':/<path>'", prompt)

    def test_clean_or_not_a_minihub_no_line(self):
        cwd = self.repo()
        self.assertIsNone(self.uncommitted(cwd))                       # clean — no line
        _, prompt = self.spawn(cwd, minihub=True, uncommitted=self.uncommitted(cwd))
        self.assertIn("You are a mini-hub", prompt)
        self.assertNotIn("uncommitted changes", prompt)
        with open(os.path.join(cwd, "wip.txt"), "w") as f:
            f.write("1")
        got = self.uncommitted(cwd)
        self.assertEqual(got["lines"], ["?? wip.txt"])
        _, prompt = self.spawn(cwd, uncommitted=got)
        self.assertNotIn("uncommitted changes", prompt)                 # not a mini-hub — no line
        self.assertIsNone(self.uncommitted(tempfile.mkdtemp()))        # not git — nothing
        self.assertNotIn("also shared by", self.spawn(cwd, minihub=True, uncommitted=got)[1])

    def test_a_git_failure_spawns_without_the_line(self):
        import subprocess
        from hosts import worktree
        with mock.patch.object(worktree, "changes", side_effect=subprocess.TimeoutExpired(["git"], 30)):
            self.assertIsNone(self.uncommitted(self.repo()))


class DirtyStash(unittest.TestCase):
    """The procedure from the addendum, on real git: a modified, new, deleted and partly staged file -> stash in the
    mini-hub's folder (next to it, a previous executor's copy) -> a new executor's copy (worktree.prepare) -> stash
    apply -> edit -> commit -> merge --ff-only in the folder succeeds; the content is the original uncommitted changes
    plus the edit; the stash is intact."""

    git = Folders.git

    def out(self, cwd, *args):
        import subprocess
        return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True,
                              env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                                   "GIT_COMMITTER_EMAIL": "t@x", "PATH": "/usr/bin:/bin:/usr/local/bin",
                                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.environ["GIT_CONFIG_GLOBAL"]}).stdout

    def write(self, path, text):
        with open(path, "w") as f:
            f.write(text)

    def read(self, path):
        with open(path) as f:
            return f.read()

    def test_the_procedure_from_the_note_works(self):
        import shlex
        from hosts import worktree
        top = os.path.realpath(tempfile.mkdtemp(prefix="minihub-stash-"))
        self.addCleanup(shutil.rmtree, top, True)
        self.git(top, "init", "-q", "-b", "main")
        for name, text in (("mod.txt", "a\n"), ("del.txt", "b\n"), ("part.txt", "c1\nc2\n")):
            self.write(os.path.join(top, name), text)
        self.git(top, "add", ".")
        self.git(top, "commit", "-q", "-m", "init")
        old, _, err = worktree.prepare(top, "maps-old")   # the previous executor
        self.assertIsNone(err)
        self.write(os.path.join(old, "his.txt"), "his\n")
        self.write(os.path.join(top, "mod.txt"), "a2\n")
        os.remove(os.path.join(top, "del.txt"))
        self.write(os.path.join(top, "part.txt"), "c1x\nc2\n")
        self.git(top, "add", "part.txt")
        self.write(os.path.join(top, "part.txt"), "c1x\nc2y\n")       # partly staged, partly not
        self.write(os.path.join(top, "new.txt"), "new\n")
        self.assertEqual(worktree.folder(top, dirty=True), ("ok", 4))
        want = {n: self.read(os.path.join(top, n)) for n in ("mod.txt", "part.txt", "new.txt")}
        # in the mini-hub's folder — exactly the command from the addendum
        self.git(top, *shlex.split(minihub.STASH.format(name="maps"))[1:])
        self.assertEqual(worktree.folder(top, dirty=True), ("ok", 0))  # the folder is clean
        self.assertEqual(self.read(os.path.join(old, "his.txt")), "his\n")   # the previous executor's copy is untouched
        (sha,) = [line.split()[0] for line in self.out(top, "stash", "list", "--format=%H %gs").splitlines()
                  if "maps: uncommitted before the mini-hub" in line]
        cwd, _, err = worktree.prepare(top, "maps-a")
        self.assertIsNone(err)
        self.git(cwd, "stash", "apply", sha)
        self.assertFalse(os.path.exists(os.path.join(cwd, "del.txt")))
        for n, text in want.items():
            self.assertEqual(self.read(os.path.join(cwd, n)), text, n)
        with open(os.path.join(cwd, "part.txt"), "a") as f:
            f.write("z\n")
        self.git(cwd, "add", "-A")
        self.git(cwd, "commit", "-q", "-m", "finished")
        self.git(top, "merge", "--ff-only", "task/maps-a")
        self.assertFalse(os.path.exists(os.path.join(top, "del.txt")))
        self.assertEqual(self.read(os.path.join(top, "part.txt")), "c1x\nc2y\nz\n")
        for n in ("mod.txt", "new.txt"):
            self.assertEqual(self.read(os.path.join(top, n)), want[n], n)
        self.assertEqual(worktree.folder(top, dirty=True), ("ok", 0))
        self.assertIn(sha, self.out(top, "stash", "list", "--format=%H"))   # the stash is still there

    def test_shared_folder_stashes_only_own_paths_from_a_subfolder(self):
        import shlex
        from hosts import worktree
        top = os.path.realpath(tempfile.mkdtemp(prefix="minihub-stash-"))
        self.addCleanup(shutil.rmtree, top, True)
        self.git(top, "init", "-q", "-b", "main")
        os.makedirs(os.path.join(top, "svc"))
        self.write(os.path.join(top, "svc", "a.py"), "a\n")
        self.write(os.path.join(top, "theirs.txt"), "b\n")
        self.git(top, "add", ".")
        self.git(top, "commit", "-q", "-m", "init")
        self.write(os.path.join(top, "svc", "a.py"), "a2\n")
        self.write(os.path.join(top, "svc", "n.py"), "n\n")
        self.write(os.path.join(top, "theirs.txt"), "b2\n")          # a folder neighbour's unfinished work
        _, lines = worktree.changes(os.path.join(top, "svc"))
        mine = [line[3:] for line in lines if line[3:].startswith("svc/")]   # paths from the root, as in the list
        # from the mini-hub's folder (a repo subfolder) — the command from the addendum with ':/...' paths
        self.git(os.path.join(top, "svc"), *shlex.split(minihub.STASH.format(name="maps"))[1:], "--",
                 *(f":/{p}" for p in mine))
        self.assertEqual(worktree.changes(top)[1], [" M theirs.txt"])   # the neighbour's changes stayed in place


class DirtyReview1(unittest.TestCase):
    """High-severity review of the uncommitted-changes branch, round 1: the core's git status does not take index.lock;
    paths in the list are as is (without git's C-quoting) and from the repo root, and are named so; backticks in the
    data — a longer fence, the data is not rewritten; a mini-hub spawn always runs one git status with a short
    timeout, periodically — only for live mini-hubs; a folder neighbour is the same folder however its path is written."""

    git, repo = Folders.git, Folders.repo
    out = DirtyStash.out
    uncommitted, hub = DirtyNote.uncommitted, DirtyNote.hub

    def write(self, path, text="1"):
        with open(path, "w") as f:
            f.write(text)

    def index(self, cwd):
        with open(os.path.join(cwd, ".git", "index"), "rb") as f:
            return f.read()

    def test_status_of_the_core_does_not_rewrite_the_index(self):
        # index.lock while the index is rewritten — a stash, merge or commit alongside would fail with "index.lock exists"
        from hosts import worktree
        cwd = self.repo()
        self.write(os.path.join(cwd, "a.txt"))
        self.git(cwd, "add", "a.txt")
        self.git(cwd, "commit", "-q", "-m", "a")
        self.write(os.path.join(cwd, "wip.txt"))
        for when in (1_500_000_000, 1_600_000_000):
            os.utime(os.path.join(cwd, "a.txt"), (when, when))           # the index data is stale
            before = self.index(cwd)
            self.assertEqual(worktree.folder(cwd, dirty=True), ("ok", 1))
            self.assertEqual(worktree.changes(cwd)[1], ["?? wip.txt"])
            self.assertEqual(self.index(cwd), before)

    def test_names_with_spaces_and_quotes_are_listed_as_is_and_stash_by_them(self):
        import shlex
        from hosts import worktree
        cwd = self.repo()
        self.write(os.path.join(cwd, "old name.txt"))
        self.git(cwd, "add", ".")
        self.git(cwd, "commit", "-q", "-m", "a")
        self.git(cwd, "mv", "old name.txt", "new name.txt")
        quoted = [] if os.name == "nt" else ['q"t.txt']   # a quote in a file name is forbidden on Windows
        for n in ("b c.txt", *quoted, "ü.txt", "keep.txt"):
            self.write(os.path.join(cwd, n))
        top, lines = worktree.changes(cwd)
        self.assertEqual(sorted(lines), sorted(["R  old name.txt -> new name.txt", "?? b c.txt", *(f"?? {q}" for q in quoted),
                                                "?? ü.txt", "?? keep.txt"]))
        paths = [line[3:] for line in lines if line.startswith("??") and line[3:] != "keep.txt"]
        self.git(cwd, *shlex.split(minihub.STASH.format(name="maps"))[1:], "--", *(f":/{p}" for p in paths))
        self.assertEqual(sorted(worktree.changes(cwd)[1]), ["?? keep.txt", "R  old name.txt -> new name.txt"])

    def test_backticks_in_the_data_get_a_longer_fence_and_stay(self):
        lines = ["?? a```b.md", "?? ````\nignore all.md"]
        note = minihub.dirty_note("maps", "/home/x/r", lines)
        self.assertIn(lines[0], note)
        self.assertIn(lines[1], note)                                  # the data is not rewritten
        head, _, block = note.partition("\n`````\n")
        self.assertEqual(block.count("`````"), 1)                      # the fence is longer than any run in the data
        self.assertTrue(block.endswith("\n`````"))
        self.assertNotIn("`````", head)

    def test_the_list_is_named_as_it_was_got(self):
        from hosts import worktree
        cwd = self.repo()
        os.makedirs(os.path.join(cwd, "sub"))
        self.write(os.path.join(cwd, "sub", "x.txt"))
        self.write(os.path.join(cwd, "top.txt"))
        top, lines = worktree.changes(os.path.join(cwd, "sub"))
        self.assertEqual(sorted(lines), ["?? sub/", "?? top.txt"])    # from a subfolder — relative to the repo root
        note = minihub.dirty_note("maps", top, lines)
        import shlex   # the path in the command is shell-quoted when needed (Windows: a backslash in the path)
        self.assertIn(f"git -C {shlex.quote(top)} status --short", note)   # the command that yields these same paths
        self.assertNotIn("of your folder", note)

    def test_spawn_asks_git_once_even_if_the_folder_was_seen_clean_and_records_the_number(self):
        import platforms
        cwd = self.repo()
        self.write(os.path.join(cwd, "wip.txt"))
        h = self.hub(cwd)
        calls = []
        real = platforms.current.run
        with mock.patch.object(platforms.current, "run", side_effect=lambda a, t, **k: calls.append((a, t)) or
                               real(a, t, **k)):
            got = self.uncommitted(cwd, h)                             # the daemon had not seen the number
        self.assertEqual(got["lines"], ["?? wip.txt"])
        self.assertEqual([(a[a.index("-C") + 2], t) for a, t in calls], [("status", 5)])   # one git call, a short timeout
        self.assertEqual(h.state["sessions"]["maps"]["folder_dirty"], 1)    # the mark is fresh since the spawn
        seq = h.state["seq"]
        self.uncommitted(cwd, h)
        self.assertEqual(h.state["seq"], seq + 1)                      # the same number — no fact (+1 is host.done)

    def test_spawn_with_git_silent_goes_without_the_line_and_says_why(self):
        import subprocess
        import platforms
        cwd = self.repo()
        self.write(os.path.join(cwd, "wip.txt"))
        h = self.hub(cwd)
        with mock.patch.object(platforms.current, "run", side_effect=subprocess.TimeoutExpired(["git", "-C", cwd,
                                                                                                  "status"], 5)):
            self.assertIsNone(self.uncommitted(cwd, h))                # the spawn goes without the line
        self.assertEqual(h.state["sessions"]["maps"]["folder_error"], "git status: did not answer in time")

    def test_spawn_while_stopping_asks_no_git(self):
        import platforms
        cwd = self.repo()
        self.write(os.path.join(cwd, "wip.txt"))
        h = self.hub(cwd)
        h.stopping.append(True)
        with mock.patch.object(platforms.current, "run", side_effect=AssertionError("git on stop")):
            self.assertIsNone(self.uncommitted(cwd, h))

    def test_a_neighbour_is_the_same_folder_however_its_path_is_written(self):
        # a case-insensitive FS (macOS, Windows): a path in another case is the same folder, and realpath does not
        # fix it; here — a link, with a realpath that does not fix paths
        cwd = os.path.realpath(self.repo())
        self.write(os.path.join(cwd, "wip.txt"))
        os.makedirs(os.path.join(cwd, "sub"))
        alias = os.path.join(tempfile.mkdtemp(), "alias")
        import platforms
        platforms.current.link_dir(cwd, alias)   # Windows without the symlink right — a junction
        h = self.hub(cwd, ops=os.path.join(alias, "sub"), own=os.path.join(alias, ".claude", "worktrees", "maps-a"))
        with mock.patch("os.path.realpath", os.path.abspath):
            got = self.uncommitted(cwd, h)
        self.assertEqual(got["shared"], ["ops"])


class Born(Delegating):
    """A new direction raised at once with a task in a core working copy (owner decision 2026-10-20: "from the first
    minute") is a mini-hub from the first spawn: the copy is git with a commit (the commits field, a daemon fact),
    there is no task to wait for. The backlog may arrive after the spawn too — before the first turn. Working ones
    switch after the current task, as before."""

    COPY = {"path": "/repo/.claude/worktrees/maps", "branch": "task/maps", "repo": "/repo", "commits": True}

    def born(self, worktree=COPY, backlog="before", n=3):
        """The hub creates the direction maps and raises it with a task (hub direction new, hub task run) — as the daemon
        does (core/executors.events_for). backlog: before — the backlog before the request; after — after the spawn,
        before the process starts; started — after the start (the task is already typed). -> [the mode of each maps
        spawn], its task."""
        if n is not None:
            self.minihub(n)
        if backlog == "before":
            self.assertIsNone(self.ev("task.backlog", None, slug="maps", session="maps", **by("hub")))
        self.assertIsNone(self.run_task("maps", text="look into the epic", who="hub"))
        self.actions.clear()
        run = self.st["runs"]["maps"]
        for type, entity, data in executors.events_for(self.st, "maps", run, cwd=(worktree or {}).get("path") or "/maps",
                                                       worktree=worktree):
            self.assertIsNone(self.ev(type, entity, **data), (type, data))
        if backlog == "after":
            self.assertIsNone(self.ev("task.backlog", None, slug="maps", session="maps", **by("hub")))
        self.ev("session.started", "maps")
        if backlog == "started":
            self.assertIsNone(self.ev("task.backlog", None, slug="maps", session="maps", **by("hub")))
        spawns = [a.get("minihub") for a in self.actions if a.get("do") == "spawn" and a["session"] == "maps"]
        if len(spawns) > 1:
            self.ev("session.started", "maps")                         # respawned before the task
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "maps"]
        return spawns, d

    def typed(self, d):
        """What the core typed into the maps process — the task text."""
        (text,) = [a["text"] for a in self.actions if a.get("do") == "type" and a.get("message") == d["message"]]
        return text

    def test_a_new_direction_in_a_core_copy_starts_as_a_minihub(self):
        spawns, d = self.born()
        self.assertEqual(spawns, [True])                               # the edit ban and the addendum apply from the first process
        maps = self.st["sessions"]["maps"]
        self.assertTrue(minihub.on(self.st, maps))
        self.assertIsNone(minihub.pending(self.st, maps))
        self.assertIn("you are a mini-hub from the first minute", self.typed(d))     # the task does not call it an executor
        self.assertNotIn("You are the executor of this task", self.typed(d))
        self.assertIn("you are a mini-hub from the first minute", d["context"])
        self.actions.clear()
        self.folder("maps")                                           # the daemon's folder check — no restart
        self.assertEqual([a for a in self.actions if a.get("do") == "spawn"], [])
        self.ev("turn.started", "maps", marker=d["message"])
        self.assertIsNone(self.run_task("maps-a", who="maps"))
        self.ev("turn.result", "maps", text="delegated maps-a")
        self.ev("turn.stopped", "maps")
        self.assertEqual(self.st["delegations"][d["id"]]["state"], "working")   # with the executor, not replied
        self.assertTrue(self.st["runs"]["maps-a"].get("minihub"))
        self.assertEqual([a for a in self.actions if a.get("do") == "spawn" and a["session"] == "maps"], [])

    def test_a_backlog_after_the_spawn_but_before_the_task_respawns_it_before_the_task(self):
        # as happened on 2026-10-20: the hub raised an executor with a task and created the direction afterwards — the process was already started without the rules
        spawns, d = self.born(backlog="after")
        self.assertEqual(spawns, [None, True])                         # respawned before the task, already as a mini-hub
        self.assertTrue(minihub.on(self.st, self.st["sessions"]["maps"]))
        self.assertIn("you are a mini-hub from the first minute", self.typed(d))     # the task was typed into the second process
        released = [i for i, a in enumerate(self.actions) if a.get("do") == "release" and a["session"] == "maps"]
        typed = [i for i, a in enumerate(self.actions) if a.get("do") == "type" and a.get("message") == d["message"]]
        self.assertTrue(released and typed and released[-1] < typed[0])

    def test_a_backlog_after_the_task_was_typed_waits_for_the_task(self):
        spawns, d = self.born(backlog="started")                       # the task is already with the process — a working one
        self.assertEqual(spawns, [None])
        maps = self.st["sessions"]["maps"]
        self.assertFalse(minihub.on(self.st, maps))
        self.assertIn("You are the executor of this task", self.typed(d))
        self.folder("maps")                                           # the task is in progress — the switch comes after it
        self.assertFalse(minihub.on(self.st, self.st["sessions"]["maps"]))

    def test_not_a_fresh_core_copy_starts_as_before(self):
        for copy in (None, {**self.COPY, "commits": False}, {**self.COPY, "keep_branch": True},
                     {k: v for k, v in self.COPY.items() if k != "commits"}):   # a journal from before the change: no field
            with self.subTest(copy=copy):
                self.setUp()
                spawns, d = self.born(worktree=copy)
                self.assertEqual(spawns, [None])
                self.assertEqual(minihub.pending(self.st, self.st["sessions"]["maps"]),
                                 PENDING_TASK)
                self.assertIn("You are the executor of this task", self.typed(d))

    def test_zero_or_before_the_setting_starts_as_before(self):
        for n in (0, None):
            with self.subTest(n=n):
                self.setUp()
                spawns, d = self.born(n=n)
                self.assertEqual(spawns, [None])
                self.assertFalse(minihub.on(self.st, self.st["sessions"]["maps"]))
                self.assertIn("You are the executor of this task", self.typed(d))

    def test_a_task_that_went_to_the_process_once_waits_for_it(self):
        self.minihub()
        self.assertIsNone(self.run_task("maps", text="look into the epic", who="hub"))
        run = self.st["runs"]["maps"]
        for type, entity, data in executors.events_for(self.st, "maps", run, cwd=self.COPY["path"], worktree=self.COPY):
            self.ev(type, entity, **data)
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "maps"]
        self.st["messages"][d["message"]]["attempt"] = 1               # it was typed — the attempt went back to the queue
        self.assertIsNone(self.ev("task.backlog", None, slug="maps", session="maps", **by("hub")))
        self.assertFalse(minihub.on(self.st, self.st["sessions"]["maps"]))
        self.assertIn("You are the executor of this task", self.st["delegations"][d["id"]]["context"])

    def test_the_eval_case_carries_the_rules_as_they_are(self):
        import json
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "evals"))
        from common import find_case
        path = find_case("born-minihub")
        if not path:   # a Russian owner's case: evals/locale/ru/ is not in a published copy
            self.skipTest("evals/locale/ru/ is not in this copy")
        with open(path, encoding="utf-8") as f:
            case = json.load(f)
        for turn in case["turns"]:   # each version's own rule goes in (⟦RULE⟧ + consts): an edit of the rule needs no rebuild
            self.assertEqual(turn["consts"]["RULE"], {"file": "core/executors.py", "name": "BORN_RULE",
                                                      "format": {"raiser": "hub"}})
            for text in turn["input"].values():
                self.assertIn("⟦RULE⟧", text)
            self.assertIn("{raiser}", executors.BORN_RULE)

    def test_a_plain_executor_is_not_a_minihub(self):
        self.minihub()
        self.assertIsNone(self.run_task("notes-a"))
        run = self.st["runs"]["notes-a"]
        self.actions.clear()
        for type, entity, data in executors.events_for(self.st, "notes-a", run, cwd="/repo/.claude/worktrees/notes-a",
                                                       worktree={**self.COPY, "path": "/repo/.claude/worktrees/notes-a"}):
            self.ev(type, entity, **data)
        (spawn,) = [a for a in self.actions if a.get("do") == "spawn"]
        self.assertFalse(spawn.get("minihub"))
        self.assertNotIn("minihub_since", self.st["sessions"]["notes-a"])


class BornDaemon(unittest.TestCase):
    """The daemon: a core copy for a new direction (git) — the first process already runs in mini-hub mode."""

    git, repo = Folders.git, Folders.repo

    def raise_direction(self, repo):
        """The hub creates the direction maps and raises it with a task in repo -> (hub, what the first maps process was started with)."""
        h = DaemonSettings.hub(self, {})
        spawned = {}
        h.host.spawn = lambda name, **a: spawned.setdefault(name, a) and "spawned"
        hub = {"channel": "session", "owner": False, "sender": "hub"}
        h.record("session.requested", "hub", {"cwd": tempfile.mkdtemp(), "session_id": "s-hub", "kind": "hub",
                                              "channel": "cli", "owner": True})
        h.record("session.started", "hub")
        h.record("task.backlog", None, {"slug": "maps", "session": "maps", **hub})
        h.record("task.run_requested", "maps", {"task": "look into the epic", "repo": repo, **hub})
        h.run_tasks()
        return h, spawned.get("maps")

    def test_a_repo_without_commits_starts_as_before(self):
        h, spawn = self.raise_direction(self.repo(commit=False))
        if spawn is None:   # this machine's git makes no copy in an empty repo — no spawn, and the rule is not needed
            self.assertNotIn("maps", h.state["sessions"])
            return
        self.assertIs(h.state["sessions"]["maps"]["worktree"]["commits"], False)
        self.assertFalse(spawn.get("minihub"))

    def test_the_first_process_of_a_new_direction_is_a_minihub(self):
        h, spawn = self.raise_direction(self.repo())
        spawned = {"maps": spawn}
        self.assertTrue(spawned["maps"].get("minihub"))                # the edit ban applies from the first process
        self.assertIsNone(spawned["maps"].get("uncommitted"))          # the copy is fresh — no uncommitted changes
        maps = h.state["sessions"]["maps"]
        self.assertTrue(minihub.on(h.state, maps))
        h.record("session.started", "maps")
        h.check_folders()                                              # the daemon checked the folder — the mode is the same
        self.assertTrue(minihub.on(h.state, h.state["sessions"]["maps"]))
        self.assertNotIn("respawn_minihub", h.state["sessions"]["maps"])


class AutoSoloWords(unittest.TestCase):
    """The state keeps the reason of the automatic "solo" as text (solo_auto); earlier cores wrote it in Russian: it is
    the same reason, and the refusal says it in English."""
    def test_an_earlier_reason_reads_as_the_same_code(self):
        from core import coretext, langdata
        st = {"owner": {"parallel": 3}, "sessions": {}}
        for code, old in langdata.legacy("solo_auto").items():
            with self.subTest(code=code):
                self.assertEqual(minihub.auto_code(old), code)
                self.assertEqual(minihub.auto_code(minihub.AUTO[code]), code)
                sess = {"name": "notes", "role": "agent", "solo_auto": old}
                why = minihub.alone(st, sess)
                self.assertIn(f"({minihub.AUTO[code]})", why)
                self.assertEqual("--allow-empty" in why, code == "empty")   # the first-commit hint as before
                self.assertIn(i18n.tr("ru", f"core.minihub.auto_{code}"), coretext.human(why, "ru"))
        self.assertIsNone(minihub.auto_code("something else"))


class Counted(dict):
    """A state collection that counts the records handed out by values(): how many times a caller walks it."""
    walked = 0

    def values(self):
        for v in super().values():
            Counted.walked += 1
            yield v


class PendingOnePass(Delegating):
    """delegations.pending runs on every tick of the core and asks waiting_on about every task: the children of tasks
    are indexed once per call, not by a walk over all sessions and delegations per task."""

    def many(self, n):
        self.minihub()
        for i in range(n):
            name = f"spec-{i}"
            self.ev("session.requested", name, cwd=f"/{name}", session_id=f"s-{name}", role="agent", about=name,
                    **OWNER)
            self.ev("session.started", name)
            self.ev("delegation.created", None, session=name, for_session="docs", context="T", **by("docs"))
            self.turn(name, f"m{self.seq}", "done")
        did, mid = self.task()
        self.turn("notes", mid, "delegated to two", runs=["notes-a", "notes-b"])
        self.start("notes-a")
        self.start("notes-b")
        return did

    def walks(self):
        st = dict(self.st, sessions=Counted(self.st["sessions"]), delegations=Counted(self.st["delegations"]))
        Counted.walked = 0
        out = delegations.pending(st, now=10 ** 6)
        return Counted.walked, out

    def test_pending_walks_the_state_a_fixed_number_of_times(self):
        did = self.many(12)
        records = len(self.st["sessions"]) + len(self.st["delegations"])
        walked, out = self.walks()
        self.assertLessEqual(walked, 3 * records)   # the old code: a walk over both per task, ~12 here
        with mock.patch.object(minihub, "children", return_value=None):
            self.assertEqual(delegations.pending(self.st, now=10 ** 6), out)   # the full walk gives the same deadlines
        self.assertEqual(minihub.waiting_on(self.st, self.st["delegations"][did], minihub.children(self.st)),
                         ["notes-a", "notes-b"])

    def test_the_index_gives_waiting_on_the_same_answer_for_every_task(self):
        self.many(3)
        kids = minihub.children(self.st)
        for d in self.st["delegations"].values():
            with self.subTest(d["id"]):
                self.assertEqual(minihub.waiting_on(self.st, d, kids), minihub.waiting_on(self.st, d))

    def test_the_index_holds_every_kind_of_child(self):
        # a queued request, a session brought up before its task arrived, a subtask in work: each kind of child is
        # found through the index as by the full walk
        self.minihub()
        did, mid = self.task()
        self.turn("notes", mid, "delegated to two", runs=["notes-a", "notes-b"])
        d = self.st["delegations"][did]

        def same(expected):
            self.assertEqual(minihub.waiting_on(self.st, d), expected)
            self.assertEqual(minihub.waiting_on(self.st, d, minihub.children(self.st)), expected)

        same(["notes-a", "notes-b"])                       # both requests in the queue
        run = self.st["runs"]["notes-a"]
        (req, _task) = executors.events_for(self.st, "notes-a", run, cwd="/x", worktree=None)
        self.assertIsNone(self.ev(req[0], req[1], **req[2]))
        self.assertEqual(self.st["sessions"]["notes-a"]["state"], "starting")
        self.assertNotIn("notes-a", self.st["runs"])
        same(["notes-a", "notes-b"])                       # one starting without its task, one queued
        self.start("notes-b")
        self.assertNotIn("notes-b", self.st["runs"])
        same(["notes-a", "notes-b"])                       # one starting, one with a subtask in work


if __name__ == "__main__":
    unittest.main()
