"""The owner answers a session's question card with text, not a button. End to end:
fake Bot API → spool → journal → the real core reduction. Event shapes are as in the journal:
- 07.10: a session has two "publish?" cards; the owner writes the hub from Telegram "Roll out <session>" — the card
  stayed open, the session's result went to its feed. The core does not guess which card was answered: the hub sees the
  open cards with numbers and passes `hub send <session> --for mN --card qN` — the named card is closed, the result
  goes to the owner in Telegram;
- 06.10: the owner copied a retelling of a card from the hub's reply and added "go" — that is not the card's text,
  the core leaves it alone (the hub decides); the card's own text copied verbatim is an answer to it with the owner's
  remaining words;
- a reply to any message of the card: the card itself (already worked), its reminder, a closed one — with the
  "answered" / "question withdrawn" note in the card itself (or a result line, if the edit failed)."""
import tests  # noqa: F401 — keep the machine's settings out
import html
import re
import unittest

from core import owner
from tests.test_telegram import OWNER
from tests.test_telegram_flow import Flow

# card shapes are as in the journal of 07.10 / 06.10 (texts are neutral)
PUBLISH = [{"question": "The flaky test is fixed, in main. For the personal hub to get it and stop rolling the update back, "
                        "a release to GitHub is needed. Only this test fix goes out (nothing else is unpublished). Publish?",
            "header": "Release", "multiSelect": False,
            "options": [{"label": "Publish (Recommended)", "description": "With a compatibility check; the personal hub will pick it up "
                                                                          "at its next update check."},
                        {"label": "Later", "description": "Goes out with the next release."}]}]
PLUGIN = [{"question": "A colleague has a new one: plugin recent-first 1.1.0, sessions in the left list by last activity. "
                       "It reaches the catalog with the release. Publish?", "header": "Release", "multiSelect": False,
           "options": [{"label": "Publish (Recommended)", "description": "With a compatibility check on Mac and Windows."},
                       {"label": "Later", "description": "Everything goes out with the next release."}]}]
SECRETS = [{"question": "The api stand crashes in CrashLoop: deploy/stage.json lacks AUTH_SECRET and two more secrets. The fix "
                        "is ready in branch fix/stage-secrets. Push and open an MR to main?", "header": "Push + MR",
            "multiSelect": False, "options": [{"label": "Yes, push + MR (Recommended)", "description": "I push the branch and "
                                                                                                       "open the MR"},
                                              {"label": "No", "description": "The fix stays local only"}]},
           {"question": "How to bring up the stand of a colleague's branch?", "header": "Stand", "multiSelect": False,
            "options": [{"label": "Comment in the MR (Recommended)", "description": "I ask to merge main into his branch"},
                        {"label": "Do nothing", "description": "The stand comes up by itself"}]}]


