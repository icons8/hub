"""Stuck tasks (the hub communication direction, slice 7; the owner's complaint: sessions do not report what they are
doing). A task is in progress, but the executor is free and waits for nothing (the turn ended without a result, the
task was not picked up): the core nudges it with a ladder (delegations.NUDGES); after the last step it sends the hub
one fact (TIMEOUT) and the hub decides. A busy session gets nothing until its turn ends; a session waiting for the
owner (a card, a "yes" for a tool) is not stuck; a nudge does not wake a sleeping one. Nothing new for the owner:
the turn result after a nudge is an ordinary reply to him.
The clock works as in hubd: pending_timers -> timer.fired."""
import tests  # noqa: F401 — ignore the machine's settings
import unittest

from core import delegations, forms, machines, reducer
from tests.test_limit_reset_resume import CORE, LIMIT_TEXT

T0 = 1_000_000.0
MIN = 60


class Ladder(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.now = reducer.empty(), 0, T0
        self.ev("session.requested", "hub", cwd="/w", session_id="sid-hub", kind="hub", channel="cli", owner=True)
        self.ev("session.started", "hub", session_id="sid-hub")
        self.ev("session.requested", "w", cwd="/w", session_id="sid-w", channel="cli", owner=True)
        self.ev("session.started", "w", session_id="sid-w")
        self.mid = self.ev("message.received", None, text="fix the build", channel="web", owner=True, to="hub")
        self.ev("turn.started", "hub", marker=self.mid)
        ev = self.ev("delegation.created", None, session="w", **{"for": self.mid}, channel="session", owner=False,
                     sender="hub")
        self.did, self.task = f"d{ev}", f"m{ev}"
        self.ev("turn.result", "hub", text="—")
        self.ev("turn.stopped", "hub")

    # --- helpers -----------------------------------------------------------------------------------------------
    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.now, "type": type, "entity": entity,
                                                   "data": data})
        self.assertIsNone(why, f"{type}: {why}")
        return f"m{self.seq}" if type == "message.received" else self.seq

    def at(self, minutes):
        """The clock reaches T0 + minutes: deadlines fire as in hubd.fire_timers (one per tick)."""
        end = T0 + minutes * MIN
        while True:
            due = [t for t in reducer.pending_timers(self.st) if t[0] <= end]
            if not due:
                break
            deadline, ident, attempt = due[0]
            self.now = max(self.now, deadline)
            self.ev("timer.fired", ident, attempt=attempt, n=attempt)
        self.now = end

    def turn(self, name, marker, text):
        self.ev("turn.started", name, marker=marker)
        self.ev("turn.result", name, text=text)
        self.ev("turn.stopped", name)

    def to(self, name, prefix=""):
        return [m for m in self.st["messages"].values() if m["session"] == name and m["origin"] == forms.ORIGIN_CORE
                and m["text"].startswith(prefix)]

    def reminders(self):
        """Nudges of the "in progress without a result" ladder (not "open after a reply": those are in OpenAfterAnswer)."""
        return [m for m in self.to("w") if m.get("for_delegation") == self.did and " is open, " not in m["text"]]

    def idle_turn(self, marker, minutes):
        """A turn by marker lasting `minutes`, without a result."""
        self.ev("turn.started", "w", marker=marker)
        self.now += minutes * MIN
        self.ev("turn.result", "w", text="")
        self.ev("turn.stopped", "w")
        return (self.now - T0) / MIN


