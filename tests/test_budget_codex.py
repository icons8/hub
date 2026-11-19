"""Codex (experimental): the limit economy (core/budget) on a hub whose limits come from Codex. The facts are
the ones hosts/codex_limits writes from app-server's RateLimitSnapshot: windows by duration, used as a share, the
"codex" engine, and on a reached limit rateLimitReachedType (not a window name, unlike Claude's limit type)."""
import tests  # noqa: F401 — keep the machine's settings out
import os
import tempfile
import unittest

import platforms

from core import budget, journal, usage
from hosts import codex_limits

NOW = 1_791_160_000
H = 3600


def snap(primary=None, secondary=None, reached=None, mins=(300, 10080)):
    """A RateLimitSnapshot as app-server sends it: (usedPercent, resets in seconds from now) per window."""
    out = {"limitId": "codex", "planType": "plus", "rateLimitReachedType": reached}
    for key, w, m in (("primary", primary, mins[0]), ("secondary", secondary, mins[1])):
        if w is not None:
            out[key] = {"usedPercent": w[0], "windowDurationMins": m, "resetsAt": NOW + w[1]}
    return out


def limits(s, allowed=None):
    data = codex_limits.limits_fact(s, allowed)
    return {k: v for k, v in data.items() if not k.startswith("_")}   # the public fact (codex_limits._emit)


def st(lim):
    return {"limits": {**lim, "ts": NOW}, "account": {}}


class Economy(unittest.TestCase):
    def test_a_codex_window_moves_the_mode_like_a_claude_one(self):
        self.assertEqual(budget.decide(st(limits(snap((87, H), (20, 3 * 24 * H)))), None, 0.95, NOW)["mode"], "economy")
        self.assertEqual(budget.decide(st(limits(snap((20, H), (96, 3 * 24 * H)))), None, 0.95, NOW)["mode"], "owner")
        self.assertEqual(budget.decide(st(limits(snap((10, H), (20, 3 * 24 * H)))), None, 0.95, NOW)["mode"], "normal")

    def test_a_reached_codex_limit_is_full_before_its_reset(self):
        # Codex says rate_limit_reached, not which window: as the core's continuation picks it (reducer._codex_limit) —
        # no used-up window, so the nearest reset; full until then, and the mode says when it frees
        lim = limits(snap((60, H), (40, 3 * 24 * H), reached="rate_limit_reached"))
        out = budget.decide(st(lim), None, 0.95, NOW)
        self.assertEqual((out["mode"], out["window"], out["used"], out["until"]), ("owner", "five_hour", 1, NOW + H))
        self.assertEqual(budget.decide(st(lim), None, 0.95, NOW + 2 * H)["mode"], "normal")   # the window has reset

    def test_the_used_up_window_holds_the_rejection(self):
        lim = limits(snap((80, H), (100, 3 * 24 * H), reached="rate_limit_reached"))
        out = budget.decide(st(lim), None, 0.95, NOW + 2 * H)
        self.assertEqual((out["mode"], out["window"], out["until"]), ("owner", "seven_day", NOW + 3 * 24 * H))

    def test_a_window_past_at_the_reading_is_not_the_rejected_one(self):
        # a stale 5 h window (reset before the reading) used up: the rejection is the week's, still ahead
        lim = limits(snap((100, -H), (90, 3 * 24 * H), reached="rate_limit_reached"))
        out = budget.decide(st(lim), None, 0.95, NOW)
        self.assertEqual((out["mode"], out["window"]), ("owner", "seven_day"))

    def test_ordinary_usage_refused_is_a_reached_limit(self):
        # account/rateLimits/read: ordinaryUsageAllowed false — rejected with no reached type
        lim = limits(snap((30, H), (50, 3 * 24 * H)), allowed=False)
        self.assertEqual(lim["status"], "rejected")
        out = budget.decide(st(lim), None, 0.95, NOW)
        self.assertEqual((out["mode"], out["window"], out["until"]), ("owner", "five_hour", NOW + H))

    def test_a_codex_window_of_another_length_counts(self):
        lim = limits(snap((90, H), (5, 3 * 24 * H), mins=(60, 10080)))
        self.assertIn("codex_60m", lim["windows"])
        out = budget.decide(st(lim), None, 0.95, NOW)
        self.assertEqual((out["mode"], out["window"]), ("economy", "codex_60m"))
        # a window of no length is not a window: not on the "Usage" page, not in the economy either
        lim = limits(snap((99, H), (5, 3 * 24 * H), mins=(0, 10080)))
        self.assertEqual(budget.decide(st(lim), None, 0.95, NOW)["mode"], "normal")

    def test_a_reading_without_its_time_is_taken_as_of_now(self):
        lim = limits(snap((100, -H), (90, 3 * 24 * H), reached="rate_limit_reached"))
        out = budget.decide({"limits": lim, "account": {}}, None, 0.95, NOW)
        self.assertEqual((out["mode"], out["window"]), ("owner", "seven_day"))

    def test_only_codex_windows_of_a_length_count(self):
        lim = limits(snap((10, H), (5, 3 * 24 * H)))
        lim["windows"]["seven_day_opus"] = {"used": 0.99, "resets": NOW + H}   # a Claude name is not a Codex window
        self.assertEqual(budget.decide(st(lim), None, 0.95, NOW)["mode"], "normal")

    def test_claude_limits_are_as_before(self):
        # Claude's type names its window; an unknown window of Claude does not count, as before Codex
        claude = {"status": "rejected", "type": "seven_day", "windows": {
            "five_hour": {"used": 0.2, "resets": NOW + H}, "seven_day": {"used": 0.3, "resets": NOW + 3 * 24 * H},
            "codex_60m": {"used": 0.99, "resets": NOW + H}}}
        out = budget.decide(st(claude), None, 0.95, NOW)
        self.assertEqual((out["mode"], out["window"]), ("owner", "seven_day"))
        claude = {"status": "rejected", "type": "something_else", "windows": {
            "five_hour": {"used": 0.2, "resets": NOW + H}, "codex_60m": {"used": 0.99, "resets": NOW + H}}}
        self.assertEqual(budget.decide(st(claude), None, 0.95, NOW)["mode"], "normal")


class Forecast(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="budget-codex-")
        self.addCleanup(platforms.current.rmtree, self.dir)
        usage.reset()
        self.seq = 0
        self.lines = []

    def ev(self, ts, data):
        self.seq += 1
        self.lines.append({"seq": self.seq, "ts": ts, "src": "host", "type": "account.limits", "entity": None,
                           "data": data})

    def test_the_pace_of_codex_windows(self):
        # the economy card's forecast reads the Codex series of a hub on Codex
        for i, used in enumerate((10, 20, 30)):
            self.ev(NOW - (2 - i) * H, limits(snap((used, 3 * H), (5, 3 * 24 * H))))
        with open(os.path.join(self.dir, "journal.jsonl"), "w") as f:
            f.write("".join(journal._line(x) for x in self.lines))
        eta = budget.forecast(self.dir, {}, None, "five_hour", NOW, engine="codex")
        self.assertAlmostEqual(eta, NOW + 7 * H, delta=60)   # 10% an hour from 30%


if __name__ == "__main__":
    unittest.main()
