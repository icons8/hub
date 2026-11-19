"""Time zone database: on Windows without the tzdata package there is none, so tests with named zones are skipped there
(in that case the core takes the machine's local time, core/zones.py)."""
import unittest
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    ZoneInfo("Europe/Paris")
    HAVE = True
except ZoneInfoNotFoundError:
    HAVE = False

need = unittest.skipUnless(HAVE, "no time zone database (Windows without the tzdata package)")
