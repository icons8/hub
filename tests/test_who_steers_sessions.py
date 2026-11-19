"""Who may take a session into manual mode, give it back, put it to sleep or wake it, and who may cancel a request for
an executor: the core decides by who says it. A working session could take another into manual mode (its process was
killed), make the core send another a handover in the hub's name, or raise a sleeping one; and a fact that named nobody
cancelled the owner's request for an executor (None == None)."""
import unittest

from core import guards, reducer

OWNER = {"channel": "cli", "owner": True}
WEB = {"channel": "web", "owner": True}
MODULE = {"channel": "mod:demo", "owner": True}   # a module handler serving the owner's request (Module API)
PLUGIN = {"channel": "plugin", "owner": False, "sender": "ext:demo"}


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


class Fleet(unittest.TestCase):
    """A hub, two working sessions a and b, and e: an executor a raised."""

    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        for name, extra in (("hub", {"kind": "hub"}), ("a", {}), ("b", {}),
                            ("e", {"role": "executor", "raised_by": "a"})):
            self.ok("session.requested", name, cwd="/w", session_id=f"s-{name}", **OWNER, **extra)
            self.ok("session.started", name, session_id=f"s-{name}", pid=100 + self.seq)
            self.st["sessions"][name].update(extra)

    def ev(self, type, entity=None, **data):
        """Fold one event; the refusal reason or None."""
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "src": "spool",
                                                      "type": type, "entity": entity, "data": data})
        self.actions += acts
        return why

    def ok(self, type, entity=None, **data):
        why = self.ev(type, entity, **data)
        self.assertIsNone(why, f"{type} {entity} {data}")

    def state(self, name):
        return self.st["sessions"][name]["state"]

    def asleep(self, name):
        self.st["sessions"][name].update(state="asleep", closing="none")

    def handoffs(self, name):
        return [m for m in self.st["messages"].values()
                if m["session"] == name and str(m["text"]).startswith(reducer.HANDOFF_MARK)]

    def releases(self, name):
        return [a for a in self.actions if a.get("do") == "release" and a.get("session") == name]


class TakeAndGive(Fleet):
    def test_a_session_does_not_take_another(self):
        self.assertIsNotNone(self.ev("session.take_requested", "b", **by("a")))
        self.assertEqual(self.state("b"), "idle")
        self.assertFalse(self.releases("b"))

    def test_the_raiser_does_not_take_its_executor_either(self):
        self.assertIsNotNone(self.ev("session.take_requested", "e", **by("a")))   # the terminal is the owner's
        self.assertEqual(self.state("e"), "idle")

    def test_the_owner_and_the_hub_take_and_give(self):
        self.ok("session.take_requested", "b", **OWNER)
        self.assertEqual(self.state("b"), "held")
        self.assertIsNotNone(self.ev("session.give_requested", "b", **by("a")))
        self.assertIsNotNone(self.ev("session.give_requested", "b", **by("b")))   # its terminal is the owner's
        self.assertIsNotNone(self.ev("session.give_requested", "b", **PLUGIN))
        self.assertEqual(self.state("b"), "held")
        self.ok("session.give_requested", "b", **by("hub"))
        self.assertEqual(self.state("b"), "starting")
        self.ok("session.take_requested", "a", **by("hub"))
        self.ok("session.give_requested", "a", **WEB)

    def test_the_refusal_names_who_may_before_the_state(self):
        # an asleep session: "wake it first" is advice only to whoever may take it
        self.asleep("b")
        why = self.ev("session.take_requested", "b", **by("a"))
        self.assertEqual(why, guards.from_hub_or_owner(self.st, by("a")))
        self.assertIn("hub session wake", self.ev("session.take_requested", "b", **OWNER))


