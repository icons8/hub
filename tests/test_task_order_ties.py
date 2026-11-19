"""Tasks made in the same millisecond. The event time is rounded to the millisecond, so on a fast machine two tasks of
one session tie on `created`; every "the latest task" pick must then take the greater number (delegations.born), not the
first of the equal ones (max() and a stable sort both gave the oldest)."""
import tests  # noqa: F401 — ignore this machine's settings
import unittest

from core import delegations, migrate, reducer, taskrec

OWNER = {"channel": "cli", "owner": True}
BY = {"channel": "session", "owner": False, "sender": "w2"}
T = 5000.123   # one millisecond for all the tasks


class Ties(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        for n in ("boss", "w", "w2"):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", **OWNER)
            self.ev("session.started", n)

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        return why

    def tasks(self, name, n=3, **fields):
        """n tasks of the session, all with one `created`; their ids go up, the first one is the oldest."""
        ids = []
        for i in range(n):
            self.assertIsNone(self.ev("delegation.created", None, ts=T, session=name, for_session="boss",
                                      context=f"job {i}", channel="session", owner=False, sender="boss"))
            ids.append(f"d{self.seq}")
        for did in ids:
            self.assertEqual(self.st["delegations"][did]["created"], T)
            self.st["delegations"][did].update(fields)
        return ids

    def test_born_orders_equal_times_by_the_number(self):
        a, b = self.tasks("w", 2)
        ds = self.st["delegations"]
        self.assertEqual(max([ds[a], ds[b]], key=delegations.born)["id"], b)
        self.assertEqual(sorted([ds[b], ds[a]], key=delegations.born)[0]["id"], a)

    def test_the_last_task_of_a_session_is_the_latest_of_equal_times(self):
        ids = self.tasks("w")
        self.assertEqual(reducer._last_task(self.st, "w"), ids[-1])

    def test_a_reminder_after_a_restart_is_about_the_latest_of_equal_times(self):
        ids = self.tasks("w")
        self.assertEqual(delegations.open_task(self.st, "w")[0], ids[-1])

    def test_the_reply_continues_the_latest_handed_in_task_of_equal_times(self):
        ids = self.tasks("w", state="replied")
        sess = self.st["sessions"]["w"]
        sess.update(role="executor", raised_by="boss")
        delegations._followup(self.st, sess, {"sender": "boss", "created": T + 1, "about": None})
        self.assertEqual(sess["delegation"], ids[-1])

    def test_a_note_to_the_raiser_carries_the_last_result_of_equal_times(self):
        ids = self.tasks("w", state="replied")
        for i, did in enumerate(ids):
            self.st["delegations"][did]["reply"] = f"result {i}"
        self.st["sessions"]["w"].update(role="executor", raised_by="boss", state="retired")
        up, note = reducer._to_raiser(self.st, "w")
        self.assertIn("result 2", note)

    def test_a_result_is_told_by_the_session_of_the_latest_task_of_the_chain_with_equal_times(self):
        # two sessions got a task for one message of the owner at the same millisecond: the later one is the chain's
        # latest, and the result that reached it is folded into it
        first, second = self.tasks("w", 1)[0], self.tasks("w2", 1)[0]
        for did in (first, second):
            self.st["delegations"][did]["for"] = "m1"
        self.st["messages"]["m1"] = {"id": "m1", "session": "z", "sender": "w2", "text": "x", "state": "delivered"}
        sess = self.st["sessions"]["boss"]
        sess.update(turn_root="m1", turn_his=False, turn_head="m1", sent_in_turn=["w", "w2"], sent_chars={})
        self.assertEqual(reducer._merged_into(self.st, sess, None, {"text": "done"}, T + 1), "w2")

    def test_a_message_to_a_departed_executor_carries_its_last_result_of_equal_times(self):
        ids = self.tasks("w", state="replied")
        for i, did in enumerate(ids):
            self.st["delegations"][did]["reply"] = f"result {i}"
        self.st["sessions"]["w"].update(role="executor", raised_by="boss", state="removed")
        self.assertIsNone(self.ev("message.received", None, to="w", text="and also this", **BY))
        m = self.st["messages"][f"m{self.seq}"]
        self.assertIn("result 2", m["routing_note"])
    def test_the_raiser_of_equal_times_is_the_one_of_the_latest_task(self):
        first = self.tasks("w", 1)[0]
        second = self.tasks("w", 1)[0]
        self.st["delegations"][first]["for_session"] = "w2"
        self.st["delegations"][second]["for_session"] = "boss"
        self.assertEqual(migrate.raiser_of(self.st, "w"), "boss")

    def test_open_tasks_of_equal_times_come_oldest_first_whatever_the_order_of_the_state(self):
        ids = self.tasks("w")
        ds = self.st["delegations"]
        self.st["delegations"] = {i: ds[i] for i in reversed(ids)}
        self.assertEqual([d["id"] for d in taskrec.open_of(self.st, "w")], ids)
        self.assertEqual(taskrec.resolve(self.st, None, "w"), ids[-1])


if __name__ == "__main__":
    unittest.main()
