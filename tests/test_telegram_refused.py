"""An event the core rejected does not go to Telegram as if it had happened: `hub notify` from a session (core: "a
session writes to the owner through the turn result") is not a "🔔 Monitor". Exceptions: a rejected reply to a card
(the card stays), a turn result and a file rejected by a core code failure (the reply matters more), and the owner's
own message, which gets a 👎 and the reason."""
import tests  # noqa: F401 — hide the machine's settings
import unittest

from core import board, coretext, i18n, journal
from tests import test_telegram as base

CRASH = "core failure on event turn.result: KeyError: 'x' (reducer.py:1)"


class Refused(unittest.TestCase):
    # helpers are copied, not inherited: a subclass of TelegramTest would run all its tests again
    # (and the module is imported as a module: the loader also runs a TestCase class found in the module namespace)
    T = base.TelegramTest
    setUp, tearDown, spooled, msg, ingest = T.setUp, T.tearDown, T.spooled, T.msg, T.ingest

    def refuse(self, ev, reason="rejected by the test"):
        """The hubd line for an event that its fold rejected."""
        self.j.append("core.rejected", ev.get("entity"), {"of": ev["seq"], "type": ev["type"], "reason": reason})

    def texts(self):
        return [b.get("text", "") for b in self.fake.sent()]

    def session_notify(self):
        # as `hub notify` from a session writes it (bin/hub put: channel, owner, sender on top)
        return self.j.append("owner.notify", None, {"text": "roll it out", "urgent": True, "channel": "session",
                                                    "owner": False, "sender": "maps"})

    def test_a_notice_the_core_refused_does_not_reach_the_chat(self):
        self.refuse(self.session_notify(), "a session writes to the owner through the turn result, not hub notify")
        self.bot.tick()
        self.assertEqual(self.texts(), [])

    def test_nor_before_the_core_has_written_its_refusal(self):
        # the last journal line: hubd writes core.rejected right after, and the bot may read in between
        self.session_notify()
        self.bot.tick()
        self.assertEqual(self.texts(), [])
        self.assertEqual(self.bot.outbox, [])

    def test_an_accepted_notice_still_goes(self):
        self.j.append("owner.notify", None, {"text": "disk full", "title": "monitor", "channel": "cli", "owner": True})
        self.bot.tick()
        self.assertTrue(any("disk full" in t for t in self.texts()), self.texts())

    def test_a_notice_refused_by_a_core_crash_stays_out_too(self):
        ev = self.j.append("owner.notify", None, {"text": "disk full", "channel": "cli", "owner": True})
        self.refuse(ev, CRASH)
        self.bot.tick()
        self.assertEqual(self.texts(), [])

    def test_an_answer_refused_by_a_core_crash_still_reaches_the_owner(self):
        # a turn result for its message, rejected by a core code failure (not by a rule), is still a reply to it
        self.fake.updates = [self.msg("how much space?", 5)]
        self.bot.poll(timeout=0)
        self.ingest()
        mid = [f"m{e['seq']}" for e in journal.read(self.j.path) if e["type"] == "message.received"][-1]
        self.j.append("turn.started", "hub", {"marker": mid})
        ev = self.j.append("turn.result", "hub", {"text": "Free 40 GB", "turns": 1})
        self.refuse(ev, CRASH)
        self.bot.tick()
        self.assertEqual(sum("Free 40 GB" in t for t in self.texts()), 1, self.texts())

    def test_a_crash_is_known_by_its_code_not_its_words(self):
        # reason code (core.rejected.code, core/forms.py): the reason text is translated, and the reply still arrives
        self.fake.updates = [self.msg("how much space?", 5)]
        self.bot.poll(timeout=0)
        self.ingest()
        mid = [f"m{e['seq']}" for e in journal.read(self.j.path) if e["type"] == "message.received"][-1]
        self.j.append("turn.started", "hub", {"marker": mid})
        ev = self.j.append("turn.result", "hub", {"text": "Free 40 GB", "turns": 1})
        self.j.append("core.rejected", "hub", {"of": ev["seq"], "type": ev["type"], "code": "core_failure",
                                               "reason": "failure on turn.result: KeyError: 'x' (reducer.py:1)"})
        self.bot.tick()
        self.assertEqual(sum("Free 40 GB" in t for t in self.texts()), 1, self.texts())

    def test_an_answer_refused_by_a_rule_stays_out(self):
        self.fake.updates = [self.msg("how much space?", 5)]
        self.bot.poll(timeout=0)
        self.ingest()
        mid = [f"m{e['seq']}" for e in journal.read(self.j.path) if e["type"] == "message.received"][-1]
        self.j.append("turn.started", "hub", {"marker": mid})
        self.refuse(self.j.append("turn.result", "hub", {"text": "Free 40 GB", "turns": 1}), "not that turn")
        self.bot.tick()
        self.assertFalse(any("Free" in t for t in self.texts()), self.texts())

    def test_his_own_refused_message_gets_a_not_delivered_receipt(self):
        self.fake.updates = [self.msg("make a report", 5)]
        self.bot.poll(timeout=0)
        (e,) = self.spooled()
        ev = self.j.append(e["type"], e.get("entity"), e.get("data"), src=e.get("src"))
        self.refuse(ev, "no such session")
        self.bot.tick()
        self.bot.tick()
        self.assertEqual([r["reaction"][0]["emoji"] for r in self.fake.sent("setMessageReaction")], ["👎"])
        (line,) = self.fake.sent()
        self.assertIn("Not delivered: no such session", line["text"])
        self.assertEqual(line["reply_parameters"]["message_id"], 5)

    def test_the_reason_is_in_the_owners_language(self):
        # C1: a reason made of the core's phrases (core/coretext.py) comes with its marks; the bot says it in his language
        self.fake.updates = [self.msg("make a report", 5)]
        self.bot.poll(timeout=0)
        (e,) = self.spooled()
        ev = self.j.append(e["type"], e.get("entity"), e.get("data"), src=e.get("src"))
        why = board.HANDED_HEAD.format(name="e")
        self.j.append("core.rejected", None, {"of": ev["seq"], "type": ev["type"], "reason": str(why),
                                              "l10n": coretext.marks(why)})
        i18n.use("ru")   # a Russian owner: the bot speaks the process's language
        try:
            self.bot.tick()
            self.bot.tick()
        finally:
            i18n.use("en")
        (line,) = self.fake.sent()
        self.assertIn(i18n.tr("ru", "core.msg.handed_head", name="e"), line["text"])
        self.assertNotIn(str(why), line["text"])


if __name__ == "__main__":
    unittest.main()
