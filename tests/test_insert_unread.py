"""An honest status of a line inserted in the middle of a turn (owner-stop concept, slice 4).

The core types an insertion (`--now`, the watchdog) into the session's stdin at once and the message becomes
`delivered`; Claude Code reads it only between steps, so a step of minutes keeps it unread. The core sees the reading
only as a turn with the message's mark; a turn that ends without one read it inside (or starts the next on it). Until
then the snapshot says `inserted_unread` and the label is "inserted, not read yet" instead of "delivered" — for every session,
not only for an executor ("STOP" inserted mid-step waited 4 min 45 s while the page said "delivered")."""
import tests  # noqa: F401 — ignore this machine's settings
import unittest

from core import reducer, views

HUB = {"channel": "session", "owner": False, "sender": "hub"}


class Unread(unittest.TestCase):
    def setUp(self):
        self.events, self.seq = [], 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", channel="cli", owner=True)
        self.ev("session.started", "hub")
        self.ev("session.requested", "maps", cwd="/w", session_id="s-maps", channel="cli", owner=True)
        self.ev("session.started", "maps")
        self.ev("turn.started", "maps", marker=None, prompt_head="long work")
        self.ev("turn.tool", "maps", id="t1", name="Bash", input={"command": "sleep 600"})

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.events.append({"seq": self.seq, "ts": 1000 + self.seq, "type": type, "entity": entity, "data": data})
        return self.events[-1]

    def snap(self):
        r = views.Reader()
        r.feed_all(self.events)
        return r.snapshot()

    def insert(self):
        e = self.ev("message.received", None, to="maps", text="urgent: take this into account", now=True, **HUB)
        mid = f"m{e['seq']}"
        st = reducer.empty()
        for x in self.events:
            st, _, why = reducer.reduce(st, x)
            self.assertIsNone(why, x)
        self.assertTrue(st["messages"][mid].get("injected"))
        return mid

    def test_an_insertion_mid_step_is_unread_not_delivered(self):
        mid = self.insert()
        m = self.snap()["messages"][mid]
        self.assertEqual(m["state"], "delivered")   # typed into stdin: the state machine is the same
        self.assertTrue(m["inserted_unread"])
        self.assertEqual(m["label"], "inserted, not read yet")

    def test_a_turn_with_its_mark_reads_it(self):
        mid = self.insert()
        self.ev("turn.tool_result", "maps", id="t1", text="ok")
        self.ev("turn.started", "maps", marker=mid, prompt_head="(urgent — came in the middle of your turn: take it into account now) …")
        m = self.snap()["messages"][mid]
        self.assertFalse(m["inserted_unread"])
        self.assertEqual(m["label"], "delivered")

    def test_the_end_of_the_turn_ends_unread(self):
        # Claude Code does not always start a marked turn on an insertion: once the turn is over it was read inside
        mid = self.insert()
        self.ev("turn.result", "maps", text="done")
        self.ev("turn.stopped", "maps")
        self.assertFalse(self.snap()["messages"][mid]["inserted_unread"])

    def test_a_new_process_ends_unread(self):
        mid = self.insert()
        self.ev("session.exited", "maps", code=1)
        self.assertFalse(self.snap()["messages"][mid]["inserted_unread"])

    def test_taking_the_session_into_the_terminal_ends_unread(self):
        # review: a way out of busy other than idle or a new process (hub take, SessionEnd) — the turn is over all the same
        mid = self.insert()
        self.ev("session.take_requested", "maps", channel="cli", owner=True)
        self.assertFalse(self.snap()["messages"][mid]["inserted_unread"])

    def test_a_message_that_waited_for_the_turn_end_is_never_unread(self):
        e = self.ev("message.received", None, to="maps", text="after the turn", channel="web", owner=True)
        mid = f"m{e['seq']}"
        self.assertFalse(self.snap()["messages"][mid]["inserted_unread"])   # queued
        self.ev("turn.result", "maps", text="done")
        self.ev("turn.stopped", "maps")
        self.ev("turn.started", "maps", marker=mid, prompt_head="after the turn")
        m = self.snap()["messages"][mid]
        self.assertEqual((m["state"], m["inserted_unread"]), ("delivered", False))


if __name__ == "__main__":
    unittest.main()
