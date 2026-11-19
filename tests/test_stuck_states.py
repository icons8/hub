"""States the core did not leave without a restart: a handoff finished without HANDOFF-DONE; an attachment with no path;
a task "waiting for the owner" after the question was withdrawn; a rename that left executors, requests and reminders
under the old name."""
import tests  # noqa: F401 — hide the machine's settings
import unittest

from core import reducer

OWNER = {"channel": "cli", "owner": True}
Q = {"questions": [{"question": "Where to?", "options": [{"label": "Left"}, {"label": "Right"}]}]}


def by(sender):
    return {"channel": "session", "owner": False, "sender": sender}


class Fleet(unittest.TestCase):
    """The hub and two sessions, a and b, alive and free; self.now moves the event time."""

    def setUp(self):
        self.st, self.seq, self.now, self.actions = reducer.empty(), 0, 1000, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        for n in ("hub", "a", "b"):
            if n != "hub":
                self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", **OWNER)
            self.ev("session.started", n, session_id=f"s-{n}")

    def ev(self, type_, entity=None, **data):
        self.seq += 1
        self.now += 1
        self.st, self.actions, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.now, "type": type_,
                                                              "entity": entity, "data": data})
        return why

    def typed(self):
        return [a["text"] for a in self.actions if a.get("do") == "type"]

    def fire_due(self):
        """Like hubd: every timeout that comes due is a timer.fired."""
        for deadline, tid, attempt in reducer.pending_timers(self.st):
            if deadline <= self.now:
                self.ev("timer.fired", tid, attempt=attempt, n=attempt)

    def to_hub(self):
        return [m["text"] for m in self.st["messages"].values() if m["session"] == "hub"]


class HandoverWithoutTheMark(Fleet):
    def handover(self, name="b"):
        self.assertIsNone(self.ev("session.close_requested", name, **OWNER))
        self.assertEqual(self.st["sessions"][name]["closing"], "handoff")
        (mid,) = [m["id"] for m in self.st["messages"].values()
                  if m["session"] == name and m["text"].startswith(reducer.HANDOFF_MARK) and m["state"] == "typing"]
        self.ev("turn.started", name, marker=mid, prompt_head="")
        return mid

    def test_a_handover_turn_without_the_mark_ends_by_the_timer_and_the_hub_is_told(self):
        self.handover()
        self.ev("turn.result", "b", text="All fine.")
        self.ev("turn.stopped", "b")
        self.assertEqual(self.st["sessions"]["b"]["closing"], "handoff")   # not cleared at once: the result may lag behind
        self.now += reducer.HANDOFF_WAIT
        self.fire_due()
        sess = self.st["sessions"]["b"]
        self.assertEqual((sess["closing"], sess["state"]), ("none", "idle"))
        told = [t for t in self.to_hub() if reducer.DONE_MARK in t and "b" in t]
        self.assertEqual(len(told), 1, self.to_hub())
        self.assertIn("All fine.", told[0])
        self.assertEqual([t for t in reducer.pending_timers(self.st) if str(t[1]).startswith("h:")], [])
        # putting to sleep again is possible, not "handing over" until a restart
        self.handover()

    def test_a_long_handover_turn_is_not_cut(self):
        self.handover()
        for _ in range(3):   # the turn runs longer than the timeout: the timeout is postponed, the handoff is not cleared
            self.now += reducer.HANDOFF_WAIT
            self.fire_due()
            self.assertEqual(self.st["sessions"]["b"]["closing"], "handoff")
        self.ev("turn.result", "b", text=reducer.DONE_MARK + " all handed over")
        self.assertEqual((self.st["sessions"]["b"]["state"], self.st["sessions"]["b"]["closing"]), ("asleep", "none"))

    def test_a_result_of_the_turn_before_the_handover_keeps_it(self):
        # a turn result before the handoff came when the handoff turn had already started (hooks come through the spool,
        # results from the process)
        self.ev("turn.started", "b", marker=None, prompt_head="work")
        self.assertIsNone(self.ev("session.close_ready", "b", **by("b")))
        self.assertEqual(self.st["sessions"]["b"]["closing"], "ready")
        self.ev("turn.stopped", "b")
        self.assertEqual(self.st["sessions"]["b"]["closing"], "handoff")
        mid = next(m["id"] for m in self.st["messages"].values() if m["text"].startswith(reducer.HANDOFF_MARK))
        self.ev("turn.started", "b", marker=mid, prompt_head="")
        self.ev("turn.result", "b", text="work done")    # a late result of the previous turn
        self.ev("turn.result", "b", text=reducer.DONE_MARK + " handed over")
        self.assertEqual(self.st["sessions"]["b"]["state"], "asleep")

    def test_the_hub_is_not_told_the_late_result_of_the_turn_before(self):
        self.ev("turn.started", "b", marker=None, prompt_head="work")
        self.assertIsNone(self.ev("session.close_ready", "b", **by("b")))
        self.ev("turn.stopped", "b")
        mid = next(m["id"] for m in self.st["messages"].values() if m["text"].startswith(reducer.HANDOFF_MARK))
        self.ev("turn.started", "b", marker=mid, prompt_head="")
        self.ev("turn.result", "b", text="work done")    # a late result of the previous turn
        self.ev("turn.result", "b", text="")                  # the handoff turn: no text and no mark
        self.ev("turn.stopped", "b")
        self.now += reducer.HANDOFF_WAIT
        self.fire_due()
        (told,) = [t for t in self.to_hub() if reducer.DONE_MARK in t]
        self.assertNotIn("work done", told)

    def test_the_mark_a_moment_after_the_turn_still_puts_it_to_sleep(self):
        self.handover()
        self.now += reducer.HANDOFF_WAIT - 5
        self.ev("turn.stopped", "b")
        self.now += 5
        self.fire_due()                          # just became free: the result may lag behind, not cleared yet
        self.assertEqual(self.st["sessions"]["b"]["closing"], "handoff")
        self.ev("turn.result", "b", text=reducer.DONE_MARK + " handed over")
        self.assertEqual(self.st["sessions"]["b"]["state"], "asleep")

    def test_a_late_timer_after_sleep_does_nothing(self):
        self.handover()
        self.ev("turn.result", "b", text=reducer.DONE_MARK + " handed over")
        before = len(self.to_hub())
        self.now += 10 * reducer.HANDOFF_WAIT
        self.assertIsNone(self.ev("timer.fired", "h:b", attempt=None, n=None))
        self.assertEqual(len(self.to_hub()), before)
        self.assertEqual(self.st["sessions"]["b"]["state"], "asleep")


