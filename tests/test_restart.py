"""`hub restart` (owner decision 2026-09-30: restart is a command, also available to the hub itself so that it can
restart itself; before that a session wrote a script in /tmp and poked launchctl). The request is a fact in the
journal; the core waits until sessions are idle and have no background work, then asks hub-up (a restart file in the
state folder); after the restart it tells the hub."""
import tests  # noqa: F401 — hide the machine's settings
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from unittest import mock

from core import forms, i18n, reducer, restart, spool
from tests.fakes import lang

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")
HUB_UP = os.path.join(REPO, "bin", "hub-up")
OWNER = {"channel": "cli", "owner": True}


def plain(want):
    """What hubd asks for, without the marks of the core's phrases (<field>_l10n: the owner reads them in his language)."""
    return {k: v for k, v in want.items() if not k.endswith("_l10n")} if isinstance(want, dict) else want


def en(f, *args, **kw):
    """A `hub status` line as a session reads it: in English (the owner's terminal renders his language)."""
    with mock.patch.object(i18n, "current", return_value="en"):
        return f(*args, **kw)


class Core(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.later = reducer.empty(), 0, 0   # later is a clock shift: "N seconds passed"
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "a", cwd="/w", session_id="s-a", **OWNER)
        for name in ("hub", "a"):
            self.ev("session.started", name)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.seq + self.later, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def from_hub(self, **data):
        return self.ev("hub.restart_requested", None, channel="session", owner=False, sender="hub", **data)

    def test_whole_hub_waits_for_free_sessions_then_tells_the_hub(self):
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.assertIsNone(self.from_hub(why="new code"))
        self.assertEqual(self.st["restart"]["state"], "waiting")
        self.assertEqual(restart.blockers(self.st), ["a: a turn is running"])
        self.assertIsNone(restart.due(self.st))
        self.ev("turn.result", "a", text="done")
        self.assertEqual(restart.blockers(self.st), [])
        self.assertEqual(restart.due(self.st), {"parts": []})
        self.assertIsNone(self.ev("hub.restart_started", None, parts=[]))
        self.assertEqual(self.st["restart"]["state"], "started")
        self.assertIsNone(restart.due(self.st))  # we do not ask a second time
        self.ev("core.started", None, pid=1)
        self.assertNotIn("restart", self.st)
        told = [m for m in self.st["messages"].values() if m["session"] == "hub" and "was restarted" in m["text"]]
        self.assertEqual([m["state"] for m in told], ["aside"])   # no turn (test_hub_aside)
        self.assertIn("new code", told[0]["text"])

    def run_job(self, job, command="x"):
        self.ev("schedule.set", job, command=command, every="5m", **OWNER)
        self.ev("schedule.due", job, slot=self.seq + self.later)
        slot = self.st["schedule"][job]["run_slot"]
        self.ev("schedule.started", job, slot=slot)
        return slot

    def test_a_schedule_command_that_restarts_the_clock_is_not_an_unknown_outcome(self):
        # 29.10: the daily restart job took the clock down with the hub; the next clock reported "unknown" and the hub
        # got a false letter every day
        for parts in ([], ["scheduler"]):
            with self.subTest(parts=parts):
                job = "j" + "".join(parts)
                slot = self.run_job(job)
                self.ev("hub.restart_requested", None, parts=parts, schedule_job=job, **OWNER)
                self.ev("schedule.ran", job, slot=slot, exit="unknown")
                self.assertEqual(self.st["schedule"][job]["last_exit"], 0)
                self.assertFalse([m for m in self.st["messages"].values() if "the outcome is unknown" in m["text"]])

    def test_a_restart_asked_by_another_job_or_by_the_owner_leaves_the_outcome_unknown(self):
        slot = self.run_job("victim")
        self.run_job("asker")
        self.ev("hub.restart_requested", None, parts=[], schedule_job="asker", **OWNER)
        self.ev("hub.restart_requested", None, parts=["scheduler"], **OWNER)
        self.ev("schedule.ran", "victim", slot=slot, exit="unknown")
        self.assertNotIn("last_exit", self.st["schedule"]["victim"])
        self.assertEqual(len([m for m in self.st["messages"].values() if "the outcome is unknown" in m["text"]]), 1)

    def test_web_goes_at_once_while_the_whole_hub_waits(self):
        # 19.10: an update asks for both the page and the whole hub; the page restarts at once, the whole hub waits for
        # a busy session
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.ev("hub.restart_requested", None, parts=["web"], **OWNER)
        self.ev("hub.restart_requested", None, why="update", **OWNER)
        self.assertEqual(restart.due(self.st), {"parts": ["web"]})
        self.ev("hub.restart_started", None, parts=["web"])
        self.assertIsNone(restart.due(self.st))                      # the page was restarted, the whole hub still waits
        self.assertEqual(self.st["restart"]["state"], "waiting")
        self.ev("turn.result", "a", text="done")
        self.assertEqual(restart.due(self.st), {"parts": []})

    def test_a_session_respawning_after_a_crash_does_not_hold_the_restart(self):
        # 14.10: the hub session kept crashing at start and stayed "starting" forever, so the restart after a
        # self-update waited for it endlessly and only --now helped. A crashing one is restarted by the process anyway:
        # no turn is cut
        self.assertIsNone(self.ev("session.exited", "hub", code=1))
        self.assertEqual((self.st["sessions"]["hub"]["state"], self.st["sessions"]["hub"]["restarts"]), ("starting", 1))
        self.from_hub(why="new code")
        self.assertEqual(restart.blockers(self.st), [])
        self.assertEqual(restart.due(self.st), {"parts": []})
        self.ev("session.requested", "b", cwd="/w", session_id="s-b", **OWNER)   # the first start: we wait, as before
        self.assertEqual(restart.blockers(self.st), ["b: coming up"])
        self.ev("session.started", "b")
        self.ev("session.exited", "b", code=1)   # not the hub: we wait, as before
        self.assertEqual(restart.blockers(self.st), ["b: coming up"])

    def test_background_blocks_and_now_does_not(self):
        self.ev("session.background", "a", id="b1", kind="shell", title="npm test")
        self.from_hub()
        self.assertEqual(restart.blockers(self.st), ["a: in the background npm test"])
        self.assertIsNone(self.ev("hub.restart_requested", None, now=True, **OWNER))  # the owner says "now"
        self.assertEqual(restart.due(self.st), {"parts": []})

    def test_a_long_silent_background_command_is_a_service_not_a_blocker(self):
        # 03.10: a never-ending background job (a console on :7070) held an ordinary hub restart forever, so long-lived
        # background servers are not counted as blockers; after the restart the core tells the session to resume the cut
        # background anyway
        self.ev("session.background", "a", id="srv", kind="shell", title="Restart local console")
        t0 = self.st["sessions"]["a"]["background"]["srv"]["since"]
        self.from_hub()
        self.assertEqual(restart.blockers(self.st, now=t0 + 10 * 60), ["a: in the background Restart local console"])   # recent
        # 11.11: a silent wait (`until …; sleep`) or a quiet run under Claude Code's own 2 h term is work, not a
        # service: at 31 min it still holds the restart (it used to be cut there)
        self.assertEqual(len(restart.blockers(self.st, now=t0 + 31 * 60)), 1)
        self.assertEqual(restart.blockers(self.st, now=t0 + 121 * 60), [])                                    # a service
        self.assertEqual(restart.due(self.st, now=t0 + 121 * 60), {"parts": []})
        self.st["sessions"]["a"]["background"]["srv"]["out_at"] = t0 + 119 * 60                               # writing
        self.assertEqual(len(restart.blockers(self.st, now=t0 + 121 * 60)), 1)
        self.ev("session.background", "a", id="ag", kind="agent", title="log analysis")
        self.st["sessions"]["a"]["background"]["srv"].pop("out_at")
        ag = self.st["sessions"]["a"]["background"]["ag"]["since"]
        self.assertEqual(restart.blockers(self.st, now=ag + 120 * 60), ["a: in the background log analysis"])   # an agent is work

    def test_a_long_turn_holds_the_restart_only_up_to_the_ceiling(self):
        # friction digest 16.10: a restart waited for someone else's turn up to 188 min (58 of 355 took over 10 min). A
        # ceiling (hub.json restart_wait_minutes, 20 by default): after it the restart goes ahead, with the cut ones in
        # the event
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.from_hub(why="new code")
        t0 = self.st["restart"]["since"]
        self.assertIsNone(restart.due(self.st, t0 + 19 * 60))
        self.assertEqual(restart.left(self.st, t0 + 19 * 60), 60)
        self.assertEqual(plain(restart.due(self.st, t0 + 20 * 60)), {"parts": [], "cut": ["a: a turn is running"]})
        self.assertIsNone(restart.due(self.st, t0 + 20 * 60, minutes=30))
        self.assertIsNone(restart.due(self.st, t0 + 600 * 60, minutes=0))    # 0 means no ceiling, as before
        self.assertIsNone(restart.left(self.st, t0, minutes=0))
        self.assertIsNone(restart.due(self.st))                               # no clock means no ceiling
        # from hub.json: a non-number, a negative, or infinity gives the default (the core counts by it on every tick)
        for bad in ("20", -1, True, float("nan")):
            self.assertEqual(restart.ceiling({"restart_wait_minutes": bad}), restart.WAIT_MINUTES, bad)
        self.assertEqual((restart.ceiling({}), restart.ceiling({"restart_wait_minutes": 0})), (20, 0))
        for huge in (1e307, 10 ** 400, float("inf")):   # a huge value means a week: the deadline is computed without overflow
            self.assertEqual(restart.ceiling({"restart_wait_minutes": huge}), restart.WAIT_MAX)
            self.assertIsNone(restart.due(self.st, t0 + 60, restart.ceiling({"restart_wait_minutes": huge})))

    def test_asking_again_does_not_restart_the_clock(self):
        # the owner asked to restart again while waiting: the deadline counts from the first request, who and why are
        # the new ones
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.from_hub(why="new code")
        t0 = self.st["restart"]["since"]
        self.ev("hub.restart_requested", None, why="once more", **OWNER)
        r = self.st["restart"]
        self.assertEqual((r["since"], r["by"], r["why"], r["now"]), (t0, lang.legacy("restart_by"), "once more", False))
        self.assertEqual(restart.due(self.st, t0 + 20 * 60)["parts"], [])
        self.ev("hub.restart_requested", None, now=True, **OWNER)              # --now: at once
        self.assertEqual(restart.due(self.st, t0 + 1), {"parts": []})

    def test_the_hub_hears_whose_turns_the_ceiling_cut(self):
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.from_hub()
        self.ev("hub.restart_started", None, parts=[], cut=["a: a turn is running"])
        self.ev("core.started", None, pid=1)
        (told,) = [m for m in self.st["messages"].values() if m["session"] == "hub" and "was restarted" in m["text"]]
        self.assertIn("did not get there: a: a turn is running", told["text"])

    def test_a_sleeping_or_parked_session_never_holds_the_restart(self):
        self.ev("session.parked", "a", channel="core", why="idle")
        self.st["sessions"]["a"]["background"] = {"b1": {"id": "b1", "kind": "shell", "title": "npm test",
                                                         "since": self.seq}}   # a sleeping session's leftover background is not work
        self.from_hub()
        self.assertEqual(self.st["sessions"]["a"]["state"], "parked")
        self.assertEqual(restart.blockers(self.st), [])

    def cut_by_restart(self):
        """A restart cut a's turn: no process after the core starts (reconcile), so a is brought up and its last
        message is "continue"."""
        self.ev("session.exited", "a", why="reconcile")
        self.ev("session.started", "a")
        return [m for m in self.st["messages"].values() if m["session"] == "a"][-1]

    def test_the_continuation_after_a_restart_answers_the_owners_message(self):
        # as after a limit: the continuation is a reply to its message (re: Telegram answers in its thread)
        self.ev("message.received", None, to="a", text="do it", channel="telegram", owner=True)
        mid = f"m{self.seq}"
        self.ev("turn.started", "a", marker=mid)
        go = self.cut_by_restart()
        self.assertIn("A hub restart cut off", go["text"])
        self.assertEqual(go.get("re"), mid)

    def test_the_continuation_after_a_restart_is_still_that_task(self):
        self.ev("message.received", None, text="count", channel="telegram", owner=True)
        owner_mid = f"m{self.seq}"
        self.ev("delegation.created", None, session="a", **{"for": owner_mid}, channel="session", owner=False,
                sender="hub")
        did = f"d{self.seq}"
        self.ev("turn.started", "a", marker=self.st["delegations"][did]["message"])
        go = self.cut_by_restart()
        self.assertEqual(go.get("for_delegation"), did)
        self.assertNotIn("re", go)
        self.ev("turn.started", "a", marker=go["id"])
        self.ev("turn.result", "a", text="42")
        d = self.st["delegations"][did]
        self.assertEqual((d["state"], d["reply"]), ("replied", "42"))

    def test_nothing_to_continue_is_not_an_answer_to_the_task(self):
        # journal fold: the continuation waited in the queue, but the executor finished and handed in the task earlier,
        # so its "nothing to continue" went to the assigner as a second reply
        self.ev("message.received", None, text="count", channel="telegram", owner=True)
        self.ev("delegation.created", None, session="a", **{"for": f"m{self.seq}"}, channel="session", owner=False,
                sender="hub")
        did = f"d{self.seq}"
        self.ev("turn.started", "a", marker=self.st["delegations"][did]["message"])
        go = self.cut_by_restart()
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.ev("turn.result", "a", text="43")
        self.assertEqual(self.st["delegations"][did]["reply"], "43")
        before = len(self.st["messages"])
        self.ev("turn.started", "a", marker=go["id"])
        self.ev("turn.result", "a", text="«**Nothing to continue.**»")
        self.assertEqual(len(self.st["messages"]), before)
        self.assertEqual(self.st["delegations"][did]["reply"], "43")

    def cut_with_an_older_message(self):
        """Journal fold: a restart cut a's task turn; the core queued "continue", but a message queued while a was down
        is older, so it is typed first."""
        self.ev("message.received", None, text="count", channel="telegram", owner=True)
        self.ev("delegation.created", None, session="a", **{"for": f"m{self.seq}"}, channel="session", owner=False,
                sender="hub")
        did = f"d{self.seq}"
        self.ev("turn.started", "a", marker=self.st["delegations"][did]["message"])
        self.ev("session.exited", "a", why="reconcile")
        self.ev("message.received", None, to="a", text="also check the phone", channel="session", owner=False,
                sender="hub")
        other = f"m{self.seq}"
        self.ev("session.started", "a")
        go = [m for m in self.st["messages"].values() if m["session"] == "a"][-1]
        self.assertEqual((go.get("for_delegation"), go["state"], self.st["messages"][other]["state"]),
                         (did, "queued", "typing"))
        self.ev("turn.started", "a", marker=other)
        return did, go["id"]

    def handed_in_with_a_card(self):
        """...and in the turn on the older message a finished the task, asking the owner with a card; the "continue"
        is typed after that result."""
        did, go = self.cut_with_an_older_message()
        self.ev("permission.requested", "a", request_id="r1", tool="AskUserQuestion", interactive=True,
                input={"questions": [{"question": "Close the whole direction?"}]})
        self.ev("turn.result", "a", text="Done: all 7 points are fixed and live; a card is with the owner")
        self.ev("turn.stopped", "a")
        self.assertEqual((self.st["delegations"][did]["state"], self.st["messages"][go]["state"]),
                         ("waiting-owner", "typing"))
        return did, go

    def queued(self, name):
        return [m["id"] for m in self.st["messages"].values() if m["session"] == name and m["state"] in ("queued", "typing")]

    def test_nothing_to_continue_on_a_task_waiting_for_the_owner_is_not_its_result(self):
        # the late "continue" is no answer of the owner and its "nothing to continue" no result of the task, which
        # still waits for the card (journal 12.11: the assigner got REPLIED "nothing to continue")
        did, go = self.handed_in_with_a_card()
        self.ev("turn.started", "a", marker=go)
        self.assertEqual(self.st["delegations"][did]["state"], "waiting-owner")
        self.ev("turn.result", "a", text="nothing to continue")
        self.ev("turn.stopped", "a")
        d = self.st["delegations"][did]
        self.assertEqual(d["state"], "waiting-owner")
        self.assertNotEqual(d.get("reply"), "nothing to continue")
        self.assertFalse([m for m in self.st["messages"].values() if m["session"] == "hub"
                          and "nothing to continue" in m["text"]])

    def test_the_owners_answer_to_the_card_still_resumes_the_task(self):
        did, go = self.handed_in_with_a_card()
        self.ev("turn.started", "a", marker=go)
        self.ev("turn.result", "a", text="nothing to continue")
        self.ev("turn.stopped", "a")
        self.ev("permission.answered", "a", request_id="r1", behavior="allow",
                answers={"Close the whole direction?": "Yes"}, **OWNER)
        ans = next(m for m in self.st["messages"].values() if m["session"] == "a" and m.get("answers_question"))
        self.ev("turn.started", "a", marker=ans["id"])
        self.assertEqual(self.st["delegations"][did]["state"], "working")
        self.ev("turn.result", "a", text="Closed the direction")
        d = self.st["delegations"][did]
        self.assertEqual((d["state"], d["reply"]), ("replied", "Closed the direction"))

    def test_a_continuation_of_a_task_handed_in_without_a_card_is_no_second_reply(self):
        did, go = self.cut_with_an_older_message()
        self.ev("turn.result", "a", text="42")
        self.ev("turn.stopped", "a")
        self.assertEqual((self.st["delegations"][did]["state"], self.st["delegations"][did]["reply"]), ("replied", "42"))
        self.ev("turn.started", "a", marker=go)
        self.ev("turn.result", "a", text="nothing to continue")
        self.ev("turn.stopped", "a")
        self.assertEqual((self.st["delegations"][did]["state"], self.st["delegations"][did]["reply"]), ("replied", "42"))

    def test_a_continuation_of_a_task_still_in_work_stays(self):
        # the turn on the older message was about something else: the cut task still needs its "continue"
        self.ev("message.received", None, text="count", channel="telegram", owner=True)
        self.ev("delegation.created", None, session="a", **{"for": f"m{self.seq}"}, channel="session", owner=False,
                sender="hub")
        did = f"d{self.seq}"
        self.ev("turn.started", "a", marker=self.st["delegations"][did]["message"])
        self.ev("session.exited", "a", why="reconcile")
        self.ev("message.received", None, text="what time is it?", channel="telegram", owner=True)
        self.ev("delegation.created", None, session="a", **{"for": f"m{self.seq}"}, channel="session", owner=False,
                sender="hub")   # another task, set while a is down: typed first
        other = self.st["delegations"][f"d{self.seq}"]
        self.ev("session.started", "a")
        go = [m for m in self.st["messages"].values() if m["session"] == "a" and "restart cut off" in m["text"]][-1]["id"]
        self.ev("turn.started", "a", marker=other["message"])
        self.ev("turn.result", "a", text="It is noon")
        self.ev("turn.stopped", "a")
        self.assertEqual([self.st["delegations"][x]["state"] for x in (did, other["id"])], ["working", "replied"])
        self.assertIn(go, self.queued("a"))   # typed next
        self.ev("turn.started", "a", marker=go)
        self.ev("turn.result", "a", text="43")
        self.assertEqual(self.st["delegations"][did]["reply"], "43")

    def test_the_cut_background_notice_is_typed_after_another_turn(self):
        # a "continue" typed after another turn is still needed: in the journal week to 12.11, 6 of 8 such ones made the
        # session start again the CI waits the restart had cut, so a later turn does not withdraw it
        self.ev("session.background", "a", id="b1", kind="agent", title="wait for CI")
        self.ev("session.exited", "a", why="reconcile")
        self.ev("message.received", None, to="a", text="also check the phone", channel="session", owner=False,
                sender="hub")
        other = f"m{self.seq}"
        self.ev("session.started", "a")
        go = [m for m in self.st["messages"].values() if m["session"] == "a" and "wait for CI" in m["text"]][-1]["id"]
        self.ev("turn.started", "a", marker=other)
        self.ev("turn.result", "a", text="The phone is fine")
        self.ev("turn.stopped", "a")
        self.assertIn(go, self.queued("a"))

    def test_the_continuation_of_a_turn_that_asked_its_card_itself_is_the_tasks(self):
        # the cut turn asked the owner and worked on: its "continue" is the task's, its result the reply, as before
        self.ev("message.received", None, text="count", channel="telegram", owner=True)
        self.ev("delegation.created", None, session="a", **{"for": f"m{self.seq}"}, channel="session", owner=False,
                sender="hub")
        did = f"d{self.seq}"
        self.ev("turn.started", "a", marker=self.st["delegations"][did]["message"])
        self.ev("permission.requested", "a", request_id="r1", tool="AskUserQuestion", interactive=True,
                input={"questions": [{"question": "Which color?"}]})
        self.ev("session.exited", "a", why="reconcile")
        self.ev("session.started", "a")
        go = [m for m in self.st["messages"].values() if m["session"] == "a" and m.get("for_delegation") == did
              and m["id"] != self.st["delegations"][did]["message"]][-1]["id"]
        self.ev("turn.started", "a", marker=go)
        self.ev("turn.result", "a", text="Finished the count: 42 (the color question is still open)")
        d = self.st["delegations"][did]
        self.assertEqual(d.get("reply"), "Finished the count: 42 (the color question is still open)")

    def ask_hub_to_restart(self, who="a", end=True, waits=True):
        """`who` writes the hub "restart the hub" mid-task and ends its turn; the hub, in the turn for that message,
        runs hub restart. `waits` means it says it is waiting for the restart (hub waiting): only a waiting one is
        woken (owner decision)."""
        self.ev("message.received", None, to="hub", text="restart the whole hub: new code", channel="session",
                owner=False, sender=who)
        ask = f"m{self.seq}"
        if waits:
            self.ev("session.waiting", who, text="hub restart", channel="session", owner=False, sender=who)
        if end:
            self.ev("turn.result", who, text="will continue after the restart")
            self.ev("turn.stopped", who)
        self.ev("turn.started", "hub", marker=ask)
        self.from_hub(why="new code")
        self.ev("turn.result", "hub", text="—")
        self.ev("turn.stopped", "hub")

    def restart_now(self, **started):
        self.ev("hub.restart_started", None, parts=[], **started)
        self.ev("core.started", None, pid=1)

    def asked_go(self, name):
        return [m for m in self.st["messages"].values() if m["session"] == name and m.get("origin") == forms.ORIGIN_CORE
                and "was restarted on a request" in m["text"]]

    def test_who_asked_the_hub_for_a_restart_mid_task_is_told_to_continue(self):
        # 23.10: a session asked the hub to restart the hub and ended its turn with "I will continue after the restart":
        # the turn is not cut, there is no CUT_TURN, and after the restart it stood idle until someone wrote to it
        self.ev("message.received", None, to="a", text="update and check", channel="telegram", owner=True)
        owner_mid = f"m{self.seq}"
        self.ev("turn.started", "a", marker=owner_mid)
        self.ask_hub_to_restart()
        self.restart_now()
        (go,) = self.asked_go("a")
        self.assertEqual(go.get("re"), owner_mid)                  # the continuation replies to the owner's word
        self.assertEqual(self.st["sessions"]["a"]["state"], "idle")
        self.assertIsNotNone(self.ev("session.parked", "a", channel="core", why="restart"))   # not parked: it has something to do
        self.assertEqual([m for m in self.st["messages"].values() if m["session"] == "a"
                          and "restart cut off" in m["text"]], [])

    def test_no_continue_after_a_new_turn_sleep_or_a_restart_the_owner_asked(self):
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.ask_hub_to_restart()
        self.ev("message.received", None, to="a", text="drop it, do something else", channel="web", owner=True)
        self.ev("turn.started", "a", marker=f"m{self.seq}")   # a new task before the restart: the earlier "continue" is stale
        self.ev("turn.result", "a", text="ok")
        self.ev("turn.stopped", "a")
        self.restart_now()
        self.assertEqual(self.asked_go("a"), [])

        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.ask_hub_to_restart()
        self.ev("session.parked", "a", channel="core", why="idle")   # went to sleep before the restart
        self.restart_now()
        self.assertEqual(self.asked_go("a"), [])

        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.ask_hub_to_restart()
        self.ev("message.received", None, to="a", text="and this too", channel="session", owner=False, sender="hub")
        self.restart_now()   # a message from before the restart waits in the queue and wakes it, no second "continue" needed
        self.assertEqual(self.asked_go("a"), [])

        self.ev("message.received", None, to="hub", text="restart the hub", channel="cli", owner=True)
        self.ev("turn.started", "hub", marker=f"m{self.seq}")   # the owner asked, so the hub reports to them
        self.from_hub()
        self.restart_now()
        self.assertEqual([m for m in self.st["messages"].values() if "was restarted on a request" in m["text"]], [])

    def test_the_continue_carries_the_task_the_turn_worked_on(self):
        # PR review: by the restart the assignment is already off the session (it handed in the turn result), but the
        # continuation is still the same assignment, otherwise the result of the continued work would not reach the
        # assigner
        self.ev("delegation.created", None, session="a", context="merge and check", **OWNER)   # the owner's task
        did = f"d{self.seq}"
        self.ev("turn.started", "a", marker=self.st["delegations"][did]["message"])
        self.ask_hub_to_restart()
        self.restart_now()
        (go,) = self.asked_go("a")
        self.assertEqual(go.get("for_delegation"), did)

    def test_a_new_ask_between_the_restart_and_the_rise_keeps_who_to_wake(self):
        # PR review: a whole-hub request that arrives after hub.restart_started and before the restart does not lose
        # whom to wake
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.ask_hub_to_restart()
        self.ev("hub.restart_started", None, parts=[])
        self.from_hub(why="once more")
        self.from_hub(why="and more")   # second review round: and a third request while the second waits
        self.ev("core.started", None, pid=1)
        self.assertEqual(len(self.asked_go("a")), 1)

    def test_a_core_that_restarted_while_waiting_still_wakes_who_asked(self):
        # the core came up while the restart was waiting for busy sessions (it crashed): the requester is still waiting
        # and idle, so it is woken as after a requested restart (one rule, no separate "restart started" flag)
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.ask_hub_to_restart()
        self.ev("core.started", None, pid=1)
        self.assertEqual(len(self.asked_go("a")), 1)

    def test_a_wait_mark_set_after_the_restart_began_wakes_only_with_an_open_task(self):
        # the `hub waiting` mark comes later, not with the request, and there is no assignment: it was not waiting for
        # the restart, nothing to wake it for
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.ask_hub_to_restart(waits=False)
        self.ev("session.waiting", "a", text="review", channel="session", owner=False, sender="a")
        self.restart_now()
        self.assertEqual(self.asked_go("a"), [])

    def test_only_the_message_that_started_the_hub_turn_counts_as_the_ask(self):
        # PR review: someone else's message (also) was taken into the hub's turn too; its author did not ask for a
        # restart, so wake only the one whose message started the turn
        self.ev("session.requested", "b", cwd="/w", session_id="s-b", **OWNER)
        self.ev("session.started", "b")
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.ev("message.received", None, to="hub", text="restart the whole hub: new code", channel="session",
                owner=False, sender="a")
        ask = f"m{self.seq}"
        self.ev("session.waiting", "a", text="hub restart", channel="session", owner=False, sender="a")
        self.ev("turn.result", "a", text="will continue after the restart")
        self.ev("turn.stopped", "a")
        self.ev("turn.started", "b", marker=None, prompt_head="y")
        self.ev("message.received", None, to="hub", text="how is the task going?", channel="session", owner=False,
                sender="b")
        other = f"m{self.seq}"
        self.ev("session.waiting", "b", text="the hub's answer", channel="session", owner=False, sender="b")
        self.ev("turn.result", "b", text="asked the hub")
        self.ev("turn.stopped", "b")
        self.ev("turn.started", "hub", marker=ask, also=[other])
        self.from_hub(why="new code")
        self.ev("turn.result", "hub", text="—")
        self.ev("turn.stopped", "hub")
        self.restart_now()
        self.assertEqual(len(self.asked_go("a")), 1)
        self.assertEqual(self.asked_go("b"), [])

    def test_only_a_session_that_waits_is_woken(self):
        # owner decision: wake a session only if it is waiting. It handed in a report and waits for nothing, so do not
        # wake it (in the owner's journal almost all such requests are the end of the work, and "nothing to continue"
        # would cost a turn)
        self.ev("delegation.created", None, session="a", context="merge", **OWNER)
        did = f"d{self.seq}"
        self.ev("turn.started", "a", marker=self.st["delegations"][did]["message"])
        self.ask_hub_to_restart(waits=False)
        self.assertEqual(self.st["delegations"][did]["state"], "replied")
        self.restart_now()
        self.assertEqual(self.asked_go("a"), [])
        # a hub waiting mark older than WAITING_HOLD at request time is forgotten and does not count as waiting
        from core import executors
        self.ev("session.waiting", "a", text="review", channel="session", owner=False, sender="a")
        self.later = executors.WAITING_HOLD + 60
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.ask_hub_to_restart(waits=False)
        self.restart_now()
        self.assertEqual(self.asked_go("a"), [])

    def test_a_session_that_waited_when_it_asked_is_woken_after_a_long_wait_and_the_hubs_reply(self):
        # review: the hub's reply "restarting" makes its mark stale, and the restart waits for busy sessions longer
        # than WAITING_HOLD, so "waiting" is judged at the moment of the request
        from core import executors
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.ask_hub_to_restart()
        self.ev("message.received", None, to="a", text="restarting", channel="session", owner=False, sender="hub")
        self.ev("turn.started", "a", marker=f"m{self.seq}")   # read the hub's reply: a new turn
        self.ev("turn.result", "a", text="—")
        self.ev("turn.stopped", "a")
        self.later = executors.WAITING_HOLD + 60
        self.restart_now()
        self.assertEqual(self.asked_go("a"), [])   # a new turn after the request: the earlier "continue" is dropped, as before

        self.later = 0
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.ask_hub_to_restart()
        self.ev("message.received", None, to="a", text="restarting", channel="session", owner=False, sender="hub",
                fyi=True)
        self.later = executors.WAITING_HOLD + 60
        self.restart_now()
        self.assertEqual(len(self.asked_go("a")), 1)

    def test_a_turn_the_ceiling_cut_gets_only_the_cut_continue(self):
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.ask_hub_to_restart(end=False)                  # the turn runs up to the ceiling
        self.restart_now(cut=["a: a turn is running"])
        go = self.cut_by_restart()
        self.assertIn("A hub restart cut off", go["text"])
        self.assertEqual(self.asked_go("a"), [])

    def test_an_executor_that_handed_in_does_not_wake_who_got_its_answer(self):
        # an executor merged a release, asked the hub to restart the hub and handed in the task; the assigner took the
        # reply and ended its turn with "I will check after the restart". Only the requester is woken: the executor
        # handed in (nothing to wait for), the assigner did not ask the hub and will write itself (the worst case is
        # as before this note)
        self.ev("session.requested", "b", cwd="/w", session_id="s-b", **OWNER)
        self.ev("session.started", "b")
        self.ev("delegation.created", None, session="b", context="merge the release", channel="session", owner=False,
                sender="a", for_session="a")
        did = f"d{self.seq}"
        self.ev("turn.started", "b", marker=self.st["delegations"][did]["message"])
        self.ev("message.received", None, to="hub", text="restart the whole hub", channel="session", owner=False,
                sender="b")
        ask = f"m{self.seq}"
        self.ev("turn.result", "b", text="merged, the hub is asked to restart")
        self.ev("turn.stopped", "b")
        (answer,) = [m for m in self.st["messages"].values() if m["session"] == "a" and m.get("task") == did]
        self.ev("turn.started", "a", marker=answer["id"])
        self.ev("turn.started", "hub", marker=ask)
        self.from_hub()
        self.ev("turn.result", "hub", text="—")
        self.ev("turn.stopped", "hub")
        self.ev("session.waiting", "a", text="hub restart", channel="session", owner=False, sender="a")
        self.ev("turn.result", "a", text="after the restart I will check that the hub is up")
        self.ev("turn.stopped", "a")
        self.restart_now()
        self.assertEqual(self.asked_go("a"), [])
        self.assertEqual(self.asked_go("b"), [])

    def test_cancel(self):
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.from_hub()
        self.assertIsNone(self.ev("hub.restart_cancelled", None, **OWNER))
        self.assertNotIn("restart", self.st)
        self.assertEqual(self.ev("hub.restart_cancelled", None, **OWNER), "no restart was requested")

    def test_whole_hub_only_owner_or_hub_session(self):
        why = self.ev("hub.restart_requested", None, channel="session", owner=False, sender="a")
        self.assertIn("hub", why)
        self.assertNotIn("restart", self.st)

    def test_parts_go_at_once_from_any_session(self):
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.assertIsNone(self.ev("hub.restart_requested", None, channel="session", owner=False, sender="a",
                                  parts=["web", "telegram"]))
        self.assertEqual(restart.due(self.st), {"parts": ["web", "telegram"]})
        self.ev("hub.restart_started", None, parts=["web", "telegram"])
        self.assertIsNone(restart.due(self.st))
        self.assertIn("hubd", self.ev("hub.restart_requested", None, parts=["hubd"], **OWNER))
        self.assertIn("no such part", self.ev("hub.restart_requested", None, parts=["nope"], **OWNER))

    # --- the page holds what the server does not have: a recording on the page must not be cut by a hub restart
    # (including an automatic one, after an update or an exchange between hubs)
    def hold(self, *holds, tab="t1", at=None):
        if at is not None:
            self.seq = at - 1
        return self.ev("page.held", None, tab=tab, holds=list(holds), channel="web", owner=True)

    def test_a_recording_on_the_page_holds_the_whole_restart_until_it_is_delivered(self):
        self.hold("video-feedback", at=100)
        self.from_hub(why="exchange")
        self.assertIsNone(restart.due(self.st, 130))
        self.assertIn("a recording on the page", en(restart.note, self.st, 130))
        self.hold("video-feedback", at=150)          # the tab is alive and still holds
        self.assertIsNone(restart.due(self.st, 220))
        self.hold(at=230)                            # delivered: the hold is released
        self.assertEqual(restart.due(self.st, 231), {"parts": []})

    def test_a_closed_tab_stops_holding_by_itself(self):
        self.hold("voice", at=100)
        self.from_hub()
        self.assertIsNone(restart.due(self.st, 100 + restart.HOLD_TTL - 1))
        self.assertEqual(restart.due(self.st, 100 + restart.HOLD_TTL), {"parts": []})   # no word from it means no tab

    def test_a_recording_started_late_is_not_cut_by_the_sessions_ceiling(self):
        # 05.11: a restart asked at 11:04:02 waited busy sessions up to 20 min from the request; the owner started a
        # screen recording at 11:15:16 and the hub restarted at 11:24:02 in the middle of it (the recording was lost).
        # A recording on the page holds the restart for as long as it goes, not only up to restart_wait_minutes
        self.ev("turn.started", "a", marker=None, prompt_head="x")   # a busy session keeps the restart waiting
        self.from_hub(why="new code")
        since = self.st["restart"]["since"]
        self.assertIsNone(restart.due(self.st, since + 600))
        end = since + restart.WAIT_MINUTES * 60
        for at in range(since + 674, end + 1, 30):        # page.held every 30 s, as the tab sends it, up to the ceiling
            self.hold("plugin", at=at)
        self.assertIn("then also a: a turn is running; waits for them up to 9 min more, then a restart without them",
                      en(restart.note, self.st, end - 500))
        self.assertIsNone(restart.due(self.st, end))
        self.assertIn("a recording on the page", en(restart.note, self.st, end))
        self.assertIn("then also a: a turn is running; the ceiling has passed — a restart without them",
                      en(restart.note, self.st, end))
        self.assertIn("waits without a ceiling", en(restart.note, self.st, end, minutes=0))
        self.hold(at=end + 30)                            # delivered: the hold is released
        self.assertEqual(plain(restart.due(self.st, end + 31)), {"parts": [], "cut": ["a: a turn is running"]})

    def test_the_page_holds_the_restart_only_up_to_its_own_ceiling(self):
        self.hold("video-feedback", at=100)
        self.from_hub()
        since = self.st["restart"]["since"]
        end = since + restart.PAGE_MINUTES * 60      # a tab that forgot its mark does not stop updates for good
        self.hold("video-feedback", at=end - 10)
        self.assertIsNone(restart.due(self.st, end - 1))
        want = restart.due(self.st, end)
        self.assertEqual(plain(want), {"parts": [], "page": "not delivered (video-feedback)"})
        # the hub is told after the restart that the recording did not wait, separately from "the core said to continue"
        # for cut turns
        self.ev("hub.restart_started", None, **want)
        self.ev("core.started", None, pid=1)
        (told,) = [m for m in self.st["messages"].values() if m["session"] == "hub" and "was restarted" in m["text"]]
        self.assertIn("a recording on the page did not wait out the ceiling (not delivered (video-feedback)) — it may "
                      "have been cut", told["text"])
        self.assertNotIn("told them to continue", told["text"])

    def test_the_cut_and_the_page_reach_the_owner_in_his_language_through_the_journal(self):
        # the cut sessions and the page's hold are the core's phrases: the hub reads the English, the Russian owner the
        # Russian — their marks ride in hub.restart_started (cut_l10n, page_l10n) and in the state
        from core import coretext
        self.ev("turn.started", "a", marker=None, prompt_head="x")
        self.from_hub(why="new code")
        since = self.st["restart"]["since"]
        self.hold("video-feedback", at=since + restart.PAGE_MINUTES * 60 - 10)
        want = json.loads(json.dumps(restart.due(self.st, since + restart.PAGE_MINUTES * 60)))   # as the journal keeps it
        self.assertEqual(want["cut"], ["a: a turn is running"])
        self.ev("hub.restart_started", None, **want)
        self.ev("core.started", None, pid=1)
        (told,) = [m for m in self.st["messages"].values() if m["session"] == "hub" and "was restarted" in m["text"]]
        self.assertIn("a: a turn is running", told["text"])
        ru = coretext.shown(told, "ru")
        self.assertIn(i18n.tr("ru", "core.restart.cut", cut="a: " + i18n.tr("ru", "core.restart.busy")), ru)
        self.assertIn(i18n.tr("ru", "core.restart.page_cut", page=i18n.tr("ru", "core.restart.not_delivered",
                                                                          what="video-feedback")), ru)
        self.assertNotIn("turn is running", ru)
        # an earlier core's record (no marks) shows as it is
        self.ev("hub.restart_requested", None, **OWNER)
        old = "a: an earlier core's words"
        self.ev("hub.restart_started", None, parts=[], cut=[old])
        self.ev("core.started", None, pid=2)
        told = [m for m in self.st["messages"].values() if m["session"] == "hub" and old in m["text"]]
        self.assertEqual(len(told), 1)

    def test_now_of_one_part_does_not_release_the_page_for_the_others(self):
        # --now applies only to the named parts: a page waiting on a recording is not restarted by someone else's hurry
        self.hold("voice", at=100)
        self.ev("hub.restart_requested", None, parts=["web"], **OWNER)
        self.ev("hub.restart_requested", None, parts=["telegram", "ext:a:b"], now=True, **OWNER)
        self.assertEqual(restart.due(self.st, 101), {"parts": ["telegram", "ext:a:b"]})
        self.ev("hub.restart_started", None, parts=["telegram", "ext:a:b"])
        self.assertIsNone(restart.due(self.st, 102))
        self.ev("hub.restart_requested", None, parts=["ext:a:b"], **OWNER)   # again, now without --now: it waits
        self.assertIsNone(restart.due(self.st, 103))
        self.hold(at=104)
        self.assertEqual(restart.due(self.st, 105), {"parts": ["web", "ext:a:b"]})

    def test_parts_left_by_an_older_core_do_not_wait_for_the_page(self):
        # a request from the old core has no deadline: the page waiting for it would have no ceiling, so it goes at
        # once, as it did under the old core
        self.st["restart_parts"], self.st["restart_parts_now"] = ["web"], True   # and the flag of an earlier version
        self.hold("voice", at=100)
        self.assertEqual(restart.due(self.st, 101), {"parts": ["web"]})
        self.assertIsNone(restart.parts_note(self.st, 101))
        self.assertIsNone(self.ev("hub.restart_requested", None, parts=["telegram"], now=True, **OWNER))
        self.assertIsNone(self.ev("hub.restart_started", None, parts=["telegram"]))

    def test_parts_waiting_from_an_older_core_take_new_requests(self):
        # state of the old core: parts wait with no deadline and no hurry; a new request does not crash the fold, and
        # the deadline counts from it
        self.st["restart_parts"] = ["web"]
        self.hold("voice", at=100)
        self.assertIsNone(self.ev("hub.restart_requested", None, parts=["ext:a:b"], **OWNER))
        self.assertEqual(self.st["restart_parts_since"], 101)
        self.assertIsNone(restart.due(self.st, 102))
        end = 101 + restart.PAGE_MINUTES * 60
        self.hold("voice", at=end - 10)                       # the tab is alive and holds until the ceiling
        self.assertIsNone(restart.due(self.st, end - 1))
        self.assertEqual(restart.due(self.st, end)["parts"], ["web", "ext:a:b"])

    def test_the_page_waits_for_the_longer_of_its_ceiling_and_the_sessions_one(self):
        # restart_wait_minutes above PAGE_MINUTES raises the page ceiling too; 0 means no ceiling at all
        self.hold("voice", at=100)
        self.from_hub()
        since = self.st["restart"]["since"]
        at = since + restart.PAGE_MINUTES * 60 + 60
        self.hold("voice", at=at - 150)                   # the tab still holds just before the page ceiling
        self.assertIn(f"no longer than {restart.PAGE_MINUTES} min", en(restart.note, self.st, at - 120, minutes=20))
        self.hold("voice", at=at - 30)
        self.assertIsNone(restart.due(self.st, at, minutes=restart.PAGE_MINUTES + 5))
        self.assertEqual(restart.due(self.st, at, minutes=20)["parts"], [])
        self.assertIsNone(restart.due(self.st, at, minutes=0))
        self.assertIn("no ceiling", en(restart.note, self.st, at, minutes=0))

    def test_now_does_not_wait_for_the_page(self):
        self.hold("video-feedback", at=100)
        self.ev("hub.restart_requested", None, now=True, **OWNER)
        self.assertEqual(restart.due(self.st, 101), {"parts": []})

    def test_a_held_page_holds_web_and_plugin_parts_but_not_the_rest(self):
        self.hold("video-feedback", at=100)
        self.ev("hub.restart_requested", None, parts=["web", "telegram", "ext:me:video-feedback"], **OWNER)
        self.assertEqual(restart.due(self.st, 101), {"parts": ["telegram"]})
        self.ev("hub.restart_started", None, parts=["telegram"])
        self.assertIsNone(restart.due(self.st, 102))
        self.assertIn("a recording on the page", en(restart.parts_note, self.st, 102))
        self.hold(at=103)
        self.assertEqual(restart.due(self.st, 104), {"parts": ["web", "ext:me:video-feedback"]})

    def test_only_the_page_reports_its_holds(self):
        why = self.ev("page.held", None, tab="t1", holds=["x"], channel="session", owner=False, sender="a")
        self.assertIsNotNone(why)
        self.assertFalse(self.st.get("page_holds"))


