"""A question card to the owner older than owner.ASK_TTL is not open: it stays in the state (core/askexpired wakes the
session that asked it), but the places that decide "waits for the owner" - sleep guard, the board, the stuck-task
ladder, executor cleanup, the watcher, `hub status --json` - count only the open ones (owner.open_asks)."""
import tests  # noqa: F401 — ignore the machine's settings
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from core import askexpired, board, delegations, executors, guards, journal, owner, reducer
from modules.watch import watch

T0 = 1_000_000.0
FRESH, STALE = T0 + owner.ASK_TTL - 1, T0 + owner.ASK_TTL + 1
Q = {"questions": [{"question": "Which tiles to use?", "options": [{"label": "Raster"}, {"label": "Vector"}]}]}
HUB_CLI = os.path.join(os.path.dirname(__file__), "..", "bin", "hub")


class StaleAsks(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/w", session_id="sid-hub", kind="hub", channel="cli", owner=True)
        self.ev("session.started", "hub", session_id="sid-hub")
        self.ev("session.requested", "w", cwd="/w", session_id="sid-w", channel="cli", owner=True)
        self.ev("session.started", "w", session_id="sid-w")
        self.ev("permission.requested", "w", request_id="r1", tool="AskUserQuestion", interactive=True, input=Q)
        self.sess = self.st["sessions"]["w"]
        self.sess["role"] = "executor"

    def ev(self, type, entity=None, ts=T0, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts, "type": type, "entity": entity, "data": data})
        return why

    def raised(self):
        """askexpired raised the card once: the fact it writes, its message delivered."""
        (ask,) = self.st["sessions"]["w"]["asks"]   # reduce may have replaced the record self.sess points to
        self.assertIsNone(self.ev("ask.expired", "w", ts=STALE, channel="core", asks=[ask["id"]]))
        self.assertTrue(self.st["sessions"]["w"].get("revived"))
        for m in self.st["messages"].values():
            m["state"] = "delivered"

    def delegate(self):
        self.ev("message.received", None, text="fix the build", channel="web", owner=True, to="hub")
        mid = f"m{self.seq}"
        self.ev("turn.started", "hub", marker=mid)
        self.assertIsNone(self.ev("delegation.created", None, session="w", **{"for": mid}, channel="session", owner=False, sender="hub"))
        (d,) = self.st["delegations"].values()
        for m in self.st["messages"].values():
            m["state"] = "delivered"
        return d

    def test_the_card_stays_in_the_state_but_is_not_open(self):
        self.assertEqual(len(owner.open_asks(self.sess, FRESH)), 1)
        self.assertEqual(owner.open_asks(self.sess, STALE), [])
        self.assertEqual(len(self.sess["asks"]), 1)

    def test_a_session_with_a_stale_card_can_be_put_to_sleep(self):
        self.assertIn("q", guards.closable(self.st, self.sess, now=FRESH))
        self.assertIsNone(guards.closable(self.st, self.sess, now=STALE))

    def test_a_sleep_request_goes_through_the_guard_at_the_event_time(self):
        req = dict(channel="cli", owner=True)
        self.assertIsNotNone(self.ev("session.close_requested", "w", ts=FRESH, **req))
        self.assertIsNone(self.ev("session.close_requested", "w", ts=STALE, **req))

    def test_the_board_does_not_show_a_stale_card_as_waiting(self):
        d = self.delegate()
        d["state"] = "working"
        self.assertTrue(board.worker(self.st, "w", FRESH)["waiting"])
        # until askexpired raised it the card still holds (askexpired.holds): waiting, not "stuck"
        self.assertEqual({k: board.worker(self.st, "w", STALE)[k] for k in ("waiting", "stuck")},
                         {"waiting": True, "stuck": False})
        self.raised()
        self.assertFalse(board.worker(self.st, "w", STALE)["waiting"])

    def test_the_board_lead_edit_is_not_waiting_on_a_stale_card(self):
        self.ev("turn.started", "w", marker="m9")
        edit = {"by": "w", "seq": self.seq + 1, "ts": FRESH}
        self.assertTrue(board.editing_now(self.st, edit, FRESH)["waiting"])
        self.assertFalse(board.editing_now(self.st, {**edit, "ts": STALE}, STALE)["waiting"])

    def test_a_task_behind_a_stale_card_is_stuck_again(self):
        d = self.delegate()
        d["state"] = "working"
        self.assertIsNone(delegations.stuck(self.st, d, now=FRESH))
        # not raised yet: a reminder timed from before the card would come first and push askexpired's raise back
        self.assertIsNone(delegations.stuck(self.st, d, now=STALE))
        self.assertEqual(delegations.pending(self.st, STALE), [(d["deadline"], d["id"], None)])
        self.raised()
        self.assertIsNotNone(delegations.stuck(self.st, d, now=STALE))
        self.assertEqual([t for t in delegations.pending(self.st, FRESH)], [(d["deadline"], d["id"], None)])
        self.assertLess(delegations.pending(self.st, STALE)[0][0], d["deadline"])

    def test_a_quiet_reminder_waits_for_the_stale_card_to_be_raised(self):
        d = self.delegate()
        d.update(state="replied", answered_at=T0)
        self.assertIsNone(delegations.quiet(self.st, d, now=STALE))
        self.raised()
        self.assertIsNotNone(delegations.quiet(self.st, d, now=STALE))

    def test_an_executor_with_a_stale_card_is_cleaned_up(self):
        self.sess["state"] = "parked"
        self.sess["since"] = T0
        self.assertTrue(executors._open_work(self.st, "w", FRESH))
        self.assertTrue(executors._open_work(self.st, "w", STALE))   # not raised yet (askexpired): it still holds
        self.assertEqual(executors.retiring(self.st, STALE, keep=1), [])
        self.raised()   # it wakes the parked executor: the question is asked again, not lost
        sess = self.st["sessions"]["w"]
        self.assertEqual(sess["state"], "starting")
        sess["state"], sess["since"] = "parked", STALE   # its turn on it is over, parked again
        self.assertFalse(executors._open_work(self.st, "w", STALE + 2))
        self.assertEqual(executors.retiring(self.st, STALE + 2, keep=1), [("remove", "w")])

    def test_a_retired_executor_is_not_held_by_a_card_nobody_will_raise(self):
        # askexpired wakes only idle and parked sessions: holding a retired one would keep it for MAX_AGE for nothing
        self.sess["state"] = "retired"
        self.sess["since"] = T0
        self.assertTrue(executors._open_work(self.st, "w", FRESH))
        self.assertFalse(executors._open_work(self.st, "w", STALE))
        self.assertEqual(executors.retiring(self.st, STALE, keep=1), [("remove", "w")])

    def test_an_idle_executor_with_a_stale_card_is_put_to_sleep(self):
        self.sess["raised_by"] = "hub"
        self.assertEqual(executors.retiring(self.st, FRESH), [])
        self.assertEqual(executors.retiring(self.st, STALE), [])   # not raised yet (askexpired): it still holds
        self.raised()
        self.assertEqual(executors.retiring(self.st, STALE), [("sleep", "w")])

    def test_a_stale_card_is_raised_before_the_executor_sleeps(self):
        # askexpired wakes only an idle or parked session: had the executor fallen asleep on the stale card first,
        # nobody would ever ask the question again (review of PR #394)
        self.sess["raised_by"] = "hub"
        (ask,) = self.sess["asks"]
        self.assertEqual(askexpired.due(self.st, STALE), [("w", [ask["id"]])])
        self.assertEqual(executors.retiring(self.st, STALE), [])
        self.ev("ask.expired", "w", ts=STALE, channel="core", asks=[ask["id"]])
        for m in self.st["messages"].values():
            m["state"] = "delivered"
        self.assertEqual(askexpired.due(self.st, STALE + askexpired.PAUSE), [])
        self.assertEqual(executors.retiring(self.st, STALE + askexpired.PAUSE), [("sleep", "w")])

    def test_a_stale_card_is_not_the_open_one_a_new_question_repeats(self):
        # the hub is told to withdraw a repeated question only in favour of an open card: nobody answers a stale one
        self.ev("session.requested", "v", cwd="/w", session_id="sid-v", channel="cli", owner=True)
        self.ev("session.started", "v", session_id="sid-v")
        dup = lambda: [m for m in self.st["messages"].values() if m["session"] == "hub" and "repeats the open" in m["text"]]
        self.ev("permission.requested", "v", ts=STALE, request_id="r2", tool="AskUserQuestion", interactive=True, input=Q)
        self.assertEqual(dup(), [])
        self.ev("permission.requested", "w", ts=STALE, request_id="r3", tool="AskUserQuestion", interactive=True, input=Q)
        self.assertEqual(len(dup()), 1)   # v's card is open: a repeat of it is reported as before

    def test_a_turn_ending_on_a_stale_card_is_a_reply(self):
        for now, state in ((FRESH, "waiting-owner"), (STALE, "replied")):
            with self.subTest(now=now):
                self.setUp()
                did = self.delegate()["id"]
                self.ev("turn.started", "w", ts=now, marker=None)
                sess, d = self.st["sessions"]["w"], self.st["delegations"][did]
                sess["asks"][0]["delegation"] = did
                sess["delegation"], d["state"] = did, "waiting-owner"
                self.assertIsNone(self.ev("turn.result", "w", ts=now, text="done"))
                self.assertEqual(self.st["delegations"][did]["state"], state)

    def test_the_watcher_does_not_list_a_stale_card_as_in_flight(self):
        self.assertIn("open question", watch.promise_facts(self.st, "w", FRESH))
        self.assertNotIn("open question", watch.promise_facts(self.st, "w", STALE))


