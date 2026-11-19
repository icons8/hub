"""A text answer clears a form question even when the owner wrote to the hub rather than to the session (owner decision
2026-10-04: a text answer to an ask must clear the question): a session has a form open, the owner writes to the hub, the
hub delegates their words to that session — the delegation queued behind the form, and the form waited for an answer
that would never come."""
import tests  # noqa: F401 — keep the machine's settings out of the test
import unittest

from core import reducer

OWNER = {"channel": "cli", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}


class FormByDelegation(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "maps", cwd="/m", session_id="s-m", role="agent", about="Maps", **OWNER)
        self.ev("session.started", "hub")
        self.ev("session.started", "maps")
        self.ev("turn.started", "maps", marker=None, prompt_head="plan")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def ask(self, tool="AskUserQuestion"):
        self.ev("permission.requested", "maps", request_id="r1", tool=tool, interactive=True,
                input={"questions": [{"question": "Which tiles to use?"}]})
        # a form question does not hold the turn (release 1); a tool permission request does, as before
        self.assertEqual(self.st["sessions"]["maps"]["state"], "busy" if tool == "AskUserQuestion" else "dialog")

    def delegate(self, text, **extra):
        self.ev("message.received", None, to="hub", text=text, **OWNER)
        self.ev("delegation.created", None, session="maps", **{"for": f"m{self.seq}"}, context="", **HUB, **extra)
        return self.st["delegations"][f"d{self.seq}"]

    def test_owner_words_delegated_by_the_hub_answer_the_open_form(self):
        self.ask()
        qid = self.st["sessions"]["maps"]["asks"][0]["id"]
        d = self.delegate("check the tile server first", card=qid)   # --card: the hub named the card
        m = self.st["messages"][d["message"]]
        self.assertEqual(m.get("answers_question"), qid)                       # a task carrying their words is an answer
        self.assertIn(qid, m["text"])
        self.assertEqual(self.st["sessions"]["maps"]["asks"], [])
        # the task arrives the usual way, the turn's result for it is the delegation's reply
        self.ev("turn.result", "maps", text="asked earlier")
        self.ev("turn.stopped", "maps")
        self.ev("turn.started", "maps", marker=d["message"], prompt_head="task")
        self.ev("turn.result", "maps", text="Tile server is up")
        self.assertEqual(self.st["delegations"][d["id"]]["state"], "replied")

    def test_a_task_without_card_is_not_an_answer(self):
        # hub task new --for mN without --card is a new request from the owner; the session's only open card
        # used to be closed and given the "this is the owner's answer to your question" header
        self.ask()
        d = self.delegate("let us also do this one")
        m = self.st["messages"][d["message"]]
        self.assertFalse(m.get("answers_question"))
        self.assertNotIn("answer to your question", m["text"])
        self.assertEqual(len(self.st["sessions"]["maps"]["asks"]), 1)            # the card stays open
        wrong = self.delegate("and this one", card="q999")                              # a named card that does not exist — no answer either
        self.assertFalse(self.st["messages"][wrong["message"]].get("answers_question"))
        self.assertEqual(len(self.st["sessions"]["maps"]["asks"]), 1)

    def test_words_written_before_the_question_do_not_answer_it(self):
        # the owner wrote the hub a new request, 2 s later the session asked a question with a card, the hub set a
        # task from their words — the core took them for an answer, closed the card and added the "owner's answer" header
        self.ev("message.received", None, to="hub", text="let us make another plugin", **OWNER)
        words = f"m{self.seq}"
        self.ask()
        self.ev("delegation.created", None, session="maps", **{"for": words}, context="", **HUB)
        m = self.st["messages"][self.st["delegations"][f"d{self.seq}"]["message"]]
        self.assertFalse(m.get("answers_question"))
        self.assertNotIn("answer to your question", m["text"])
        self.assertEqual(len(self.st["sessions"]["maps"]["asks"]), 1)            # the card stays open

    def test_an_answer_arriving_mid_turn_does_not_steal_the_turn_of_another_task(self):
        # in the middle of a turn on task d2 the owner's answer to a question about d1 arrived — the turn
        # switched to d1; the result of d2 and the next question (and its answer) went to the owner under the d1 message
        self.ev("turn.stopped", "maps")
        d1 = self.delegate("fix the migration")
        self.ev("turn.started", "maps", marker=d1["message"], prompt_head="x")
        self.ask()
        self.ev("turn.result", "maps", text="fixed; publish?")
        self.ev("turn.stopped", "maps")
        self.ev("delegation.created", None, session="maps", context="sort out someone else's bot", **HUB)
        d2 = self.st["delegations"][f"d{self.seq}"]
        self.ev("turn.started", "maps", marker=d2["message"], prompt_head="x")
        self.ev("permission.answered", "maps", request_id="r1", behavior="allow",
                answers={"Which tiles to use?": "publish"}, channel="web", owner=True)
        (ans,) = [m for m in self.st["messages"].values() if m.get("for_delegation") == d1["id"]]
        self.ev("turn.started", "maps", marker=ans["id"], prompt_head="x")     # inserted in the middle of the d2 turn
        self.ev("permission.requested", "maps", request_id="r2", tool="AskUserQuestion", interactive=True,
                input={"questions": [{"question": "What do we take?"}]})
        self.assertEqual(self.st["sessions"]["maps"]["asks"][-1].get("delegation"), d2["id"])
        self.assertEqual(self.st["delegations"][d1["id"]]["state"], "working")     # the owner answered — not "waiting for you"
        self.ev("turn.result", "maps", text="bot analysis: options in the card")
        self.ev("turn.stopped", "maps")
        self.assertEqual(self.st["delegations"][d2["id"]]["state"], "waiting-owner")   # waiting for an answer to its own question
        self.ev("permission.answered", "maps", request_id="r2", behavior="allow", answers={"What do we take?": "later"},
                channel="web", owner=True)
        (ans2,) = [m for m in self.st["messages"].values() if m.get("for_delegation") == d2["id"]]
        self.ev("turn.started", "maps", marker=ans2["id"], prompt_head="x")
        self.ev("turn.result", "maps", text="the cross is done")
        self.assertEqual(self.st["delegations"][d2["id"]]["reply"], "the cross is done")   # the result goes into its own delegation
        self.assertIsNone(self.st["delegations"][d1["id"]].get("reply"))

    def test_a_hubs_own_task_does_not_answer_the_form(self):
        self.ask()
        self.ev("delegation.created", None, session="maps", context="refresh the cache", **HUB)
        self.assertEqual(len(self.st["sessions"]["maps"]["asks"]), 1)        # the question stays open: these are not the owner's words
        self.assertEqual(self.st["sessions"]["maps"]["state"], "busy")

    def test_a_permission_prompt_is_not_answered_with_words(self):
        self.ask(tool="Bash")
        self.delegate("check the tile server first")
        self.assertFalse([a for a in self.actions if a.get("do") == "respond"])


if __name__ == "__main__":
    unittest.main()