class StuckTasks(Ladder):
    def test_idle_after_an_empty_turn_reminders_then_one_fact_to_the_hub(self):
        idle = self.idle_turn(self.task, 2)    # the turn ended without a result: the task is in progress, the session is free
        steps = [s / MIN for s in delegations.NUDGES]
        self.at(idle + steps[0] - 0.5)
        self.assertEqual(self.reminders(), [])
        for i, step in enumerate(steps[:-1], 1):
            self.at(idle + step)
            self.assertEqual(len(self.reminders()), i, f"step {i}")
            self.assertEqual(self.to("hub", f"TIMEOUT {self.did}"), [])
            # the nudge was taken, the turn on it again ended without a result: the next step counts from its end
            idle = self.idle_turn(self.reminders()[-1]["id"], 1)
        text = self.reminders()[0]["text"]
        self.assertIn(self.did, text)
        self.assertIn("from the owner", text)
        self.assertIn("the last reminder", self.reminders()[-1]["text"])
        self.at(idle + steps[-1] - 0.5)
        self.assertEqual(self.to("hub", f"TIMEOUT {self.did}"), [])
        self.at(idle + steps[-1])
        (fact,) = self.to("hub", f"TIMEOUT {self.did}")
        self.assertIn("reminders without an answer", fact["text"])
        self.at(idle + 600)                    # then silence: the hub decides; the 90-minute TIMEOUT is not needed a second time
        self.assertEqual(len(self.reminders()), len(delegations.NUDGES) - 1)
        self.assertEqual(len(self.to("hub", f"TIMEOUT {self.did}")), 1)
        self.assertEqual(self.st["delegations"][self.did]["state"], "working")

    def test_answer_after_a_reminder_is_the_reply_to_the_owner(self):
        self.now += 1
        self.turn("w", self.task, "")
        self.at(delegations.NUDGES[0] // MIN + 1)
        (r,) = self.reminders()
        self.turn("w", r["id"], "Build fixed: PR #12")
        d = self.st["delegations"][self.did]
        self.assertEqual((d["state"], d["reply"]), ("replied", "Build fixed: PR #12"))
        self.assertEqual(len(self.to("hub", f"REPLIED {self.did}")), 1)
        self.at(600)
        self.assertEqual(len(self.reminders()), 1)
        self.assertEqual(self.to("hub", f"TIMEOUT {self.did}: w is free"), [])

    def test_never_picked_up_idle_executor_is_reminded(self):
        # the task message arrived (the turn started by marker), but Claude Code finished without a result and without a
        # Stop hook: the session is free
        self.ev("turn.started", "w", marker=self.task)
        self.ev("turn.stopped", "w")
        self.at(delegations.NUDGES[0] // MIN + 1)
        self.assertEqual(len(self.reminders()), 1)

    def test_busy_on_the_task_gets_nothing_until_the_turn_ends(self):
        self.ev("turn.started", "w", marker=self.task)
        self.at(89)
        self.assertEqual(self.reminders(), [])
        self.at(91)                            # as before: 90 minutes without a result, one TIMEOUT to the hub
        (fact,) = self.to("hub", f"TIMEOUT {self.did}")
        self.assertIn("90 minutes", fact["text"])
        self.at(300)
        self.assertEqual(self.reminders(), [])
        self.assertEqual(len(self.to("hub", f"TIMEOUT {self.did}")), 1)

    def test_turn_end_starts_the_idle_clock(self):
        self.ev("turn.started", "w", marker=self.task)
        self.at(30)
        self.ev("turn.result", "w", text="")
        self.ev("turn.stopped", "w")
        self.at(30 + delegations.NUDGES[0] // MIN - 1)
        self.assertEqual(self.reminders(), [])
        self.at(30 + delegations.NUDGES[0] // MIN + 1)
        self.assertEqual(len(self.reminders()), 1)

    def test_waiting_on_the_owners_card_is_not_stuck(self):
        self.ev("turn.started", "w", marker=self.task)
        self.ev("permission.requested", "w", request_id="r1", tool="AskUserQuestion",
                input={"questions": [{"question": "Which option?", "options": [{"label": "A"}, {"label": "B"}]}]},
                interactive=True)
        self.assertEqual(self.st["delegations"][self.did]["state"], "waiting-owner")
        self.at(300)
        self.assertEqual(self.reminders(), [])

    def test_waiting_for_the_owners_yes_on_a_tool_is_not_stuck(self):
        self.ev("turn.started", "w", marker=self.task)
        self.st["sessions"]["w"]["requests"] = [{"tool": "jira", "state": "needs-owner", "since": self.now}]
        self.ev("turn.result", "w", text="")
        self.ev("turn.stopped", "w")
        self.at(80)
        self.assertEqual(self.reminders(), [])

    def test_a_question_deferred_while_the_owner_is_away_holds_only_until_he_returns(self):
        # the owner is away: a tool's "yes" window is deferred (owner.deferred), the session goes on and ends the turn
        # without a result; while he is away the executor waits for him, not stuck
        self.ev("owner.away", None, channel="web", owner=True)
        self.ev("turn.started", "w", marker=self.task)
        self.ev("permission.requested", "w", request_id="r1", tool="Bash", input={"command": "deploy"},
                interactive=True)
        self.assertEqual(len(self.st["owner"]["deferred"]), 1)
        self.ev("turn.result", "w", text="")
        self.ev("turn.stopped", "w")
        self.at(80)
        self.assertEqual(self.reminders(), [])
        # he is back: the question went to him in the return summary and nothing will answer it in the session; the
        # record stays until his next absence, but no longer means the executor waits — the ladder counts from his
        # return (not from the turn's end: that would nudge the minute he opens the summary)
        self.ev("owner.back", None, channel="web", owner=True)
        self.assertEqual(len(self.st["owner"]["deferred"]), 1)
        self.at(80 + delegations.NUDGES[0] // MIN - 0.5)
        self.assertEqual(self.reminders(), [])
        self.at(80 + delegations.NUDGES[0] // MIN)
        self.assertEqual(len(self.reminders()), 1)

    def test_queued_message_means_not_stuck_yet(self):
        # the task is still queued (the session did not take it): that is not executor idle time; the watcher reports
        # undelivered messages itself
        self.assertTrue(any(m["id"] == self.task and m["state"] in ("queued", "typing")
                            for m in self.st["messages"].values()))
        self.st["messages"][self.task]["deadline"] = None   # the typing confirmation did not fire and is not repeated
        self.at(80)
        self.assertEqual(self.reminders(), [])

    def test_background_work_is_not_stuck(self):
        self.ev("turn.started", "w", marker=self.task)
        self.ev("session.background", "w", id="b1", kind="bash", title="full test run")
        self.ev("turn.result", "w", text="")
        self.ev("turn.stopped", "w")
        self.at(80)
        self.assertEqual(self.reminders(), [])

    def test_an_executor_whose_turn_the_network_cut_is_not_free(self):
        # the core continues the turn itself once the API answers (core/netprobe.py): no reminder in the pause, as with a limit cut
        self.ev("turn.started", "w", marker=self.task)
        self.ev("turn.result", "w", text="API Error: Connection error.", is_error=True)
        self.ev("turn.stopped", "w")
        d, late = self.st["delegations"][self.did], T0 + 80 * MIN
        self.assertTrue(self.st["sessions"]["w"].get("net_cut"))
        self.assertIsNone(delegations.free(self.st, d, now=late))
        self.st["sessions"]["w"].pop("net_cut")   # without the mark the same executor is free
        self.assertIsNotNone(delegations.free(self.st, d, now=late))

    def test_stopped_executor_is_not_woken(self):
        self.ev("turn.started", "w", marker=self.task)
        self.ev("turn.result", "w", text="")
        self.ev("turn.stopped", "w")
        self.st["sessions"]["w"]["role"] = "executor"   # an immediate "stop" goes to an executor only
        self.ev("session.stop_requested", "w", channel="session", owner=False, sender="hub")
        self.at(80)
        self.assertEqual(self.reminders(), [])
        self.assertEqual(self.st["sessions"]["w"]["state"], "retired")

    def test_done_task_gets_nothing(self):
        self.ev("turn.started", "w", marker=self.task)
        self.ev("turn.result", "w", text="")
        self.ev("turn.stopped", "w")
        self.ev("delegation.done", self.did, channel="session", owner=False, sender="hub")
        self.at(300)
        self.assertEqual(self.reminders(), [])

    def test_continued_task_starts_a_fresh_ladder(self):
        self.now += 1
        self.turn("w", self.task, "")
        self.at(delegations.NUDGES[0] // MIN + 1)
        self.assertEqual(self.st["delegations"][self.did]["nudges"], 1)
        self.turn("w", self.reminders()[0]["id"], "waiting for access to jira")
        self.ev("turn.started", "w", marker=None)   # the tool was connected: the core continues the task
        self.st["sessions"]["w"]["state"] = "idle"
        delegations.continued(self.st, "w", "m999", {"ts": self.now}, self.did)
        self.assertEqual(self.st["delegations"][self.did].get("nudges", 0), 0)


class OpenAfterAnswer(Ladder):
    """An interim reply and silence (owner decision 2026-10-08: nudge open tasks that have no `hub wait`). Replay of
    30.09-07.10: an executor replied ("waiting for the pilot to finish, 5-15 minutes"), the task is not closed, the
    executor is free: 64 tasks of 10+ minutes, up to 773 minutes of silence. A task open after a reply, with a free
    executor that waits for nothing (no `hub wait`, no `hub waiting`) 30 min after the end of the turn, gets one nudge
    to the executor; 60 more minutes without change give one fact to the hub (once per task). A new reply or a wait
    resets the count."""
    def setUp(self):
        super().setUp()
        self.OPEN, self.FACT = delegations.OPEN_NUDGE // MIN, delegations.OPEN_FACT // MIN
        self.now += MIN
        self.turn("w", self.task, "Started the pilot, waiting for the end - 5-15 minutes")   # an interim reply, the task is open
        self.t = (self.now - T0) / MIN

    def nudges(self):
        return [m for m in self.to("w") if m.get("for_delegation") == self.did and " is open, " in m["text"]]

    def test_a_question_deferred_while_away_holds_the_open_reminder_until_he_returns(self):
        self.ev("owner.away", None, channel="web", owner=True)
        self.ev("turn.started", "w", marker=None)
        self.ev("permission.requested", "w", request_id="r1", tool="Bash", input={"command": "deploy"},
                interactive=True)
        self.ev("turn.result", "w", text="")
        self.ev("turn.stopped", "w")
        self.assertEqual((len(self.st["owner"]["deferred"]), self.st["delegations"][self.did]["state"]), (1, "replied"))
        back = self.t + self.OPEN + 10
        self.at(back)
        self.assertEqual(self.nudges(), [])
        self.ev("owner.back", None, channel="web", owner=True)
        self.at(back + self.OPEN - 0.5)
        self.assertEqual(self.nudges(), [])
        self.at(back + self.OPEN)
        self.assertEqual(len(self.nudges()), 1)

    def test_a_reminder_sent_before_the_absence_is_not_repeated_after_the_return(self):
        self.at(self.t + self.OPEN)
        self.assertEqual(len(self.nudges()), 1)
        self.ev("owner.away", None, channel="web", owner=True)
        self.ev("turn.started", "w", marker=None)
        self.ev("permission.requested", "w", request_id="r1", tool="Bash", input={"command": "deploy"},
                interactive=True)
        self.ev("turn.result", "w", text="")
        self.ev("turn.stopped", "w")
        self.assertEqual((len(self.st["owner"]["deferred"]), self.st["delegations"][self.did]["state"]), (1, "replied"))
        back = self.t + self.OPEN + self.FACT + 10
        self.at(back)
        self.ev("owner.back", None, channel="web", owner=True)
        self.at(back + self.FACT - 0.5)
        self.assertEqual((len(self.nudges()), self.facts()), (1, []))
        self.at(back + self.FACT)
        self.assertEqual((len(self.nudges()), len(self.facts())), (1, 1))

    def facts(self):
        return self.to("hub", f"TIMEOUT {self.did}: task {self.did} has been silent")

    def test_quiet_after_an_interim_answer_one_reminder_then_one_fact_to_the_hub(self):
        self.assertEqual(self.st["delegations"][self.did]["state"], "replied")
        self.at(self.t + self.OPEN - 1)
        self.assertEqual(self.nudges(), [])
        self.at(self.t + self.OPEN)
        (n,) = self.nudges()
        for part in (f"Task {self.did} is open", f"free for {self.OPEN} min", "hub wait", "in a result"):
            self.assertIn(part, n["text"])
        self.assertEqual(self.facts(), [])
        self.at(self.t + self.OPEN + self.FACT - 1)
        self.assertEqual(self.facts(), [])
        self.at(self.t + self.OPEN + self.FACT)
        (f,) = self.facts()
        self.assertIn(f"silent for {self.OPEN + self.FACT} min", f["text"])
        # the fact leads the hub to an action and keeps it away from the owner
        for part in ("check", "done → close it", f"hub task done {self.did}", "nudge the executor",
                     f"hub session wake w --about {self.did}", "not needed → close it", "do not write to the owner"):
            self.assertIn(part, f["text"])
        self.assertNotIn("ask the owner", f["text"])
        self.at(self.t + 2000)                 # then the hub decides: no second nudge, no second fact
        self.assertEqual((len(self.nudges()), len(self.facts())), (1, 1))
        self.assertEqual(self.reminders(), [])

    def replies(self):
        return self.to("hub", f"REPLIED {self.did}")

    def test_an_answer_to_the_reminder_goes_to_the_hub_as_a_result_and_no_second_reminder_follows(self):
        # a continuation of an owner's task does not go to the hub, so a "done" in answer to the reminder reached
        # nobody and the task waited for the fact an hour later; the answer is what the reminder asked for: the hub
        # gets it at once as a result, no second reminder follows, and the fact stays for a task that falls silent
        self.at(self.t + self.OPEN)
        (n,) = self.nudges()
        before = len(self.replies())
        self.turn("w", n["id"], "Waiting for two things, the task stays open")
        (r,) = self.replies()[before:]
        self.assertIn("Waiting for two things", r["text"])
        self.assertNotIn("continuation", r["text"])
        t = (self.now - T0) / MIN
        self.at(t + self.FACT - 1)
        self.assertEqual((len(self.nudges()), self.facts()), (1, []))
        self.at(t + self.FACT)
        self.assertEqual((len(self.nudges()), len(self.facts()), len(self.replies())), (1, 1, before + 1))
        self.at(t + 2000)
        self.assertEqual((len(self.nudges()), len(self.facts())), (1, 1))

    def test_a_wait_on_another_topic_after_the_answer_brings_no_second_reminder(self):
        # replay of a colleague's hub: the executor answered "done" to the reminder, then set `hub wait` on another
        # topic; the wait moved the countdown mark past the reminder, so once it fired the same reminder came back and
        # the hub never got the fact. Now the hub gets the answer once, and only the fact if the task stays silent
        self.at(self.t + self.OPEN)
        (n,) = self.nudges()
        before = len(self.replies())
        self.now += 2 * MIN
        self.turn("w", n["id"], "Done, the build is green")
        self.now += 5 * MIN
        self.ev("schedule.set", "w-wait-ef56-1", session="w", at=self.now + 20 * MIN,
                prompt="[waiting for reply] another branch: CI", channel="session", owner=False, sender="w")
        self.now += 20 * MIN
        self.ev("schedule.due", "w-wait-ef56-1", slot=self.now, channel="core")
        (wake,) = [m for m in self.st["messages"].values()
                   if m["session"] == "w" and m["state"] in ("queued", "typing")]
        self.turn("w", wake["id"], "CI on the other branch is green")
        woke = (self.now - T0) / MIN
        self.at(woke + self.OPEN + 1)     # before: a second reminder here, and never the fact
        self.assertEqual((len(self.nudges()), self.facts(), len(self.replies())), (1, [], before + 1))
        self.at(woke + self.FACT)
        self.assertEqual((len(self.nudges()), len(self.facts()), len(self.replies())), (1, 1, before + 1))

    def test_an_answer_turn_longer_than_the_window_is_still_the_answer(self):
        # review: the answer is the turn that read the reminder, not a result inside 10 minutes of it
        self.at(self.t + self.OPEN)
        (n,) = self.nudges()
        before = len(self.replies())
        self.ev("turn.started", "w", marker=n["id"])
        self.now += delegations.NUDGE_ANSWER + 2 * MIN
        self.ev("turn.result", "w", text="Checked the whole build: done, close it")
        self.ev("turn.stopped", "w")
        self.assertEqual(len(self.replies()), before + 1)

    def test_a_background_turn_in_the_window_is_not_the_answer(self):
        # review: a turn Claude Code started itself (a background helper ended) continues the task, but it did not
        # read the reminder: it stays a continuation, and the answer to the reminder still goes to the hub
        self.at(self.t + self.OPEN)
        (n,) = self.nudges()
        before = len(self.replies())
        self.now += MIN
        self.turn("w", None, "helper log: 3 files processed")
        self.assertEqual(len(self.replies()), before)
        self.now += MIN
        self.turn("w", n["id"], "Done, close it")
        self.assertEqual(len(self.replies()), before + 1)

    def test_a_reminder_from_an_earlier_core_is_answered_by_time(self):
        # a reminder sent before the update has no id in the task: a result within NUDGE_ANSWER is its answer
        self.at(self.t + self.OPEN)
        (n,) = self.nudges()
        del self.st["delegations"][self.did]["open_nudge_mid"]
        before = len(self.replies())
        self.now += 2 * MIN
        self.turn("w", None, "Done, close it")
        self.assertEqual(len(self.replies()), before + 1)

    def test_a_dash_to_the_reminder_is_no_answer(self):
        # "—" says nothing new: the hub gets nothing now, and the fact on schedule
        self.at(self.t + self.OPEN)
        (n,) = self.nudges()
        before = len(self.replies())
        self.turn("w", n["id"], "—")
        self.at(self.t + self.OPEN + self.FACT)
        self.assertEqual((len(self.facts()), len(self.replies())), (1, before))

    def test_a_second_result_after_the_answer_is_an_ordinary_continuation(self):
        self.at(self.t + self.OPEN)
        (n,) = self.nudges()
        before = len(self.replies())
        self.turn("w", n["id"], "Done, close it")
        self.now += MIN
        self.turn("w", n["id"], "And attached the log")
        self.assertEqual(len(self.replies()), before + 1)

    def test_an_answer_to_the_reminder_on_a_session_task_goes_to_the_session_only(self):
        # a task set by a session: the answer goes to the session as before, the hub is not told twice (a week of
        # the journal: one extra REPLIED); no second reminder, the fact if the task falls silent
        self.ev("delegation.done", self.did, channel="session", owner=False, sender="hub")
        self.ev("session.requested", "maps", cwd="/m", session_id="sid-m", channel="cli", owner=True)
        self.ev("session.started", "maps", session_id="sid-m")
        ev = self.ev("delegation.created", None, session="w", for_session="maps", context="check",
                     channel="session", owner=False, sender="maps")
        self.did = f"d{ev}"
        self.now += MIN
        self.turn("w", f"m{ev}", "Started the check, waiting for the logs")
        t = (self.now - T0) / MIN
        self.at(t + self.OPEN)
        (n,) = self.nudges()
        before, got = len(self.replies()), len(self.to_session("maps"))
        self.turn("w", n["id"], "Checked, it all adds up")
        self.assertEqual((len(self.to_session("maps")), len(self.replies())), (got + 1, before))
        self.at(t + self.OPEN + 2000)
        self.assertEqual((len(self.nudges()), len(self.facts()), len(self.replies())), (1, 1, before))

    def to_session(self, name):
        return [m for m in self.st["messages"].values() if m["session"] == name and m.get("task") == self.did]


    def test_the_answer_to_the_reminder_starts_the_silence_count_for_the_fact(self):
        # the fact after an answered reminder counts the silence from the answer, not from the reply before the reminder
        self.at(self.t + self.OPEN)
        (n,) = self.nudges()
        self.now += 3 * MIN
        self.turn("w", n["id"], "Done, close it")
        d = self.st["delegations"][self.did]
        self.assertEqual((d["answered_at"], d["quiet_mark"], d.get("open_answered")), (self.now, self.now, True))
        t = (self.now - T0) / MIN
        self.at(t + self.FACT)
        (f,) = self.facts()
        self.assertIn(f"silent for {self.FACT} min", f["text"])

    def test_a_result_after_the_window_is_not_an_answer_to_the_reminder(self):
        self.at(self.t + self.OPEN)
        (n,) = self.nudges()
        self.now += delegations.NUDGE_ANSWER + 1
        self.turn("w", n["id"], "Answered late")
        d = self.st["delegations"][self.did]
        self.assertEqual(d["quiet_mark"], d["answered_at"])

    def test_an_answer_turn_cut_by_a_limit_and_continued_is_still_the_answer(self):
        # review: the core continues a cut turn with its own message; the answer in the continued turn is the answer
        self.at(self.t + self.OPEN)
        (n,) = self.nudges()
        before = len(self.replies())
        resets = self.now + 3600
        self.ev("turn.started", "w", marker=n["id"])
        self.seq += 1   # the limits carry their own "type" (ev takes it as the event type)
        limits = {"status": "rejected", "type": "five_hour", "windows": {"five_hour": {"used": 1, "resets": resets}},
                  "channel": "cli", "owner": True}
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.now, "type": "account.limits",
                                                   "entity": None, "data": limits})
        self.assertIsNone(why)
        self.ev("turn.result", "w", text=LIMIT_TEXT, is_error=True)
        self.ev("turn.stopped", "w")
        self.now = resets + 1
        self.ev("session.limit_reset", "w", resets=resets, **CORE)
        go = [m for m in self.st["messages"].values() if m["session"] == "w"][-1]
        self.assertEqual(go.get("for_delegation"), self.did)
        self.turn("w", go["id"], "Done, close it")
        self.assertEqual(len(self.replies()), before + 1)

    def test_a_new_reply_after_the_answered_reminder_starts_the_ladder_again(self):
        # review: a new interim reply on the task later (here a background helper's turn continues it) is a new
        # round: the countdown starts again with a reminder, as before the answer
        self.at(self.t + self.OPEN)
        (n,) = self.nudges()
        self.turn("w", n["id"], "Waiting for the pilot")
        self.now += 20 * MIN
        self.turn("w", None, "The pilot is at 80%, about 20 more minutes")
        t = (self.now - T0) / MIN
        self.at(t + self.OPEN)
        self.assertEqual((len(self.nudges()), self.facts()), (2, []))

    def test_an_answer_on_a_task_passed_on_for_the_owner_goes_to_that_session_only(self):
        # review: `--for` - a session passed the owner's words on and gets the result itself; the hub is not told twice
        self.ev("delegation.done", self.did, channel="session", owner=False, sender="hub")
        self.ev("session.requested", "maps", cwd="/m", session_id="sid-m", channel="cli", owner=True)
        self.ev("session.started", "maps", session_id="sid-m")
        ev = self.ev("delegation.created", None, session="w", **{"for": self.mid}, for_session="maps",
                     channel="session", owner=False, sender="maps")
        self.did = f"d{ev}"
        self.now += MIN
        self.turn("w", f"m{ev}", "Started the check, waiting for the logs")
        t = (self.now - T0) / MIN
        self.at(t + self.OPEN)
        (n,) = self.nudges()
        before, got = len(self.replies()), len(self.to_session("maps"))
        self.turn("w", n["id"], "Checked, it all adds up")
        self.assertEqual((len(self.to_session("maps")), len(self.replies())), (got + 1, before))

    def test_hub_wait_means_not_stuck(self):
        self.ev("schedule.set", "w-wait-ab12-1", session="w", at=self.now + 240 * MIN, prompt="[waiting for reply] pilot",
                channel="session", owner=False, sender="w")
        self.at(self.t + 200)
        self.assertEqual(self.nudges(), [])

    def test_a_command_handed_to_her_is_no_wait(self):
        # a command given to it (retarget writes session) is its own, but the clock runs it without a session: it does
        # not wake the executor, so silence remains silence
        self.ev("schedule.set", "mon", every="1h", command="true", channel="cli", owner=True)
        self.ev("schedule.retarget", "mon", session="w", channel="cli", owner=True)
        self.at(self.t + self.OPEN)
        self.assertEqual(len(self.nudges()), 1)

    def test_waiting_note_means_not_stuck(self):
        self.ev("session.waiting", "w", text="the end of the pilot, the log in CI", channel="session", owner=False, sender="w")
        self.at(self.t + 300)
        self.assertEqual(self.nudges(), [])

    def test_a_wait_set_after_the_reminder_cancels_the_fact(self):
        self.at(self.t + self.OPEN)
        (n,) = self.nudges()
        self.ev("turn.started", "w", marker=n["id"])
        self.ev("schedule.set", "w-wait-cd34-1", session="w", at=self.now + 120 * MIN, prompt="[waiting for reply] pilot",
                channel="session", owner=False, sender="w")
        self.ev("turn.result", "w", text="—")
        self.ev("turn.stopped", "w")
        self.at(self.t + self.OPEN + self.FACT + 30)
        self.assertEqual(self.facts(), [])

    def test_a_label_after_the_reminder_that_goes_stale_starts_with_a_reminder_again(self):
        # review 14.10: a `hub waiting` marker after a nudge, then someone wrote to the session, so the marker is
        # stale; the count runs from the marker, and the nudge to the executor comes first again, not an immediate
        # fact to the hub; the marker is older than OPEN_STALE, so the nudge comes at once and the fact OPEN_FACT later
        self.at(self.t + self.OPEN)
        (n,) = self.nudges()
        self.ev("turn.started", "w", marker=n["id"])
        self.ev("session.waiting", "w", text="the end of the pilot", channel="session", owner=False, sender="w")
        self.ev("turn.result", "w", text="—")
        self.ev("turn.stopped", "w")
        self.at(self.t + self.OPEN + 200)
        m = self.ev("message.received", None, to="w", text="how is it going?", channel="session", owner=False, sender="hub")
        self.ev("turn.started", "w", marker=m)
        self.ev("turn.result", "w", text="still going")
        self.ev("turn.stopped", "w")
        self.assertIsNone(reducer.tools.waiting_now(self.st["sessions"]["w"]))
        self.at(self.t + self.OPEN + 200)
        self.assertEqual((len(self.nudges()), len(self.facts())), (2, 0))
        self.at(self.t + self.OPEN + 200 + self.FACT - 1)
        self.assertEqual((len(self.nudges()), len(self.facts())), (2, 0))
        self.at(self.t + self.OPEN + 200 + self.FACT)
        self.assertEqual((len(self.nudges()), len(self.facts())), (2, 1))

    def test_a_new_answer_resets_the_clock(self):
        # an answer long after the reminder (not to it) is a new reply: the countdown starts again
        self.at(self.t + self.OPEN)
        (n,) = self.nudges()
        self.now += 20 * MIN
        self.turn("w", self.task, "The pilot is still going, 80% - waiting about 20 more minutes")
        t = (self.now - T0) / MIN
        self.at(t + self.OPEN - 1)
        self.assertEqual((len(self.nudges()), self.facts()), (1, []))
        self.at(t + self.OPEN)
        self.assertEqual(len(self.nudges()), 2)
        self.at(t + self.OPEN + self.FACT)
        self.assertEqual(len(self.facts()), 1)

    def test_the_fact_comes_once_per_task(self):
        self.at(self.t + self.OPEN + self.FACT)
        self.assertEqual(len(self.facts()), 1)
        self.now += MIN
        self.turn("w", None, "Still waiting for the pilot")
        t = (self.now - T0) / MIN
        self.at(t + self.OPEN + self.FACT + 10)
        self.assertEqual(len(self.facts()), 1)

    def test_busy_executor_gets_nothing(self):
        other = self.ev("message.received", None, text="and also check the logs", channel="web", owner=True, to="w")
        self.ev("turn.started", "w", marker=other)
        self.at(self.t + 300)
        self.assertEqual(self.nudges(), [])

    def test_first_real_answer_after_a_dash_goes_to_the_hub(self):
        # the first result "—" moved the task to replied, the real result ("merged", "built") went as a
        # continuation — the hub is not given the continuation of an owner task, so nobody was left to close it
        d = self.make_dash_task()
        before = len(self.to("hub", f"REPLIED {d}"))
        self.now += MIN
        self.turn("w", None, "All merged, tests green")
        (r,) = self.to("hub", f"REPLIED {d}")[before:]
        self.assertIn("All merged", r["text"])
        self.assertNotIn("continuation", r["text"])
        self.now += MIN
        self.turn("w", None, "And attached the log")   # from here on an ordinary continuation, it does not go to the hub
        self.assertEqual(len(self.to("hub", f"REPLIED {d}")), before + 1)

    def test_a_task_set_by_a_session_does_not_tell_the_hub_twice(self):
        # an executor raised by a session, not by the owner: the real result goes to the one that raised it, the hub gets nothing new
        self.ev("session.requested", "maps", cwd="/m", session_id="sid-m", channel="cli", owner=True)
        self.ev("session.started", "maps", session_id="sid-m")
        ev = self.ev("delegation.created", None, session="w", for_session="maps", context="check",
                     channel="session", owner=False, sender="maps")
        d = f"d{ev}"
        self.now += MIN
        self.turn("w", f"m{ev}", "—")
        before = len(self.to("hub", f"REPLIED {d}"))
        self.now += MIN
        self.turn("w", None, "Checked, it all adds up")
        self.assertEqual(len(self.to("hub", f"REPLIED {d}")), before)

    def make_dash_task(self):
        self.ev("delegation.done", self.did, channel="session", owner=False, sender="hub")
        mid = self.ev("message.received", None, text="merge the branch", channel="web", owner=True, to="hub")
        ev = self.ev("delegation.created", None, session="w", **{"for": mid}, channel="session", owner=False,
                     sender="hub")
        self.now += MIN
        self.turn("w", f"m{ev}", "—")
        self.assertEqual(self.st["delegations"][f"d{ev}"]["state"], "replied")
        return f"d{ev}"

    def test_closed_task_gets_nothing(self):
        self.ev("delegation.done", self.did, channel="session", owner=False, sender="hub")
        self.at(self.t + 300)
        self.assertEqual((self.nudges(), self.facts()), ([], []))

    def test_nothing_to_the_owner(self):
        before = len(self.st["messages"])
        self.at(self.t + self.OPEN + self.FACT)
        new = list(self.st["messages"].values())[before:]
        self.assertEqual(sorted(m["session"] for m in new), ["hub", "w"])

class OpenWhileBusy(Ladder):
    """An executor that keeps working on other things after an interim reply: every turn moved the count, so the open
    task never got its reminder. Replay of a real case (anonymized): reply, then 21.8 h with 54 turns on other work
    and 5 parkings, 0 reminders, 0 facts. OPEN_STALE caps the count; a sleeping executor is not woken, the hub gets
    the fact instead."""
    nudges, facts = OpenAfterAnswer.nudges, OpenAfterAnswer.facts

    def setUp(self):
        super().setUp()
        self.OPEN, self.FACT = delegations.OPEN_NUDGE // MIN, delegations.OPEN_FACT // MIN
        self.now += MIN
        self.turn("w", self.task, "Started the pilot, waiting for the end - 5-15 minutes")   # an interim reply, the task is open
        self.t = (self.now - T0) / MIN

    def other(self, minutes=5):
        """A turn on someone else's message, `minutes` long."""
        asleep = self.st["sessions"]["w"]["state"] in machines.SLEEP
        m = self.ev("message.received", None, to="w", text="one more question", channel="session", owner=False, sender="o")
        if asleep:   # the message raises it
            self.ev("session.started", "w", session_id="sid-w")
        self.ev("turn.started", "w", marker=m)
        self.now += minutes * MIN
        self.ev("turn.result", "w", text="a reply on another matter")
        self.ev("turn.stopped", "w")

    def busy_until(self, end, every=20):
        """Turns on other work every `every` minutes up to T0 + end: the session is never idle for OPEN_NUDGE."""
        cur = (self.now - T0) / MIN
        while cur + every <= end:
            self.at(cur + every - 5)
            self.other(5)
            cur = (self.now - T0) / MIN
        self.at(end)

    def test_a_busy_executor_still_gets_the_reminder_and_the_hub_the_fact(self):
        stale = delegations.OPEN_STALE // MIN
        self.busy_until(self.t + stale - 1)
        self.assertEqual((self.nudges(), self.facts()), ([], []))
        self.busy_until(self.t + stale + 20)
        self.assertEqual((len(self.nudges()), self.facts()), (1, []))
        nudged = (self.nudges()[0]["created"] - T0) / MIN
        self.busy_until(nudged + stale + 20)
        self.assertEqual((len(self.nudges()), len(self.facts())), (1, 1))
        self.busy_until(nudged + 3 * stale)        # once per task
        self.assertEqual((len(self.nudges()), len(self.facts())), (1, 1))

    def test_a_sleeping_executor_is_not_woken_the_hub_gets_the_fact(self):
        self.at(self.t + 10)
        self.ev("session.ended", "w", reason="prompt_input_exit")   # closed by a human
        self.assertEqual(self.st["sessions"]["w"]["state"], "asleep")
        stale = delegations.OPEN_STALE // MIN
        self.at(self.t + stale - 1)
        self.assertEqual((self.nudges(), self.facts()), ([], []))
        self.at(self.t + stale)
        self.assertEqual((self.nudges(), len(self.facts())), ([], 1))
        self.assertIn("asleep, the core did not wake it", self.facts()[0]["text"])   # no reminder was sent: the fact does not claim it
        self.assertEqual(self.st["sessions"]["w"]["state"], "asleep")
        self.at(self.t + 3 * stale)
        self.assertEqual((self.nudges(), len(self.facts())), ([], 1))

    def test_a_parked_agent_is_not_woken_the_hub_gets_the_fact(self):
        self.ev("session.role_set", "w", role="agent", about="Builds", channel="cli", owner=True)
        self.at(self.t + 10)
        self.ev("session.parked", "w", channel="core", why="idle")
        self.assertEqual(self.st["sessions"]["w"]["state"], "parked")
        self.at(self.t + delegations.OPEN_STALE // MIN)
        self.assertEqual((self.nudges(), len(self.facts())), ([], 1))
        self.assertEqual(self.st["sessions"]["w"]["state"], "parked")

    def test_a_retired_executor_gets_nothing(self):
        self.st["sessions"]["w"]["state"] = "retired"
        self.at(self.t + 3 * delegations.OPEN_STALE // MIN)
        self.assertEqual((self.nudges(), self.facts()), ([], []))

    def test_the_sleeping_one_is_not_restarted_for_a_stalled_task(self):
        self.ev("session.ended", "w", reason="prompt_input_exit")
        d = self.st["delegations"][self.did]
        self.assertIsNone(reducer._stalled_task(self.st, self.st["sessions"]["w"], self.now))
        self.assertIsNotNone(delegations.quiet(self.st, d))

    def test_the_ceiling_counts_from_the_owners_return(self):
        self.ev("owner.away", None, channel="web", owner=True)
        self.ev("turn.started", "w", marker=None)
        self.ev("permission.requested", "w", request_id="r1", tool="Bash", input={"command": "deploy"},
                interactive=True)
        self.ev("turn.result", "w", text="")
        self.ev("turn.stopped", "w")
        stale = delegations.OPEN_STALE // MIN
        back = self.t + stale + 30
        self.at(back)
        self.ev("owner.back", None, channel="web", owner=True)
        self.busy_until(back + stale - 1)
        self.assertEqual(self.nudges(), [])
        self.busy_until(back + stale + 20)
        self.assertEqual(len(self.nudges()), 1)

    def test_replay_of_the_case_busy_and_parked_for_a_day(self):
        # minutes after the reply: (start, end) of turns on other work, "P" a parking (process gone after a restart),
        # "S" the session raised again; taken from the journal, texts and names dropped
        plan = [(13.9, "P"), (73.2, "S")] + [(74 + 3 * i, 75 + 3 * i) for i in range(21)] + [(138.3, "P"), (149.1, "S")] \
            + [(150 + 4 * i, 151 + 4 * i) for i in range(21)] + [(251, "P"), (380.1, "S")] \
            + [(381 + 3 * i, 382 + 3 * i) for i in range(5)] + [(423.7, "P"), (450.8, "S"), (450.9, 451.3),
                                                                (459.1, "P"), (1169.7, "S")]
        for at, what in plan:
            self.at(self.t + at)
            if what == "P":
                self.ev("session.parked", "w", channel="core", why="restart")
            elif what == "S":
                self.other(0.1)   # a message from another session raises it
            else:
                self.other(what - at)
        self.at(self.t + 1170)
        # on main: 0 reminders, 0 facts for 19.5 hours; now the hub knows by OPEN_STALE + the reminder
        self.assertEqual(len(self.facts()), 1)
        self.assertLessEqual((self.facts()[0]["created"] - T0) / MIN - self.t,
                             2 * delegations.OPEN_STALE // MIN + delegations.OPEN_NUDGE // MIN)


class SessionTaskDash(Ladder):
    """A task assigned by a session: the executor's "—" result (passed it on, a check with no result) is not sent to
    the assigner, it only woke it with an empty line; the first real result after "—" still arrives."""

    def setUp(self):
        super().setUp()
        self.ev("session.requested", "boss", cwd="/w", session_id="sid-boss", channel="cli", owner=True)
        self.ev("session.started", "boss", session_id="sid-boss")
        ev = self.ev("delegation.created", None, session="w", text="collect the report", for_session="boss",
                     channel="session", owner=False, sender="boss")
        self.sd = f"d{ev}"
        self.now += MIN
        self.turn("w", f"m{ev}", "—")

    def answers(self):
        return [m for m in self.st["messages"].values() if m["session"] == "boss" and m.get("task") == self.sd]

    def test_dash_is_not_sent_to_the_assigning_session(self):
        self.assertEqual(self.answers(), [])
        self.assertEqual(self.st["delegations"][self.sd]["state"], "replied")

    def test_first_real_answer_after_a_dash_reaches_it(self):
        self.now += MIN
        self.turn("w", None, "Report collected: 12 rows")
        (m,) = self.answers()
        self.assertIn("Report collected", m["text"])


if __name__ == "__main__":
    unittest.main()
