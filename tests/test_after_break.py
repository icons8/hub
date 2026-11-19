"""A result a session sends after the owner's task to it broke (state stopped) is an answer to him: "Answered you" and
Telegram, as the task's answer would have been (owner decision 18.11, "fix it in the core"; a plugin did it on the
page). The turn after the break is often not tied to the task: the hub asks "where is the plan?", the core says
"continue" — the result went to the folded "Sent on their own". Only for a while after the break (BREAK_WINDOW), only
a real line (RELAY_MIN characters, not a "—" pass-on line), only while that task is the session's latest and his own
(a session that set a task gets the report itself). Never his word: no from_owner, re, answers_question."""
import tests  # noqa: F401 — do not see the machine's settings
import unittest

from core import delegations, forms, reducer

S = {"channel": "session", "owner": False}
PLAN = "The plan: move the tile cache in three steps, the details below"


class AfterTheBreak(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.now = reducer.empty(), 0, 1_800_000_000
        for n, x in (("hub", {"kind": "hub"}), ("analyst", {}), ("web", {})):
            self.ev("session.requested", n, cwd=f"/{n}", session_id=n, channel="web", owner=True, **x)
            self.ev("session.started", n)

    def ev(self, type_, entity=None, src="session", after=60, **data):
        self.seq += 1
        self.now += after
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.now, "src": src, "type": type_,
                                                   "entity": entity, "data": data})
        self.assertIsNone(why, (type_, why))
        return self.seq

    def turn(self, name, marker, text, after=60, **extra):
        self.ev("turn.started", name, src="hook", after=after, **({"marker": marker} if marker else {}))
        seq = self.ev("turn.result", name, src="hook", text=text, **extra)
        self.ev("turn.stopped", name, src="hook")
        return seq

    def broken_task(self, **task):
        """The owner asks the hub; the hub gives the task to the analyst; its turn breaks on an API error."""
        word = f"m{self.ev('message.received', None, src='web', to='hub', text='Ask the analyst for the plan', channel='web', owner=True)}"
        self.ev("turn.started", "hub", src="hook", marker=word)
        did = self.ev("delegation.created", None, session="analyst", **{"for": word, **task}, context="the plan", **S,
                      sender="hub")
        self.ev("turn.result", "hub", src="hook", text="Gave it to the analyst")
        self.ev("turn.stopped", "hub", src="hook")
        self.turn("analyst", f"m{did}", "API Error: 500 Internal server error", is_error=True)
        self.assertEqual(self.st["delegations"][f"d{did}"]["state"], "stopped")
        return f"d{did}"

    def asked_by_hub(self, text=PLAN, after=60):
        note = self.ev("message.received", None, to="analyst", text="Where is the plan?", **S, sender="hub")
        return self.turn("analyst", f"m{note}", text, after=after)

    def answered(self, seq):
        return self.st["sessions"]["analyst"].get("owner_reply") == seq

    def test_the_report_on_the_hubs_question_after_the_break_answers_him(self):
        self.broken_task()
        result = self.asked_by_hub()
        sess = self.st["sessions"]["analyst"]
        self.assertTrue(self.answered(result))
        self.assertEqual(sess["reply_head"], PLAN)
        self.assertFalse(sess.get("turn_owner") or sess.get("turn_his"))   # not his word

    def test_the_report_on_the_cores_continue_after_the_break_answers_him(self):
        self.broken_task()
        self.seq += 1   # the core's own word to the session, as it makes them (a continuation, a reminder)
        msg = reducer.new_message(self.st, f"m{self.seq}", "analyst", "Continue where you stopped.", self.now,
                                  origin=forms.ORIGIN_CORE)
        self.assertTrue(forms.is_core(msg))
        self.assertTrue(self.answered(self.turn("analyst", msg["id"], PLAN)))

    def test_a_core_note_about_another_sessions_hand_in_is_not_the_report(self):
        self.broken_task()
        self.seq += 1
        msg = reducer.new_message(self.st, f"m{self.seq}", "analyst", "An executor handed in: record the lessons.",
                                  self.now, origin=forms.ORIGIN_CORE, lessons_of="d1")
        self.assertTrue(delegations._service(msg))
        self.assertFalse(self.answered(self.turn("analyst", msg["id"], "Recorded the lessons in CLAUDE.md, three lines")))

    def test_long_after_the_break_it_is_on_its_own(self):
        self.broken_task()
        result = self.asked_by_hub(after=delegations.BREAK_WINDOW + 60)
        self.assertEqual(self.st["sessions"]["analyst"].get("owner_out"), result)   # new, as before
        self.assertFalse(self.answered(result))

    def test_a_short_line_or_a_pass_on_line_is_not_an_answer(self):
        self.broken_task()
        self.assertFalse(self.answered(self.asked_by_hub("Done, all good")))
        self.assertFalse(self.answered(self.asked_by_hub("— Done 42 of 42, 0 in progress, nothing waits")))

    def test_only_the_first_report_after_the_break_and_only_on_the_word_of_who_gave_the_task(self):
        self.broken_task()
        note = self.ev("message.received", None, to="analyst", text="Unrelated: check the logs", **S, sender="web")
        self.assertFalse(self.answered(self.turn("analyst", f"m{note}", "The logs are clean, nothing to worry about")))
        self.assertTrue(self.answered(self.asked_by_hub()))
        self.assertFalse(self.answered(self.asked_by_hub("And one more thing about the tiles, unrelated to the plan")))

    def test_a_note_written_before_the_break_is_not_the_report(self):
        # the hub's note queued before the turn broke, read after it: about something else, not the report
        word = f"m{self.ev('message.received', None, src='web', to='hub', text='Ask the analyst for the plan', channel='web', owner=True)}"
        self.ev("turn.started", "hub", src="hook", marker=word)
        did = self.ev("delegation.created", None, session="analyst", **{"for": word}, context="the plan", **S,
                      sender="hub")
        self.ev("turn.result", "hub", src="hook", text="Gave it to the analyst")
        self.ev("turn.stopped", "hub", src="hook")
        note = self.ev("message.received", None, to="analyst", text="Also: the logs rotate tonight", **S, sender="hub")
        self.turn("analyst", f"m{did}", "API Error: 500", is_error=True)
        self.assertEqual(self.st["delegations"][f"d{did}"]["state"], "stopped")
        self.assertFalse(self.answered(self.turn("analyst", f"m{note}", "Noted: the logs rotate tonight, nothing to do")))
        self.assertTrue(self.answered(self.asked_by_hub()))   # the report on the hub's later question still counts

    def test_his_own_task_with_no_assigner_takes_no_word_of_another_hub(self):
        word = f"m{self.ev('message.received', None, src='web', to='analyst', text='Make the plan', channel='web', owner=True)}"
        did = self.ev("delegation.created", None, session="analyst", **{"for": word}, context="the plan",
                      channel="web", owner=True)
        self.assertFalse(self.st["delegations"][f"d{did}"].get("by"))
        self.turn("analyst", f"m{did}", "API Error: 500", is_error=True)
        self.assertEqual(self.st["delegations"][f"d{did}"]["state"], "stopped")
        self.seq += 1   # words with no named session: another hub's, as the core delivers them
        msg = reducer.new_message(self.st, f"m{self.seq}", "analyst", "From another hub: is the plan ready?", self.now,
                                  origin="from core, hub bus", channel="hubbus")
        self.assertFalse(self.answered(self.turn("analyst", msg["id"], PLAN)))

    def test_a_later_task_takes_the_session_on(self):
        self.broken_task()
        self.ev("delegation.created", None, session="analyst", for_session="web", context="logs", **S, sender="web")
        self.assertFalse(self.answered(self.asked_by_hub()))

    def test_a_task_a_session_set_is_reported_to_it_not_to_him(self):
        word = f"m{self.ev('message.received', None, src='web', to='web', text='Get the plan', channel='web', owner=True)}"
        self.ev("turn.started", "web", src="hook", marker=word)
        did = self.ev("delegation.created", None, session="analyst", for_session="web", **{"for": word},
                      context="the plan", **S, sender="web")   # his words passed on (--for), the task is web's
        self.assertEqual(self.st["delegations"][f"d{did}"]["for"], word)
        self.turn("analyst", f"m{did}", "API Error: 500", is_error=True)
        self.assertEqual(self.st["delegations"][f"d{did}"]["state"], "stopped")
        self.assertFalse(self.answered(self.asked_by_hub()))

    def test_a_task_answered_as_usual_is_not_touched(self):
        word = f"m{self.ev('message.received', None, src='web', to='hub', text='Ask the analyst', channel='web', owner=True)}"
        self.ev("turn.started", "hub", src="hook", marker=word)
        did = self.ev("delegation.created", None, session="analyst", **{"for": word}, context="the plan", **S,
                      sender="hub")
        self.assertTrue(self.answered(self.turn("analyst", f"m{did}", PLAN)))   # the answer to his task, as before
        self.assertFalse(self.answered(self.asked_by_hub("Unrelated: the logs are clean, nothing to worry about")))


if __name__ == "__main__":
    unittest.main()
