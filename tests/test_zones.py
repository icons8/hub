"""Time zones without the tzdata database (06.10, a Windows box: `hub show` crashed — Python on Windows knows no zones
without the tzdata package, ZoneInfo('UTC') raises). Without it: UTC is UTC, a named zone is the machine's local time,
not a crash."""
import tests  # noqa: F401 — keeps the machine's settings out
import datetime
import unittest
import zoneinfo
from unittest import mock

from core import zones


def has_tzdata():
    try:
        zoneinfo.ZoneInfo("Europe/Paris")
        return True
    except zoneinfo.ZoneInfoNotFoundError:
        return False


class Zones(unittest.TestCase):
    def setUp(self):
        zones._find.cache_clear()   # the lookup is cached by name; a mock must not see (or leave) other tests' results
        self.addCleanup(zones._find.cache_clear)

    @unittest.skipUnless(has_tzdata(), "no zone database (Windows without tzdata): a real zone will not appear")
    def test_known_zone_is_the_real_one(self):
        self.assertEqual(str(zones.zone("Europe/Paris")), "Europe/Paris")

    def test_without_tzdata_it_does_not_fall(self):
        with mock.patch.object(zones, "ZoneInfo", side_effect=zoneinfo.ZoneInfoNotFoundError("no tzdata")):
            self.assertIs(zones.zone("UTC"), datetime.timezone.utc)
            self.assertIs(zones.zone(None), datetime.timezone.utc)
            local = zones.zone("Europe/Paris")
            self.assertIsNotNone(datetime.datetime.fromtimestamp(0, local).utcoffset())   # local time, not an error

    def test_a_missing_zone_is_looked_up_once(self):
        # Hot path: the schedule asks for the zone hundreds of times per page snapshot; a failed ZoneInfo() walks the
        # whole tz search path (~0.5 ms), so "no such zone" (and "local") is remembered by name.
        missing = mock.Mock(side_effect=zoneinfo.ZoneInfoNotFoundError("no tzdata"))
        with mock.patch.object(zones, "ZoneInfo", missing):
            first = [zones.zone(n) for n in ("local", "Nowhere/Zone", "UTC", None)]
            second = [zones.zone(n) for n in ("local", "Nowhere/Zone", "UTC", None)]
        self.assertEqual(missing.call_count, 3)   # local, Nowhere/Zone, UTC (None is the same lookup as "UTC")
        self.assertEqual(first, second)
        self.assertIs(second[2], datetime.timezone.utc)
        self.assertIs(second[3], datetime.timezone.utc)

    def test_a_found_zone_is_looked_up_once(self):
        found = mock.Mock(return_value=datetime.timezone(datetime.timedelta(hours=3)))
        with mock.patch.object(zones, "ZoneInfo", found):
            a, b = zones.zone("Europe/Moscow"), zones.zone("Europe/Moscow")
        self.assertIs(a, b)
        self.assertEqual(found.call_count, 1)

    def test_local_offset_is_not_frozen(self):
        # A hub runs for weeks: the machine's offset follows daylight saving time, so the fallback is taken per call.
        winter = datetime.timezone(datetime.timedelta(hours=1))
        summer = datetime.timezone(datetime.timedelta(hours=2))
        fake_now = mock.Mock()
        fake_now.return_value.astimezone.return_value.tzinfo = winter
        with mock.patch.object(zones, "ZoneInfo", side_effect=zoneinfo.ZoneInfoNotFoundError("no tzdata")), \
                mock.patch.object(zones.datetime, "datetime", mock.Mock(now=fake_now)):
            self.assertIs(zones.zone("local"), winter)
            fake_now.return_value.astimezone.return_value.tzinfo = summer
            self.assertIs(zones.zone("local"), summer)

    def test_core_uses_it(self):
        import inspect
        from core import owner, reducer, schedule, usage, views
        for m in (owner, reducer, schedule, usage, views):
            self.assertNotIn("ZoneInfo(", inspect.getsource(m), m.__name__)


if __name__ == "__main__":
    unittest.main()