class CardTextAnswer(Flow):
    def owner_says(self, text, reply_to=None):
        """The owner's message in Telegram → journal; returns the message's mN in the core and its Telegram id."""
        self.uid += 1
        self.fake.updates = [self.msg(text, self.uid, reply_to=reply_to)]
        self.bot.poll(timeout=0)
        self.ingest()
        self.bot.tick()
        ev = [e for e in self.events() if e["type"] == "message.received" and (e["data"].get("tg") or {}).get("id") == self.uid]
        return (f"m{ev[-1]['seq']}" if ev else None), self.uid

    def events(self):
        from core import journal
        return list(journal.read(self.j.path))

    def asks(self, name="w"):
        return [a["id"] for a in self.core()[0]["sessions"][name].get("asks") or []]

    def msg_of(self, text_part):
        return [m for m in self.core()[0]["messages"].values() if text_part in m["text"]]

    def typed(self, mid):
        """What the core typed to the session for message mN (marker, text, the core's addenda)."""
        return next(a["text"] for a in self.core()[1] if a.get("do") == "type" and a.get("message") == mid)

    def card_as_seen(self):
        """The text of the last card as the owner sees and copies it in Telegram (HTML → text)."""
        edit = [b for m, b in self.fake.calls if m == "editMessageText"][-1]
        return html.unescape(re.sub(r"<[^>]+>", "", edit["text"]))

    # --- what already worked ------------------------------------------------------------------------------------
    def test_a_reply_to_the_card_answers_it_in_words_and_the_result_comes_back(self):
        self.ask("r1", PUBLISH)
        card = self.fake.next_id
        self.reply(card, "ship it")
        (text,) = self.answers("w")
        self.assertIn("Publish?: ship it", text)
        mid = next(m["id"] for m in self.core()[0]["messages"].values() if m.get("answers_question"))
        self.j.append("turn.started", "w", {"marker": mid})
        self.j.append("turn.result", "w", {"text": "Released"})
        self.bot.tick()
        sent = self.fake.sent()[-1]
        self.assertEqual((sent["text"], sent["reply_parameters"]["message_id"]), ("<b>w</b>:\nReleased", card))
        # a reply to this result continues the conversation with the same session
        mid, _ = self.owner_says("did you set the tag?", reply_to=self.fake.next_id)
        self.assertEqual(self.core()[0]["messages"][mid]["session"], "w")

    def test_words_on_the_page_answer_the_card(self):
        self.ask("r1", PUBLISH)
        q = PUBLISH[0]["question"]
        self.j.append("permission.answered", "w", {"request_id": "r1", "behavior": "allow", "answers": {q: "only after 18:00"},
                                                   "channel": "web", "owner": True})
        self.bot.tick()
        (text,) = self.answers("w")
        self.assertIn("Publish?: only after 18:00", text)
        self.assertEqual(self.asks(), [])

    # --- a reply to any message of the card ---------------------------------------------------------------------
    def test_a_reply_to_the_cards_reminder_answers_the_card(self):
        self.ask("r1", PUBLISH)
        self.j.append("timer.fired", "q:w:r1", {"n": 1})
        self.bot.tick()
        self.assertIn("Reminder", self.fake.sent()[-1]["text"])
        self.reply(self.fake.next_id, "yes, publish")
        (text,) = self.answers("w")
        self.assertIn("Publish?: yes, publish", text)
        self.assertEqual(self.asks(), [])

    def test_the_reminder_of_a_long_card_is_paragraphs_with_a_bold_question_and_short_links(self):
        from core import i18n
        self.ask("r1", [{"question": "Accept the mockups? Shots: file:///home/me/shots/DOC-83/ and "
                                     "https://www.figma.com/design/abc/Portal?node-id=1-2.", "options": [{"label": "Yes"}]},
                        {"question": "Now or later? It is two days.", "options": [{"label": "Now"}]}])
        self.j.append("timer.fired", "q:w:r1", {"n": 1})
        self.bot.tick()
        text = self.fake.sent()[-1]["text"]
        head, first, second = text.split("\n\n")
        self.assertIn(html.escape(i18n.t("telegram.reminder_head", session="w")), head)
        self.assertTrue(first.startswith("<b>Accept the mockups?</b> Shots: <code>DOC-83</code> and "), first)
        self.assertIn('<a href="https://www.figma.com/design/abc/Portal?node-id=1-2">'
                      + html.escape(i18n.t("core.owner.link_design")) + "</a>", first)
        self.assertEqual(second, "<b>Now or later?</b> It is two days.")
        self.assertNotIn("file://", text)   # the address does not open on the phone: the name only

    def test_a_reply_to_the_closed_card_note_goes_to_that_session(self):
        self.ask("r1", PUBLISH)
        (card,) = self.cards(1)
        self.press(self.button(card, "Later"))
        self.assertIn("✅ Answer: Later", self.fake.last_text())
        # the outcome of a closed card is in the card itself: a reply to it goes to the same session
        mid, _ = self.owner_says("and remind me tomorrow", reply_to=self.fake.next_id)
        self.assertEqual(self.core()[0]["messages"][mid]["session"], "w")   # not to the hub

    def test_a_reply_to_the_withdrawn_card_note_goes_to_that_session(self):
        self.ask("r1", PUBLISH)
        q = self.asks()[0]
        self.j.append("question.withdrawn", "w", {"id": q, "why": "not needed any more", "channel": "session", "owner": False,
                                                  "sender": "w"})
        self.bot.tick()
        self.assertIn("Question withdrawn", self.fake.last_text())
        mid, _ = self.owner_says("why did you withdraw it?", reply_to=self.fake.next_id)
        self.assertEqual(self.core()[0]["messages"][mid]["session"], "w")

    def test_a_reply_to_a_card_the_core_already_closed_is_not_lost(self):
        # the card was closed by a reply via the hub and the bot has not removed the buttons yet: the core would reject
        # a "custom option" and the words would be lost
        self.ask("r1", PUBLISH)
        card = self.fake.next_id
        self.j.append("question.withdrawn", "w", {"id": self.asks()[0], "channel": "session", "owner": False, "sender": "w"})
        self.bot.reader.feed(self.events()[-1], None)   # the core already knows, the bot has not gone through the cards yet
        self.reply(card, "publish after all")
        got = self.msg_of("publish after all")
        self.assertEqual([m["session"] for m in got], ["w"])

    # --- text to the hub without a reply --------------------------------------------------------------------------
    def test_plain_words_to_the_hub_stay_with_the_hub_and_it_sees_the_open_cards(self):
        # 07.10: "roll it out …" with two cards of a session — the core does not know which one it answers: the hub decides
        self.ask("r1", PUBLISH)
        self.ask("r2", PLUGIN)
        q1, q2 = self.asks()
        mid, tg = self.owner_says("Ship w")
        self.assertEqual(self.core()[0]["messages"][mid]["session"], "hub")
        self.assertEqual(self.asks(), [q1, q2])
        seen = self.typed(mid)
        self.assertIn(f"{q1} at w", seen)
        self.assertIn(f"{q2} at w", seen)
        self.assertIn(f"--for {mid} --card", seen)
        # the hub passes the owner's words to the card they meant (not the last one)
        self.j.append("message.received", None, {"to": "w", "text": "Owner: «Ship w» is a «yes» to the release",
                                                 "for": mid, "card": q1, "channel": "session", "owner": False,
                                                 "sender": "hub"})
        st = self.core()[0]
        relayed = next(m for m in st["messages"].values() if m.get("answers_question"))
        self.assertEqual((relayed["answers_question"], self.asks()), (q1, [q2]))
        self.j.append("turn.started", "w", {"marker": relayed["id"]})
        self.j.append("turn.result", "w", {"text": "Released"})
        self.bot.tick()
        (sent,) = [m for m in self.fake.sent() if "Released" in m["text"]]
        self.assertEqual((sent["text"], sent["reply_parameters"]["message_id"]), ("<b>w</b>:\nReleased", tg))
        closed = [t for t in self.fake.texts() if "Question withdrawn" in t]
        self.assertEqual(len(closed), 1)                       # the buttons are removed on one card, the second is still live

    def test_the_hub_cannot_name_a_card_that_is_not_open(self):
        self.ask("r1", PUBLISH)
        mid, _ = self.owner_says("yes")
        self.j.append("message.received", None, {"to": "w", "text": "yes", "for": mid, "card": "q999", "channel": "session",
                                                 "owner": False, "sender": "hub"})
        from core import reducer
        st, why = reducer.empty(), None
        for e in self.events():
            st, _, why = reducer.reduce(st, e)
        self.assertIn("q999", why or "")
        self.assertEqual(len(self.asks()), 1)

    def test_words_relayed_without_card_do_not_close_the_only_open_card(self):
        # 10.10: hub task new / hub send --for mN without --card is a new request, not an answer; the only open card
        # used to be closed and got the "this is the owner's answer to your question" header
        self.ask("r1", PUBLISH)
        mid, _ = self.owner_says("do one more thing")
        self.j.append("message.received", None, {"to": "w", "text": "Owner: «do one more thing»", "for": mid,
                                                 "channel": "session", "owner": False, "sender": "hub"})
        st = self.core()[0]
        self.assertFalse([m for m in st["messages"].values() if m.get("answers_question")])
        self.assertEqual(len(self.asks()), 1)

    def test_no_note_when_no_card_is_open(self):
        mid, _ = self.owner_says("how are you?")
        self.assertNotIn("--card", self.typed(mid))

    def test_a_retold_card_with_go_stays_with_the_hub(self):
        # 06.10: a retelling of the card copied from the hub's reply, not the card itself — the core does not guess
        self.ask("r1", SECRETS)
        mid, _ = self.owner_says("1. w: the api stand settings lack the JWT secret and two more secrets, the fix is ready "
                                 "in the branch. Question: push the branch and open an MR to main, and second, how to bring up "
                                 "the stand. Recommended option: push and MR. go")
        self.assertEqual(self.core()[0]["messages"][mid]["session"], "hub")
        self.assertEqual(len(self.asks()), 1)
        self.assertIn(f"{self.asks()[0]} at w", self.typed(mid))

    # --- the card text copied verbatim ---------------------------------------------------------------------------
    def test_the_card_text_copied_verbatim_with_go_answers_that_card(self):
        self.ask("r1", SECRETS)
        q = self.asks()[0]
        mid, tg = self.owner_says(SECRETS[0]["question"] + " go")
        st = self.core()[0]
        msg = st["messages"][mid]
        self.assertEqual((msg["session"], msg.get("answers_question"), self.asks()), ("w", q, []))
        self.assertTrue(msg["text"].endswith(": go"), msg["text"])
        self.assertNotIn("AUTH_SECRET", msg["text"].split("»")[-1])   # the session gets the owner's words, not a repeat of its question
        self.j.append("turn.started", "w", {"marker": mid})
        self.j.append("turn.result", "w", {"text": "MR opened"})
        self.bot.tick()
        sent = self.fake.sent()[-1]
        self.assertEqual((sent["text"], sent["reply_parameters"]["message_id"]), ("<b>w</b>:\nMR opened", tg))

    def test_a_stale_named_card_does_not_stop_a_verbatim_copy_from_answering(self):
        # a client named a card (/api/send {card}) that is already closed: the message is treated as without card,
        # a verbatim copy of the card still answers it
        self.ask("r1", SECRETS)
        q = self.asks()[0]
        self.j.append("message.received", None, {"to": "hub", "text": SECRETS[0]["question"] + " go", "card": "q1",
                                                 "channel": "web", "owner": True})
        msg = max(self.core()[0]["messages"].values(), key=lambda m: m["created"])
        self.assertEqual((msg["session"], msg.get("answers_question"), self.asks()), ("w", q, []))

    def test_the_whole_telegram_card_copied_with_words_answers_it(self):
        self.ask("r1", PUBLISH)
        q = self.asks()[0]
        mid, _ = self.owner_says(self.card_as_seen() + "\n\nPublish, but without the plugin")
        msg = self.core()[0]["messages"][mid]
        self.assertEqual((msg["session"], msg.get("answers_question")), ("w", q))
        self.assertTrue(msg["text"].endswith(": Publish, but without the plugin"), msg["text"])

    def test_the_card_text_alone_is_not_an_answer(self):
        self.ask("r1", PUBLISH)
        mid, _ = self.owner_says(PUBLISH[0]["question"])
        self.assertEqual(self.core()[0]["messages"][mid]["session"], "hub")
        self.assertEqual(len(self.asks()), 1)

    def test_a_text_matching_two_open_cards_stays_with_the_hub(self):
        self.ask("r1", PUBLISH)
        self.ask("r2", PUBLISH)
        mid, _ = self.owner_says(PUBLISH[0]["question"] + " yes")
        self.assertEqual(self.core()[0]["messages"][mid]["session"], "hub")
        self.assertEqual(len(self.asks()), 2)

    def test_a_short_question_is_not_matched_inside_other_words(self):
        self.ask("r1", [{"question": "Publish?", "options": [{"label": "Yes"}, {"label": "No"}]}])
        mid, _ = self.owner_says("Publish? — ask the colleague, he knows")
        self.assertEqual(self.core()[0]["messages"][mid]["session"], "hub")
        self.assertEqual(len(self.asks()), 1)

    def test_an_answered_card_text_is_just_words_to_the_hub(self):
        self.ask("r1", PUBLISH)
        (card,) = self.cards(1)
        self.press(self.button(card, "Later"))
        mid, _ = self.owner_says(PUBLISH[0]["question"] + " yes after all")
        self.assertEqual(self.core()[0]["messages"][mid]["session"], "hub")