class Cli(unittest.TestCase):
    def cli(self, *args, **env):
        state = tempfile.mkdtemp(prefix="restart-")
        e = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0", **env}
        self.done = subprocess.run([sys.executable, HUB, "restart", *args], env=e, capture_output=True, text=True)
        return [ev for _, ev in spool.take(os.path.join(state, "spool"))]

    def test_commands(self):
        (ev,) = self.cli("--now", "new", "code", HUB_SESSION="hub")
        self.assertEqual(ev["type"], "hub.restart_requested")
        self.assertEqual({k: ev["data"][k] for k in ("parts", "now", "why", "sender")},
                         {"parts": [], "now": True, "why": "new code", "sender": "hub"})
        (ev,) = self.cli("web", "telegram")
        self.assertEqual((ev["data"]["parts"], ev["data"]["why"]), (["web", "telegram"], None))
        (ev,) = self.cli("--cancel")
        self.assertEqual(ev["type"], "hub.restart_cancelled")

    def test_a_module_part_restarts_only_the_module(self):
        # `mod:<module>` was not counted as a part: a restart of the whole hub went out with why "mod:x"
        cfg = os.path.join(tempfile.mkdtemp(), "hub.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"extensions": {"x": {"path": "/x", "run": ["python3", "main.py"]}}}, f)
        (ev,) = self.cli("mod:notes", "ext:x", HUB_CONFIG=cfg)
        self.assertEqual((ev["data"]["parts"], ev["data"]["why"]), (["mod:notes", "ext:x"], None))

    def test_a_plugin_part_is_named_in_any_case_and_an_unknown_one_is_refused(self):
        # 20.10: `hub restart ext:Alice:stats` (an account with capitals, as in the marketplace) was "accepted" while
        # hub-up silently skipped it: the plugin key is lowercase. An unknown part is a refusal to the command, not
        # "accepted" and silence in the hub-up log
        cfg = os.path.join(tempfile.mkdtemp(), "hub.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"extensions": {"alice:stats": {"path": "/x", "run": ["python3", "main.py"]}}}, f)
        (ev,) = self.cli("ext:Alice:Stats", "web", HUB_CONFIG=cfg)
        self.assertEqual(ev["data"]["parts"], ["ext:alice:stats", "web"])
        self.assertEqual(self.cli("ext:alice:nope", HUB_CONFIG=cfg), [])
        self.assertNotEqual(self.done.returncode, 0)
        self.assertIn("ext:alice:nope", self.done.stderr)

    def test_parts_with_an_explanation_do_not_become_the_whole_hub(self):
        # another hub's report: `hub restart ext:x because it hung` went out as a restart of the whole hub: the words
        # that are not all parts were all taken for the reason. The leading words that are parts stay parts, the rest
        # is the explanation
        cfg = os.path.join(tempfile.mkdtemp(), "hub.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"extensions": {"x": {"path": "/x", "run": ["python3", "main.py"]}}}, f)
        (ev,) = self.cli("ext:x", "hung", "after", "the", "update", HUB_CONFIG=cfg)
        self.assertEqual((ev["data"]["parts"], ev["data"]["why"]), (["ext:x"], None))
        (ev,) = self.cli("web", "ext:x", "--now", "page", "is", "stuck", HUB_CONFIG=cfg)
        self.assertEqual((ev["data"]["parts"], ev["data"]["now"]), (["web", "ext:x"], True))
        # a bare part name followed by text is an ordinary sentence: the whole hub, as before
        (ev,) = self.cli("update", "the", "schedule", "module", HUB_CONFIG=cfg)
        self.assertEqual((ev["data"]["parts"], ev["data"]["why"]), ([], "update the schedule module"))
        # words that open with a reason stay the reason of a whole-hub restart, a part named later in it included
        (ev,) = self.cli("new", "code", "for", "web")
        self.assertEqual((ev["data"]["parts"], ev["data"]["why"]), ([], "new code for web"))

    def test_a_plugin_gone_from_settings_but_alive_is_found_by_the_cli(self):
        # a plugin whose key was removed from `extensions` keeps running until hub-up is told to stop it: the CLI
        # used to refuse `ext:<name>` as "no such part" because it knew only the settings
        state = tempfile.mkdtemp(prefix="restart-")
        cfg = os.path.join(tempfile.mkdtemp(), "hub.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"extensions": {"kept": {"path": "/x", "run": ["python3", "main.py"]}}}, f)
        # a live process with "hub-up" in its command line stands in for hub-up (restart.running checks both)
        fake = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "hub-up"])
        try:
            with open(os.path.join(state, restart.UP), "w", encoding="utf-8") as f:
                json.dump({"pid": fake.pid, "exts": {"ext:Old:plug": state}}, f)
            e = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0", "HUB_CONFIG": cfg}
            done = subprocess.run([sys.executable, HUB, "restart", "ext:old:plug"], env=e, capture_output=True, text=True)
            evs = [ev for _, ev in spool.take(os.path.join(state, "spool"))]
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual([ev["data"]["parts"] for ev in evs], [["ext:Old:plug"]])
            done = subprocess.run([sys.executable, HUB, "restart", "ext:other:plug"], env=e, capture_output=True, text=True)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("ext:other:plug", done.stderr)
            self.assertIn("ext:kept", done.stderr)
        finally:
            fake.kill()
            fake.wait(20)

    def test_an_exact_plugin_name_wins_over_another_case(self):
        # review: keys `Foo` and `foo`: an exact name is not rewritten into its neighbor
        exts = {"Foo": {}, "foo": {}}
        self.assertEqual(restart.ext_parts(["ext:Foo", "ext:foo", "ext:FOO"], exts)[0][:2], ["ext:Foo", "ext:foo"])
        self.assertEqual(restart.ext_parts(["ext:Alice:X"], {"alice:x": {}}), (["ext:alice:x"], []))


class Daemon(unittest.TestCase):
    """hubd: a busy session holds the restart up to the ceiling from hub.json; then the file for hub-up; after the
    restart a cut session gets "continue" on the same task and the hub is told whom it did not wait for."""

    def test_the_ceiling_restarts_and_the_cut_turn_continues(self):
        from core.daemon import Hub

        class Host:
            def alive(self, name):
                return False   # no processes after the restart

        state, now = tempfile.mkdtemp(prefix="restart-d-"), [1000.0]
        h = Hub(state, Host(), registry_dir=state, clock=lambda: now[0])
        h.execute = lambda a: None
        h.config["restart_wait_minutes"] = 5
        h.record("session.requested", "a", {"cwd": "/w", "session_id": "s-a", **OWNER})
        h.record("session.started", "a", {})
        h.record("message.received", None, {"to": "a", "text": "do it", "channel": "telegram", "owner": True})
        mid = f"m{h.state['seq']}"
        h.record("turn.started", "a", {"marker": mid})
        h.record("hub.restart_requested", None, {"channel": "cli", "owner": True})
        flag = os.path.join(state, restart.FLAG)
        now[0] += 4 * 60
        h.restart()
        self.assertFalse(os.path.exists(flag))
        now[0] += 60
        h.restart()
        with open(flag) as f:
            self.assertEqual(json.load(f), {"parts": []})   # hub-up reads only the parts: the format is unchanged
        h.record("core.started", None, {"pid": 2})
        h.reconcile()
        h.record("session.started", "a", {})
        go = [m for m in h.state["messages"].values() if m["session"] == "a"][-1]
        self.assertIn("A hub restart cut off", go["text"])
        self.assertEqual(go.get("re"), mid)

    def hub(self):
        from core.daemon import Hub

        class Host:
            def alive(self, name):
                return False

        state = tempfile.mkdtemp(prefix="restart-f-")
        h = Hub(state, Host(), registry_dir=state, clock=lambda: 1000.0)
        h.execute = lambda a: None
        return h, os.path.join(state, restart.FLAG)

    def test_parts_after_a_full_restart_do_not_overwrite_it(self):
        # after update.applied: the whole hub and web are two requests in a row, and hub-up reads the file once a
        # second. The second overwrote the first: hub-up restarted only web and the processes stayed on the old code
        from core import journal
        h, flag = self.hub()
        h.record("hub.restart_requested", None, {"channel": "cli", "owner": True, "parts": ["web"]})
        h.record("hub.restart_requested", None, {"channel": "cli", "owner": True})
        h.restart()
        h.restart()
        with open(flag) as f:
            self.assertEqual(json.load(f), {"parts": []})
        started = [e["data"]["parts"] for e in journal.read(h.journal.path) if e["type"] == "hub.restart_started"]
        self.assertEqual(started, [[]])   # the fact is what lies in the file
        self.assertEqual(h.state["restart_parts"], ["web"])

    def test_a_request_waits_until_hub_up_takes_the_previous_one(self):
        h, flag = self.hub()
        h.record("hub.restart_requested", None, {"channel": "cli", "owner": True, "parts": ["telegram"]})
        h.restart()
        h.record("hub.restart_requested", None, {"channel": "cli", "owner": True, "parts": ["files"]})
        h.restart()
        with open(flag) as f:
            self.assertEqual(json.load(f), {"parts": ["telegram"]})
        os.unlink(flag)   # hub-up has read it
        h.restart()
        with open(flag) as f:
            self.assertEqual(json.load(f), {"parts": ["files"]})
        self.assertNotIn("restart_parts", h.state)
        # nobody has read the file for a long time (the earlier service layout without hub-up): the request goes on top,
        # as before
        old = time.time() - restart.UNREAD - 1
        os.utime(flag, (old, old))
        h.record("hub.restart_requested", None, {"channel": "cli", "owner": True})
        h.restart()
        with open(flag) as f:
            self.assertEqual(json.load(f), {"parts": []})


NEW = "b" * 40   # the commit the update installed


class Heal(unittest.TestCase):
    """An update to the version that fixes the request race is still driven by the old hubd: on some hubs only the
    page restarts, while the core and modules stay on the old code with the new tree (26.10, test stand). The page is
    already new code: it sees that the core started before the applied update and asks for an ordinary restart of the
    whole hub, once per version."""

    def setUp(self):
        self.st, self.seq = reducer.empty(), 0

    def ev(self, type, ts, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts, "type": type, "entity": None,
                                                   "data": data})
        return why

    def test_the_core_is_behind_when_it_started_before_the_applied_update(self):
        self.assertIsNone(restart.behind(self.st))   # there were no updates
        self.ev("core.started", 100, pid=1)
        self.ev("update.applied", 200, to=NEW, **{"from": "a" * 40})
        self.assertEqual(restart.behind(self.st), NEW)
        self.assertEqual(restart.behind(self.st, NEW), NEW)
        self.assertIsNone(restart.behind(self.st, "c" * 40))   # the disk already has other code (a rollback, own commits)
        self.ev("update.failed", 250, why="tests")   # a failed one afterwards did not change the code
        self.assertEqual(restart.behind(self.st), NEW)
        self.ev("core.started", 300, pid=2)
        self.assertIsNone(restart.behind(self.st))
        stale = dict(self.st, started_seq=1)   # the start number lags (the old core folded the start), so time decides
        self.assertIsNone(restart.behind(stale))
        self.ev("update.applied", 400, to="c" * 40)
        self.ev("core.started", 390, pid=3)   # the clock stepped back: the core still started after the update
        self.assertIsNone(restart.behind(self.st))

    def heal(self, ts):
        return self.ev("hub.restart_requested", ts, parts=[], why=restart.HEAL_WHY, heal=NEW, channel="web", owner=True)

    def test_no_heal_while_the_whole_hub_restart_waits_or_has_just_started(self):
        self.ev("core.started", 100, pid=1)
        self.ev("update.applied", 200, to=NEW)
        self.ev("hub.restart_requested", 201, parts=[], why="update: 1 commit(s)", channel="cli", owner=True)
        self.assertIsNotNone(self.heal(400))   # waits for busy sessions: its own reason stays
        self.assertEqual(self.st["restart"]["why"], "update: 1 commit(s)")
        self.ev("hub.restart_started", 500, parts=[])
        self.assertIsNotNone(self.heal(500 + restart.HEAL_AFTER - 1))   # hub-up is bringing the core up: do not ask a second time
        self.assertEqual(self.st["restart"]["state"], "started")
        # started long ago, yet the core is still the old one: the old hubd overwrote the request with a part, so ask
        self.assertIsNone(self.heal(500 + restart.HEAL_AFTER))
        self.assertEqual(self.st["restart"]["state"], "waiting")

    def test_the_owner_cancelling_the_restart_after_an_update_stops_healing(self):
        self.ev("core.started", 100, pid=1)
        self.ev("update.applied", 200, to=NEW)
        self.ev("hub.restart_requested", 201, parts=[], channel="cli", owner=True)
        self.ev("hub.restart_cancelled", 300, channel="cli", owner=True)
        self.assertIsNone(restart.heal(self.st, 1000))
        self.assertIsNotNone(self.heal(1000))

    def test_a_heal_request_restarts_the_whole_hub_once_per_version(self):
        self.ev("core.started", 100, pid=1)
        self.ev("update.applied", 200, to=NEW)
        self.assertIsNone(self.heal(300))
        self.assertEqual((self.st["restart"]["state"], self.st["restart"]["why"]), ("waiting", restart.HEAL_WHY))
        self.assertEqual(self.st["restart_heal"], NEW)
        self.ev("hub.restart_started", 301, parts=[])
        self.ev("core.started", 310, pid=2)
        self.assertEqual(self.st["restart_heal"], NEW)   # the mark survives the restart
        # the request lay in the mailbox until the core came up on the new code: it is not needed, do not ask for a
        # second restart
        self.assertIsNotNone(self.heal(320))
        self.assertNotIn("restart", self.st)

    def web(self, events, head=NEW):
        from core import journal
        from modules.web.server import Web
        state = tempfile.mkdtemp(prefix="restart-heal-")
        self.addCleanup(shutil.rmtree, state, ignore_errors=True)
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        for type, ts, data in events:
            j.append(type, None, data, ts=ts)
        web = Web(state)
        self.addCleanup(web.stop.set)
        web.disk_head = lambda: head
        web.tail.poll()
        return web

    def asked(self, web):
        return [e["data"] for _, e in spool.take(web.spool) if e["type"] == "hub.restart_requested"]

    def test_the_page_asks_for_a_full_restart_once_when_the_core_is_behind(self):
        web = self.web([("core.started", 100, {"pid": 1}), ("update.applied", 200, {"to": NEW})])
        later = web.started + restart.HEAL_AFTER
        web.heal(web.started + 1)
        self.assertEqual(self.asked(web), [])   # right after the restart, no: an ordinary whole-hub restart is under way
        web.heal(later)
        got = self.asked(web)
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["parts"], got[0]["now"], got[0]["heal"], got[0]["channel"]), ([], False, NEW, "web"))
        self.assertIn("the previous code", got[0]["why"])
        web.heal(later + 60)
        self.assertEqual(len(self.asked(web)), 1)   # at most once per version
        # the page was restarted, while the core is still the old one and has already written the request: the mark is
        # in the state
        again = self.web([("core.started", 100, {"pid": 1}), ("update.applied", 200, {"to": NEW}),
                          ("hub.restart_requested", 300, {"parts": [], "heal": NEW, "channel": "web", "owner": True}),
                          ("hub.restart_started", 301, {"parts": []})])
        again.heal(again.started + restart.HEAL_AFTER)
        self.assertEqual(self.asked(again), [])

    def test_the_page_does_not_ask_when_there_is_nothing_to_heal(self):
        cases = {
            "core on the new code": [("update.applied", 100, {"to": NEW}), ("core.started", 200, {"pid": 1})],
            "restart of the whole hub already waits for busy sessions": [
                ("core.started", 100, {"pid": 1}), ("update.applied", 200, {"to": NEW}),
                ("hub.restart_requested", 201, {"parts": [], "channel": "cli", "owner": True})],
            "no updates yet (development hub)": [("core.started", 100, {"pid": 1})],
        }
        for name, events in cases.items():
            web = self.web(events)
            web.heal(web.started + restart.HEAL_AFTER)
            self.assertEqual(self.asked(web), [], name)
        web = self.web([("core.started", 100, {"pid": 1}), ("update.applied", 200, {"to": NEW})], head="c" * 40)
        web.heal(web.started + restart.HEAL_AFTER)
        self.assertEqual(self.asked(web), [], "the code on disk is not the one the update installed")


