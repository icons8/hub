"""The hub does not hold its turn on a question to the owner (the hub's form
held its queue for six hours, so sessions could not reach it). A hub question asked as a form becomes a card (page,
Telegram, reminders) and the turn ends at once; the owner's answer reaches the hub as an ordinary message marked with
the question id. A question has a lifetime; an answer to an expired one carries a warning. Other sessions ask as before."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from core import owner, reducer, views

OWNER = {"channel": "web", "owner": True}
Q = {"questions": [{"question": "Which tiles to use?", "options": [{"label": "Raster"}, {"label": "Vector"}]}]}


class HubAsks(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "maps", cwd="/m", session_id="s-m", **OWNER)
        self.ev("session.started", "hub")
        self.ev("session.started", "maps")
        self.ev("turn.started", "hub", marker=None, prompt_head="x")

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def ask(self, name="hub", rid="r1", ts=None):
        self.actions = []
        self.ev("permission.requested", name, ts=ts, request_id=rid, tool="AskUserQuestion", interactive=True, input=Q)

    def hub_msgs(self):
        return [m for m in self.st["messages"].values() if m["session"] == "hub"]

    def snap_hub(self, now=None):
        r = views.Reader()
        r.state = self.st
        return next(s for s in r.snapshot(now=now)["sessions"] if s["name"] == "hub")

    def test_the_hub_does_not_hold_its_turn_on_a_question(self):
        self.ask()
        (resp,) = [a for a in self.actions if a.get("do") == "respond"]
        self.assertEqual(resp["response"]["behavior"], "deny")
        qid = self.st["sessions"]["hub"]["asks"][0]["id"]
        self.assertIn(qid, resp["response"]["message"])                         # the hub knows its question's id
        self.assertEqual(self.st["sessions"]["hub"]["state"], "busy")           # not "dialog": the turn runs to its end
        self.ev("turn.result", "hub", text="Asked the owner (q)")
        self.ev("turn.stopped", "hub")
        # a session writes to the hub: it arrives, it does not wait for the owner's answer
        self.ev("message.received", None, to="hub", text="done with X", channel="session", owner=False, sender="maps")
        self.assertTrue(any(a.get("do") == "type" and a["session"] == "hub" for a in self.actions))
        # the card is on the page (like a form) and in reminders
        self.assertEqual([p["request_id"] for p in self.snap_hub(now=self.seq + 1000)["pending"]], ["r1"])
        self.assertTrue(any(q[1] == "hub" for q in owner.questions(self.st)))

    def test_the_owner_closes_a_question_without_answering(self):
        # Esc did not close the Ask panel, a close button was needed; close = withdraw the question everywhere
        self.ask()
        qid = self.st["sessions"]["hub"]["asks"][0]["id"]
        self.assertIsNotNone(self.ev("question.dismissed", "hub", request_id="r1", channel="session", owner=False,
                                     sender="maps"))                            # only the owner can withdraw it
        self.assertIsNone(self.ev("question.dismissed", "hub", request_id="r1", **OWNER))
        self.assertEqual(self.st["sessions"]["hub"]["asks"], [])
        self.assertEqual(self.snap_hub(now=self.seq + 1000)["pending"], [])
        self.assertFalse(any(q[1] == "hub" for q in owner.questions(self.st)))  # and from reminders
        (msg,) = [m for m in self.hub_msgs() if m.get("answers_question") == qid]
        self.assertIn("without answering", msg["text"])                                   # the session knows there will be no answer
        self.assertIsNotNone(self.ev("question.dismissed", "hub", request_id="r1", **OWNER))   # the second time there is nothing to withdraw

    def test_a_session_with_an_open_question_is_not_put_to_sleep(self):
        # the task was closed and the core put to sleep a session with an open question card; the hub decided
        # the question was abandoned and asked the owner the same question with its own card, so both waited
        from core.guards import closable
        hub = {"channel": "session", "owner": False, "sender": "hub"}
        self.ev("message.received", None, to="hub", text="make it a direction", **OWNER)
        self.ev("delegation.created", None, session="maps", **{"for": f"m{self.seq}"}, context="", **hub)
        d = self.st["delegations"][f"d{self.seq}"]
        self.ev("turn.started", "maps", marker=d["message"], prompt_head="x")
        self.ask(name="maps", rid="r9")
        self.ev("turn.result", "maps", text="the plan is ready, the question is a card")
        self.ev("turn.stopped", "maps")
        self.assertIn("open question", closable(self.st, self.st["sessions"]["maps"]) or "")
        self.ev("delegation.done", d["id"], **hub)
        self.assertEqual(self.st["sessions"]["maps"]["state"], "idle")             # not put to sleep, not handing over its work
        self.assertEqual(self.st["sessions"]["maps"].get("closing", "none"), "none")

    def test_the_owners_answer_comes_as_a_message_marked_with_the_question(self):
        self.ask()
        qid = self.st["sessions"]["hub"]["asks"][0]["id"]
        self.ev("turn.result", "hub", text="asked")
        self.ev("turn.stopped", "hub")
        self.actions = []
        self.ev("permission.answered", "hub", request_id="r1", behavior="allow", answers={"Which tiles to use?": "Vector"},
                **OWNER)
        self.assertFalse([a for a in self.actions if a.get("do") == "respond"])  # the turn ended long ago, nobody to respond to
        (m,) = [m for m in self.hub_msgs() if qid in m["text"]]
        self.assertIn("Vector", m["text"])
        self.assertIn("Which tiles to use?", m["text"])
        self.assertTrue(m.get("from_owner"))
        self.assertEqual(self.st["sessions"]["hub"]["asks"], [])
        self.assertEqual(self.snap_hub(now=self.seq + 1000)["pending"], [])

    def test_owner_words_to_the_hub_answer_its_open_question(self):
        # a text reply to an ask must withdraw the question
        self.ask()
        qid = self.st["sessions"]["hub"]["asks"][0]["id"]
        self.ev("turn.result", "hub", text="asked")
        self.ev("turn.stopped", "hub")
        self.ev("message.received", None, to="hub", text="vector, of course", **OWNER)
        m = self.hub_msgs()[-1]
        self.assertIn(qid, m.get("answers_question") or "")
        self.assertEqual(self.st["sessions"]["hub"]["asks"], [])

    def test_an_answer_to_an_expired_question_is_flagged(self):
        self.ask(ts=1000)
        late = 1000 + owner.ASK_TTL + 60
        self.assertEqual(self.snap_hub(now=late)["pending"], [])                # expired: no card
        self.assertFalse(any(q[1] == "hub" for q in owner.questions(self.st, now=late)))
        self.ev("permission.answered", "hub", ts=late, request_id="r1", behavior="allow", answers={"Which tiles to use?": "Raster"},
                **OWNER)
        m = [m for m in self.hub_msgs() if "Raster" in m["text"]][0]
        self.assertIn("the question is stale", m["text"])

    def test_every_session_asks_without_holding_its_turn(self):
        # spec "tasks, specialists, executors" (release 1): a form question holds nobody's turn
        self.ev("turn.started", "maps", marker=None, prompt_head="y")
        self.ask(name="maps", rid="r9")
        self.assertEqual(self.st["sessions"]["maps"]["state"], "busy")
        (resp,) = [a for a in self.actions if a.get("do") == "respond"]
        self.assertIn(self.st["sessions"]["maps"]["asks"][0]["id"], resp["response"]["message"])

    def test_a_delegation_waits_for_the_owner_while_its_question_is_open(self):
        self.ev("message.received", None, to="hub", text="draw the map", **OWNER)
        self.ev("delegation.created", None, session="maps", **{"for": f"m{self.seq}"}, context="", channel="session",
                owner=False, sender="hub")
        did = f"d{self.seq}"
        self.ev("turn.started", "maps", marker=self.st["delegations"][did]["message"], prompt_head="task")
        self.ask(name="maps", rid="r5")
        self.assertEqual(self.st["delegations"][did]["state"], "waiting-owner")
        self.ev("turn.result", "maps", text="asked")
        self.ev("turn.stopped", "maps")
        self.ev("permission.answered", "maps", request_id="r5", behavior="allow",
                answers={"Which tiles to use?": "Vector"}, **OWNER)
        ans = [m for m in self.st["messages"].values() if m.get("answers_question")][-1]
        self.ev("turn.started", "maps", marker=ans["id"], prompt_head="answer")
        self.assertEqual(self.st["delegations"][did]["state"], "working")

    def test_a_hub_permission_prompt_still_waits(self):
        self.actions = []
        self.ev("permission.requested", "hub", request_id="r2", tool="Bash", interactive=True, input={"command": "ls"})
        self.assertEqual(self.st["sessions"]["hub"]["state"], "dialog")


if __name__ == "__main__":
    unittest.main()