class StatusJson(unittest.TestCase):
    def test_status_json_lists_only_open_cards_and_keeps_the_stale_ones_apart(self):
        state = tempfile.mkdtemp(prefix="st-stale-")
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        old, now = time.time() - 2 * owner.ASK_TTL, time.time()
        j.append("session.requested", "hub", {"cwd": "/h", "session_id": "s-hub", "kind": "hub", "channel": "cli", "owner": True}, ts=old)
        j.append("session.requested", "maps", {"cwd": "/m", "session_id": "s-m", "channel": "cli", "owner": True}, ts=old)
        j.append("session.started", "maps", {"session_id": "s-m"}, ts=old)
        j.append("permission.requested", "maps", {"request_id": "r1", "tool": "AskUserQuestion", "interactive": True, "input": Q}, ts=old)
        j.append("permission.requested", "maps", {"request_id": "r2", "tool": "AskUserQuestion", "interactive": True, "input": Q}, ts=now)
        env = {**os.environ, "HUB_STATE_DIR": state}
        env.pop("HUB_SESSION", None)
        out = subprocess.run([sys.executable, HUB_CLI, "status", "--json"], capture_output=True, text=True, env=env).stdout
        maps = json.loads(out)["sessions"]["maps"]
        self.assertEqual([a["request_id"] for a in maps["asks"]], ["r2"])
        self.assertEqual([a["request_id"] for a in maps["asks_stale"]], ["r1"])


if __name__ == "__main__":
    unittest.main()