class Status(unittest.TestCase):
    """`hub status`: who holds the restart and how much longer it waits until the ceiling (friction digest 16.10)."""

    def status(self, cfg):
        from core import journal
        state = tempfile.mkdtemp(prefix="restart-st-")
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        j.append("session.requested", "a", {"cwd": "/w", "session_id": "s-a", **OWNER})
        j.append("session.started", "a", {"session_id": "s-a"})
        j.append("turn.started", "a", {"marker": None, "prompt_head": "x"})
        j.append("hub.restart_requested", None, {"channel": "cli", "owner": True})
        conf = os.path.join(state, "hub.json")
        with open(conf, "w") as f:
            f.write(cfg)
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CONFIG": conf, "HUB_LANG": "en"}   # the English the hub reads
        env.pop("HUB_SESSION", None)
        out = subprocess.run([sys.executable, HUB, "status"], env=env, capture_output=True, text=True).stdout
        return next(ln for ln in out.splitlines() if ln.startswith("the hub restart"))

    def test_who_holds_and_how_long_until_the_ceiling(self):
        self.assertRegex(self.status("{}"), r"a: a turn is running; waits for them up to (19|20) min more, then a restart without them")
        self.assertIn("up to 5 min more", self.status('{"restart_wait_minutes": 5}'))
        self.assertIn("without a ceiling", self.status('{"restart_wait_minutes": 0}'))
        self.assertIn("up to 20 min more", self.status('{"restart_wait_minutes": "x"}'))   # not a number: the default
        self.assertIn("up to 20 min more", self.status("{"))                               # broken hub.json
        self.assertIn("up to 20 min more", self.status("[]"))                              # not an object