class Attachments(Fleet):
    def test_an_attachment_without_a_path_does_not_jam_the_core(self):
        self.assertIsNone(self.ev("message.received", None, to="b", text="look",
                                  attachments=[{"name": "x.png"}, "junk", {"path": "/tmp/a.png", "size": "big"}],
                                  **OWNER))
        (text,) = self.typed()
        self.assertIn("/tmp/a.png", text)
        self.assertNotIn("x.png", text)
        self.assertIsNone(self.ev("turn.started", "a", marker=None, prompt_head="next"))
        self.assertIsNone(self.ev("message.received", None, to="a", text="more", **OWNER))

    def test_an_older_state_with_such_an_attachment_is_typed(self):
        # an earlier core could store this in a queue message: printing cleans it instead of crashing
        self.assertIsNone(self.ev("turn.started", "b", marker=None, prompt_head="busy"))
        self.assertIsNone(self.ev("message.received", None, to="b", text="look", **OWNER))
        (msg,) = [m for m in self.st["messages"].values() if m["session"] == "b" and m["state"] == "queued"]
        msg["attachments"] = [{"name": "x.png"}, "junk", {"path": "/a", "size": "?"}]
        self.assertIsNone(self.ev("turn.stopped", "b"))
        (text,) = self.typed()
        self.assertIn("- /a (, 0 KB)", text)
        self.assertNotIn("x.png", text)


class WithdrawnQuestion(Fleet):
    def test_the_task_is_in_work_again(self):
        self.assertIsNone(self.ev("delegation.created", None, session="b", context="take a look", **by("hub")))
        (d,) = self.st["delegations"].values()
        self.ev("turn.started", "b", marker=d["message"], prompt_head="")
        self.ev("permission.requested", "b", request_id="r1", tool="AskUserQuestion", interactive=True, input=Q)
        self.assertEqual(self.st["delegations"][d["id"]]["state"], "waiting-owner")
        qid = self.st["sessions"]["b"]["asks"][-1]["id"]
        self.assertIsNone(self.ev("question.withdrawn", "b", id=qid, **by("b")))
        self.assertEqual(self.st["delegations"][d["id"]]["state"], "working")

    def test_another_open_question_of_the_task_keeps_it_waiting(self):
        self.assertIsNone(self.ev("delegation.created", None, session="b", context="take a look", **by("hub")))
        (d,) = self.st["delegations"].values()
        self.ev("turn.started", "b", marker=d["message"], prompt_head="")
        self.ev("permission.requested", "b", request_id="r1", tool="AskUserQuestion", interactive=True, input=Q)
        self.ev("permission.requested", "b", request_id="r2", tool="AskUserQuestion", interactive=True, input=Q)
        qid = self.st["sessions"]["b"]["asks"][0]["id"]
        self.assertIsNone(self.ev("question.withdrawn", "b", id=qid, **by("b")))
        self.assertEqual(self.st["delegations"][d["id"]]["state"], "waiting-owner")

    def test_a_pending_permission_keeps_it_waiting(self):
        self.assertIsNone(self.ev("delegation.created", None, session="b", context="take a look", **by("hub")))
        (d,) = self.st["delegations"].values()
        self.ev("turn.started", "b", marker=d["message"], prompt_head="")
        self.ev("permission.requested", "b", request_id="r1", tool="AskUserQuestion", interactive=True, input=Q)
        self.ev("permission.requested", "b", request_id="r2", tool="Bash", input={"command": "ls"})
        self.assertEqual(self.st["sessions"]["b"]["state"], "dialog")
        qid = self.st["sessions"]["b"]["asks"][-1]["id"]
        self.assertIsNone(self.ev("question.withdrawn", "b", id=qid, **by("b")))
        self.assertEqual(self.st["delegations"][d["id"]]["state"], "waiting-owner")


class RenameCarriesEverything(Fleet):
    def test_executors_requests_cards_and_reminders_follow_the_name(self):
        self.st["sessions"]["b"].update(role="executor", raised_by="a", bus="a")
        self.st["runs"] = {"x": {"by": "a", "task": "t", "repo": None, "ts": 1, "state": "queued", "why": "no room"}}
        self.ev("permission.requested", "a", request_id="r1", tool="AskUserQuestion", interactive=True, input=Q)
        self.ev("turn.stopped", "a")
        self.st["owner"]["reminders"]["q:a:r1"] = 1
        self.assertIsNone(self.ev("session.renamed", "a", to="api", **OWNER))
        self.assertEqual(self.st["sessions"]["b"]["raised_by"], "api")
        self.assertEqual(self.st["sessions"]["b"]["bus"], "api")     # an executor of a mini-hub: the questions go to it
        self.assertEqual(self.st["runs"]["x"]["by"], "api")
        self.assertEqual(self.st["owner"]["reminders"], {"q:api:r1": 1})
        self.assertTrue(self.st["sessions"]["api"]["asks"])


if __name__ == "__main__":
    unittest.main()
