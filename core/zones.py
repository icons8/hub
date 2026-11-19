"""Time zone by name, also where there is no zone database (Windows without the tzdata package, 2026-10-06:
`hub show` failed on ZoneInfo('UTC')). No database: UTC stays UTC, a named zone is the machine's local time.

The lookup is cached by name: a failed ZoneInfo() costs ~0.5 ms (it walks the whole tz search path) and the schedule and
the page views ask for the zone hundreds of times per snapshot. The machine's local offset is not cached: it moves with
daylight saving time while the hub keeps running."""
import datetime
import functools
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@functools.lru_cache(maxsize=256)
def _find(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def zone(name):
    found = _find(name or "UTC")
    if found is not None:
        return found
    if not name or name == "UTC":
        return datetime.timezone.utc
    return datetime.datetime.now().astimezone().tzinfo
