"""A closed card in Telegram carries its outcome itself: the "answer", "question withdrawn" and "connected" lines are
appended to the card text, and the buttons are removed by the same edit; there is no separate message (the hub sent
~130 of them in a day, all silent but cluttering the chat). If the edit fails (API error, text too long), as before:
the buttons go and the outcome line is sent as a reply.
End-to-end run: a fake Bot API -> mailbox -> journal -> the real core fold (tests/test_telegram_flow.Flow)."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from tests import test_telegram_flow

CARD = [{"question": "Ship the build today?", "options": [{"label": "Yes"}, {"label": "No"}]}]


class CardClosedInPlace(test_telegram_flow.Flow):
    def edits(self):
        return self.fake.sent("editMessageText")

    def test_answer_on_the_page_is_written_into_the_card(self):
        self.ask("r1", CARD)
        self.cards(1)
        before = len(self.fake.sent())
        self.j.append("permission.answered", "w", {"request_id": "r1", "behavior": "allow",
                                                   "answers": {CARD[0]["question"]: "Yes"}, "channel": "web", "owner": True})
        self.bot.tick()
        self.assertEqual(len(self.fake.sent()), before)                 # no new message
        last = self.edits()[-1]
        self.assertIn("Ship the build today?", last["text"])         # the card text stayed
        self.assertIn("✅ Answer: Yes (on the page)", last["text"])
        self.assertEqual(last["reply_markup"], {"inline_keyboard": []})  # buttons removed by the same edit

    def test_pressed_button_closes_the_card_in_place(self):
        self.ask("r1", CARD)
        (card,) = self.cards(1)
        before = len(self.fake.sent())
        self.press(self.button(card, "No"))
        self.assertEqual(len(self.fake.sent()), before)
        self.assertIn("✅ Answer: No", self.edits()[-1]["text"])

    def test_withdrawn_card_says_so_in_place(self):
        self.ask("r1", CARD)
        q = self.core()[0]["sessions"]["w"]["asks"][0]["id"]
        before = len(self.fake.sent())
        self.j.append("question.withdrawn", "w", {"id": q, "channel": "session", "owner": False, "sender": "w"})
        self.bot.tick()
        self.assertEqual(len(self.fake.sent()), before)
        self.assertIn("⌛ Question withdrawn", self.edits()[-1]["text"])

    def test_permission_card_closes_in_place(self):
        self.ask("r1", None, tool="Bash", input={"command": "make deploy"})
        before = len(self.fake.sent())
        self.j.append("permission.answered", "w", {"request_id": "r1", "behavior": "allow", "channel": "web", "owner": True})
        self.bot.tick()
        self.assertEqual(len(self.fake.sent()), before)
        self.assertIn("make deploy", self.edits()[-1]["text"])
        self.assertIn("✅ Answer: allowed (on the page)", self.edits()[-1]["text"])

    def test_the_answer_text_is_escaped(self):
        self.ask("r1", CARD)
        self.j.append("permission.answered", "w", {"request_id": "r1", "behavior": "allow",
                                                   "answers": {CARD[0]["question"]: "<b>yes</b> & at once"},
                                                   "channel": "web", "owner": True})
        self.bot.tick()
        self.assertIn("&lt;b&gt;yes&lt;/b&gt; &amp; at once", self.edits()[-1]["text"])

    def test_when_the_edit_fails_the_note_goes_as_before(self):
        self.ask("r1", CARD)
        self.fake.fail["editMessageText"] = [400]
        self.j.append("permission.answered", "w", {"request_id": "r1", "behavior": "allow",
                                                   "answers": {CARD[0]["question"]: "Yes"}, "channel": "web", "owner": True})
        self.bot.tick()
        self.assertTrue(self.fake.sent("editMessageReplyMarkup"))
        self.assertIn("✅ Answer: Yes (on the page)", self.fake.sent()[-1]["text"])

    def test_two_question_form_gets_one_note_per_question_and_no_extra_messages(self):
        self.ask("r2", [{"question": "Color?", "options": [{"label": "Blue"}, {"label": "Red"}]},
                        {"question": "Size?", "options": [{"label": "S"}, {"label": "M"}]}])
        self.cards(2)
        before = len(self.fake.sent())
        self.j.append("permission.answered", "w", {"request_id": "r2", "behavior": "allow",
                                                   "answers": {"Color?": "Blue", "Size?": "M"}, "channel": "web",
                                                   "owner": True})
        self.bot.tick()
        self.assertEqual(len(self.fake.sent()), before)
        closed = [e["text"] for e in self.edits() if "✅ Answer" in e["text"]]
        self.assertEqual(len(closed), 2)
        self.assertTrue(any("Color?" in t for t in closed) and any("Size?" in t for t in closed))

    def test_each_card_of_a_form_shows_only_its_own_answer(self):
        # the owner read the same answer line "Blue, M" on both cards as "one answer was written to both"
        self.ask("r2", [{"question": "Color?", "options": [{"label": "Blue"}, {"label": "Red"}]},
                        {"question": "Size?", "options": [{"label": "S"}, {"label": "M"}]}])
        self.cards(2)
        self.j.append("permission.answered", "w", {"request_id": "r2", "behavior": "allow",
                                                   "answers": {"Color?": "Blue", "Size?": "M"}, "channel": "web",
                                                   "owner": True})
        self.bot.tick()
        by = {("Color?" in e["text"]): e["text"] for e in self.edits() if "✅ Answer" in e["text"]}
        self.assertIn("✅ Answer: Blue", by[True])
        self.assertNotIn("M", by[True].split("✅ Answer:")[1])
        self.assertIn("✅ Answer: M", by[False])
        self.assertNotIn("Blue", by[False].split("✅ Answer:")[1])

    def test_a_question_without_a_saved_text_is_closed_as_before(self):
        # review: Telegram rejected one question's text as HTML (it went out as plain text, so there is no card text):
        # its buttons are removed and the outcome is sent as a line, as before (not silently); a question with a known
        # text carries the outcome itself
        self.ask("r2", [{"question": "Color?", "options": [{"label": "Blue"}, {"label": "Red"}]},
                        {"question": "Size?", "options": [{"label": "S"}, {"label": "M"}]}])
        (ask,) = self.bot.st["asks"].values()
        first = ask["qmsg"]["0"]
        self.bot.st["card_html"].pop(str(first))
        before = len(self.fake.sent())
        self.j.append("permission.answered", "w", {"request_id": "r2", "behavior": "allow",
                                                   "answers": {"Color?": "Blue", "Size?": "M"}, "channel": "web",
                                                   "owner": True})
        self.bot.tick()
        self.assertEqual([b["message_id"] for b in self.fake.sent("editMessageReplyMarkup")], [first])
        (note,) = self.fake.sent()[before:]
        self.assertEqual(note["reply_parameters"]["message_id"], first)
        self.assertEqual(len([e for e in self.edits() if "✅ Answer" in e["text"]]), 1)

    def test_a_long_card_is_closed_in_its_button_message_only(self):
        # review: the start of a long card carries no buttons, so it gets no edits (extra calls count against the Telegram limit)
        self.j.append("permission.requested", "w", {"request_id": "r1", "tool": "Bash", "input": {"command": "make"},
                                                    "why": "explains for a long time " * 400})
        self.bot.tick()
        (ask,) = self.bot.st["asks"].values()
        self.assertGreater(len(ask["msgs"]), 1)
        before = len(self.fake.sent())
        self.j.append("permission.answered", "w", {"request_id": "r1", "behavior": "allow", "channel": "web", "owner": True})
        self.bot.tick()
        self.assertEqual(len(self.fake.sent()), before)
        self.assertEqual(self.fake.sent("editMessageReplyMarkup"), [])
        self.assertEqual([e["message_id"] for e in self.edits()][-1], ask["msgs"][-1])

    def test_links_do_not_count_against_the_length_limit(self):
        # the Telegram limit counts visible text; long link addresses in the card's HTML are no reason to send the outcome separately
        self.ask("r1", CARD)
        (key,) = self.bot.st["card_html"]
        self.bot.st["card_html"][key] += "".join(f'<a href="https://files.example/{"p" * 300}/{i}>f{i}</a> ' for i in range(20))
        before = len(self.fake.sent())
        self.j.append("permission.answered", "w", {"request_id": "r1", "behavior": "allow",
                                                   "answers": {CARD[0]["question"]: "Yes"}, "channel": "web", "owner": True})
        self.bot.tick()
        self.assertEqual(len(self.fake.sent()), before)
        self.assertIn("✅ Answer: Yes", self.edits()[-1]["text"])

    def test_tool_card_closes_in_place(self):
        self.j.append("catalog.loaded", None, {"tools": {"browser:mac": {"kind": "mcp", "risk": ["owner-data"], "server": {}}}})
        self.j.append("tools.requested", "w", {"tool": "browser:mac", "why": "open the page " * 400,
                                               "channel": "session", "sender": "w", "owner": False})
        self.j.append("tools.granted", "w", {"tool": "browser:mac", "channel": "session", "owner": False, "sender": "hub"})
        self.bot.tick()
        (card,) = self.cards(1)
        self.bot.save()                                                  # the card text survives a save
        before = len(self.fake.sent())
        self.press(self.button(card, "Connect"))
        self.assertEqual(len(self.fake.sent()), before)
        self.assertIn("✅ connected", self.edits()[-1]["text"])

    def test_texts_of_cards_the_bot_forgot_are_not_kept(self):
        self.ask("r1", CARD)
        self.bot.st["card_html"]["999999"] = "a forgotten card"
        self.bot.save()
        self.assertNotIn("999999", self.bot.st["card_html"])
        self.assertEqual(len(self.bot.st["card_html"]), 1)              # the open one stays


def load_tests(loader, standard, pattern):
    """Only its own tests: a Flow subclass would otherwise also run all of Flow's (they run in their own module)."""
    own = sorted(n for n in vars(CardClosedInPlace) if n.startswith("test_"))
    return unittest.TestSuite(CardClosedInPlace(n) for n in own)


if __name__ == "__main__":
    unittest.main()
