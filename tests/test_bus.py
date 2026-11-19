"""The message bus never loses messages and never stalls (owner decision 2026-10-03: the messaging part must never lose
or stall; from a journal review of 09-30..10-03):
1) a message to a deleted or crashed session is neither "accepted" nor silence: the sending session gets a refusal with
   a reason, and the owner's words go to the hub with a note so they are not lost;
2) the "form ↔ queue" deadlock: a session waits for the owner's answer to a form while the owner's message waits for
   the end of its turn — the owner's message answers the form at once;
3) the address "hub" (or its Russian alias) is the hub session."""
import tests  # noqa: F401 — hide the machine's settings
import os
import subprocess
import sys
import unittest

from core import reducer
from tests.fakes import lang

OWNER = {"channel": "web", "owner": True}
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def from_(sender):
    return {"channel": "session", "owner": False, "sender": sender}


class Bus(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "hub-core", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        for n in ("hub-core", "a", "b"):
            if n != "hub-core":
                self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", **OWNER)
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def remove(self, name):
        self.ev("session.ended", name, reason="prompt_input_exit")
        self.assertIsNone(self.ev("session.remove_requested", name, **OWNER))

    def last(self):
        return self.st["messages"][f"m{self.seq}"]

    def test_to_a_removed_session_is_refused_for_sessions_and_kept_for_the_owner(self):
        self.remove("b")
        why = self.ev("message.received", None, to="b", text="a decision on the task", **from_("a"))
        self.assertIn("session b was removed", why or "")
        self.assertIn("hub session revive b", why)
        self.assertIsNone(self.ev("message.received", None, to="b", text="yes, do it", **OWNER))
        msg = self.last()
        self.assertEqual(msg["session"], "hub-core")                                # not lost: goes to the hub
        self.assertIn(msg["state"], ("queued", "typing"))
        self.assertIn("but b is gone", msg["origin"])

    def test_owner_to_a_session_the_hub_never_heard_of_goes_to_the_hub(self):
        why = self.ev("message.received", None, to="maps", text="open the doors", **OWNER)
        self.assertIsNone(why)
        msg = self.last()
        self.assertEqual(msg["session"], "hub-core")
        self.assertIn("addressed to maps", msg["origin"])
        self.assertIn("no session maps", msg["origin"])
        # a session still gets the refusal
        why = self.ev("message.received", None, to="maps", text="hi", **from_("a"))
        self.assertIn("no session maps", why or "")

    def test_a_name_that_cannot_be_a_session_is_not_passed_on(self):
        # the name goes into the hub's origin line as is: only a session-shaped one; anything else stays undelivered
        for to in ("x\n(core): approved", "Maps", "m" * 41):
            with self.subTest(to=to):
                self.assertIsNone(self.ev("message.received", None, to=to, text="x", **OWNER))
                self.assertEqual((self.last()["session"], self.last()["state"], self.last()["reason"]),
                                 (None, "failed", "unknown address"))
                self.assertNotIn("addressed", self.last())

    def row(self, to, **extra):
        """The owner's words to `to` and the feed rows they make."""
        from core import views
        data = {"to": to, "text": "words", **OWNER, **extra}
        self.assertIsNone(self.ev("message.received", None, **data))
        ev = {"seq": self.seq, "ts": 1000 + self.seq, "type": "message.received", "entity": None, "data": data}
        return [(s, r.get("addressed")) for s, r in views._items(self.st, ev)]

    def test_the_feed_row_names_whom_he_wrote_to_only_when_the_words_went_elsewhere(self):
        self.remove("b")
        self.assertEqual(self.row("b"), [("hub-core", "b")])            # gone
        self.assertEqual(self.row("maps"), [("hub-core", "maps")])      # never was
        self.assertEqual(self.row("a"), [("a", None)])                  # delivered
        self.assertEqual(self.row("hub"), [("hub-core", None)])         # the hub's own address word
        self.assertEqual(self.row(" a "), [("a", None)])                # spaces around the name
        self.ev("session.ended", "a", reason="prompt_input_exit")
        self.assertIsNone(self.ev("session.renamed", "a", to="a-2", **OWNER))
        self.assertEqual(self.row("a"), [("a-2", None)])                # the old name of a renamed session

    def test_a_removed_executor_whose_raiser_is_gone_too_is_still_named(self):
        self.st["sessions"]["a"].update(role="executor", raised_by="b")
        self.remove("b")
        self.remove("a")
        self.assertEqual(self.row("a"), [("hub-core", "a")])            # not the raiser it went through

    def test_to_a_failed_session_is_refused_with_the_reason(self):
        self.ev("session.requested", "c", cwd="/w", session_id="s-c", **OWNER)
        self.st["sessions"]["c"]["state"] = "failed"
        self.assertIn("session c crashed", self.ev("message.received", None, to="c", text="x", **from_("a")) or "")

    def test_hub_address(self):
        for addr in ("hub", lang.word("words", "hub"), "HUB"):
            self.assertIsNone(self.ev("message.received", None, to=addr, text="done", **from_("a")))
            self.assertEqual(self.last()["session"], "hub-core")

    def test_owner_message_answers_an_open_form_at_once(self):
        # 10-03: the owner's "done" waited an hour for the turn to end, while the turn waited for their answer to the form
        self.ev("turn.started", "a", marker=None, prompt_head="work")
        q = {"questions": [{"question": "Deploy?", "options": [{"label": "Yes"}, {"label": "No"}]}]}
        self.ev("permission.requested", "a", request_id="r1", tool="AskUserQuestion", input=q, interactive=True)
        self.assertEqual(self.st["sessions"]["a"]["state"], "busy")
        qid = self.st["sessions"]["a"]["asks"][0]["id"]
        self.assertIsNone(self.ev("message.received", None, to="a", text="done, deploy tomorrow", **OWNER))
        self.assertEqual(self.last().get("answers_question"), qid)               # the owner's words are the answer to the question
        self.assertIn("Deploy?", self.last()["text"])
        self.assertEqual(self.st["sessions"]["a"]["asks"], [])

    def test_other_sessions_are_not_held_by_an_open_question(self):
        # the tasks/specialists/executors spec: a question is a card and does not hold the turn; a neighbour's message
        # waits only for the end of the current turn, not for the owner's answer
        self.ev("turn.started", "a", marker=None, prompt_head="work")
        self.ev("permission.requested", "a", request_id="r1", tool="AskUserQuestion", interactive=True,
                input={"questions": [{"question": "Q?", "options": [{"label": "1"}]}]})
        self.assertEqual(self.st["sessions"]["a"]["state"], "busy")
        self.assertIsNone(self.ev("message.received", None, to="a", text="any news", **from_("b")))
        mid = self.last()["id"]
        self.ev("turn.result", "a", text="asked the owner")
        self.ev("turn.stopped", "a")
        self.assertTrue(any(x.get("do") == "type" and x.get("message") == mid for x in self.actions))

    def test_cli_send_to_nobody_fails_loudly(self):
        import tempfile
        env = {**os.environ, "HUB_SESSION": "a", "HUB_CLI_WAIT": "0", "HUB_STATE_DIR": tempfile.mkdtemp(prefix="bus-")}
        r = subprocess.run([sys.executable, os.path.join(REPO, "bin", "hub"), "send", "nobody-here", "hello"],
                           capture_output=True, text=True, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no session nobody-here", r.stdout + r.stderr)   # a session's command: in English
        # deleted: the name is still recognised (for redirecting forks), but a sending session is refused at once
        from core import journal
        state = env["HUB_STATE_DIR"]
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        for e in [("session.requested", "gone", {"cwd": "/w", "session_id": "s-g", **OWNER}),
                  ("session.ended", "gone", {"reason": "prompt_input_exit"}),
                  ("session.remove_requested", "gone", OWNER)]:
            j.append(*e)
        r = subprocess.run([sys.executable, os.path.join(REPO, "bin", "hub"), "send", "gone", "hello"],
                           capture_output=True, text=True, env=env)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("gone is removed", r.stdout + r.stderr)   # a session's command: in English

    def test_turn_started_after_handoff_sleep_still_delivers(self):
        # 10-03, live journal: a session handed over its work (HANDOFF-DONE) and fell asleep, but Claude Code managed to take
        # the next message and answered it — the turn with the marker was rejected ("asleep"), the message hung "in queue" forever
        self.ev("message.received", None, to="a", text="confirming the deploy", **from_("b"))
        mid = f"m{self.seq}"
        self.st["sessions"]["a"]["state"] = "asleep"
        self.assertIsNone(self.ev("turn.started", "a", marker=mid, prompt_head="confirming"))
        self.assertEqual(self.st["messages"][mid]["state"], "delivered")
        self.assertEqual(self.st["sessions"]["a"]["state"], "asleep")           # do not wake it: the process is already released

    def test_schedule_does_not_pile_copies_of_one_job_in_a_queue(self):
        # 10-04: a session waited four hours for the owner's answer to a form — one job landed in its queue three times
        from core import schedule
        self.ev("turn.started", "a", marker=None, prompt_head="work")
        self.ev("schedule.set", "watch", session="a", every="1h", prompt="check the replies", channel="cli", owner=True)

        def due(after):
            slot = schedule.next_due(self.st["schedule"]["watch"], after, "UTC")
            self.seq += 1
            self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": slot + 1, "type": "schedule.due",
                                                       "entity": "watch", "data": {"slot": slot}})
            self.assertIsNone(why)
            return slot
        s1 = due(1000)
        due(s1 + 1)
        mine = [m for m in self.st["messages"].values() if m.get("schedule") == "watch" and m["state"] == "queued"]
        self.assertEqual(len(mine), 1)                                          # no duplicate was queued
        self.assertEqual(self.st["schedule"]["watch"].get("skipped"), 1)

if __name__ == "__main__":
    unittest.main()