class HubUp(unittest.TestCase):
    """hub-up sees the restart file: with parts, only those are restarted; empty means all children and itself (new
    hub-up code)."""

    def test_flag_restarts_children(self):
        state = tempfile.mkdtemp(prefix="hubup-")
        log = os.path.join(state, "starts.log")
        part = os.path.join(state, "part.py")
        with open(part, "w") as f:
            # stops like a real part when hub-up asks (platforms: SIGTERM / the core's stop pipe)
            f.write("import os, sys, time\nsys.path.insert(0, sys.argv[3])\nfrom platforms import current\n"
                    "current.on_core_stop(lambda *_: os._exit(0), lambda: os._exit(0))\n"
                    "open(sys.argv[1], 'a').write(sys.argv[2] + ' %d %s\\n' % (os.getppid(), "
                    "os.environ.get('HUB_RESTART_ASKED', '-')))\n"
                    "time.sleep(60)\n")
        env = {**os.environ, "HUB_STATE_DIR": state,
               "HUB_UP_PARTS": json.dumps({"hubd": [part, log, "hubd", REPO], "web": [part, log, "web", REPO]})}
        p = subprocess.Popen([sys.executable, HUB_UP], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            def lines():
                if not os.path.exists(log):
                    return []
                with open(log) as f:
                    return [x.split() for x in f.read().split("\n") if x]
            starts = lambda: [x[0] for x in lines()]  # noqa: E731
            self.until(lambda: sorted(starts()) == ["hubd", "web"])
            with open(os.path.join(state, "restart"), "w") as f:
                json.dump({"parts": ["web"]}, f)
            self.until(lambda: starts() == ["hubd", "web", "web"])
            self.assertFalse(os.path.exists(os.path.join(state, "restart")))
            # a restart by `hub restart web` carries a mark (the browser then also kills its own Chrome); the first
            # start does not
            self.assertEqual([x[2] for x in lines()], ["-", "-", "1"])
            with open(os.path.join(state, "restart"), "w") as f:
                json.dump({"parts": []}, f)
            self.until(lambda: sorted(starts()) == ["hubd", "hubd", "web", "web", "web"])
            self.assertIsNone(p.poll())  # the same process (launchd never lost it), hub-up code reloaded through exec
            parents = {x[1] for x in lines()}
            if os.name == "nt":  # exec there is a new process: the supervisor (p) started the new hub-up
                self.assertEqual(len(parents), 2)
                self.assertNotIn(str(p.pid), parents)
            else:
                self.assertEqual(parents, {str(p.pid)})
        finally:
            p.terminate()
            p.wait(20)

    def test_a_new_plugin_process_starts_on_restart_and_unknown_parts_are_logged(self):
        # log analysis 04.10: `hub restart ext:<new plugin>` silently did nothing, the process came up only with a
        # full restart; hub-up lines without a date cannot be placed in time
        state = tempfile.mkdtemp(prefix="hubup-")
        log, out, exts = (os.path.join(state, n) for n in ("starts.log", "out.log", "exts.json"))
        part = os.path.join(state, "part.py")
        with open(part, "w") as f:
            f.write("import sys, time\nopen(sys.argv[1], 'a').write(sys.argv[2] + '\\n')\ntime.sleep(60)\n")
        with open(exts, "w") as f:
            json.dump({}, f)
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_UP_EXT_FILE": exts,
               "HUB_UP_PARTS": json.dumps({"hubd": [part, log, "hubd"]})}
        with open(out, "w") as o:
            p = subprocess.Popen([sys.executable, HUB_UP], env=env, stdout=o, stderr=subprocess.STDOUT)
        try:
            starts = lambda: open(log).read().split() if os.path.exists(log) else []  # noqa: E731
            self.until(lambda: starts() == ["hubd"])
            with open(exts, "w") as f:
                json.dump({"ext:fresh": {"cmd": [sys.executable, part, log, "fresh"], "cwd": state}}, f)
            with open(os.path.join(state, "restart"), "w") as f:
                json.dump({"parts": ["ext:fresh", "ext:nope"]}, f)
            self.until(lambda: starts() == ["hubd", "fresh"])
            # the hub-up log is UTF-8 (on Windows the supervisor writes it so), not in the locale encoding: PR #18
            # review
            self.until(lambda: "ext:nope" in self.text(out))
            text = self.text(out)
            self.assertIn("no such part", text)
            self.assertRegex(text, r"(?m)^\d\d\.\d\d \d\d:\d\d:\d\d hub-up: hubd started")
            with open(exts, "w") as f:
                json.dump({"ext:fresh": {"cmd": [sys.executable, part, log, "fresh-v2"], "cwd": state}}, f)
            with open(os.path.join(state, "restart"), "w") as f:
                json.dump({"parts": ["ext:fresh"]}, f)
            self.until(lambda: starts() == ["hubd", "fresh", "fresh-v2"])
        finally:
            p.terminate()
            p.wait(20)

    def test_a_plugin_part_in_another_case_is_restarted(self):
        # 20.10: `hub restart ext:Alice:x`: hub-up logged "no such part" and did nothing, it worked only in
        # lowercase (the plugin key is the account in lowercase); a part name is case-insensitive
        state = tempfile.mkdtemp(prefix="hubup-")
        log, exts = (os.path.join(state, n) for n in ("starts.log", "exts.json"))
        part = os.path.join(state, "part.py")
        with open(part, "w", encoding="utf-8") as f:
            f.write("import sys, time\nopen(sys.argv[1], 'a').write(sys.argv[2] + '\\n')\ntime.sleep(60)\n")
        with open(exts, "w", encoding="utf-8") as f:
            json.dump({"ext:alice:stats": {"cmd": [sys.executable, part, log, "stats"], "cwd": state}}, f)
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_UP_EXT_FILE": exts,
               "HUB_UP_PARTS": json.dumps({"hubd": [part, log, "hubd"]})}
        p = subprocess.Popen([sys.executable, HUB_UP], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            starts = lambda: open(log, encoding="utf-8").read().split() if os.path.exists(log) else []  # noqa: E731
            self.until(lambda: sorted(starts()) == ["hubd", "stats"])
            with open(os.path.join(state, "restart"), "w", encoding="utf-8") as f:
                json.dump({"parts": ["ext:Alice:Stats"]}, f)
            self.until(lambda: sorted(starts()) == ["hubd", "stats", "stats"])
        finally:
            p.terminate()
            p.wait(20)

    def test_a_disabled_plugin_is_stopped_and_not_revived_as_a_crash(self):
        # a plugin was disabled in settings (disabled: true) -> a manual hub restart ext:<name> (after a disable, a
        # restart is sent by itself only for an enable): the process did not stop and kept running; killing it by hand
        # did not help either, since the common "crashed, bring it up" check counted it as crashed and started it
        # again with the old, still cached command
        state = tempfile.mkdtemp(prefix="hubup-")
        log, exts = (os.path.join(state, n) for n in ("starts.log", "exts.json"))
        part = os.path.join(state, "part.py")
        with open(part, "w") as f:
            f.write("import sys, time\nopen(sys.argv[1], 'a').write(sys.argv[2] + '\\n')\ntime.sleep(60)\n")
        with open(exts, "w") as f:
            json.dump({"ext:tasks": {"cmd": [sys.executable, part, log, "tasks"], "cwd": state}}, f)
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_UP_EXT_FILE": exts,
               "HUB_UP_PARTS": json.dumps({"hubd": [part, log, "hubd"]})}
        p = subprocess.Popen([sys.executable, HUB_UP], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            starts = lambda: open(log).read().split() if os.path.exists(log) else []  # noqa: E731
            self.until(lambda: starts() == ["hubd", "tasks"])
            with open(exts, "w") as f:   # the plugin was disabled: settings no longer return it
                json.dump({}, f)
            with open(os.path.join(state, "restart"), "w") as f:
                json.dump({"parts": ["ext:tasks"]}, f)
            self.until(lambda: starts() == ["hubd", "tasks"])   # it stopped
            time.sleep(3)   # the common "crashed, bring it up" check runs once a second and must not start it again
            self.assertEqual(starts(), ["hubd", "tasks"])
        finally:
            p.terminate()
            p.wait(20)

    @staticmethod
    def text(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()

    def until(self, pred, timeout=20.0):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return
            time.sleep(0.1)
        self.fail("did not wait")


if __name__ == "__main__":
    unittest.main()