class SleepAndWake(Fleet):
    def test_a_session_does_not_put_another_to_sleep(self):
        self.assertIsNotNone(self.ev("session.close_requested", "b", **by("a")))
        self.assertIsNotNone(self.ev("session.close_ready", "b", **by("a")))
        self.assertIsNotNone(self.ev("session.close_requested", "b", **PLUGIN))
        self.assertEqual(self.st["sessions"]["b"]["closing"], "none")
        self.assertFalse(self.handoffs("b"))

    def test_itself_the_hub_the_owner_a_module_and_the_core_put_it_to_sleep(self):
        for who in (by("b"), by("hub"), OWNER, WEB, MODULE, {}, {"channel": "core"}):
            self.setUp()
            self.ok("session.close_requested", "b", **who)
            self.assertEqual(self.st["sessions"]["b"]["closing"], "handoff", who)
            self.assertTrue(self.handoffs("b"), who)
        self.setUp()
        self.ok("session.close_ready", "b", **by("b"))
        self.assertEqual(self.st["sessions"]["b"]["closing"], "handoff")

    def test_whoever_raised_an_executor_puts_it_to_sleep(self):
        self.ok("session.close_requested", "e", **by("a"))
        self.assertEqual(self.st["sessions"]["e"]["closing"], "handoff")
        self.setUp()
        self.assertIsNotNone(self.ev("session.close_requested", "e", **by("b")))

    def test_a_direction_is_put_to_sleep_by_the_hub_not_by_its_raiser(self):
        # as stop and remove (guards.from_hub_owner_or_raiser): a direction is not the raiser's executor
        self.st["sessions"]["e"]["backlog"] = "maps"
        self.assertIsNotNone(self.ev("session.close_requested", "e", **by("a")))
        self.ok("session.close_requested", "e", **by("hub"))

    def test_a_sub_executor_is_put_to_sleep_by_its_raiser_and_the_mini_hub_of_its_bus(self):
        # the owner speaks of the bus's executors to the mini-hub
        self.st["sessions"]["e"]["bus"] = "a"
        self.ok("session.requested", "f", cwd="/w", session_id="s-f", **OWNER)
        self.ok("session.started", "f", session_id="s-f", pid=200)
        self.st["sessions"]["f"].update(role="executor", bus="a", raised_by="e")
        self.assertEqual(reducer.minihub.hub_of(self.st, self.st["sessions"]["f"]), "a")
        self.assertIsNotNone(self.ev("session.close_requested", "f", **by("b")))
        self.ok("session.close_requested", "f", **by("e"))
        self.assertEqual(self.st["sessions"]["f"]["closing"], "handoff")
        self.setUp()
        self.st["sessions"]["e"]["bus"] = "a"
        self.ok("session.requested", "f", cwd="/w", session_id="s-f", **OWNER)
        self.ok("session.started", "f", session_id="s-f", pid=200)
        self.st["sessions"]["f"].update(role="executor", bus="a", raised_by="e")
        self.ok("session.close_requested", "f", **by("a"))

    def test_a_session_does_not_wake_another(self):
        for name in ("b", "e"):
            self.asleep(name)
        self.assertIsNotNone(self.ev("session.wake_requested", "b", **by("a")))
        self.assertIsNotNone(self.ev("session.wake_requested", "b", **PLUGIN))
        self.assertEqual(self.state("b"), "asleep")
        self.ok("session.wake_requested", "b", **by("hub"))
        self.assertEqual(self.state("b"), "starting")
        self.assertIsNotNone(self.ev("session.wake_requested", "e", **by("b")))
        self.ok("session.wake_requested", "e", **by("a"))   # the raiser wakes its own
        self.assertEqual(self.state("e"), "starting")

    def test_the_owner_and_a_module_wake(self):
        for who in (OWNER, WEB, MODULE):
            self.setUp()
            self.asleep("b")
            self.ok("session.wake_requested", "b", **who)
            self.assertEqual(self.state("b"), "starting", who)

    def test_a_parked_one_too(self):
        self.st["sessions"]["b"]["state"] = "parked"
        self.assertIsNotNone(self.ev("session.wake_requested", "b", **by("a")))
        self.assertEqual(self.state("b"), "parked")

    def test_waking_a_live_session_keeps_its_hint(self):
        # "write to it" is for anyone: a refused permission would only send it to the hub for the same answer
        self.assertIn("hub send", self.ev("session.wake_requested", "b", **by("a")))

    def test_the_stop_hint_names_close_only_to_whoever_may_close(self):
        self.assertNotIn("hub session close", self.ev("session.stop_requested", "b", **by("a")))
        self.assertIn("hub session close", self.ev("session.stop_requested", "b", **by("hub")))
        self.assertIn("hub session close", self.ev("session.stop_requested", "b", **by("b")))

    def test_stopping_no_such_session_is_a_plain_refusal(self):
        why = self.ev("session.stop_requested", "nosuch", **by("a"))
        self.assertIsNotNone(why)
        self.assertNotEqual(getattr(why, "code", None), reducer.forms.CORE_FAILURE, why)

    def test_writing_to_a_sleeping_session_still_wakes_it(self):
        self.asleep("b")
        self.ok("message.received", None, to="b", text="look", **by("a"))
        self.assertEqual(self.state("b"), "starting")


class CancelARun(Fleet):
    def queue(self, by_whom=None):
        self.st.setdefault("runs", {})["x"] = {"by": by_whom, "task": "t", "ts": 1, "state": "queued", "why": "no room"}

    def test_a_fact_that_names_nobody_does_not_cancel_the_owners_request(self):
        self.queue()
        self.assertIsNotNone(self.ev("task.run_cancelled", "x", channel="cli", owner=False))
        self.assertIsNotNone(self.ev("task.run_cancelled", "x"))
        self.assertIsNotNone(self.ev("task.run_cancelled", "x", **by("a")))
        self.assertIn("x", self.st["runs"])
        self.ok("task.run_cancelled", "x", **OWNER)
        self.assertNotIn("x", self.st["runs"])

    def test_the_requester_and_the_hub_cancel(self):
        self.queue("a")
        self.assertIsNotNone(self.ev("task.run_cancelled", "x", **by("b")))
        self.ok("task.run_cancelled", "x", **by("a"))
        self.queue("a")
        self.ok("task.run_cancelled", "x", **by("hub"))
        self.assertNotIn("x", self.st["runs"])


if __name__ == "__main__":
    unittest.main()
