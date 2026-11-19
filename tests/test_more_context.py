""""Need more context" is a standard answer to any card (owner decision 2026-10-06: a global option so that the session
starts replying with what is needed — screenshots, specifications, mermaid diagrams). The core delivers this answer to
the session together with a hint about what is expected of it; other answers work as before."""
import tests  # noqa: F401 — keep the machine's settings out
import unittest

from core import owner, reducer

OWNER = {"channel": "telegram", "owner": True}
Q = {"questions": [{"question": "Push the runbook?", "options": [{"label": "Yes"}, {"label": "Later"}]}]}


class MoreContext(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "maps", cwd="/m", session_id="s-m", **OWNER)
        self.ev("session.started", "maps")
        self.ev("turn.started", "maps", marker=None, prompt_head="x")
        self.ev("permission.requested", "maps", request_id="r1", tool="AskUserQuestion", interactive=True, input=Q)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def answer(self, label):
        self.assertIsNone(self.ev("permission.answered", "maps", request_id="r1", behavior="allow",
                                  answers={"Push the runbook?": label}, **OWNER))
        return self.st["messages"][f"m{self.seq}"]["text"]

    def test_more_context_comes_with_what_to_do(self):
        text = self.answer(owner.MORE_CONTEXT)
        self.assertIn("Push the runbook?: " + owner.MORE_CONTEXT, text)
        self.assertIn(owner.MORE_HINT, text)

    def test_after_it_the_same_questions_come_back_and_the_card_is_not_made_smaller(self):
        # 2026-11-07: "decide yourself" and a card cut to a yes/no after the owner did not understand it — the owner's own
        # decision taken by the session; the hint (and the card rule in the system prompt) says: explain, ask the same again
        from hosts import streamjson
        for text in (owner.MORE_HINT, streamjson.CARDS):
            with self.subTest(text=text[:20]):
                self.assertIn("the same questions and the same options", text)
                self.assertIn("decision", text)
        self.assertNotIn("decide yourself;", streamjson.CARDS[streamjson.CARDS.index("They answered"):])

    def test_other_answers_stay_as_they_were(self):
        self.assertNotIn(owner.MORE_HINT, self.answer("Yes"))

    def test_the_answer_earlier_pages_and_bots_sent_is_the_same_answer(self):
        # a Telegram bot not restarted since the update, an old journal: the Russian text of the button
        from core import langdata
        self.assertTrue(owner.asks_more({"Push the runbook?": owner.MORE_CONTEXT}))
        self.assertTrue(owner.asks_more({"a": "Yes", "Push the runbook?": langdata.legacy("more_context")}))
        self.assertFalse(owner.asks_more({"Push the runbook?": "Yes"}))
        self.assertFalse(owner.asks_more(None))


if __name__ == "__main__":
    unittest.main()
