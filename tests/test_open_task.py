"""The "Connected: X - finish the task" note pointed to a three-day-old assignment from a removed session (10-03: a
session got a design tool for a page, and the core told it to finish a network request from 10-02 by an operator
session that no longer exists). An open assignment is an unclosed one from a session that still exists (an
"answered" one too: the session was waiting for a tool)."""
import tests  # noqa: F401 - ignore the machine's settings
import unittest

from core import delegations, reducer

OWNER = {"channel": "web", "owner": True}


class OpenTask(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        for n, kind in (("hub", "hub"), ("dev", None), ("asker", None)):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", kind=kind, **OWNER)
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def test_task_from_a_session_that_is_gone_is_not_open(self):
        self.ev("delegation.created", None, session="dev", for_session="asker", context="question",
                channel="session", owner=False, sender="hub")
        self.assertIsNotNone(delegations.open_task(self.st, "dev")[0])
        self.ev("session.ended", "asker")
        self.ev("session.remove_requested", "asker", **OWNER)
        self.assertEqual(delegations.open_task(self.st, "dev"), (None, None))

    def test_tool_connected_does_not_revive_a_dead_task(self):
        self.ev("delegation.created", None, session="dev", for_session="asker", context="old question",
                channel="session", owner=False, sender="hub")
        did = f"d{self.seq}"
        self.ev("session.ended", "asker")
        self.ev("session.remove_requested", "asker", **OWNER)
        self.ev("catalog.loaded", None, tools={"ux": {"kind": "plugin", "path": "/p", "risk": [], "description": "ux"}})
        self.ev("tools.requested", "dev", tool="ux", why="page", channel="session", owner=False, sender="dev")
        self.ev("tools.granted", "dev", tool="ux", channel="session", owner=False, sender="hub")
        self.ev("turn.stopped", "dev")
        note = [m for m in self.st["messages"].values() if m["session"] == "dev" and m["text"].startswith("Connected")]
        self.assertTrue(note)
        self.assertNotIn("old question", note[-1]["text"])
        self.assertNotEqual(self.st["delegations"][did].get("message"), note[-1]["id"])


if __name__ == "__main__":
    unittest.main()
