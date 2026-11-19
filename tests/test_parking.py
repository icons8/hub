"""A session with nothing to do does not hold a process (owner decision 2026-10-07: with many sessions open the startup is
slow; do not start a session's process until someone writes to it): after a hub restart 22 free sessions came up at once
(30-60 s, load up to 100), while four worked during the hour. A free session with nothing to do is parked: the process is
not started or is killed, the conversation stays; the first message (owner, hub, schedule) brings it back with the same
conversation. Free for longer than park_after hours: the same. The model is not called."""
import tests  # noqa: F401 — hide the machine's settings
import tempfile
import unittest

from core import parking, reducer, views
from core.daemon import Hub

OWNER = {"channel": "web", "owner": True}


class Core(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.requested", "a", cwd="/w", session_id="s-a", **OWNER)
        for n in ("hub", "a"):
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, self.acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                           "entity": entity, "data": data})
        return why

    def test_parked_session_releases_and_a_message_raises_it_with_its_conversation(self):
        self.st["sessions"]["a"]["has_history"] = True   # there already was a conversation
        self.assertIsNone(self.ev("session.parked", "a", channel="core"))
        a = self.st["sessions"]["a"]
        self.assertEqual(a["state"], "parked")
        self.assertIn({"do": "release", "session": "a"}, [{k: x[k] for k in ("do", "session")} for x in self.acts])
        self.ev("message.received", None, to="a", text="hello", **OWNER)
        spawn, = [x for x in self.acts if x["do"] == "spawn"]
        self.assertEqual((spawn["session"], spawn["session_id"], spawn["resume"]), ("a", "s-a", True))
        self.assertNotIn("parked", self.st["sessions"]["a"])
        self.ev("session.started", "a")
        self.assertTrue(any(x["do"] == "type" for x in self.acts))      # the message arrived after startup

    def test_only_a_free_session_is_parked(self):
        self.ev("message.received", None, to="a", text="work", **OWNER)
        self.assertIsNotNone(self.ev("session.parked", "a", channel="core"))     # in a turn: no
        self.assertIsNotNone(self.ev("session.parked", "hub", channel="core"))   # the hub is always alive

    def test_page_shows_a_parked_session_as_free_not_asleep(self):
        self.ev("session.parked", "a", channel="core")
        r = views.Reader()
        r.state = self.st
        a = next(s for s in r.snapshot()["sessions"] if s["name"] == "a")
        self.assertEqual((a["state"], a["label"], a["parked"]), ("idle", "idle", True))

    def test_what_may_be_parked(self):
        now = 10_000
        a = self.st["sessions"]["a"]
        self.assertTrue(parking.parkable(self.st, a))
        self.assertFalse(parking.parkable(self.st, self.st["sessions"]["hub"]))
        a["background"] = {"b1": {"id": "b1", "kind": "bash", "since": now - 60}}
        self.assertFalse(parking.parkable(self.st, a))                           # waits for its background work
        a["background"] = {}
        self.st["messages"]["m9"] = {"id": "m9", "session": "a", "state": "queued"}
        self.assertFalse(parking.parkable(self.st, a))                           # there is something to deliver
        del self.st["messages"]["m9"]
        self.assertTrue(parking.idle_too_long(a, now=a["since"] + 2 * 3600 + 1, hours=2))
        self.assertFalse(parking.idle_too_long(a, now=a["since"] + 3600, hours=2))
        self.assertFalse(parking.idle_too_long(a, now=a["since"] + 10 ** 6, hours=0))   # 0 means do not kill


class FakeHost:
    def __init__(self):
        self.living, self.released = set(), []

    def alive(self, name):
        return name in self.living

    def release(self, name, **_):
        self.released.append(name)
        self.living.discard(name)


class Daemon(unittest.TestCase):
    def hub(self, state, host, clock):
        h = Hub(state, host, registry_dir=state, clock=clock)
        h.execute = lambda a: host.release(a["session"]) if a["do"] == "release" else None   # the host needs nothing else
        return h

    def test_after_a_restart_free_sessions_are_parked_not_raised(self):
        state, host, now = tempfile.mkdtemp(), FakeHost(), [1000.0]
        h = self.hub(state, host, lambda: now[0])
        for name, data in (("a", {}), ("b", {})):
            h.record("session.requested", name, {"cwd": "/w", "session_id": "s-" + name, **OWNER})
            h.record("session.started", name, {})
        h.record("message.received", None, {"to": "b", "text": "a task", **OWNER})   # b has a queue
        h.reconcile()      # no processes: the hub was restarted
        self.assertEqual(h.state["sessions"]["a"]["state"], "parked")
        self.assertEqual(h.state["sessions"]["a"]["state"], "parked")
        self.assertEqual(h.state["sessions"]["b"]["state"], "starting")        # with a job: it comes up

    def test_a_session_free_for_hours_is_parked(self):
        state, host, now = tempfile.mkdtemp(), FakeHost(), [1000.0]
        h = self.hub(state, host, lambda: now[0])
        h.config["park_after_hours"] = 2
        h.record("session.requested", "a", {"cwd": "/w", "session_id": "s-a", **OWNER})
        h.record("session.started", "a", {})
        host.living.add("a")
        h.park_idle()
        self.assertEqual(h.state["sessions"]["a"]["state"], "idle")
        now[0] += 2 * 3600 + 5
        h.park_idle()
        self.assertEqual(h.state["sessions"]["a"]["state"], "parked")
        self.assertEqual(host.released, ["a"])


if __name__ == "__main__":
    unittest.main()
