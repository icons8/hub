"""A repeated `hub notify --id X` edits the bot's earlier message instead of sending a new one (9 progress lines of
"3 of 9" were 9 messages); the removal (owner.notify_resolved) appends the result to the same message. The path of
the whole bot against a fake Bot API: a repeat, the removal, a failed edit, a restart, a late send."""
import tests  # noqa: F401 - ignore the machine's settings
import io
import time
import unittest
from unittest import mock

from core import owner
from modules.telegram import bot as tg
from tests import test_telegram as base


class NotifyEdit(base.TelegramTest):
    def notify(self, text, nid="build", ts=None, **extra):
        data = {"text": text, "title": "Build", "channel": "cli", "owner": True, **extra}
        if nid:
            data["id"] = nid
        return self.j.append("owner.notify", None, data, ts=ts)

    def resolve(self, nid="build", ts=None, **extra):
        return self.j.append("owner.notify_resolved", None, {"id": nid, "channel": "cli", "owner": True, **extra}, ts=ts)

    def posts(self):
        return self.fake.sent()

    def edits(self):
        return self.fake.sent("editMessageText")

    def test_a_repeat_with_the_same_id_edits_the_message(self):
        self.notify("progress 1 of 9")
        self.bot.tick()
        (first,) = self.posts()
        self.notify("progress 2 of 9")
        self.notify("progress 3 of 9")
        self.bot.tick()
        self.assertEqual(len(self.posts()), 1)                       # no new messages
        self.assertEqual([e["message_id"] for e in self.edits()], [self.fake.next_id] * 2)
        self.assertIn("progress 3 of 9", self.edits()[-1]["text"])
        self.assertIn("<b>Build</b>", self.edits()[-1]["text"])      # the heading stays
        self.assertEqual(first["parse_mode"], "HTML")
        self.assertEqual(self.edits()[-1]["parse_mode"], "HTML")

    def test_other_ids_other_senders_and_notices_without_an_id_are_separate_messages(self):
        self.notify("a", nid="one")
        self.notify("b", nid="two")
        self.notify("c", nid=None)
        self.notify("d", nid=None)
        self.notify("e", nid="one", channel="core", owner=False)      # the same id from another sender is another notice
        self.bot.tick()
        self.assertEqual(len(self.posts()), 5)
        self.assertEqual(self.edits(), [])

    def test_the_session_is_signed(self):
        self.notify("waiting for review", session="hub")
        self.notify("waiting for review 2", session="hub")
        self.bot.tick()
        self.assertIn("from session", self.posts()[0]["text"])
        self.assertIn("hub", self.posts()[0]["text"])
        self.assertIn("from session", self.edits()[0]["text"])

    def test_the_removal_appends_to_the_same_message_and_closes_it(self):
        self.notify("progress 1 of 9")
        self.bot.tick()
        mid = self.fake.next_id
        self.resolve()
        self.bot.tick()
        (edit,) = self.edits()
        self.assertEqual(edit["message_id"], mid)
        self.assertIn("progress 1 of 9", edit["text"])                # the text stays, the result is added
        self.assertIn("✅", edit["text"])
        self.assertEqual(len(self.posts()), 1)
        self.notify("progress 1 of 3")                               # a new occurrence of the same id - a new message
        self.bot.tick()
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(len(self.edits()), 1)

    def test_the_removal_with_a_text_is_the_result(self):
        self.notify("disk full")
        self.resolve(text="space freed: 40 GB")
        self.bot.tick()
        self.assertIn("space freed: 40 GB", self.edits()[-1]["text"])

    def test_the_removal_of_a_notice_the_bot_never_showed_says_nothing(self):
        self.resolve("never-shown")
        self.bot.tick()
        self.assertEqual(self.fake.calls, [])

    def test_the_removal_by_the_full_id_of_the_hub_edits_too(self):
        self.notify("disk full", nid="disk")
        self.bot.tick()
        key = owner.notice_key({"ts": 0, "src": "core"}, {"id": "disk", "channel": "cli", "owner": True})
        self.assertEqual(key, "n:owner:disk")
        self.resolve(key)
        self.bot.tick()
        self.assertEqual(len(self.edits()), 1)

    def test_a_pause_of_a_day_starts_a_new_message(self):
        now = time.time()
        self.notify("one", ts=now - 3 * 24 * 3600)
        self.notify("two", ts=now - 3 * 24 * 3600 + 60)               # inside the life of the notice - an edit
        self.notify("three", ts=now - 3600)                             # a day and more after the last - another occurrence
        self.bot.tick()
        self.assertEqual(len(self.posts()), 2)
        self.assertEqual(len(self.edits()), 1)

    def test_a_failed_edit_goes_as_a_new_message_and_the_next_repeat_edits_it(self):
        self.notify("one")
        self.bot.tick()
        self.fake.fail["editMessageText"] = [(400, "Bad Request: message to edit not found")]
        self.notify("two")
        self.bot.tick()
        self.assertEqual(len(self.posts()), 2)
        self.assertIn("two", self.posts()[-1]["text"])
        new = self.fake.next_id
        self.notify("three")
        self.bot.tick()
        self.assertEqual(self.edits()[-1]["message_id"], new)         # the notice is bound to the new message
        self.assertEqual(len(self.posts()), 2)

    def test_not_modified_is_already_done(self):
        self.notify("the same")
        self.bot.tick()
        self.fake.fail["editMessageText"] = [(400, "Bad Request: message is not modified: specified new message content")]
        self.notify("the same")
        self.bot.tick()
        self.assertEqual(len(self.posts()), 1)

    def test_a_network_failure_of_the_edit_repeats_the_edit_not_a_new_message(self):
        self.notify("one")
        self.bot.tick()
        self.fake.fail["editMessageText"] = ["drop"]
        self.notify("two")
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.bot.tick()
        self.assertEqual(self.edits(), [])
        self.bot.hold_until = 0
        self.bot.tick()
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(len(self.edits()), 1)
        self.assertIn("two", self.edits()[0]["text"])

    def test_an_escalation_to_urgent_rings_as_a_new_message(self):
        self.notify("disk 80%", nid="disk")
        self.notify("disk 99%", nid="disk", urgent=True)               # ⛔ after 🔔: an edit would not ring
        self.notify("disk 99.5%", nid="disk", urgent=True)             # urgent after urgent - the same alarm, an edit
        self.bot.tick()
        self.assertEqual(len(self.posts()), 2)
        self.assertTrue(self.posts()[1]["text"].startswith("⛔"))
        self.assertEqual([e["message_id"] for e in self.edits()], [self.fake.next_id])

    def test_a_network_failure_of_the_new_message_keeps_the_old_binding(self):
        self.notify("short")
        self.bot.tick()
        mid = self.fake.next_id
        self.fake.fail["sendMessage"] = ["drop"]
        self.notify("long " + "word " * 1000)
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.bot.tick()
        self.assertEqual(self.bot.st["notices"]["n:owner:build"]["mid"], mid)   # the event has not been done
        self.bot.hold_until = 0
        self.bot.tick()
        self.assertNotIn("n:owner:build", self.bot.st["notices"])               # the long one is not bound

    def test_a_text_longer_than_a_message_is_a_new_message(self):
        self.notify("short")
        self.notify("long " + "word " * 1000)
        self.bot.tick()
        self.assertEqual(self.edits(), [])
        self.assertGreaterEqual(len(self.posts()), 3)                 # the short one and the long one in parts
        # the long one is not bound: the next short repeat is a new message again, not an edit of a part
        before = len(self.posts())
        self.notify("short again")
        self.bot.tick()
        self.assertEqual(self.edits(), [])
        self.assertEqual(len(self.posts()), before + 1)

    def test_a_notice_with_a_file_is_a_new_message(self):
        import os
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "demo.mp4")
        with open(path, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypmp42" + b"x" * 100)
        self.notify("one")
        self.notify("two with a file", file=path)
        self.bot.tick()
        self.assertEqual(self.edits(), [])
        self.assertEqual(len(self.posts()), 2)

    def test_a_restart_of_the_bot_keeps_the_binding(self):
        self.notify("one")
        self.bot.tick()
        mid = self.fake.next_id
        self.bot = tg.Bot(self.state, tg.Api("1:x", base=self.fake.base))
        self.bot.catch_up()
        self.notify("two")
        self.bot.tick()
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.edits()[-1]["message_id"], mid)
        self.bot = tg.Bot(self.state, tg.Api("1:x", base=self.fake.base))
        self.bot.catch_up()
        self.resolve()
        self.bot.tick()
        self.assertEqual(len(self.edits()), 2)

    def test_a_restart_before_the_state_was_saved_does_not_double_the_message(self):
        # the first message went, the bot died before save(): after the restart the event repeats - the old state has no
        # binding, the notice comes as a message once more (the allowed case: no binding - a new message), not an error
        self.notify("one")
        self.bot.tick()
        self.assertEqual(len(self.posts()), 1)
        self.bot = tg.Bot(self.state, tg.Api("1:x", base=self.fake.base))
        self.bot.st["notices"].clear()
        self.bot.catch_up()
        self.notify("two")
        self.bot.tick()
        self.assertEqual(len(self.posts()), 2)

    def test_a_late_send_keeps_the_order_of_the_occurrences(self):
        # the network is down: the notice, its repeat, the removal and a new occurrence of the same id wait in a queue; each edit
        # goes to the message of its own occurrence (the address is taken from the binding at the moment of the event)
        self.fake.fail["sendMessage"] = ["drop"]
        self.notify("first")
        self.notify("first, update")
        self.resolve()
        self.notify("second")
        self.notify("second, update")
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.bot.tick()
        self.bot.hold_until = 0
        self.bot.tick()
        posts, edits = self.posts(), self.edits()
        self.assertEqual(len(posts), 2)
        first, second = self.fake.next_id - 1, self.fake.next_id
        self.assertEqual([e["message_id"] for e in edits], [first, first, second])
        self.assertIn("✅", edits[1]["text"])
        self.assertIn("second, update", edits[2]["text"])

    def test_the_bindings_do_not_grow_without_limit(self):
        for i in range(tg.KEEP_NOTICES + 20):
            self.notify("a", nid=f"id{i}")
        self.bot.tick()
        self.bot.save()
        self.assertLessEqual(len(self.bot.st["notices"]), tg.KEEP_NOTICES)
        self.assertLessEqual(len(self.bot.st["card_html"]), tg.KEEP_NOTICES)



def load_tests(loader, standard, pattern):
    """Only our own: TelegramTest's tests run in test_telegram (as the base here they ran once more)."""
    return unittest.TestSuite(NotifyEdit(n) for n in NotifyEdit.__dict__ if n.startswith("test"))


if __name__ == "__main__":
    unittest.main()
