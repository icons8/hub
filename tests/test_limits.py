"""Account limits (owner decision 2026-09-30: show limits like Claude Code does): claude sends rate_limit_event in
stream-json, the host turns it into an account.limits fact (only when the numbers changed), the core keeps the latest,
the page draws them."""
import tempfile
import unittest

from core import budget, reducer
from core.views import Reader
from hosts.streamjson import StreamHost


def event(five, week, status="allowed"):
    return {"type": "rate_limit_event", "rate_limit_info": {
        "status": status, "resetsAt": 1000, "rateLimitType": "five_hour", "isUsingOverage": False,
        "unifiedWindows": {"five_hour": {"utilization": five, "resetsAt": 1000},
                           "seven_day": {"utilization": week, "resetsAt": 9000}}}}


class HostLimits(unittest.TestCase):
    def test_fact_only_when_numbers_change(self):
        host = StreamHost(tempfile.mkdtemp(prefix="lim-"))
        for msg in (event(0.16, 0.52), event(0.16, 0.52), event(0.17, 0.52)):
            host.inbox.put(("a", msg))
        facts = host.poll()
        self.assertEqual([f[0] for f in facts], ["account.limits", "account.limits"])
        self.assertEqual(facts[0], ("account.limits", None, {
            "status": "allowed", "type": "five_hour", "overage": False,
            "windows": {"five_hour": {"used": 0.16, "resets": 1000}, "seven_day": {"used": 0.52, "resets": 9000}}}))
        self.assertEqual(facts[1][2]["windows"]["five_hour"]["used"], 0.17)


class CoreLimits(unittest.TestCase):
    def test_last_limits_kept_and_shown(self):
        st = reducer.empty()
        data = {"status": "allowed_warning", "type": "seven_day", "overage": False,
                "windows": {"seven_day": {"used": 0.91, "resets": 9000}}}
        st, acts, why = reducer.reduce(st, {"seq": 1, "ts": 50, "type": "account.limits", "entity": None, "data": data})
        self.assertIsNone(why)
        self.assertEqual(acts, [])
        self.assertEqual(st["limits"], {**data, "ts": 50})
        views = Reader()
        views.state = st
        self.assertEqual(views.snapshot(now=60)["limits"], {**data, "ts": 50})

    def test_old_snapshot_without_limits(self):
        st = reducer.empty()
        del st["limits"]
        views = Reader()
        views.state = st
        self.assertEqual(views.snapshot(now=1)["limits"], {})


FAR = 10 ** 10
LATER = FAR + 5000


def reading(five=None, week=None, five_at=FAR, week_at=LATER, status="allowed", **extra):
    windows = {}
    if five is not None:
        windows["five_hour"] = {"used": five, "resets": five_at}
    if week is not None:
        windows["seven_day"] = {"used": week, "resets": week_at}
    return {"status": status, "type": "five_hour", "overage": False, "windows": windows, **extra}


class Feed:
    """account.limits events one after another into one state."""

    def __init__(self, st=None):
        self.st, self.seq = st or reducer.empty(), 0

    def give(self, data, ts=1000):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts + self.seq, "type": "account.limits",
                                                   "entity": None, "data": data})
        assert why is None, why
        return self

    def shares(self, key=None):
        seen = self.st["account"]["seen"][key if key is not None else ""]
        return {k: w["used"] for k, w in seen["windows"].items() if "used" in w}


