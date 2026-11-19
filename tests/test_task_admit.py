"""The task.admit point (Module API 2.10, the task.admit right): before the daemon raises an executor it asks the modules
that declared the point; {hold: reason} keeps the request in the queue (the reason is its `why`), {} lets it through,
a module that is silent, crashed, answered wrongly or is not ready never holds. The daemon on a real git with a fake
module."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import subprocess
import tempfile
import types
import unittest

from core import coretext, executors, journal, minihub, modrun, modules, version
from core.daemon import Hub

OWNER = {"channel": "cli", "owner": True}


def git(cwd, *a):
    return subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True,
                          capture_output=True, text=True).stdout.strip()


class Mods:
    """Modules without processes: answer(name, point, payload) -> the raw answer (None: silent; an exception: crashed),
    checked by modrun.POINTS as in Modules.call (a wrong answer is a failure: None)."""

    def __init__(self, answer):
        self.answer, self.up, self.calls = answer, {}, []

    def answering(self, point):
        return [n for n, points in self.up.items() if point in points]

    def call(self, name, point, payload, ctx=None):
        self.calls.append((name, point, payload))
        try:
            raw = self.answer(name, point, payload)
        except Exception:  # noqa: BLE001 — a crash of the module: no answer
            return None
        if raw is None:
            return None
        try:
            return modrun.POINTS[point]["check"](raw, {"name": name})
        except modrun.BadResult:
            return None


class Point(unittest.TestCase):
    def test_the_point_is_in_the_tables(self):
        self.assertIn("task.admit", version.MODULE_POINTS["2.10"])
        self.assertIn("task.admit", modrun.callable_points())
        self.assertEqual(modrun.POINTS["task.admit"]["cap"], "task.admit")
        self.assertEqual(modrun.POINTS["task.admit"]["budget"], 2.0)
        self.assertIn("task.admit", modules.CAPABILITIES)
        self.assertEqual(version.manifest_since("task.admit"), "2.10")

    def test_it_is_not_optional(self):
        # a module with a lower api floor may not declare it: only the codex points are optional
        self.assertNotIn("task.admit", version.OPTIONAL_POINTS)
        self.assertNotIn("task.admit", modrun.offered("^2.9"))
        self.assertIn("task.admit", modrun.offered("^2.10"))

    def test_hello_needs_the_right(self):
        hello = {"name": "gate", "version": "1.0.0", "points": ["task.admit"]}
        ctx = {"name": "gate", "version": "1.0.0", "capabilities": ["task.admit"], "api": "^2.10"}
        self.assertEqual(modrun.POINTS["hello"]["check"](hello, ctx)["points"], ["task.admit"])
        with self.assertRaises(modrun.BadResult):
            modrun.POINTS["hello"]["check"](hello, {**ctx, "capabilities": []})
        with self.assertRaises(modrun.BadResult):
            modrun.POINTS["hello"]["check"](hello, {**ctx, "api": "^2.9"})

    def test_answer_schema(self):
        check = modrun.POINTS["task.admit"]["check"]
        self.assertEqual(check({}, {}), {})
        self.assertEqual(check({"hold": "CI is red"}, {}), {"hold": "CI is red"})
        self.assertEqual(len(check({"hold": "x" * 900}, {})["hold"]), 200)
        for bad in ({"hold": ""}, {"hold": "  "}, {"hold": 5}, {"hold": None}, {"hold": True}, {"why": "x"},
                    {"hold": "x", "extra": 1}, [], "hold", None):
            with self.assertRaises(modrun.BadResult, msg=repr(bad)):
                check(bad, {})


class Admit(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="admit-"))
        self.repo = os.path.join(self.tmp, "repo")
        git(self.tmp, "init", "-q", "-b", "main", self.repo)
        with open(os.path.join(self.repo, "a.txt"), "w") as f:
            f.write("a\n")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-q", "-m", "init")
        self.h = Hub(os.path.join(self.tmp, "state"), types.SimpleNamespace())
        self.h.execute = lambda a: None
        rec = self.h.record
        rec("session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **OWNER})
        rec("owner.settings", None, {"parallel": 3, "channel": "core"})
        rec("session.requested", "maps", {"cwd": self.repo, "session_id": "s-m", "role": "agent", **OWNER})
        rec("session.started", "maps", {})
        rec("session.folder", "maps", {"channel": "core", "git": "ok", "dirty": 0}, src="core")
        self.say = lambda name, point, payload: {}
        self.mods = Mods(lambda name, point, payload: self.say(name, point, payload))
        self.mods.up["gate"] = ["task.admit"]
        self.h.modules.mods = self.mods
        self.h.ADMIT_EVERY = 0   # a held request is asked on every pass (the throttle has its own test)

    def raise_(self, name, **extra):
        self.h.record("task.run_requested", name, {"task": "do it", "channel": "session", "owner": False,
                                                   "sender": "maps", **extra})
        self.assertIn(name, self.h.state.get("runs") or {}, self.h.state)
        self.h.run_tasks()
        return self.h.state["sessions"].get(name)

    def rec(self, name):
        return (self.h.state.get("runs") or {}).get(name)

    def why(self, name):
        return coretext.human(coretext.got(self.rec(name), "why"))

    # {} lets through, silence and failures never hold
    def test_empty_answer_lets_the_executor_up(self):
        self.assertIsNotNone(self.raise_("maps-a"))
        self.assertEqual([c[:2] for c in self.mods.calls], [("gate", "task.admit")])

    def test_no_module_with_the_point_asks_nobody(self):
        self.mods.up.clear()
        self.assertIsNotNone(self.raise_("maps-a"))
        self.assertEqual(self.mods.calls, [])

    def test_a_module_that_crashes_does_not_hold(self):
        def boom(name, point, payload):
            raise RuntimeError("down")
        self.say = boom
        self.assertIsNotNone(self.raise_("maps-a"))

    def test_a_silent_module_does_not_hold(self):
        self.say = lambda name, point, payload: None   # timed out or not ready: Modules.call gives None
        self.assertIsNotNone(self.raise_("maps-a"))

    def test_a_wrong_answer_does_not_hold(self):
        self.say = lambda name, point, payload: {"hold": ""}
        self.assertIsNotNone(self.raise_("maps-a"))
        self.say = lambda name, point, payload: "no"
        self.assertIsNotNone(self.raise_("maps-b"))

    def test_a_call_that_raises_inside_the_core_pass_does_not_hold(self):
        class Broken(Mods):
            def answering(self, point):
                raise RuntimeError("records unreadable")
        self.h.modules.mods = Broken(lambda *a: {"hold": "x"})
        self.assertIsNotNone(self.raise_("maps-a"))

    # hold keeps the request, with the reason, until the module lets it go
    def test_hold_keeps_the_request_in_the_queue_with_the_reason(self):
        self.say = lambda name, point, payload: {"hold": "the deploy window is closed"}
        self.assertIsNone(self.raise_("maps-a"))
        run = self.rec("maps-a")
        self.assertEqual(run["state"], "queued")
        self.assertEqual((run["held"]["module"], run["held"]["reason"]), ("gate", "the deploy window is closed"))
        self.assertIn("the deploy window is closed", self.why("maps-a"))
        self.assertIn("gate", self.why("maps-a"))
        self.assertEqual(executors.promotable(self.h.state), [])   # the queue does not raise a held request by itself
        self.assertNotIn("maps-a", self.h.preparing)

    def test_the_module_is_asked_again_on_the_next_pass_and_release_raises_it(self):
        self.say = lambda name, point, payload: {"hold": "not now"}
        self.raise_("maps-a")
        self.h.run_tasks()
        self.assertEqual(len(self.mods.calls), 2)   # a held request is asked on every pass
        self.assertIsNone(self.h.state["sessions"].get("maps-a"))
        self.say = lambda name, point, payload: {}
        self.h.run_tasks()
        self.assertIsNotNone(self.h.state["sessions"].get("maps-a"))
        self.assertNotIn("maps-a", self.h.state.get("runs") or {})

    def test_a_module_that_stops_answering_releases_the_request(self):
        self.say = lambda name, point, payload: {"hold": "not now"}
        self.raise_("maps-a")
        self.mods.up.clear()   # suspended or switched off
        self.h.run_tasks()
        self.assertIsNotNone(self.h.state["sessions"].get("maps-a"))

    def test_a_module_that_starts_failing_releases_the_request(self):
        self.say = lambda name, point, payload: {"hold": "not now"}
        self.raise_("maps-a")
        self.say = lambda name, point, payload: None
        self.h.run_tasks()
        self.assertIsNotNone(self.h.state["sessions"].get("maps-a"))

    def test_the_reason_follows_the_module(self):
        self.say = lambda name, point, payload: {"hold": "first"}
        self.raise_("maps-a")
        self.say = lambda name, point, payload: {"hold": "second"}
        self.h.run_tasks()
        self.assertEqual(self.rec("maps-a")["held"]["reason"], "second")
        self.assertIn("second", self.why("maps-a"))
        self.assertNotIn("first", self.why("maps-a"))

    def test_an_unchanged_hold_writes_nothing_more_to_the_journal(self):
        self.say = lambda name, point, payload: {"hold": "not now"}
        self.raise_("maps-a")
        before = len(journal.read(self.h.journal.path))
        for _ in range(3):
            self.h.run_tasks()
        self.assertEqual(len(journal.read(self.h.journal.path)), before)
        self.assertEqual(len([e for e in journal.read(self.h.journal.path) if e["type"] == "task.run_held"]), 1)

    def test_a_long_reason_is_held_once(self):
        self.say = lambda name, point, payload: {"hold": "wait   a bit " * 80}
        self.raise_("maps-a")
        for _ in range(3):
            self.h.run_tasks()
        self.assertEqual(len([e for e in journal.read(self.h.journal.path) if e["type"] == "task.run_held"]), 1)
        self.assertEqual(len(self.rec("maps-a")["held"]["reason"]), 200)

    def test_a_place_freed_by_a_hold_is_not_used_by_a_request_nobody_asked(self):
        self.h.record("owner.settings", None, {"parallel": 1, "channel": "core"})
        self.say = lambda name, point, payload: {"hold": "no"} if payload["run"]["name"] == "maps-a" else {}
        for name in ("maps-a", "maps-d"):
            self.h.record("task.run_requested", name, {"task": "do it", "channel": "session", "owner": False,
                                                       "sender": "maps"})
        self.assertEqual(self.rec("maps-d")["state"], "queued")   # waits for the place maps-a holds
        self.h.run_tasks()
        self.assertEqual(self.rec("maps-a")["state"], "queued")
        self.assertIsNone(self.h.state["sessions"].get("maps-d"))   # the place freed by the hold: not raised in this tick
        self.h.run_tasks()
        self.assertIsNotNone(self.h.state["sessions"].get("maps-d"))
        self.assertIn("maps-d", [c[2]["run"]["name"] for c in self.mods.calls])   # and it was asked before it started

    def test_a_pass_that_runs_out_of_time_raises_nobody_unasked(self):
        self.h.ADMIT_PASS = -1
        self.assertIsNone(self.raise_("maps-a"))
        self.assertEqual(self.mods.calls, [])
        self.assertEqual(self.rec("maps-a")["state"], "ready")
        del self.h.ADMIT_PASS
        self.h.run_tasks()
        self.assertIsNotNone(self.h.state["sessions"].get("maps-a"))

    def test_a_held_request_is_asked_again_no_more_often_than_every_ADMIT_EVERY(self):
        self.h.ADMIT_EVERY = 2.0
        self.say = lambda name, point, payload: {"hold": "not now"}
        self.raise_("maps-a")
        self.h.run_tasks()
        self.h.run_tasks()
        self.assertEqual(len(self.mods.calls), 1)
        self.h.admit_next.clear()   # two seconds on
        self.h.run_tasks()
        self.assertEqual(len(self.mods.calls), 2)

    def test_a_miss_is_asked_again_but_an_answer_is_not(self):
        self.h.state.setdefault("runs", {})["maps-z"] = {"by": "maps", "task": "x", "ts": 1, "state": "ready", "why": None}
        self.h.preparing.clear()
        self.say = lambda name, point, payload: None
        self.h.admit_pass(held=False)
        self.h.admit_pass(held=False)
        self.assertEqual(len(self.mods.calls), 2)   # the module did not answer: asked again on the next pass
        self.say = lambda name, point, payload: {}
        self.h.admit_pass(held=False)
        self.h.admit_pass(held=False)
        self.assertEqual(len(self.mods.calls), 3)   # it let the request through: that is final

    def test_a_request_let_through_is_not_asked_twice(self):
        self.raise_("maps-a")
        self.assertEqual(len(self.mods.calls), 1)
        self.h.state.setdefault("runs", {})["maps-b"] = {"by": "maps", "task": "x", "ts": 1, "state": "ready", "why": None}
        self.h.admitted.add("maps-b")
        self.h.admit_pass(held=False)
        self.assertEqual(len(self.mods.calls), 1)

    def test_the_request_is_described_to_the_module(self):
        seen = []
        self.say = lambda name, point, payload: seen.append(payload) or {}
        self.raise_("maps-a", repo=self.repo)
        (run,) = [p["run"] for p in seen]
        self.assertEqual((run["name"], run["by"], run["task"]), ("maps-a", "maps", "do it"))
        self.assertEqual(run["repo"], self.repo)

    def test_every_ready_request_is_asked_and_judged_alone(self):
        self.say = lambda name, point, payload: {"hold": "no"} if payload["run"]["name"] == "maps-a" else {}
        self.assertIsNone(self.raise_("maps-a"))
        self.assertIsNotNone(self.raise_("maps-b"))
        self.assertEqual(self.rec("maps-a")["state"], "queued")

    def test_a_held_request_can_be_cancelled(self):
        self.say = lambda name, point, payload: {"hold": "not now"}
        self.raise_("maps-a")
        self.h.record("task.run_cancelled", "maps-a", {"channel": "session", "owner": False, "sender": "maps"})
        self.assertNotIn("maps-a", self.h.state.get("runs") or {})

    def test_the_reason_survives_a_recount_of_the_queue(self):
        self.say = lambda name, point, payload: {"hold": "not now"}
        self.raise_("maps-a")
        executors.recount(self.h.state, 1)
        self.assertIn("not now", self.why("maps-a"))
        self.assertEqual(self.rec("maps-a")["state"], "queued")

    def test_the_hold_is_a_fact_of_the_core_only(self):
        from core import admit
        self.assertIn("task.run_held", admit.CORE_ONLY)

    def test_the_hold_survives_a_replay_of_the_journal(self):
        self.say = lambda name, point, payload: {"hold": "not now"}
        self.raise_("maps-a")
        again = Hub(os.path.join(self.tmp, "state"), types.SimpleNamespace())
        run = (again.state.get("runs") or {}).get("maps-a")
        self.assertEqual((run["state"], run["held"]["reason"]), ("queued", "not now"))

    def test_the_minihub_queue_row_gives_the_reason(self):
        self.say = lambda name, point, payload: {"hold": "not now"}
        self.raise_("maps-a")
        self.h.state["runs"]["maps-a"]["minihub"] = True
        (row,) = minihub.queue(self.h.state)
        self.assertIn("not now", row["why"])

    def test_a_wake_is_not_asked(self):
        self.say = lambda name, point, payload: {"hold": "not now"}
        self.raise_("maps-a")
        self.h.state["runs"]["maps-a"]["wake"] = {"about": ""}
        self.h.state["runs"]["maps-a"].pop("held")
        self.h.state["runs"]["maps-a"]["state"] = "ready"
        del self.mods.calls[:]
        self.h.admit_pass(held=False)
        self.assertEqual(self.mods.calls, [])


if __name__ == "__main__":
    unittest.main()
