"""Reminder timeouts by the owner's local time."""
import datetime
import unittest
from zoneinfo import ZoneInfo

from core import owner
from tests.fakes import tz

W = ZoneInfo("Europe/Berlin") if tz.HAVE else None


def ts(d, h, m=0):
    return datetime.datetime(2026, 9, d, h, m, tzinfo=W).timestamp()


def at(t):
    return datetime.datetime.fromtimestamp(t, W).strftime("%d %H:%M")


@tz.need
class DueTest(unittest.TestCase):
    def setUp(self):
        self.o = {**owner.initial(), "tz": "Europe/Berlin"}

    def test_day_schedule(self):
        q = ts(29, 10)
        self.assertEqual(at(owner.due(self.o, "q", q)), "29 10:30")
        self.o["reminders"]["q"] = 1
        self.assertEqual(at(owner.due(self.o, "q", q)), "29 13:00")
        self.o["reminders"]["q"] = 2
        self.assertEqual(at(owner.due(self.o, "q", q)), "30 09:00")
        self.o["reminders"]["q"] = 3
        self.assertIsNone(owner.due(self.o, "q", q))

    def test_quiet_hours_move_to_morning(self):
        self.assertEqual(at(owner.due(self.o, "q", ts(29, 22, 40))), "30 09:00")  # 23:10 is quiet hours
        self.assertEqual(at(owner.due(self.o, "q", ts(30, 1, 0))), "30 09:00")

    def test_away_silent_and_grace_after_back(self):
        self.o["state"] = "away"
        self.assertIsNone(owner.due(self.o, "q", ts(29, 10)))
        self.o.update(state="present", back_at=ts(29, 14))
        self.assertEqual(at(owner.due(self.o, "q", ts(29, 10))), "29 14:30")  # not earlier than 30 min after the return


if __name__ == "__main__":
    unittest.main()
