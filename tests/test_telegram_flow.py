"""Controlling the hub from Telegram, end to end (confirmations were recently reworked to go only through
Ask User, so check that Telegram control still works and run various cases).
A fake Bot API -> bot facts into the mailbox -> journal -> the real core fold -> what the core answers the session
(respond)."""
import tests  # noqa: F401 — ignore the machine's settings
import json
import unittest

from core import reducer
from tests.test_telegram import OWNER, TelegramTest


class Flow(TelegramTest):
    def setUp(self):
        super().setUp()
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "s-w", "channel": "cli", "owner": True})
        self.j.append("session.started", "w", {"session_id": "s-w"})
        self.j.append("turn.started", "w", {"marker": None, "prompt_head": "work"})
        self.uid = 1000

    def core(self):
        """Fold of the whole journal: (state, core actions)."""
        st, acts = reducer.empty(), []
        with open(self.j.path) as f:
            for line in f:
                st, a, _why = reducer.reduce(st, json.loads(line))
                acts += a
        return st, acts

    def ask(self, rid, questions, tool="AskUserQuestion", **extra):
        self.j.append("permission.requested", "w", {"request_id": rid, "tool": tool, "interactive": True,
                                                    "input": {"questions": questions} if tool == "AskUserQuestion"
                                                    else extra.get("input", {})})
        self.bot.tick()

    def press(self, button):
        self.uid += 1
        self.fake.updates = [{"update_id": self.uid, "callback_query": {"id": f"c{self.uid}", "from": {"id": OWNER},
                                                                       "data": button["callback_data"]}}]
        self.bot.poll(timeout=0)
        self.ingest()
        self.bot.tick()

    def reply(self, to_message_id, text):
        self.uid += 1
        self.fake.updates = [self.msg(text, self.uid, reply_to=to_message_id)]
        self.bot.poll(timeout=0)
        self.ingest()
        self.bot.tick()

    def cards(self, n):
        """The last n messages with buttons: [(id, text, [buttons])]."""
        out = [(i, b) for i, (m, b) in enumerate(self.fake.calls) if m == "sendMessage" and b.get("reply_markup")]
        return [b for _, b in out[-n:]]

    def buttons(self, card):
        return [b for row in card["reply_markup"]["inline_keyboard"] for b in row]

    def button(self, card, text):
        return next(b for b in self.buttons(card) if b["text"].endswith(text))

    def answers(self, session):
        """The owner's answers to a session's question cards are messages to it (a question does not hold the turn, release 1)."""
        return [m["text"] for m in self.core()[0]["messages"].values()
                if m["session"] == session and m.get("answers_question")]

    def responded(self, rid):
        return [a["response"] for a in self.core()[1] if a.get("do") == "respond" and a.get("request_id") == rid]

    def hub_ask(self, rid, question, ts=None):
        self.j.append("session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", "channel": "cli", "owner": True})
        self.j.append("session.started", "hub", {"session_id": "s-h"})
        self.j.append("turn.started", "hub", {"marker": None, "prompt_head": "x"})
        self.j.append("permission.requested", "hub", {"request_id": rid, "tool": "AskUserQuestion", "interactive": True,
                                                      "input": {"questions": [{"question": question, "options": [{"label": "Yes"}, {"label": "No"}]}]}},
                      ts=ts)
        self.bot.tick()

    def test_hub_question_card_answer_reaches_the_hub_as_a_message(self):
        # the hub does not hold the form: a Telegram button is an answer to the hub as a message with a question marker
        self.hub_ask("h1", "Merge?")
        (card,) = self.cards(1)
        self.press(self.button(card, "Yes"))
        st = self.core()[0]
        self.assertEqual(st["sessions"]["hub"].get("asks"), [])
        self.assertTrue(any(m.get("answers_question") and "Merge?: Yes" in m["text"]
                            for m in st["messages"].values() if m["session"] == "hub"))
        self.assertIn("✅ Answer: Yes", self.fake.last_text())

    def test_hub_question_card_closes_when_answered_in_words(self):
        # a text answer to the hub withdraws the question and its Telegram buttons (otherwise they stay live and a
        # press goes nowhere)
        self.hub_ask("h2", "Merge?")
        self.j.append("message.received", None, {"to": "hub", "text": "merge it", "channel": "web", "owner": True})
        self.bot.tick()
        self.assertIn("Question withdrawn", self.fake.last_text())

    def test_hub_question_card_closes_when_it_expires(self):
        import time
        from core import owner
        self.hub_ask("h3", "Merge?", ts=time.time() - owner.ASK_TTL - 60)
        self.bot.tick()
        self.assertIn("Question withdrawn", self.fake.last_text())

    def test_one_question_answer_reaches_the_session(self):
        self.ask("r1", [{"question": "Which variant?", "options": [{"label": "A"}, {"label": "B"}]}])
        (card,) = self.cards(1)
        self.assertEqual(self.core()[0]["sessions"]["w"]["state"], "busy")      # a question does not hold the turn
        self.press(self.button(card, "B"))
        (text,) = self.answers("w")
        self.assertIn("Which variant?: B", text)
        self.assertIn("✅ Answer: B", self.fake.last_text())

    def test_several_questions_in_one_form_one_answer_to_the_session(self):
        self.ask("r2", [{"question": "Color?", "options": [{"label": "Blue"}, {"label": "Red"}]},
                        {"question": "Size?", "options": [{"label": "S"}, {"label": "M"}]}])
        q1, q2 = self.cards(2)
        self.press(self.button(q2, "M"))
        self.assertEqual(self.answers("w"), [])                         # an answer to one of two is not yet an answer
        self.press(self.button(q1, "Blue"))
        (text,) = self.answers("w")
        self.assertIn("Color?: Blue", text)
        self.assertIn("Size?: M", text)

    def test_multiselect_picks_several_then_done(self):
        self.ask("r3", [{"question": "What to enable?", "multiSelect": True,
                         "options": [{"label": "Logs"}, {"label": "Metrics"}, {"label": "Traces"}]}])
        (card,) = self.cards(1)
        self.press(self.button(card, "Logs"))
        self.assertEqual(self.answers("w"), [])                         # a selection, not an answer
        self.press(self.button(card, "Traces"))
        self.press(self.button(card, "Done"))
        (text,) = self.answers("w")
        self.assertIn("What to enable?: Logs, Traces", text)

    def test_own_answer_by_reply_to_the_question(self):
        self.ask("r4", [{"question": "What to name it?", "options": [{"label": "alpha"}, {"label": "beta"}]}])
        (card,) = self.cards(1)
        mid = self.fake.next_id
        self.reply(mid, "gamma")
        (text,) = self.answers("w")
        self.assertIn("What to name it?: gamma", text)
        self.assertTrue(self.fake.sent("setMessageReaction"))

    def test_words_by_reply_answer_the_whole_form(self):
        # a form of three questions, the owner replied by voice to the first card: a 👌 but nothing to the
        # session, since the bot waited for buttons on the other two. The owner's words answer the whole form
        self.ask("r12", [{"question": "What before the meeting?", "options": [{"label": "nothing"}, {"label": "rollout"}]},
                         {"question": "Price?", "options": [{"label": "$5"}, {"label": "$10"}]},
                         {"question": "Who moderates?", "options": [{"label": "me"}, {"label": "agent"}]}])
        q1, q2, q3 = self.cards(3)
        mid = self.fake.next_id                                           # the last card is the "who moderates?" question
        self.press(self.button(q2, "$10"))
        self.reply(mid, "preparing the rollout, the agent moderates")
        (text,) = self.answers("w")
        self.assertIn("Who moderates?: preparing the rollout, the agent moderates", text)
        self.assertIn("Price?: $10", text)                                 # what was chosen by button is not lost
        self.assertIn("What before the meeting?", text)
        self.assertTrue(self.fake.sent("setMessageReaction"))

    def test_reply_to_an_already_answered_question_reaches_the_session(self):
        # the owner replied to a card of an already answered question (a remark that the second one is already
        # connected too); the bot took it as a "custom option" of the closed question and silently dropped it; now it
        # is an ordinary message to the session
        self.ask("r11", [{"question": "Which account?", "options": [{"label": "dev"}, {"label": "support"}]}])
        (card,) = self.cards(1)
        mid = self.fake.next_id
        self.press(self.button(card, "dev"))
        self.reply(mid, "and the second one is connected too")
        got = [e for e in self.core()[0]["messages"].values() if e["text"] == "and the second one is connected too"]
        self.assertEqual(len(got), 1, "the message was lost")
        self.assertEqual(got[0]["session"], "w")                       # the session that asked
        self.assertEqual(len(self.responded("r11")), 1)                 # no second answer to the form

    def test_permission_allow_and_deny(self):
        self.ask("r5", None, tool="Bash", input={"command": "rm -rf build"})
        (card,) = self.cards(1)
        self.assertIn("asks for permission", card["text"])
        self.press(self.button(card, "Deny"))
        (resp,) = self.responded("r5")
        self.assertEqual(resp["behavior"], "deny")
        self.ask("r6", None, tool="Bash", input={"command": "ls"})
        (card,) = self.cards(1)
        self.press(self.button(card, "Allow"))
        self.assertEqual(self.responded("r6")[0]["behavior"], "allow")

    def test_answered_on_the_page_then_pressed_in_telegram(self):
        self.ask("r7", [{"question": "Yes?", "options": [{"label": "Yes"}, {"label": "No"}]}])
        (card,) = self.cards(1)
        self.j.append("permission.answered", "w", {"request_id": "r7", "behavior": "allow", "answers": {"Yes?": "No"},
                                                   "channel": "web", "owner": True})
        self.bot.tick()
        self.assertIn("(on the page)", self.fake.last_text())
        self.press(self.button(card, "Yes"))
        self.assertEqual(len(self.responded("r7")), 1)                  # no second answer
        self.assertIn(self.fake.sent("answerCallbackQuery")[-1]["text"], ("Already answered", "This question is no longer relevant"))

    def test_owner_away_question_is_not_a_dead_button(self):
        self.j.append("owner.away", None, {"channel": "cli", "owner": True})
        self.ask("r8", [{"question": "Deploy?", "options": [{"label": "Yes"}, {"label": "No"}]}])
        (resp,) = self.responded("r8")
        self.assertEqual(resp["behavior"], "deny")                      # the core answered for the owner: "away"
        sent = self.fake.sent()[-1] if self.fake.sent() else {}
        self.assertFalse(sent.get("reply_markup"), "buttons that will do nothing")

    def test_back_from_away_the_saved_question_comes_in_the_summary(self):
        self.j.append("owner.away", None, {"channel": "cli", "owner": True})
        self.ask("r10", [{"question": "Deploy today?", "options": [{"label": "Yes"}, {"label": "No"}]}])
        self.j.append("owner.back", None, {"channel": "telegram", "owner": True})
        self.bot.tick()
        texts = [m["text"] for m in self.fake.sent()]
        summary = next(t for t in texts if "Welcome back" in t)
        self.assertIn("Deploy today?", summary)
        # and the question itself as a card with buttons (after the return there were no questions, nothing to answer with)
        (card,) = self.cards(1)
        self.assertIn("Deploy today?", card["text"])
        self.press(self.button(card, "Yes"))
        (text,) = self.answers("w")
        self.assertIn("Deploy today?: Yes", text)

    def test_a_card_the_hub_answered_while_away_does_not_come_after_back(self):
        # spec d2549241: the hub answered the card for the owner — on return only the summary, no dead card
        self.j.append("session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", "channel": "cli",
                                                   "owner": True})
        self.j.append("session.started", "hub", {"session_id": "s-h"})
        self.j.append("owner.away", None, {"channel": "cli", "owner": True})
        self.ask("r13", [{"question": "Name the file tiles.toml?", "options": [{"label": "Yes"}, {"label": "No"}]}])
        self.j.append("turn.result", "w", {"text": "left it for the owner"})
        self.j.append("permission.answered", "w", {"request_id": "r13", "behavior": "allow", "why": "short · rename",
                                                   "answers": {"Name the file tiles.toml?": "Yes"},
                                                   "channel": "session", "owner": False, "sender": "hub"})
        self.j.append("owner.back", None, {"channel": "telegram", "owner": True})
        self.bot.tick()
        self.assertEqual(self.cards(1), [])
        summary = next(m["text"] for m in self.fake.sent() if "Welcome back" in m["text"])
        self.assertIn("Name the file tiles.toml?", summary)

    def test_an_urgent_card_while_away_comes_at_once(self):
        # spec d2549241 (owner, q2551190 "push at once"): 🚨 reaches him while he is away
        self.j.append("owner.away", None, {"channel": "cli", "owner": True})
        self.ask("r14", [{"question": "🚨 prod is down: roll back?", "options": [{"label": "Yes"}, {"label": "No"}]}])
        (card,) = self.cards(1)
        self.assertIn("prod is down", card["text"])

    def test_the_hubs_card_on_his_message_while_away_comes_at_once(self):
        # he writes to the hub while an away with a deputy holds through his words: the hub's card in that turn is his
        # to answer now, not at his return
        cli = {"channel": "cli", "owner": True}
        self.j.append("session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **cli})
        self.j.append("session.started", "hub", {"session_id": "s-h"})
        self.j.append("contact.deputy_set", None, {"person": {"id": "c0000a0b1", "name": "Anna Lee"}, "names": ["Anna"],
                                                   "zone": "map tiles", "path": {"kind": "via", "session": "w",
                                                                                 "address": {"email": "anna@x.com"}},
                                                   **cli})
        self.j.append("owner.away", None, {"deputies": True, **cli})
        word = self.j.append("message.received", None, {"to": "hub", "text": "give her two areas",
                                                        "channel": "telegram", "owner": True})
        self.j.append("turn.started", "hub", {"marker": f"m{word['seq']}", "prompt_head": "x"})
        self.j.append("permission.requested", "hub", {"request_id": "h9", "tool": "AskUserQuestion", "interactive": True,
                                                      "input": {"questions": [{"question": "Two areas — ok?", "options": [
                                                          {"label": "Yes"}, {"label": "No"}]}]}})
        self.bot.tick()
        self.assertEqual(self.core()[0]["owner"]["state"], "away")
        (card,) = self.cards(1)
        self.assertIn("Two areas — ok?", card["text"])
        self.press(self.button(card, "Yes"))
        self.assertTrue(any("Two areas — ok?: Yes" in m["text"] for m in self.core()[0]["messages"].values()
                            if m["session"] == "hub" and m.get("answers_question")))

    def test_asked_before_away_in_the_same_batch_comes_at_once(self):
        # review: the bot read "away" from the reader's state, which is already ahead of the event, so a question
        # asked while the owner was present was held until his return; the card in the core decides (asked without him or not)
        self.j.append("permission.requested", "w", {"request_id": "r12", "tool": "AskUserQuestion", "interactive": True,
                                                    "input": {"questions": [{"question": "Now?", "options": [
                                                        {"label": "Yes"}, {"label": "No"}]}]}})
        self.j.append("owner.away", None, {"channel": "cli", "owner": True})
        self.bot.tick()
        (card,) = self.cards(1)
        self.assertIn("Now?", card["text"])

    def test_writing_while_away_brings_the_owner_back_with_the_summary(self):
        # /away without a duration was forgotten for a day, so the forms of all sessions piled up silently and
        # the owner could not see them
        self.j.append("owner.away", None, {"channel": "cli", "owner": True})
        self.ask("r11", [{"question": "Paint it blue?", "options": [{"label": "Yes"}, {"label": "No"}]}])
        self.j.append("message.received", None, {"text": "well, what is it", "channel": "web", "owner": True})
        self.bot.tick()
        self.assertEqual(self.core()[0]["owner"]["state"], "present")
        self.assertIn("Paint it blue?", self.fake.sent()[-1]["text"])

    def test_two_sessions_ask_at_once_answers_do_not_cross(self):
        self.j.append("session.requested", "v", {"cwd": "/v", "session_id": "s-v", "channel": "cli", "owner": True})
        self.j.append("session.started", "v", {"session_id": "s-v"})
        self.j.append("turn.started", "v", {"marker": None, "prompt_head": "x"})
        self.ask("r9", [{"question": "Q?", "options": [{"label": "1"}, {"label": "2"}]}])
        self.j.append("permission.requested", "v", {"request_id": "r9", "tool": "AskUserQuestion", "interactive": True,
                                                    "input": {"questions": [{"question": "Q?", "options": [
                                                        {"label": "1"}, {"label": "2"}]}]}})
        self.bot.tick()
        cw, cv = self.cards(2)
        self.press(self.button(cv, "2"))
        self.assertEqual(self.answers("w"), [])                         # answers did not get mixed up
        (text,) = self.answers("v")
        self.assertIn("Q?: 2", text)

    def test_tool_card_from_telegram(self):
        self.j.append("catalog.loaded", None, {"tools": {"browser:mac": {"kind": "mcp", "risk": ["owner-data"], "server": {}}}})
        self.j.append("tools.requested", "w", {"tool": "browser:mac", "why": "open the page", "channel": "session",
                                               "sender": "w", "owner": False})
        self.j.append("tools.granted", "w", {"tool": "browser:mac", "channel": "session", "owner": False, "sender": "hub"})
        self.bot.tick()
        (card,) = self.cards(1)
        self.press(self.button(card, "Refuse"))
        st = self.core()[0]
        req = [r for r in st["sessions"]["w"]["requests"] if r["tool"] == "browser:mac"][-1]
        self.assertEqual(req["state"], "denied")
        self.assertIn("✖ refused", self.fake.last_text())


def load_tests(loader, standard, pattern):
    """Only our own: TelegramTest already runs in test_telegram."""
    suite = unittest.TestSuite()
    suite.addTests(Flow(n) for n in Flow.__dict__ if n.startswith("test"))
    return suite


if __name__ == "__main__":
    unittest.main()
