"""A direction is not an executor (10-18: the hub's "Clean up after executors" button offered to delete 14 directions).
A direction is raised through `hub task run` and then gets a backlog: in state it has role="executor", raised_by — who
raised it, and backlog. An executor for a task is role executor WITHOUT a backlog (core/minihub.runner, on the page —
isRunner in state.js): only it is cleaned up, put to sleep when done, has its background stopped on sleep, and counts
toward the raiser's parallelism slots."""
import tests  # noqa: F401 — keep the machine's settings out of the test
import unittest

from core import executors, guards, minihub, reducer, views

OWNER = {"channel": "cli", "owner": True}


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


class DirectionIsNotAnExecutor(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.started", "hub")
        self.ev("session.requested", "maps", cwd="/home/x/maps", session_id="s-maps", role="agent", about="Maps",
                **OWNER)
        self.ev("session.started", "maps")

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def raised(self, name, raiser, text="make a tile map"):
        """As the daemon does it: the executor is raised, gets a task and hands it in with an answer."""
        self.assertIsNone(self.ev("task.run_requested", name, task=text, **by(raiser)))
        run = self.st["runs"][name]
        for type, entity, data in executors.events_for(self.st, name, run, cwd=f"/home/x/{name}", worktree=None):
            self.assertIsNone(self.ev(type, entity, **data), (type, data))
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == name]
        self.ev("session.started", name)
        self.ev("turn.started", name, marker=d["message"], prompt_head="task")
        self.ev("turn.result", name, text="done\n\n**Lessons**\n- tiles — by version")
        self.ev("turn.stopped", name)
        return d

    def direction(self, name, raiser):
        """A direction as in the owner's state.json: raised as an executor, then the hub gave it a backlog."""
        d = self.raised(name, raiser)
        self.assertIsNone(self.ev("task.backlog", None, slug=name, session=name, **by("hub")))
        s = self.st["sessions"][name]
        self.assertEqual((s["role"], s["raised_by"], s["backlog"]), ("executor", raiser, name))
        return d

    def test_one_rule(self):
        self.assertTrue(minihub.runner({"role": "executor", "raised_by": "hub"}))
        self.assertFalse(minihub.runner({"role": "executor", "raised_by": "hub", "backlog": "mail"}))
        self.assertFalse(minihub.runner({"role": "agent"}))
        self.assertFalse(minihub.runner(None))

    def test_a_direction_does_not_take_its_raisers_place(self):
        # parallelism 1: a direction raised by a specialist is not its executor; the slot is free
        self.ev("owner.settings", None, timezone="UTC", parallel=1, channel="core")
        self.direction("maps-dir", "maps")
        self.assertEqual(executors.live_of(self.st, "maps"), [])
        self.assertIsNone(self.ev("task.run_requested", "maps-ci", task="fix the CI", **by("maps")))
        self.assertEqual(self.st["runs"]["maps-ci"]["state"], "ready", self.st["runs"]["maps-ci"].get("why"))
        r = views.Reader()
        r.state = self.st
        (row,) = [s for s in r.snapshot()["sessions"] if s["name"] == "maps"]
        self.assertEqual(row["executors_live"], 0)                          # and it takes no slot on the page either

    def test_a_direction_is_not_counted_under_the_hubs_ceiling(self):
        self.direction("maps-dir", "maps")
        self.assertFalse(executors.sessions_executor(self.st, self.st["sessions"]["maps-dir"]))

    def test_a_sleeping_direction_keeps_its_background_commands(self):
        # a direction's sleep is like a specialist's: until the next job, do not kill its dev server (end_tree is for an executor)
        self.assertFalse(reducer._for_good({"role": "executor", "backlog": "mail"}, "idle", "asleep", "session.exited"))
        self.assertTrue(reducer._for_good({"role": "executor"}, "idle", "asleep", "session.exited"))

    def test_a_direction_raised_by_a_specialist_is_not_told_it_is_its_executor(self):
        self.direction("maps-dir", "maps")
        self.assertNotIn("executor_of", reducer.spawn_action(self.st["sessions"]["maps-dir"], st=self.st))

    def test_a_removed_direction_sends_no_lessons_note(self):
        self.direction("maps-dir", "maps")
        before = [m["id"] for m in self.st["messages"].values() if m["session"] == "maps"]
        self.ev("session.parked", "maps-dir", channel="core")
        self.assertIsNone(self.ev("session.remove_requested", "maps-dir", **OWNER))
        self.assertEqual(self.st["sessions"]["maps-dir"]["state"], "removed")
        notes = [m for m in self.st["messages"].values() if m["session"] == "maps" and m["id"] not in before
                 and "handed in its task" in m["text"]]
        self.assertEqual(notes, [])

    def test_a_directions_decision_stays_with_it(self):
        # the owner's word said to a direction is its own business, not a "write it into your CLAUDE.md" to the specialist that raised it
        self.direction("maps-dir", "maps")
        before = len(self.st["messages"])
        self.ev("owner.decided", None, text="tiles — webp only · decided so", **by("maps-dir"))
        self.assertFalse([m for m in list(self.st["messages"].values())[before:] if m["session"] == "maps"])

    def test_the_core_does_not_retire_a_direction(self):
        self.direction("mail", "hub")
        s = {**self.st["sessions"]["mail"], "req": {"retire": True, "channel": "core"}}
        self.assertEqual(guards.executor_done(self.st, s), "not an executor's leaving")

    def test_cancelling_a_live_direction_does_not_suggest_stopping_it(self):
        # the core does not allow "hub session stop" for a direction — the hint would be false
        self.direction("mail", "hub")
        why = self.ev("task.run_cancelled", "mail", **by("hub"))
        self.assertNotIn("hub session stop", why or "")


if __name__ == "__main__":
    unittest.main()