class HeldInsideTheWindow(unittest.TestCase):
    """Readings of one token come from the processes of many sessions, out of order: a share inside its own window never
    goes down, so the budget mode and the pool's busy mark at the threshold do not flip."""

    def test_a_share_holds_inside_one_window(self):
        feed = Feed()
        for five in (0.95, 0.94, 0.95, 0.94):
            feed.give(reading(five=five, week=0.5))
            self.assertEqual(feed.shares()["five_hour"], 0.95)
            self.assertEqual(feed.st["limits"]["windows"]["five_hour"]["used"], 0.95)

    def test_the_mode_does_not_flip_on_late_readings(self):
        feed, modes, mode = Feed(), [], "normal"
        for five in (0.93, 0.94, 0.95, 0.95, 0.94, 0.95, 0.96, 0.94, 0.97):
            feed.give(reading(five=five, week=0.5))
            mode = budget.decide({**feed.st, "budget": {"mode": mode}}, None, 0.95, 1000)["mode"]
            modes.append(mode)
        self.assertEqual(modes[2:], ["owner"] * 7)

    def test_a_new_reset_is_a_new_window(self):
        feed = Feed().give(reading(five=0.97, week=0.5))
        feed.give(reading(five=0.02, week=0.5, five_at=FAR + 18000))
        self.assertEqual(feed.shares()["five_hour"], 0.02)

    def test_an_early_reset_under_the_same_stamp_is_taken(self):
        # the provider resets a window early and keeps its deadline (core/usage.py: weekly 0.99 -> 0): not a late reading,
        # else the account stays busy and the mode stays owner until the old reset, up to a week
        feed = Feed().give(reading(five=0.4, week=0.99))
        feed.give(reading(five=0.4, week=0.0))
        self.assertEqual(feed.shares()["seven_day"], 0.0)
        mode = budget.decide({**feed.st, "budget": {"mode": "owner"}}, None, 0.95, 1000)["mode"]
        self.assertEqual(mode, "normal")
        feed = Feed().give(reading(five=0.95, week=0.5)).give(reading(five=0.9, week=0.5))   # exactly RESET_DROP
        self.assertEqual(feed.shares()["five_hour"], 0.9)

    def test_a_share_that_grows_is_taken(self):
        feed = Feed().give(reading(five=0.5, week=0.5)).give(reading(five=0.6, week=0.5))
        self.assertEqual(feed.shares()["five_hour"], 0.6)

    def test_a_window_the_reading_lacks_keeps_its_value(self):
        feed = Feed().give(reading(five=0.4, week=0.93)).give(reading(five=0.41))
        self.assertEqual(feed.shares(), {"five_hour": 0.41, "seven_day": 0.93})
        self.assertEqual(feed.st["limits"]["windows"]["seven_day"], {"used": 0.93, "resets": LATER})

    def test_a_window_past_its_reset_is_not_kept(self):
        feed = Feed().give(reading(five=0.9, week=0.5, five_at=1500), ts=1000)
        feed.give(reading(week=0.5), ts=2000)
        self.assertEqual(feed.shares(), {"seven_day": 0.5})

    def test_a_rejections_mark_is_not_kept(self):
        feed = Feed().give(reading(five=1, week=0.5, status="rejected"))
        feed.give(reading(five=0.9, week=0.5))
        self.assertEqual(feed.shares()["five_hour"], 0.9)

    def test_other_kinds_of_windows_are_untouched(self):
        feed = Feed().give(reading(five=0.4, week=0.5))
        feed.give({"status": "allowed", "type": "five_hour", "windows": {"five_hour": {"used": 0.4, "resets": FAR},
                                                                         "extra": {"resets": FAR}}})
        self.assertEqual(feed.st["account"]["seen"][""]["windows"]["extra"], {"resets": FAR})
        self.assertEqual(feed.shares().get("seven_day"), 0.5)


class HeldInThePool(unittest.TestCase):
    """With an account pool the share is held per token generation (gen); a fresh token starts again."""

    def pool(self):
        st = reducer.empty()
        st["account"] = {"active": "conduit", "refreshed": {"conduit": 100}, "seen": {}, "blocked": {}}
        return Feed(st)

    def test_the_same_token_holds(self):
        feed = self.pool()
        for week in (0.95, 0.94, 0.95, 0.94):
            feed.give(reading(five=0.5, week=week, account="conduit", gen=100))
            self.assertEqual(feed.shares("conduit")["seven_day"], 0.95)
        self.assertEqual(feed.st["limits"]["windows"]["seven_day"]["used"], 0.95)

    def test_a_fresh_token_starts_again(self):
        feed = self.pool().give(reading(five=0.5, week=0.95, account="conduit", gen=100))
        feed.st["account"]["refreshed"]["conduit"] = 200
        feed.st["account"]["seen"].pop("conduit")
        feed.give(reading(five=0.1, week=0.2, account="conduit", gen=200))
        self.assertEqual(feed.shares("conduit"), {"five_hour": 0.1, "seven_day": 0.2})

    def test_a_token_change_without_a_refresh_starts_again(self):
        feed = self.pool().give(reading(five=0.5, week=0.95, account="conduit", gen=100))
        feed.st["account"]["refreshed"]["conduit"] = 1001.5   # a changed token fingerprint (the reading before is older)
        feed.give(reading(five=0.1, week=0.93, account="conduit", gen=1001.5))
        self.assertEqual(feed.shares("conduit"), {"five_hour": 0.1, "seven_day": 0.93})

    def test_a_reading_of_the_old_token_is_still_dropped(self):
        feed = self.pool()
        feed.st["account"]["refreshed"]["conduit"] = 200
        feed.give(reading(five=0.9, week=0.9, account="conduit", gen=100))
        self.assertEqual(feed.st["account"]["seen"], {})

    def test_an_inactive_account_holds_too(self):
        feed = self.pool()
        for week in (0.9, 0.89):   # a late reading's jitter (a drop of usage.RESET_DROP is an early reset)
            feed.give(reading(week=week, account="login"))
        self.assertEqual(feed.shares("login"), {"seven_day": 0.9})
        self.assertEqual(feed.st["limits"], {})


if __name__ == "__main__":
    unittest.main()
