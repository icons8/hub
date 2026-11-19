"""The line after coming back up (owner decision 2026-10-08): the hub was down (a crash, a reboot, a long restart) — the
bot writes one line «Hub is back online · was down from HH:MM to HH:MM · waiting: N messages». When it was down is a core
fact: `core.started` carries `down_since` (the last pulse of the previous hubd or its last event) and `spooled`
(messages in the inbox, not yet in the journal). A short break (up to 2 min: a quick restart) and a restart of the bot
alone — no line."""
import tests  # noqa: F401 — do not see the machine's settings
import unittest
from datetime import datetime, timezone

from modules.telegram import bot as tg
from tests import test_telegram as base


# The test clock is noon, not the machine's clock: «10 minutes ago» around midnight is already yesterday, and the line prints dates.
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc).timestamp()


class BackOnline(unittest.TestCase):
    setUp, tearDown = base.TelegramTest.setUp, base.TelegramTest.tearDown

    def queue(self, n):
        for i in range(n):
            self.j.append("message.received", None, {"text": f"task {i}", "channel": "web", "owner": True})

    def started(self, down_since, spooled=0):
        self.j.append("core.started", None, {"pid": 1, "down_since": down_since, "spooled": spooled}, ts=NOW)
        self.bot.tick()

    def lines(self):
        return [b["text"] for b in self.fake.sent() if "back online" in b["text"]]

    def test_a_long_outage_is_one_line_with_the_queue(self):
        self.queue(2)
        self.started(NOW - 25 * 60, spooled=1)
        self.assertEqual(self.lines(), ["Hub is back online · it was not there from 11:35 to 12:00 · waiting: 3 messages"])
        self.bot.tick()
        self.assertEqual(len(self.lines()), 1)   # once

    def test_an_empty_queue_is_not_mentioned(self):
        self.started(NOW - 10 * 60)
        self.assertEqual(self.lines(), ["Hub is back online · it was not there from 11:50 to 12:00"])

    def test_a_quick_restart_says_nothing(self):
        self.queue(1)
        self.started(NOW - 40)
        self.started(None)   # first core start: there is no past
        self.assertEqual(self.lines(), [])

    def test_a_restart_of_the_bot_alone_says_nothing(self):
        self.queue(1)
        self.bot.save()
        self.bot = tg.Bot(self.state, tg.Api("1:x", base=self.fake.base))
        self.bot.catch_up()
        self.bot.tick()
        self.assertEqual(self.lines(), [])

    def test_a_day_boundary_shows_dates(self):
        self.j.append("core.started", None, {"pid": 1, "down_since": NOW - 30 * 3600}, ts=NOW)
        self.bot.tick()
        self.assertEqual(self.lines(), ["Hub is back online · it was not there from 15.10 06:00 to 16.10 12:00"])

    def test_ten_minutes_over_midnight_show_dates(self):
        midnight = datetime(2026, 10, 17, 0, 3, tzinfo=timezone.utc).timestamp()
        self.j.append("core.started", None, {"pid": 1, "down_since": midnight - 10 * 60}, ts=midnight)
        self.bot.tick()
        self.assertEqual(self.lines(), ["Hub is back online · it was not there from 16.10 23:53 to 17.10 00:03"])