class QuotedCard(unittest.TestCase):
    """The pure matching function: the card's verbatim text inside the owner's words."""
    def sess(self, *asks):
        return {"name": "w", "asks": [{"id": f"q{i}", "request_id": f"r{i}", "tool": "AskUserQuestion", "since": 0,
                                       "input": {"questions": qs}} for i, qs in enumerate(asks, 1)]}

    def test_markdown_in_the_question_does_not_break_a_copy_of_the_rendered_card(self):
        st = {"sessions": {"w": self.sess([{"question": "Merge `fix/x` into **main**? Tests are green, a review exists.",
                                            "options": []}])}}
        name, ask, rest = owner.quoted_card(st, "Merge fix/x into main? Tests are green, a review exists.\nmerge", 1)
        self.assertEqual((name, ask["id"], rest), ("w", "q1", "merge"))

    def test_the_bots_title_and_hints_are_not_his_words_in_any_language(self):
        # the owner copies the card as his bot showed it: the title and the hints in the bot's language
        from core import i18n
        st = {"sessions": {"w": self.sess(PUBLISH)}}
        for lang in ("en", "ru"):
            with self.subTest(lang=lang):
                copied = "\n".join([i18n.tr(lang, "core.owner.card_asks", name="w"), PUBLISH[0]["question"],
                                    i18n.tr(lang, "telegram.ask_multi_hint"), i18n.tr(lang, "telegram.ask_own"), "go"])
                self.assertEqual(owner.quoted_card(st, copied, 1)[2], "go")

    def test_the_hubs_own_cards_are_matched_only_for_the_hub(self):
        st = {"sessions": {"w": self.sess(PUBLISH), "hub": {"name": "hub", "kind": "hub", "asks": []}}}
        self.assertIsNone(owner.quoted_card(st, PUBLISH[0]["question"] + " yes", 1, only="hub"))
        self.assertEqual(owner.quoted_card(st, PUBLISH[0]["question"] + " yes", 1)[0], "w")


