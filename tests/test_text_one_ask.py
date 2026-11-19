"""The owner's words to a session close a question only when it is the single open one; with two or more, none is
closed (the answer is a card option or a verbatim quote of its text)."""
import tests  # noqa: F401 — hide the machine's settings
import unittest

from core import owner, reducer

OWNER = {"channel": "web", "owner": True}


def q(text):
    return {"questions": [{"question": text, "options": [{"label": "Yes"}, {"label": "No"}]}]}


class TextAnswersOnlyTheOnlyAsk(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "dev", cwd="/d", session_id="s-dev", **OWNER)
        self.ev("session.started", "dev")
        self.ev("turn.started", "dev", marker=None, prompt_head="x")

    def ev(self, type, entity=None, at=None, **data):
        self.seq += 1
        self.st, _acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": at or 1000 + self.seq, "type": type,
                                                       "entity": entity, "data": data})
        return why

    def ask(self, rid, text):
        self.ev("permission.requested", "dev", request_id=rid, tool="AskUserQuestion", interactive=True, input=q(text))

    def asks(self):
        return [a["request_id"] for a in self.st["sessions"]["dev"].get("asks") or []]

    def say(self, text):
        self.ev("message.received", None, to="dev", text=text, **OWNER)
        return self.st["messages"][f"m{self.seq}"]

    def test_one_open_ask_is_answered_by_words(self):
        self.ask("r1", "Roll out the first?")
        msg = self.say("yes, roll it out")
        self.assertTrue(msg.get("answers_question"))
        self.assertEqual(self.asks(), [])

    def test_two_open_asks_stay_open_after_words(self):
        self.ask("r1", "Roll out the first?")
        self.ask("r2", "Roll out the second?")
        msg = self.say("yes, roll it out")
        self.assertFalse(msg.get("answers_question"))
        self.assertEqual(self.asks(), ["r1", "r2"])

    def test_an_expired_ask_does_not_count_as_open(self):
        # a question older than a day is no longer open as a card (owner.ASK_TTL): the second stays the only one, and
        # the words answer it
        self.ask("r1", "Roll out the first?")
        self.ev("permission.requested", "dev", at=1000 + owner.ASK_TTL - 4 * 3600, request_id="r2", tool="AskUserQuestion", interactive=True,
                input=q("Roll out the second?"))
        second = self.st["sessions"]["dev"]["asks"][1]["id"]
        self.ev("message.received", None, at=1000 + owner.ASK_TTL + 2 * 3600, to="dev", text="yes, roll it out", **OWNER)
        self.assertEqual(self.st["messages"][f"m{self.seq}"].get("answers_question"), second)
        self.assertEqual(self.asks(), ["r1"])   # the stale one stays but is not open

    def test_two_open_asks_the_named_card_is_still_answered(self):
        # a verbatim quote of the question (owner.quoted_card) arrives as an event with `card`; the number of open ones
        # does not matter
        self.ask("r1", "Roll out the first?")
        self.ask("r2", "Roll out the second?")
        first = self.st["sessions"]["dev"]["asks"][0]["id"]
        self.ev("message.received", None, to="dev", text="Roll out the first? yes", card=first, **OWNER)
        self.assertEqual(self.asks(), ["r2"])


if __name__ == "__main__":
    unittest.main()
