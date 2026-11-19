"""`hub session close` on a sleeping session (10-08: the owner put an already sleeping session to sleep and got "not idle
(asleep): a turn is running or it is waiting for an answer", while `hub show` said "idle"): a sleeping session gets a
clear refusal saying what to do instead."""
import tests  # noqa: F401 — hide the machine's settings
import unittest

from core import reducer, views

OWNER = {"channel": "cli", "owner": True}


class CloseAsleep(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.events = reducer.empty(), 0, []
        self.ev("session.requested", "maps", cwd="/w", session_id="s-maps", **OWNER)
        self.ev("session.started", "maps")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        e = {"seq": self.seq, "ts": 100 + self.seq, "type": type, "entity": entity, "data": data}
        self.events.append(e)
        self.st, _, why = reducer.reduce(self.st, e)
        return why

    def feed(self, name):
        """Feed lines of session `name` as the page sees them (rejected events produce no lines)."""
        r = views.Reader()
        for e in self.events:
            r.feed(e)
        return [it.get("text") for it in r.timelines.get(name, []) if it.get("kind") == "sys"]

    def test_asleep_says_so(self):
        self.ev("session.ended", "maps", reason="prompt_input_exit")
        self.assertEqual(self.st["sessions"]["maps"]["state"], "asleep")
        why = self.ev("session.close_requested", "maps", **OWNER)
        self.assertIn("already asleep", why)
        self.assertIn("hub session remove maps", why)
        self.assertNotIn("a turn is running", why)

    def test_owner_sleeping_a_parked_session_shows_it_asleep(self):
        # 10-12: the core parked the session on restart, the page said "idle"; the owner pressed "Put to sleep" four
        # times and got silent refusals. Now the sleep is accepted: the session really sleeps, a message wakes it
        self.ev("session.parked", "maps", channel="core", why="restart")
        self.assertEqual(views._shown(self.st["sessions"]["maps"])["label"], views.STATE_LABELS["idle"])
        self.assertIsNone(self.ev("session.close_requested", "maps", channel="web", owner=True))
        sess = self.st["sessions"]["maps"]
        self.assertEqual((sess["state"], views._shown(sess).get("parked")), ("asleep", None))
        self.assertIn("already asleep", self.ev("session.close_requested", "maps", **OWNER))   # again: now it really sleeps
        self.ev("message.received", None, to="maps", text="hi", **OWNER)
        self.assertEqual(self.st["sessions"]["maps"]["state"], "starting")

    def test_feed_says_a_parked_session_fell_asleep_without_a_handoff(self):
        # an accepted "Put to sleep" on a parked session: no process, no handover — the feed does not say the session is handing over before sleep
        self.ev("session.parked", "maps", channel="core", why="restart")
        self.assertIsNone(self.ev("session.close_requested", "maps", channel="web", owner=True))
        lines = self.feed("maps")
        self.assertNotIn("handing things over before sleep (HANDOFF-REQUEST)", lines)
        self.assertEqual(lines[-1], "fell asleep at the user's word, without a handover; a message will wake it")

    def test_feed_still_says_handoff_for_a_live_session(self):
        self.assertIsNone(self.ev("session.close_requested", "maps", **OWNER))
        self.assertEqual(self.st["sessions"]["maps"]["closing"], "handoff")
        self.assertEqual(self.feed("maps")[-1], "handing things over before sleep (HANDOFF-REQUEST)")

    def test_a_parked_agent_stays_on_duty(self):
        # an agent stays on duty until the owner dismisses it: "Put to sleep" does not put a parked one to sleep either — neither the owner nor the hub
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.role_set", "maps", role="agent", about="Maps", **OWNER)
        self.ev("session.parked", "maps", channel="core", why="idle")
        self.assertEqual(self.st["sessions"]["maps"]["state"], "parked")
        for who in (OWNER, {"channel": "session", "sender": "hub"}):
            self.assertIn("this is an agent", self.ev("session.close_requested", "maps", **who) or "")
            self.assertEqual(self.st["sessions"]["maps"]["state"], "parked")

    def test_a_session_cannot_unpark_another(self):
        self.ev("session.requested", "other", cwd="/o", session_id="s-o", **OWNER)
        self.ev("session.parked", "maps", channel="core", why="idle")
        self.assertIsNotNone(self.ev("session.close_requested", "maps", channel="session", sender="other"))
        self.assertEqual(self.st["sessions"]["maps"]["state"], "parked")

    def test_a_parked_session_with_an_open_question_is_not_put_to_sleep(self):
        # it asked with a form (card open), the turn ended, the core parked it (parking ignores the question). "Put to sleep"
        # runs the same checks as for a live one: if it fell asleep, the hub would decide the question was dropped and ask the owner a second time
        self.ev("turn.started", "maps", marker=None, prompt_head="x")
        self.ev("permission.requested", "maps", request_id="r1", tool="AskUserQuestion", interactive=True,
                input={"questions": [{"question": "Which tiles?", "options": [{"label": "A"}, {"label": "B"}]}]})
        qid = self.st["sessions"]["maps"]["asks"][0]["id"]
        self.ev("turn.stopped", "maps")
        self.assertIsNone(self.ev("session.parked", "maps", channel="core", why="idle"))
        why = self.ev("session.close_requested", "maps", channel="web", owner=True)
        self.assertIn(f"an open question to the owner {qid}", why or "")
        self.assertEqual(self.st["sessions"]["maps"]["state"], "parked")   # still "idle" for the owner

    def test_a_parked_session_with_a_task_waiting_for_the_owner_is_not_put_to_sleep(self):
        self.ev("session.parked", "maps", channel="core", why="idle")
        self.st["delegations"]["d9"] = {"id": "d9", "session": "maps", "state": "waiting-owner"}
        self.assertIn("task d9 is not closed yet", self.ev("session.close_requested", "maps", **OWNER) or "")
        self.assertEqual(self.st["sessions"]["maps"]["state"], "parked")

    def test_busy_still_says_busy(self):
        self.ev("turn.started", "maps", marker=None, prompt_head="x")
        self.assertIn("a turn is running", self.ev("session.close_requested", "maps", **OWNER))


if __name__ == "__main__":
    unittest.main()
