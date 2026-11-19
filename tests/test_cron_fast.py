"""cron_next without minute-by-minute iteration (2026-10-07, hub speed analysis, owner decision: targeted fixes): the page
snapshot computed a schedule's "next run" by brute force — 109k steps for 63 jobs, 80% of the snapshot time. The answer
must be the same as brute force gives: checked against a minute-by-minute reference."""
import tests  # noqa: F401 — do not see the machine's settings
import datetime
import time
import unittest

from core import schedule

EXPRS = ["0 11 * * 1-5", "0 10,17 * * 1-5", "0 */3 * * *", "17 7 * * 1", "*/15 * * * *", "0 0 1 * *",
         "30 9 * * 0", "0 9 13 * 5", "* * * * *"]
ZONES = ["UTC", "Europe/Paris", "America/New_York"]


def slow(expr, after, tz, until=None):
    match = schedule.matcher(expr)
    end = until if until is not None else after + 366 * 86400
    if schedule.every_hour(match):   # every real minute: a repeated hour runs twice
        at = after // 60 * 60 + 60
        while at <= end:
            if match(schedule._local(at, tz)):
                return at
            at += 60
        return None
    dt = schedule._local(after, tz).replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)
    while dt.timestamp() <= end:
        if match(dt) and dt.timestamp() > after:   # a wall-clock minute counts at its first moment: never in the past
            return dt.timestamp()
        dt += datetime.timedelta(minutes=1)
    return None


class CronFast(unittest.TestCase):
    def test_same_answer_as_minute_by_minute(self):
        starts = [1_790_000_000, 1_801_234_567, 1_774_746_000, 1_792_890_000]   # including the nights of the DST changes
        for tz in ZONES:
            for expr in EXPRS:
                for after in starts:
                    self.assertEqual(schedule.cron_next(expr, after, tz), slow(expr, after, tz), (expr, tz, after))

    def test_rare_dates(self):
        after = datetime.datetime(2026, 10, 7, tzinfo=datetime.timezone.utc).timestamp()
        for expr, want in (("0 12 29 2 *", None),                                   # 29 February — more than a year away
                           ("59 23 31 12 *", datetime.datetime(2026, 12, 31, 23, 59)),
                           ("5 4 * 1,7 *", datetime.datetime(2027, 1, 1, 4, 5))):
            got = schedule.cron_next(expr, after, "UTC")
            self.assertEqual(got, want and want.replace(tzinfo=datetime.timezone.utc).timestamp(), expr)

    def test_until_is_kept(self):
        after = 1_790_000_000
        self.assertIsNone(schedule.cron_next("0 12 29 2 *", after, "UTC", until=after + 30 * 86400))

    def test_fast(self):
        started = time.perf_counter()
        for _ in range(60):
            schedule.cron_next("0 12 29 2 *", 1_790_000_000, "Europe/Paris")   # rare: once in 4 years
            schedule.cron_next("0 11 * * 1-5", 1_790_000_000, "Europe/Paris")
        self.assertLess(time.perf_counter() - started, 0.5)


if __name__ == "__main__":
    unittest.main()
