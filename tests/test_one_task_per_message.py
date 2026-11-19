"""One owner message is one task (spec "tasks, specialists, executors", §4): a second assignment from the same
message is rejected as "already taken by X". On 04.10 one task was done in parallel by a session and its fork,
because it arrived by two paths."""
import tests  # noqa: F401 — hide the machine's settings
import unittest

from core import reducer

OWNER = {"channel": "web", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}


class OneTask(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        for n, kind in (("hub", "hub"), ("maps", None), ("geo", None)):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", **({"kind": kind} if kind else {}), **OWNER)
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def test_a_second_task_for_the_same_owner_message_is_refused(self):
        self.ev("message.received", None, to="hub", text="draw the map", **OWNER)
        mid = f"m{self.seq}"
        self.assertIsNone(self.ev("delegation.created", None, session="maps", **{"for": mid}, context="", **HUB))
        why = self.ev("delegation.created", None, session="geo", **{"for": mid}, context="", **HUB)
        self.assertIn("maps", why or "")
        self.assertEqual(len(self.st["delegations"]), 1)

    def test_a_finished_task_does_not_block_a_new_one(self):
        self.ev("message.received", None, to="hub", text="draw the map", **OWNER)
        mid = f"m{self.seq}"
        self.ev("delegation.created", None, session="maps", **{"for": mid}, context="", **HUB)
        did = f"d{self.seq}"
        self.ev("delegation.done", did, **HUB)
        self.assertIsNone(self.ev("delegation.created", None, session="geo", **{"for": mid}, context="again", **HUB))

    def test_a_task_that_answered_does_not_block_a_retry(self):
        self.ev("message.received", None, to="hub", text="check mail", **OWNER)
        mid = f"m{self.seq}"
        self.ev("delegation.created", None, session="maps", **{"for": mid}, context="first try", **HUB)
        d = self.st["delegations"][f"d{self.seq}"]
        self.ev("turn.started", "maps", marker=d["message"], prompt_head="x")
        self.ev("turn.result", "maps", text="cannot: no browser")
        self.assertIsNone(self.ev("delegation.created", None, session="maps", **{"for": mid}, context="second", **HUB))

    def test_tasks_without_an_owner_message_are_not_limited(self):
        self.assertIsNone(self.ev("delegation.created", None, session="maps", context="cache", **HUB))
        self.assertIsNone(self.ev("delegation.created", None, session="geo", context="cache", **HUB))


if __name__ == "__main__":
    unittest.main()
