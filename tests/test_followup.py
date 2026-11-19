"""A turn without a marker (Claude Code woke the session itself: a background helper finished) continues what the
session worked on last, not its old delegation (an agent worked on one session's message, and the result of the
background turn went to another one — as a reply to a long-past delegation)."""
import tests  # noqa: F401 — keep the machine's settings out
import unittest

from core import reducer

OWNER = {"channel": "web", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}


class Followup(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        for n, kind in (("hub", "hub"), ("fe", None), ("seo", None), ("sso", None)):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", kind=kind, **OWNER)
            self.ev("session.started", n)
        self.ev("delegation.created", None, session="fe", for_session="seo", context="T-1", **HUB)
        self.did = f"d{self.seq}"
        self.turn(f"m{self.seq}", "done")
        self.assertEqual(self.st["delegations"][self.did]["state"], "replied")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def turn(self, marker, text):
        self.ev("turn.started", "fe", marker=marker)
        self.ev("turn.result", "fe", text=text)
        self.ev("turn.stopped", "fe")

    def to_seo(self):
        return [m for m in self.st["messages"].values() if m["session"] == "seo"]

    def test_background_turn_right_after_the_task_continues_it(self):
        self.turn(None, "and also: checked prod")
        self.assertEqual(len(self.to_seo()), 2)

    def test_background_turn_after_other_work_is_not_the_old_task(self):
        self.ev("message.received", None, to="fe", text="sort out the authorization", channel="session", owner=False,
                sender="sso")
        self.turn(f"m{self.seq}", "started six analysts")
        self.turn(None, "the group is sorted out, waiting for the other five")
        self.assertEqual(len(self.to_seo()), 1)          # only the first reply for T-1
        self.assertEqual(self.st["delegations"][self.did].get("reply"), "done")

    def test_subtask_reply_does_not_stick_the_parent_to_a_later_background_turn(self):
        # on task T-2, fe created a subtask (executor pe, parent=T-2); pe reported — fe's turn on its reply
        # continues T-2 (the result goes to seo). But this must not pin T-2 to fe: the next turn without a marker is about
        # something else (the result of an unrelated session turn went to the owner of another task, because the
        # subtask's reply left last_task = the parent)
        self.ev("session.requested", "pe", cwd="/w", session_id="s-pe", **OWNER)
        self.ev("session.started", "pe")
        self.ev("delegation.created", None, session="fe", for_session="seo", context="T-2", **HUB)
        d2, m2 = f"d{self.seq}", f"m{self.seq}"
        self.ev("turn.started", "fe", marker=m2)                 # fe is busy with T-2 (delegation=d2)
        self.ev("delegation.created", None, session="pe", for_session="fe", context="sub",
                channel="session", owner=False, sender="fe")     # a subtask from fe → parent=d2
        sub, subm = f"d{self.seq}", f"m{self.seq}"
        self.ev("turn.result", "fe", text="set a subtask on T-2")
        self.ev("turn.stopped", "fe")                            # T-2 replied, reply #1 in seo
        self.ev("turn.started", "pe", marker=subm)
        self.ev("turn.result", "pe", text="the subtask is ready")
        reply = f"m{self.seq}x1"                                 # the core forwards the subtask's reply to fe (task = the subtask)
        self.ev("turn.stopped", "pe")
        self.assertEqual(self.st["delegations"][sub]["parent"], d2)
        base = len(self.to_seo())
        self.turn(reply, "continuing on T-2, the subtask reported")   # fe's turn on the subtask's reply → reply #2 in seo
        self.assertEqual(len(self.to_seo()), base + 1)
        self.turn(None, "this is a different matter, the background command finished")  # a turn without a marker — not about T-2
        self.assertEqual(len(self.to_seo()), base + 1)           # no third result went to seo


MONITOR = '<task-notification>\n<task-id>b1</task-id>\n<summary>Monitor event: "pipeline 7: test/build"</summary>'


class MonitorTurns(unittest.TestCase):
    """An executor watches CI with a Claude Code monitor and ends its turn on every event with "waiting…" — the core
    forwarded each one to the delegating session and the hub (9 in 16 minutes, each waking both). The core holds a
    turn caused by a monitor event: the last one goes out when the events die down (MONITOR_HOLD), or an ordinary
    result replaces it."""

    setUp, ev, turn, to_seo = Followup.setUp, Followup.ev, Followup.turn, Followup.to_seo   # helpers, not tests

    def mon(self, text):
        self.ev("turn.started", "fe", marker=None, prompt_head=MONITOR)
        self.ev("turn.result", "fe", text=text)
        self.ev("turn.stopped", "fe")

    def fire(self, at):
        self.seq += 1
        self.st, _, _ = reducer.reduce(self.st, {"seq": self.seq, "ts": at, "type": "timer.fired",
                                                 "entity": self.did, "data": {}})

    def hub_notes(self):
        return [m for m in self.st["messages"].values() if m["session"] == "hub" and "(continuation)" in m["text"]]

    def test_monitor_turns_go_out_once_with_the_latest_after_quiet(self):
        from core import delegations
        for t in ("waiting for the jobs", "three green, waiting for billing", "all tests are green, waiting for the build"):
            self.mon(t)
        self.assertEqual(len(self.to_seo()), 1)                       # only the first reply for the task
        self.assertEqual(self.hub_notes(), [])
        due = [x for x in reducer.pending_timers(self.st) if x[1] == self.did]
        self.assertEqual(due[0][0], 100 + self.seq - 1 + delegations.MONITOR_HOLD)   # from the result of the last turn
        self.fire(due[0][0] - 1)
        self.assertEqual(len(self.to_seo()), 1)                       # too early — nothing
        self.fire(due[0][0])
        seo = self.to_seo()
        self.assertEqual((len(seo), seo[-1]["text"]), (2, "all tests are green, waiting for the build"))
        self.assertEqual(self.hub_notes(), [])                        # the continuation goes to the session; the hub does not need it
        self.fire(due[0][0] + 600)
        self.assertEqual(len(self.to_seo()), 2)                       # the same does not go out a second time

    def test_an_ordinary_turn_goes_out_at_once_and_replaces_the_held_one(self):
        self.mon("waiting for the jobs")
        self.turn(None, "the build is ready, MR !5")                        # the background command finished — the result goes at once
        seo = self.to_seo()
        self.assertEqual((len(seo), seo[-1]["text"]), (2, "the build is ready, MR !5"))
        self.fire(100 + self.seq + 3600)
        self.assertEqual(len(self.to_seo()), 2)

    def test_a_monitor_notice_injected_into_an_ordinary_turn_does_not_hold_its_result(self):
        # review: a monitor event injected in the middle of an ordinary turn made the whole turn a "monitor" one — the result was held
        self.ev("turn.started", "fe", marker=None, prompt_head="<task-notification>build done</task-notification>")
        self.ev("turn.started", "fe", marker=None, prompt_head=MONITOR)
        self.ev("turn.result", "fe", text="the build is ready, MR !7")
        self.ev("turn.stopped", "fe")
        self.assertEqual(self.to_seo()[-1]["text"], "the build is ready, MR !7")

    def test_a_released_monitor_result_restarts_the_quiet_count(self):
        # review: a held result went out without answered_at — the quiet period was counted from the old reply
        self.mon("all tests are green, waiting for the build")
        at = self.st["delegations"][self.did]["held"]["ts"]
        self.fire(at + 300)
        self.assertEqual(self.st["delegations"][self.did]["answered_at"], at)

    def test_a_released_monitor_result_within_the_reminder_window_keeps_the_countdown(self):
        # the same rule as for an ordinary result: an answer soon after the core's reminder does not restart the quiet count
        d = self.st["delegations"][self.did]
        mark = d["quiet_mark"]
        self.mon("all tests are green, waiting for the build")
        at = self.st["delegations"][self.did]["held"]["ts"]
        self.st["delegations"][self.did]["open_nudged"] = at - 60
        self.fire(at + 300)
        d = self.st["delegations"][self.did]
        self.assertEqual((d["answered_at"], d["quiet_mark"]), (at, mark))


if __name__ == "__main__":
    unittest.main()
