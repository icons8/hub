"""An answered card is deleted from the chat (telegram.drop_answered in hub.json, off by default): the "✅ Answer: …" line
repeats the choice the owner just made themselves, while the chat accumulates closed questions. Off — the result is in the
card itself, as before (tests/test_card_closed_in_place). Telegram refused to delete (older than 48 h) — the result goes
in the card; a network failure — what was not deleted is deleted by the retry, what is already deleted is not touched twice."""
import tests  # noqa: F401 — keep the machine's settings out of the test
import unittest

from modules.telegram import bot as tg
from tests import test_telegram_flow

CARD = [{"question": "Ship the build today?", "options": [{"label": "Yes"}, {"label": "No"}]}]


class CardDropped(test_telegram_flow.Flow):
    def setUp(self):
        super().setUp()
        self.bot.hub_config = lambda: {"telegram": {"drop_answered": True}}

    def deleted(self):
        return [b["message_id"] for b in self.fake.sent("deleteMessage")]

    def answer_on_page(self, rid, answers=None):
        self.j.append("permission.answered", "w", {"request_id": rid, "behavior": "allow", "channel": "web",
                                                   "owner": True, **({"answers": answers} if answers else {})})
        self.bot.tick()

    def test_answer_on_the_page_deletes_the_card(self):
        self.ask("r1", CARD)
        (ask,) = self.bot.st["asks"].values()
        before = len(self.fake.sent())
        self.answer_on_page("r1", {CARD[0]["question"]: "Yes"})
        self.assertEqual(self.deleted(), ask["msgs"])
        self.assertEqual(len(self.fake.sent()), before)                  # no "answered" line
        self.assertEqual(self.fake.sent("editMessageText"), [])          # no edit in the card

    def test_pressed_button_deletes_the_card(self):
        self.ask("r1", CARD)
        (card,) = self.cards(1)
        (ask,) = self.bot.st["asks"].values()
        self.press(self.button(card, "No"))
        self.assertEqual(self.deleted(), ask["msgs"])

    def test_every_part_of_a_long_card_and_every_question_is_deleted(self):
        self.j.append("permission.requested", "w", {"request_id": "r1", "tool": "Bash", "input": {"command": "make"},
                                                    "why": "explains at length " * 400})
        self.ask("r2", [{"question": "Color?", "options": [{"label": "Blue"}, {"label": "Red"}]},
                        {"question": "Size?", "options": [{"label": "S"}, {"label": "M"}]}])
        long, form = (self.bot.st["asks"][k] for k in sorted(self.bot.st["asks"], key=lambda k: self.bot.st["asks"][k]["request_id"]))
        self.assertGreater(len(long["msgs"]), 1)
        self.answer_on_page("r1")
        self.answer_on_page("r2", {"Color?": "Blue", "Size?": "M"})
        self.assertEqual(self.deleted(), long["msgs"] + form["msgs"])

    def test_withdrawn_card_is_deleted(self):
        self.ask("r1", CARD)
        (ask,) = self.bot.st["asks"].values()
        q = self.core()[0]["sessions"]["w"]["asks"][0]["id"]
        self.j.append("question.withdrawn", "w", {"id": q, "channel": "session", "owner": False, "sender": "w"})
        self.bot.tick()
        self.assertEqual(self.deleted(), ask["msgs"])
        self.assertEqual(self.fake.sent("editMessageText"), [])

    def test_a_card_telegram_refuses_to_delete_is_closed_in_place(self):
        # Telegram does not delete after 48 h — the result goes in the card, as without the setting
        self.ask("r1", CARD)
        self.fake.fail["deleteMessage"] = [400]
        self.answer_on_page("r1", {CARD[0]["question"]: "Yes"})
        self.assertIn("✅", self.fake.sent("editMessageText")[-1]["text"])
        self.assertEqual(self.fake.sent("editMessageText")[-1]["reply_markup"], {"inline_keyboard": []})

    def test_a_network_failure_resumes_without_deleting_twice(self):
        self.j.append("permission.requested", "w", {"request_id": "r1", "tool": "Bash", "input": {"command": "make"},
                                                    "why": "explains at length " * 400})
        self.bot.tick()
        (ask,) = self.bot.st["asks"].values()
        msgs, real, calls = list(ask["msgs"]), self.bot.api.call, []

        def flaky(method, **kw):   # the second delete is a network drop: the first is already deleted
            if method == "deleteMessage":
                calls.append(kw["message_id"])
                if len(calls) == 2:
                    raise tg.TgError("connection reset", transient=True)
            return real(method, **kw)
        self.bot.api.call = flaky
        self.answer_on_page("r1")
        self.assertEqual(self.deleted(), msgs[:1])
        self.bot.hold_until = 0
        self.bot.tick()
        self.assertEqual(self.deleted(), msgs)                     # each exactly once
        self.assertEqual(self.bot.st["asks"], {})

    def test_off_by_default_the_card_is_closed_in_place(self):
        self.bot.hub_config = lambda: {}
        self.ask("r1", CARD)
        self.answer_on_page("r1", {CARD[0]["question"]: "Yes"})
        self.assertEqual(self.deleted(), [])
        self.assertIn("✅", self.fake.sent("editMessageText")[-1]["text"])


def load_tests(loader, standard, pattern):
    """Only its own tests: a Flow subclass would otherwise run all of Flow's too (they run in their own module)."""
    own = sorted(n for n in vars(CardDropped) if n.startswith("test_"))
    return unittest.TestSuite(CardDropped(n) for n in own)


if __name__ == "__main__":
    unittest.main()
