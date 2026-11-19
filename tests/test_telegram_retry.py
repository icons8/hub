"""Telegram does not lose outgoing messages on a network failure (log analysis of 2026-10-04: on 10-03 two messages to
the owner vanished on "Connection reset by peer" - the bot logged the error and moved on) and does not stay silent
when it cannot receive (getUpdates: the error was swallowed and the loop spun without a pause)."""
import tests  # noqa: F401 - ignore the machine's settings
import io
import os
import tempfile
import unittest
from unittest import mock

from core import journal
from modules.telegram import bot as tg
from modules.telegram import fmt
from tests import test_telegram as base
from tests.test_telegram import TelegramTest


class Retry(TelegramTest):
    def notify(self, text):
        self.j.append("owner.notify", None, {"text": text, "channel": "cli", "owner": True})

    def texts(self):
        return [b.get("text", "") for b in self.fake.sent()]

    def closed(self, mark):
        """Cards closed by an edit with the result mark (message ids), and how many results went as separate messages."""
        return ({b["message_id"] for b in self.fake.sent("editMessageText") if mark in b.get("text", "")},
                sum(mark in t for t in self.texts()))

    def test_a_network_failure_is_retried_and_sent_once(self):
        self.fake.fail["sendMessage"] = ["drop"]
        self.notify("first")
        self.notify("second")
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.bot.tick()
        self.assertFalse(any("second" in t for t in self.texts()))       # order: the second does not overtake the first
        self.bot.hold_until = 0                                          # the pause is over
        self.bot.tick()
        sent = self.texts()
        self.assertEqual(sum("first" in t for t in sent), 1)
        self.assertEqual(sum("second" in t for t in sent), 1)
        self.assertLess(sent.index(next(t for t in sent if "first" in t)), sent.index(next(t for t in sent if "second" in t)))

    def test_too_many_requests_waits_and_retries(self):
        self.fake.fail["sendMessage"] = [429, 502]
        self.notify("important")
        for _ in range(3):
            with mock.patch("sys.stderr", new=io.StringIO()):
                self.bot.tick()
            self.bot.hold_until = 0
        self.assertEqual(sum("important" in t for t in self.texts()), 1)

    def test_a_permanent_error_is_skipped_not_blocking(self):
        self.fake.fail["sendMessage"] = [400]
        self.notify("crooked")
        self.notify("normal")
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.bot.tick()
        self.assertIn("crooked" if False else "owner.notify", err.getvalue())
        self.assertEqual(sum("normal" in t for t in self.texts()), 1)

    def test_a_nul_in_a_text_is_sent(self):
        # code in fmt hides behind NUL-number-NUL marks: the text's own NUL was read as a mark - IndexError from tick(),
        # and after a restart the same event came first again
        self.assertEqual(fmt.to_messages("a \x007\x00 b `c`"), fmt.to_messages("a 7 b `c`"))
        self.notify("pre \x000\x00 post `code`")
        self.bot.tick()
        (sent,) = [t for t in self.texts() if "pre" in t]
        self.assertTrue(sent.endswith("pre 0 post <code>code</code>"), sent)

    def test_a_bug_on_one_event_skips_it_not_the_bot(self):
        # an error in the bot's own code on one event - log it and skip that event: escaping from tick(), it crashed the
        # bot, and after every restart the same event came first
        real = self.bot.outgoing

        def outgoing(ev):
            if "crooked" in str((ev.get("data") or {}).get("text")):
                raise IndexError("list index out of range")
            return real(ev)
        self.notify("crooked")
        self.notify("normal")
        with mock.patch.object(self.bot, "outgoing", side_effect=outgoing), \
                mock.patch("sys.stderr", new=io.StringIO()) as err:
            self.bot.tick()
        self.assertIn("IndexError", err.getvalue())
        self.assertEqual(sum("normal" in t for t in self.texts()), 1)
        self.assertFalse(self.bot.outbox)

    def flaky(self, fail_on, method="sendMessage"):
        """An api.call whose call number fail_on of the method (counted from the start of the patch) answers 429."""
        real, n, failing = self.bot.api.call, [0], method

        def call(method, **params):
            if method == failing:
                n[0] += 1
                if n[0] == fail_on:
                    raise tg.TgError(f"{method}: 429", transient=True)
            return real(method, **params)
        return mock.patch.object(self.bot.api, "call", side_effect=call)

    def restart(self):
        self.bot = tg.Bot(self.state, tg.Api("1:x", base=self.fake.base))
        self.bot.catch_up()

    def test_a_long_message_failing_mid_way_sends_each_part_once(self):
        # a long answer goes out as several messages; a 429 on the second repeated the whole event, and the first part
        # was sent again - also after a bot restart between attempts
        parts = [f"part{i} " + "x" * 3000 for i in range(3)]
        self.notify("\n\n".join(parts))
        with self.flaky(2), mock.patch("sys.stderr", new=io.StringIO()):
            self.bot.tick()
        self.assertEqual(len(self.bot.outbox), 1)                       # waiting for a retry
        self.restart()                                                  # restart before the retry
        self.bot.tick()
        for i in range(3):
            self.assertEqual(sum(f"part{i} " in t for t in self.texts()), 1, i)
        self.assertEqual(self.bot.outbox, [])
        self.assertEqual(self.bot.st["posted"], {})

    def test_a_card_failing_mid_way_is_posted_once_with_live_buttons(self):
        # two question cards: a 429 on the second repeats the event; the first one's buttons got a new random id on
        # every attempt, and the first card was sent a second time
        self.j.append("permission.requested", "hub", {"request_id": "r1", "tool": "AskUserQuestion", "interactive": True,
                                                      "input": {"questions": [
                                                          {"question": "First?", "options": [{"label": "Yes"}]},
                                                          {"question": "Second?", "options": [{"label": "No"}]}]}})
        with self.flaky(2), mock.patch("sys.stderr", new=io.StringIO()):
            self.bot.tick()
        self.restart()
        self.bot.tick()
        cards = [b for b in self.fake.sent() if "First?" in b.get("text", "")]
        self.assertEqual(len(cards), 1)
        self.assertEqual(sum("Second?" in t for t in self.texts()), 1)
        # the first card's button works: its id is in the bot's state
        data = cards[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.assertIn(data[2:], self.bot.st["keys"])
        (ask,) = self.bot.st["asks"].values()
        self.assertEqual(len(ask["msgs"]), 2)

    def shot(self, caption):
        path = os.path.join(tempfile.mkdtemp(dir=self.dir), "shot.png")
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
        self.j.append("owner.file", None, {"path": path, "caption": caption, "channel": "session", "owner": False,
                                           "sender": "maps"})

    def uploads(self):
        return [m for m, _ in self.fake.calls if m in ("sendPhoto", "sendDocument", "sendVideo")]

    def test_a_file_hit_by_a_network_failure_is_retried_not_lost(self):
        # a network failure on upload counted as a permanent error - the file was skipped for good; on any error the
        # photo went as a document (if the photo did arrive - a duplicate)
        self.fake.fail["sendPhoto"] = ["drop"]
        self.shot("first attempt")
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.bot.tick()
        self.assertEqual(self.uploads(), [])            # not a document instead of a photo
        self.assertEqual(len(self.bot.outbox), 1)       # waiting for a retry
        self.bot.hold_until = 0
        self.bot.tick()
        self.assertEqual(self.uploads(), ["sendPhoto"])

    def test_too_many_requests_on_an_upload_waits(self):
        self.fake.fail["sendDocument"] = [429]
        path = os.path.join(tempfile.mkdtemp(dir=self.dir), "notes.txt")
        with open(path, "w") as f:
            f.write("notes")
        self.j.append("owner.file", None, {"path": path, "caption": "notes", "channel": "session", "owner": False,
                                           "sender": "maps"})
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.bot.tick()
        self.assertEqual(len(self.bot.outbox), 1)
        self.bot.hold_until = 0
        self.bot.tick()
        self.assertEqual(self.uploads(), ["sendDocument"])

    def file_then_note(self):
        path = os.path.join(tempfile.mkdtemp(dir=self.dir), "demo.mp4")
        with open(path, "wb") as f:
            f.write(b"x" * 1000)
        self.j.append("owner.file", None, {"path": path, "caption": "demo", "channel": "session", "owner": False,
                                           "sender": "maps"})
        self.notify("next")
        return path

    def ticks(self, n):
        with mock.patch("sys.stderr", new=io.StringIO()):
            for _ in range(n):
                self.bot.tick()
                self.bot.hold_until = 0

    def test_an_upload_that_always_fails_does_not_hold_the_chat(self):
        # a big video over a slow channel times out every time: it would be retried forever, and everything after it
        # for the owner would wait behind it
        path = self.file_then_note()
        self.fake.fail["sendVideo"] = ["drop"] * 20
        self.ticks(8)
        self.assertEqual(20 - len(self.fake.fail["sendVideo"]), tg.UPLOAD_TRIES)
        texts = self.texts()
        self.assertTrue(any(path in t and "demo" in t for t in texts), texts)
        self.assertTrue(any("next" in t for t in texts), texts)
        self.assertEqual(self.bot.outbox, [])

    def test_a_file_waits_while_the_network_is_down(self):
        self.file_then_note()
        self.fake.fail["sendVideo"] = ["drop"] * 20
        self.fake.fail["getMe"] = ["drop"] * 20
        self.ticks(8)
        self.assertEqual([e["type"] for e in self.bot.outbox], ["owner.file", "owner.notify"])
        self.fake.fail.clear()
        self.ticks(2)
        self.assertEqual(len(self.fake.sent("sendVideo")), 1)
        self.assertTrue(any("next" in t for t in self.texts()))

    def test_a_first_line_that_failed_to_go_is_not_counted_as_sent(self):
        # the first line of a long turn was not sent (network failure) - a result equal to it counted as already sent,
        # and no answer came at all
        self.clocked()
        self.j.append("turn.started", "hub", {"marker": "m3"})
        self.j.append("session.said", "hub", {"text": "Done: all finished"})
        self.bot.tick()
        self.now += 50
        self.fake.fail["sendMessage"] = ["drop"]
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.bot.tick()
        self.assertEqual(self.texts(), [])
        self.j.append("turn.result", "hub", {"text": "Done: all finished"})
        self.bot.tick()
        self.assertEqual(self.texts(), ["Done: all finished"])

    def test_an_answer_to_him_hit_by_429_still_goes_as_the_answer(self):
        # a session turn's result for his message (a reply to it): a 429 on the answer - the retry no longer knew it
        # was an answer to him (the bot removed the turn mark before sending), and the answer was lost
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.bot.tick()
        self.bot.st["routes"]["40"] = {"session": "w"}
        self.fake.updates = [self.msg("how much space?", 5, reply_to=40)]
        self.bot.poll(timeout=0)
        self.ingest()
        mid = [f"m{e['seq']}" for e in journal.read(self.j.path) if e["type"] == "message.received"][-1]
        self.j.append("turn.started", "w", {"marker": mid})
        self.j.append("turn.result", "w", {"text": "Free 40 GB", "turns": 1})
        self.fake.fail["sendMessage"] = [429]
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.bot.tick()
            self.bot.hold_until = 0
            self.bot.tick()
        (line,) = [b for b in self.fake.sent() if "Free" in b["text"]]
        self.assertEqual(line["reply_parameters"]["message_id"], 5)

    def test_a_held_hub_answer_still_goes_after_the_hub_started_its_next_turn(self):
        # on his Telegram message the hub asked a session, it answered the hub - the hub's result goes back to him there
        # (the turn chain). That result waits for a retry (502), and the hub has already taken a message from the page:
        # the retry read the chain by the new state - "not from Telegram" - and the answer was lost
        self.fake.updates = [self.msg("ask w for the status", 5)]
        self.bot.poll(timeout=0)
        self.ingest()
        mid = [f"m{e['seq']}" for e in journal.read(self.j.path) if e["type"] == "message.received"][-1]
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        by = lambda who: {"channel": "session", "owner": False, "sender": who}  # noqa: E731
        self.j.append("turn.started", "hub", {"marker": mid})
        ask = self.j.append("message.received", None, {"to": "w", "text": "what status?", **by("hub")})
        self.j.append("turn.result", "hub", {"text": "—"})
        self.j.append("turn.stopped", "hub", {})
        self.j.append("turn.started", "w", {"marker": f"m{ask['seq']}"})
        back = self.j.append("message.received", None, {"to": "hub", "text": "status: ok", **by("w")})
        self.j.append("turn.result", "w", {"text": "replied to the hub"})
        self.j.append("turn.stopped", "w", {})
        self.j.append("turn.started", "hub", {"marker": f"m{back['seq']}"})
        self.j.append("turn.result", "hub", {"text": "w is all green"})
        self.j.append("turn.stopped", "hub", {})
        self.fake.fail["sendMessage"] = [502]
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.bot.tick()
            web = self.j.append("message.received", None, {"text": "and from the page", "channel": "web", "owner": True})
            self.j.append("turn.started", "hub", {"marker": f"m{web['seq']}"})
            self.bot.tick()                     # the pause is still on: the reader's state moved ahead
            self.bot.hold_until = 0
            self.bot.tick()
        (line,) = [b for b in self.fake.sent() if "green" in b["text"]]
        self.assertEqual(line["reply_parameters"]["message_id"], 5)

    def test_a_status_line_hit_by_the_network_is_not_counted_as_told(self):
        # "⚠️ ... limit" under his message: the "already said" mark was set before sending - the retry stayed silent
        LIMIT = base.TelegramTest.LIMIT
        self.fake.updates = [self.msg("hello", 5)]
        self.bot.poll(timeout=0)
        self.ingest()
        mid = [f"m{e['seq']}" for e in journal.read(self.j.path) if e["type"] == "message.received"][-1]
        self.j.append("turn.started", "hub", {"marker": mid})
        self.j.append("session.said", "hub", {"text": LIMIT})
        self.j.append("turn.result", "hub", {"text": LIMIT, "subtype": "success", "is_error": True, "turns": 1})
        self.fake.fail["sendMessage"] = ["drop"]
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.bot.tick()
            self.bot.hold_until = 0
            self.bot.tick()
        self.assertEqual(sum(t.startswith("⚠️") for t in self.texts()), 1, self.texts())

    def test_an_unasked_failure_message_hit_by_the_network_is_sent_once_and_its_edit_counted_once(self):
        self.j.append("turn.started", "hub", {"marker": None})
        self.failed("hub")
        self.fake.fail["sendMessage"] = ["drop"]
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.bot.tick()
            self.bot.hold_until = 0
            self.bot.tick()
        self.assertEqual(sum(t.startswith("⚠️") for t in self.texts()), 1, self.texts())
        self.assertEqual(self.bot.st["unasked"]["n"], 1)
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("turn.started", "w", {"marker": None})
        self.failed("w")
        self.fake.fail["editMessageText"] = ["drop"]   # the second failure is an edit; the network drops it once
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.bot.tick()
            self.bot.hold_until = 0
            self.bot.tick()
        self.assertEqual(sum(t.startswith("⚠️") for t in self.texts()), 1, self.texts())
        self.assertEqual(self.bot.st["unasked"]["n"], 2)                     # the repeat of the event is not a third failure
        self.assertIn("failed: 2.", [b["text"] for b in self.fake.sent("editMessageText")][-1])

    # --- the bot's own work outside events (closing receipts, withdrawn questions, "not delivered") -
    # also without losses and duplicates
    def perm(self, rid="r1"):
        self.j.append("permission.requested", "hub", {"request_id": rid, "tool": "Bash", "interactive": True,
                                                      "input": {"command": "ls"}})

    def test_the_answered_note_lost_to_the_network_is_retried_once(self):
        # "✅ Answer: ..." under a card: a network failure on it was swallowed - the receipt was lost
        self.perm()
        self.bot.tick()
        self.j.append("permission.answered", "hub", {"request_id": "r1", "behavior": "allow", "channel": "web",
                                                     "owner": True})
        self.fake.fail["editMessageText"] = ["drop"]   # the result is an edit of the card itself
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.bot.tick()
            self.bot.hold_until = 0
            self.bot.tick()
        cards, lines = self.closed("✅ Answer")
        self.assertEqual((len(cards), lines), (1, 0), self.texts())
        self.assertEqual(self.bot.outbox, [])

    def test_a_withdrawn_card_of_a_held_event_is_withdrawn_once(self):
        # the event with a card waits for a retry while the question is already closed in the core: the withdrawal went
        # over a half-sent card, the event retry recreated it - and "⌛ Question withdrawn" came twice
        self.j.append("permission.requested", "hub", {"request_id": "r1", "tool": "AskUserQuestion", "interactive": True,
                                                      "input": {"questions": [
                                                          {"question": "First?", "options": [{"label": "Yes"}]},
                                                          {"question": "Second?", "options": [{"label": "No"}]}]}})
        with self.flaky(2), mock.patch("sys.stderr", new=io.StringIO()), \
                mock.patch.object(self.bot, "open_in_core", return_value=False):
            self.bot.tick()
            self.bot.hold_until = 0
            self.bot.tick()
            self.bot.tick()
        self.assertEqual(self.closed("⌛"), ({*self.bot.st["asks"][next(iter(self.bot.st["asks"]))]["msgs"]}, 0))
        self.assertEqual(len(self.closed("⌛")[0]), 2)                     # a result in each of the two cards
        self.assertEqual(sum("First?" in t for t in self.texts()), 1)

    def test_a_withdrawal_hit_by_the_network_is_finished_next_round(self):
        self.j.append("permission.requested", "hub", {"request_id": "r1", "tool": "AskUserQuestion", "interactive": True,
                                                      "input": {"questions": [
                                                          {"question": "First?", "options": [{"label": "Yes"}]},
                                                          {"question": "Second?", "options": [{"label": "No"}]}]}})
        self.bot.tick()
        with self.flaky(2, "editMessageText"), mock.patch("sys.stderr", new=io.StringIO()), \
                mock.patch.object(self.bot, "open_in_core", return_value=False):
            self.bot.tick()     # the first card is closed, a network failure on the second
            self.bot.tick()     # the next pass withdraws the second
        cards, lines = self.closed("⌛")
        self.assertEqual((len(cards), lines), (2, 0), self.texts())
        self.assertEqual(len(self.fake.sent("editMessageText")), 2)       # the first was not edited again

    def test_an_answer_hit_by_the_network_on_the_second_question_is_not_sent_as_lines(self):
        # an answer to a two-question form: the first is closed by an edit, the second hits a network failure, the event
        # repeats whole; the first is edited with the same text again (harmless), and neither gets a separate "✅ Answer" line
        self.j.append("permission.requested", "hub", {"request_id": "r1", "tool": "AskUserQuestion", "interactive": True,
                                                      "input": {"questions": [
                                                          {"question": "First?", "options": [{"label": "Yes"}]},
                                                          {"question": "Second?", "options": [{"label": "No"}]}]}})
        self.bot.tick()
        self.j.append("permission.answered", "hub", {"request_id": "r1", "behavior": "allow", "channel": "web",
                                                     "owner": True, "answers": {"First?": "Yes", "Second?": "No"}})
        with self.flaky(2, "editMessageText"), mock.patch("sys.stderr", new=io.StringIO()):
            self.bot.tick()
            self.bot.hold_until = 0
            self.bot.tick()
        cards, lines = self.closed("✅ Answer")
        self.assertEqual((len(cards), lines), (2, 0), self.texts())
        self.assertEqual(self.bot.outbox, [])

    def test_a_not_delivered_line_hit_by_the_network_does_not_crash_the_bot(self):
        # watch_failed sent without protection: a network failure escaped from tick() and crashed the bot
        self.j.append("turn.started", "hub", {"marker": None, "prompt_head": "busy"})
        self.fake.updates = [self.msg("extra", 5)]
        self.bot.poll(timeout=0)
        self.ingest()
        self.bot.tick()
        mid = next(f"m{e['seq']}" for e in journal.read(self.j.path) if e["type"] == "message.received")
        self.j.append("message.cancel", mid, {"channel": "web", "owner": True})
        self.fake.fail["sendMessage"] = ["drop"]
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.bot.tick()
            self.bot.tick()
        self.assertEqual(sum("Removed from the queue" in t for t in self.texts()), 1, self.texts())

    def test_a_receive_failure_is_logged_and_backs_off(self):
        self.fake.fail["getUpdates"] = [502]
        with mock.patch("sys.stderr", new=io.StringIO()) as err, mock.patch("time.sleep") as nap:
            self.assertEqual(self.bot.poll(timeout=0), 0)
        self.assertIn("getUpdates", err.getvalue())
        self.assertTrue(nap.called)



def load_tests(loader, standard, pattern):
    """Only our own: TelegramTest's tests run in test_telegram (as an import and the base here they ran twice more)."""
    return unittest.TestSuite(Retry(n) for n in Retry.__dict__ if n.startswith("test"))


if __name__ == "__main__":
    unittest.main()
