"""The core's service messages to the hub that need no turn: an account switch, "hub restarted", "STOPPED" of an executor
the hub stopped itself. They do not wake the hub: they are "for information" (fyi, state aside) and ride with its next
message (a usage measurement: nine wakes of the hub in two days with nothing for the model to do). "All accounts busy"
and a stop by the owner reach the hub as a message, as before."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from core import executors, reducer

OWNER = {"channel": "cli", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}


class Aside(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.acts = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "w", cwd="/w", session_id="s-w", **OWNER)
        for name in ("hub", "w"):
            self.ev("session.started", name)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, self.acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                           "entity": entity, "data": data})
        return why

    def to_hub(self):
        return [m for m in self.st["messages"].values() if m["session"] == "hub"]

    def riders(self, *needles):
        return [m for m in self.to_hub() if m["state"] == "aside" and all(n in m["text"] for n in needles)]

    def typed_to_hub(self):
        return [a["text"] for a in self.acts if a.get("do") == "type" and a["session"] == "hub"]

    def test_account_switch_does_not_wake_the_hub_and_rides_with_the_next_message(self):
        self.assertIsNone(self.ev("account.switched", None, to="work", **{"from": "main"}, restart=True,
                                  why="main: weekly limit", channel="core"))
        self.assertEqual([m["state"] for m in self.to_hub()], ["aside"])   # not the queue: no turn, no spawn of the hub
        self.assertTrue(self.st["messages"][self.to_hub()[0]["id"]].get("fyi"))
        self.assertEqual([a for a in self.acts if a.get("session") == "hub"], [])
        self.ev("session.started", "hub")
        mid = f"m{self.seq + 1}"
        self.ev("message.received", None, to="hub", text="how are you?", **OWNER)
        self.ev("session.started", "hub")   # the hub's process goes on the new account before its message (the switch is lazy)
        (text,) = self.typed_to_hub()
        self.assertIn("how are you?", text)
        self.assertIn("→ work", text)
        self.ev("turn.started", "hub", marker=mid)
        self.assertEqual([m["state"] for m in self.to_hub() if "work" in m["text"]], ["delivered"])   # attached once
        self.ev("turn.result", "hub", text="ok")
        self.ev("turn.stopped", "hub")
        self.ev("message.received", None, to="hub", text="more", **OWNER)
        (text,) = self.typed_to_hub()
        self.assertNotIn("work", text)

    def restart(self, **ask):
        self.ev("hub.restart_requested", None, why="new code", **ask)
        self.ev("hub.restart_started", None, parts=[])
        self.ev("core.started", None, pid=1)
        return [m for m in self.to_hub() if "The hub was restarted" in m["text"]]

    def test_restart_notice_is_aside_when_nobody_waits_for_it(self):
        (told,) = self.restart(**HUB)   # the hub restarts on its own, no turn on anyone's message
        self.assertEqual(told["state"], "aside")
        self.assertTrue(self.riders("The hub was restarted", "new code"))

    def asked_by_owner(self, text="restart the hub"):
        self.ev("message.received", None, to="hub", text=text, **OWNER)   # the owner asked the hub: it reports
        return f"m{self.seq}"

    def test_restart_the_owner_asked_in_a_terminal_stays_a_message_to_the_hub(self):
        (told,) = self.restart(**OWNER)   # `hub restart` by the owner in a terminal
        self.assertEqual(told["state"], "typing")   # the hub is free: the message wakes it

    def test_restart_in_a_turn_on_the_owner_message_stays_a_message_to_the_hub(self):
        self.ev("turn.started", "hub", marker=self.asked_by_owner())
        self.assertTrue(self.st["sessions"]["hub"]["turn_owner"])
        (told,) = self.restart(**HUB)
        self.assertEqual(told["state"], "queued")   # an ordinary message: it waits for the end of the hub's turn, not an aside

    def test_restart_in_a_turn_the_owner_message_is_only_among_the_taken_stays_a_message(self):
        # the turn started on a service message (marker) and took the owner's too (also); and a turn with no marker at all
        for marker in (None, "service"):
            self.setUp()
            owner = self.asked_by_owner()
            if marker:
                self.ev("account.switched", None, to="work", restart=True, channel="core")
                marker = self.riders("→ work")[0]["id"]
            self.ev("turn.started", "hub", marker=marker, also=[owner])
            self.assertTrue(self.st["sessions"]["hub"]["turn_owner"], marker)
            (told,) = self.restart(**HUB)
            self.assertEqual(told["state"], "queued", marker)   # an ordinary message, not an aside

    def test_restart_in_a_turn_on_a_service_message_only_is_an_aside(self):
        self.ev("account.switched", None, to="work", restart=True, channel="core")
        self.ev("turn.started", "hub", marker=self.riders("→ work")[0]["id"])
        self.assertFalse(self.st["sessions"]["hub"]["turn_owner"])
        (told,) = self.restart(**HUB)
        self.assertEqual(told["state"], "aside")

    def wait_check_turn(self):
        """The owner asked, the hub set `hub wait` in that turn, the turn ended; the check fired: the hub's next turn is
        on a message of the schedule, not on the owner's, but in the owner's chain (turn_root)."""
        owner = self.asked_by_owner("restart and report back")
        self.ev("turn.started", "hub", marker=owner)
        self.ev("schedule.set", "hub-wait-1", session="hub", at=140, prompt="check", **HUB)
        self.ev("turn.result", "hub", text="waiting")
        self.ev("turn.stopped", "hub")
        self.ev("schedule.due", "hub-wait-1", slot=140)
        return next(m["id"] for m in self.to_hub() if m.get("schedule") == "hub-wait-1")

    def test_restart_in_a_later_turn_of_the_owners_chain_stays_a_message_to_the_hub(self):
        self.ev("turn.started", "hub", marker=self.wait_check_turn())
        self.assertFalse(self.st["sessions"]["hub"]["turn_owner"])   # not the owner's own message...
        self.assertTrue(self.st["sessions"]["hub"]["turn_root"])     # ...but his chain: he still waits for the report
        (told,) = self.restart(**HUB)
        self.assertEqual(told["state"], "queued")

    def test_restart_in_a_turn_woken_by_the_background_in_the_owners_chain_stays_a_message_to_the_hub(self):
        self.ev("turn.started", "hub", marker=self.asked_by_owner("restart and report back"))
        self.ev("turn.result", "hub", text="waiting")
        self.ev("turn.stopped", "hub")
        self.ev("turn.started", "hub")   # no marker: Claude Code woke the session itself (the background command ended)
        (told,) = self.restart(**HUB)
        self.assertEqual(told["state"], "queued")

    def test_restart_in_a_turn_that_took_a_sessions_message_with_a_service_one_stays_a_message(self):
        self.ev("account.switched", None, to="work", restart=True, channel="core")
        service = self.riders("→ work")[0]["id"]
        self.ev("message.received", None, to="hub", text="restart the whole hub", channel="session", owner=False,
                sender="w")
        asked = f"m{self.seq}"
        self.ev("turn.started", "hub", marker=service, also=[asked])   # the session's message is only among the taken
        (told,) = self.restart(**HUB)
        self.assertEqual(told["state"], "queued")   # w waits for the report as well as if it were the marker

    def test_restart_asked_by_a_free_hub_after_the_owners_turn_is_an_aside(self):
        # the marks of the owner's turn (turn_root, owner_chain) stay on the session after it: a free hub restarting
        # for itself later waits for nobody
        self.ev("turn.started", "hub", marker=self.asked_by_owner())
        self.ev("turn.result", "hub", text="done")
        self.ev("turn.stopped", "hub")
        self.assertTrue(self.st["sessions"]["hub"].get("turn_root") or self.st["sessions"]["hub"].get("owner_chain"))
        (told,) = self.restart(**HUB)
        self.assertEqual(told["state"], "aside")

    def test_restart_called_by_a_background_command_of_the_owners_turn_stays_a_message_to_the_hub(self):
        # the hub is free, but a background command it started in the owner's turn is still running and calls `hub restart`
        # itself: the owner waits for the report
        self.ev("turn.started", "hub", marker=self.asked_by_owner("restart and report back"))
        self.ev("session.background", "hub", id="b1", kind="shell", title="hub restart")
        self.ev("turn.result", "hub", text="started in the background")
        self.ev("turn.stopped", "hub")
        self.assertEqual(self.st["sessions"]["hub"]["state"], "idle")
        self.assertTrue(self.st["sessions"]["hub"]["background"])
        (told,) = self.restart(**HUB)
        self.assertEqual(told["state"], "typing")   # the hub is free: the message wakes it

    def test_restart_a_session_asked_stays_a_message_to_the_hub(self):
        self.ev("message.received", None, to="hub", text="restart the whole hub", channel="session", owner=False,
                sender="w")
        self.ev("turn.started", "hub", marker=f"m{self.seq}")
        (told,) = self.restart(**HUB)
        self.assertNotEqual(told["state"], "aside")

    def test_restart_asked_again_by_the_owner_while_it_waits_is_no_longer_quiet(self):
        self.ev("hub.restart_requested", None, why="new code", **HUB)
        self.ev("hub.restart_requested", None, why="new code", **OWNER)
        self.ev("hub.restart_started", None, parts=[])
        self.ev("core.started", None, pid=1)
        (told,) = [m for m in self.to_hub() if "The hub was restarted" in m["text"]]
        self.assertNotEqual(told["state"], "aside")

    def stopped_executor(self, stopper, **task):
        """The STOPPED notes the hub got when `stopper` stopped the executor w on a task with `task` (for / for_session)."""
        self.setUp()
        self.ev("session.requested", "v", cwd="/v", session_id="s-v", **OWNER)
        self.ev("session.started", "v")
        mid = f"m{self.seq + 1}"
        self.ev("message.received", None, text="fix it", to="hub", **OWNER)
        self.ev("turn.started", "hub", marker=mid)
        self.ev("delegation.created", None, session="w", **{k: mid if v is True else v for k, v in task.items()}, **HUB)
        self.ev("turn.result", "hub", text="—")
        self.ev("turn.stopped", "hub")
        self.st["sessions"]["w"]["role"] = "executor"
        before = len(self.to_hub())
        self.assertIsNone(self.ev("session.stop_requested", "w", **stopper))
        return [m["state"] for m in self.to_hub()[before:] if m["text"].startswith("STOPPED")]

    def raised_executor(self, stopper, by):
        """The STOPPED notes the hub got when `stopper` stopped the executor x raised the way the daemon raises it
        (executors.events_for): `by` is who raised it — the hub (`hub task run`) or None (the owner, in a terminal)."""
        self.setUp()
        for type, entity, data in executors.events_for(self.st, "x", {"task": "fix it", "by": by}, "/x", None):
            self.assertIsNone(self.ev(type, entity, **data), (type, data))
        self.ev("session.started", "x")
        self.assertEqual(self.st["delegations"][next(iter(self.st["delegations"]))]["state"], "working")
        before = len(self.to_hub())
        self.assertIsNone(self.ev("session.stop_requested", "x", **stopper))
        return [m["state"] for m in self.to_hub()[before:] if m["text"].startswith("STOPPED")]

    def test_executor_the_hub_raised_and_stopped_is_aside_by_the_owner_is_a_message(self):
        self.assertEqual(self.raised_executor(HUB, "hub"), ["aside"])   # the hub's own task and its own stop: nothing to do
        self.assertEqual(self.raised_executor(OWNER, "hub"), ["typing"])   # the owner's stop wakes the hub: it is not an aside

    def test_executor_the_owner_raised_and_the_hub_stopped_is_a_message(self):
        # raised in a terminal (no for, no for_session): the owner's task, nobody but the hub can tell him it was cut off
        self.assertEqual(self.raised_executor(HUB, None), ["typing"])

    def test_executor_stopped_by_the_hub_on_a_task_somebody_waits_for_is_a_message(self):
        # the owner's task (for) was cut off, a session's task (for_session) too: the hub has to tell them
        self.assertEqual(self.stopped_executor(HUB, **{"for": True}), ["typing"])
        self.assertEqual(self.stopped_executor(HUB, for_session="v"), ["typing"])
        self.assertEqual(self.stopped_executor(HUB, for_session="hub"), ["aside"])   # its own, set for itself

    def test_stop_with_the_hub_as_the_sender_outside_a_session_is_not_the_hubs_own(self):
        # the sender field alone proves nothing: only a command from a session (channel session) is the hub's own
        self.assertEqual(self.raised_executor({"channel": "cli", "owner": True, "sender": "hub"}, "hub"), ["typing"])

    def test_all_accounts_busy_still_wakes_the_hub(self):
        self.ev("account.exhausted", None, blocked={"main": 9000}, until=9000, why="limit", channel="core")
        self.assertEqual(len(self.to_hub()), 1)

    def test_two_notes_of_one_event_are_both_kept(self):
        # the active account is free again and the switch is announced: two notes of one event
        self.ev("account.switched", None, to="main", channel="cli", owner=True)   # main is the active one (no note: not the core's)
        self.ev("account.switched", None, to="main", restart=True, free=True, channel="core")
        notes = self.to_hub()
        self.assertEqual(len(notes), 2)
        self.assertTrue(all(m["state"] == "aside" for m in notes))
        self.assertTrue(any("is free again" in m["text"] for m in notes))   # no language in the state: English
        self.assertTrue(any("a new token" in m["text"] or "Claude account:" in m["text"] for m in notes))

    def test_notes_that_piled_up_ride_together_with_the_next_message(self):
        for i in range(3):
            self.ev("account.switched", None, to=f"a{i}", restart=True, channel="core")
        self.assertEqual(len(self.riders()), 3)
        self.ev("message.received", None, to="hub", text="what is new?", **OWNER)
        self.ev("session.started", "hub")   # on the new account before the message
        (text,) = self.typed_to_hub()
        self.assertTrue(all(f"→ a{i}" in text for i in range(3)))
        mid = max(self.st["messages"], key=lambda k: self.st["messages"][k]["created"])
        self.ev("turn.started", "hub", marker=self.st["messages"][mid]["id"])
        self.assertEqual({m["state"] for m in self.to_hub()}, {"delivered"})   # delivered together with the carrier


if __name__ == "__main__":
    unittest.main()
