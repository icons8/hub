"""A turn started by a core service message is not the answer to the session's task.

A session can keep a task between turns: its turn on the task ended with no text (it only ran tools) and the task stays
"in work", so the session's next turn answers it. When that next turn is started by a core service message that is about
no task (the "executor handed in, record the lessons" note, a removal note), its reply ("Already recorded. —") is about
the note: it must not become the task's result, close the task or go to whoever set it (REPLIED). The task stays open
and the session's next real turn answers it."""
import tests  # noqa: F401 — ignore this machine's settings
import unittest

from core import executors, forms, reducer

OWNER = {"channel": "cli", "owner": True}


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


class ServiceTurn(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.started", "hub")
        self.ev("session.requested", "frontend", cwd="/fe", session_id="s-fe", role="agent", about="Frontend", **OWNER)
        self.ev("session.started", "frontend")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        self.assertIsNone(why, f"{type}: {why}")
        return f"m{self.seq}"

    def turn(self, name, marker, text):
        self.ev("turn.started", name, marker=marker, prompt_head="x")
        self.ev("turn.result", name, text=text)
        self.ev("turn.stopped", name)

    def to(self, name, pred=lambda m: True):
        return [m for m in self.st["messages"].values() if m["session"] == name and pred(m)]

    def owners_task(self):
        """The owner writes to the hub, the hub gives it to frontend; frontend raises an executor and its turn ends
        with no text: the task stays in work, carried to frontend's next turn."""
        m = self.ev("message.received", None, to="hub", text="fix the CI", **OWNER)
        self.turn("hub", m, "Passed to frontend.")
        self.ev("delegation.created", None, session="frontend", **{"for": m}, context="fix the CI", **by("hub"))
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend"]
        self.ev("turn.started", "frontend", marker=d["message"], prompt_head="x")
        self.ev("task.run_requested", "frontend-ci", task="fix the CI secrets", **by("frontend"))
        self.ev("turn.result", "frontend", text="")
        self.ev("turn.stopped", "frontend")
        self.assertEqual(self.task(d["id"])["state"], "working")
        return d["id"]

    def hand_in(self):
        """The executor reports and hands in: its result waits for frontend, the core sends the lessons note."""
        run = self.st["runs"]["frontend-ci"]
        for type, entity, data in executors.events_for(self.st, "frontend-ci", run, cwd="/fe/w", worktree=None):
            self.ev(type, entity, **data)
        (sub,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ci"]
        self.ev("session.started", "frontend-ci")
        self.turn("frontend-ci", sub["message"], "Secrets moved to the vault, CI green. Lessons: CI secrets only via vault")
        self.ev("session.close_requested", "frontend-ci", retire=True, channel="core")
        (note,) = self.to("frontend", lambda m: forms.is_core(m) and "lessons" in m["text"].lower())
        (result,) = self.to("frontend", lambda m: m.get("task") == sub["id"])
        return note, result

    def task(self, did):
        return self.st["delegations"][did]

    def replied(self, text):
        return self.to("hub", lambda m: m["text"].startswith("REPLIED") and text in m["text"])

    def test_the_reply_to_a_lessons_note_does_not_answer_the_owners_task(self):
        did = self.owners_task()
        note, result = self.hand_in()
        self.turn("frontend", note["id"], "Already recorded in my notes.\n\n—")
        self.assertEqual(self.task(did)["state"], "working")       # still open: the note is not the task
        self.assertNotIn("Already recorded", self.task(did).get("reply") or "")
        self.assertEqual(self.replied("Already recorded"), [])         # nothing to the hub under the owner's task
        self.assertEqual(self.to("frontend", lambda m: "Task closed" in m["text"]), [])
        self.ev("turn.result", "frontend", text="Already recorded, late copy.")   # hubd's late second result of that turn
        self.assertEqual(self.task(did)["state"], "working")
        # the session's next real turn answers the task, as before the note
        self.turn("frontend", result["id"], "CI fixed: secrets are in the vault.")
        self.assertEqual(self.task(did)["state"], "replied")
        self.assertIn("CI fixed", self.task(did)["reply"])
        self.assertEqual(len(self.replied("CI fixed")), 1)

    def test_a_label_less_turn_after_the_note_still_answers_the_task(self):
        # a background helper finished after the note turn (no label): the carried task is still the session's
        did = self.owners_task()
        note, _ = self.hand_in()
        self.turn("frontend", note["id"], "Already recorded.")
        self.ev("turn.started", "frontend", prompt_head="background task finished")
        self.ev("turn.result", "frontend", text="CI fixed by the background run.")
        self.ev("turn.stopped", "frontend")
        self.assertEqual(self.task(did)["state"], "replied")
        self.assertIn("background run", self.task(did)["reply"])

    def test_an_urgent_task_inserted_into_the_note_turn_does_not_drop_the_carried_one(self):
        # the hub's urgent task (`--now`) goes into the note's turn: that turn answers the urgent task, and the carried
        # one is still set aside for the next turn, not lost
        did = self.owners_task()
        note, _ = self.hand_in()
        self.ev("turn.started", "frontend", marker=note["id"], prompt_head="x")
        m = self.ev("message.received", None, to="hub", text="prod is down", **OWNER)
        self.ev("delegation.created", None, session="frontend", **{"for": m}, context="prod is down", now=True,
                **by("hub"))
        (urgent,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend" and d["id"] != did]
        self.assertTrue(self.st["messages"][urgent["message"]].get("injected"))
        self.ev("turn.started", "frontend", marker=urgent["message"], prompt_head="x")
        self.ev("turn.result", "frontend", text="Prod is back up.")
        self.ev("turn.stopped", "frontend")
        self.assertEqual((self.task(urgent["id"])["state"], self.task(did)["state"]), ("replied", "working"))
        asked = self.ev("message.received", None, to="frontend", text="how is it going?", **OWNER)
        self.turn("frontend", asked, "CI fixed: secrets are in the vault.")
        self.assertEqual(self.task(did)["state"], "replied")
        self.assertIn("CI fixed", self.task(did)["reply"])

    def urgent(self, text):
        """The hub's urgent task (`--now`) to frontend, busy: it goes into the running turn."""
        m = self.ev("message.received", None, to="hub", text=text, **OWNER)
        self.ev("delegation.created", None, session="frontend", **{"for": m}, context=text, now=True, **by("hub"))
        d = max((d for d in self.st["delegations"].values() if d["session"] == "frontend"), key=lambda d: d["created"])
        self.assertTrue(self.st["messages"][d["message"]].get("injected"))
        return d

    def test_an_urgent_task_carried_with_the_task_is_set_aside_too(self):
        # an urgent task went into the task's turn, and that turn ended with no text: both are carried, and the
        # reply to the note answers neither
        did = self.owners_task()
        self.ev("turn.started", "frontend", prompt_head="background task finished")
        urgent = self.urgent("prod is down")
        self.ev("turn.started", "frontend", marker=urgent["message"], prompt_head="x")
        self.ev("turn.result", "frontend", text="")
        self.ev("turn.stopped", "frontend")
        note, _ = self.hand_in()
        self.turn("frontend", note["id"], "Already recorded in my notes.")
        self.assertEqual((self.task(did)["state"], self.task(urgent["id"])["state"]), ("working", "working"))
        self.assertEqual(self.replied("Already recorded"), [])
        asked = self.ev("message.received", None, to="frontend", text="how is it going?", **OWNER)
        self.turn("frontend", asked, "CI fixed and prod is back up.")
        self.assertEqual((self.task(did)["state"], self.task(urgent["id"])["state"]), ("replied", "replied"))

    def test_a_reminder_on_another_task_inserted_into_the_note_turn_is_that_tasks_turn(self):
        # the core's reminder about another task of the session comes mid-turn of the note: the turn is that task's,
        # the set-aside one waits for the next turn
        did = self.owners_task()
        note, result = self.hand_in()
        m = self.ev("message.received", None, to="hub", text="rotate the logs", **OWNER)
        self.ev("delegation.created", None, session="frontend", **{"for": m}, context="rotate the logs", **by("hub"))
        other = max((d for d in self.st["delegations"].values() if d["session"] == "frontend"), key=lambda d: d["created"])
        self.ev("turn.started", "frontend", marker=note["id"], prompt_head="x")
        mid = f"m{self.seq}x9"
        reducer.new_message(self.st, mid, "frontend", "Task is still open — what is its state?", 1000 + self.seq,
                            origin=forms.ORIGIN_CORE, for_delegation=other["id"])
        self.st["messages"][mid]["injected"] = True
        self.ev("turn.started", "frontend", marker=mid, prompt_head="x")
        self.ev("turn.result", "frontend", text="Logs rotated.")
        self.ev("turn.stopped", "frontend")
        self.assertEqual((self.task(other["id"])["state"], self.task(did)["state"]), ("replied", "working"))
        self.assertIn("Logs rotated", self.task(other["id"])["reply"])
        self.turn("frontend", result["id"], "CI fixed: secrets are in the vault.")
        self.assertEqual(self.task(did)["state"], "replied")

    def test_a_task_closed_while_set_aside_is_not_taken_back(self):
        did = self.owners_task()
        note, _ = self.hand_in()
        self.turn("frontend", note["id"], "Already recorded.")
        self.ev("delegation.done", did, **by("hub"))
        state = self.task(did)["state"]
        asked = self.ev("message.received", None, to="frontend", text="what about the docs?", **OWNER)
        self.turn("frontend", asked, "The docs are up to date.")
        self.assertEqual(self.task(did)["state"], state)
        self.assertNotIn("docs", self.task(did).get("reply") or "")
        self.assertNotIn("service_held", self.st["sessions"]["frontend"])

    def test_a_second_connected_note_still_answers_the_task(self):
        # two tools connected one after the other while the task is in work: the second "Connected" carries no task
        # and is not the task's message (continued() leaves an already working task on the first), yet it is about
        # the task (a week of a real journal: the executor's report on it would not have reached whoever set the task)
        did = self.owners_task()
        mid = f"m{self.seq}x2"
        reducer.new_message(self.st, mid, "frontend", "Connected: vault. The session was restarted with the same "
                            "conversation — continue the task this was needed for.", 1000 + self.seq,
                            origin=forms.ORIGIN_CORE)
        self.assertNotEqual(self.task(did)["message"], mid)
        self.turn("frontend", mid, "CI fixed: secrets are in the vault.")
        self.assertEqual(self.task(did)["state"], "replied")
        self.assertEqual(len(self.replied("CI fixed")), 1)

    def test_a_note_turn_that_took_the_executors_result_too_answers_the_task(self):
        # Claude Code typed the note and the executor's result as one prompt (the turn's `also`): the reply may be
        # about the task, so it answers it as before
        did = self.owners_task()
        note, result = self.hand_in()
        self.ev("turn.started", "frontend", marker=note["id"], prompt_head="x", also=[result["id"]])
        self.ev("turn.result", "frontend", text="CI fixed: secrets are in the vault. Lessons recorded.")
        self.ev("turn.stopped", "frontend")
        self.assertEqual(self.task(did)["state"], "replied")
        self.assertEqual(len(self.replied("CI fixed")), 1)

    def test_a_core_message_about_the_task_still_answers_it(self):
        # the core's reminder about this very task (for_delegation) is the task's turn: its result is the answer
        did = self.owners_task()
        mid = f"m{self.seq}x9"
        reducer.new_message(self.st, mid, "frontend", "Task is still open — what is its state?", 1000 + self.seq,
                            origin=forms.ORIGIN_CORE, for_delegation=did)
        self.turn("frontend", mid, "CI fixed, here is the result.")
        self.assertEqual(self.task(did)["state"], "replied")
        self.assertEqual(len(self.replied("CI fixed")), 1)


if __name__ == "__main__":
    unittest.main()