class CardCli(unittest.TestCase):
    def test_hub_send_for_with_card(self):
        import os
        import subprocess
        import sys
        import tempfile
        from core import journal, spool
        state = tempfile.mkdtemp(prefix="card-")
        journal.Journal(os.path.join(state, "journal.jsonl")).append("session.requested", "hub", {"cwd": "/h", "kind": "hub"})
        hub = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0", "HUB_SESSION": "hub"}
        r = subprocess.run([sys.executable, hub, "send", "w", "--for", "m3", "--card", "q5", "this is yes to the release"], env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        (ev,) = [e for _, e in spool.take(os.path.join(state, "spool"))]
        self.assertEqual({k: ev["data"].get(k) for k in ("to", "text", "for", "card")},
                         {"to": "w", "text": "this is yes to the release", "for": "m3", "card": "q5"})

    def test_hub_task_new_with_card_is_in_the_help(self):
        import os
        import subprocess
        import sys
        import tempfile
        import platforms
        from core import journal, spool
        state = tempfile.mkdtemp(prefix="card-")
        self.addCleanup(platforms.current.rmtree, state)
        journal.Journal(os.path.join(state, "journal.jsonl")).append("session.requested", "hub", {"cwd": "/h", "kind": "hub"})
        hub = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0", "HUB_SESSION": "hub", "PYTHONUTF8": "1"}
        r = subprocess.run([sys.executable, hub, "task", "new", "--help"], env=env, capture_output=True, text=True,
                           encoding="utf-8")
        self.assertIn("hub task new <session> --for mN [--card qN]", r.stdout)   # the flag works (below), the help names it
        r = subprocess.run([sys.executable, hub, "task", "new", "w", "--for", "m3", "--card", "q5", "do"], env=env,
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        (ev,) = [e for _, e in spool.take(os.path.join(state, "spool"))]
        self.assertEqual((ev["data"].get("for"), ev["data"].get("card")), ("m3", "q5"))


def load_tests(loader, standard, pattern):
    """Only our own: TelegramTest and Flow run in their own modules."""
    suite = unittest.TestSuite()
    suite.addTests(CardTextAnswer(n) for n in CardTextAnswer.__dict__ if n.startswith("test"))
    suite.addTests(loader.loadTestsFromTestCase(QuotedCard))
    suite.addTests(loader.loadTestsFromTestCase(CardCli))
    return suite


if __name__ == "__main__":
    unittest.main()
