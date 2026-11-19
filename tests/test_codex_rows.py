"""Codex (experimental): limits of Claude and of Codex are two rows, not one. Both hosts write
account.limits with no account name, told apart only by the engine field; a hub with codex/… sessions next to claude
ones (a mixed hub) used to keep whichever came last in st["limits"] and in the default login's row (account.seen[""]):
the limits line jumped between the two, and the economy (core/budget) judged by the latest. Now the Codex row is its
own (st["codex"]["limits"]); st["limits"] is the hub's engine — Codex only on a hub entirely on Codex (the daemon's
mark codex.host whole). A turn the Codex limit cut may name a later reset than the reading (the week): the economy
holds until that reset too."""
import tests  # noqa: F401 — keep the machine's settings out
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

import platforms

from core import budget, coretext, i18n, reducer

NOW = 1_791_160_000
H = 3600
WEEK = 7 * 24 * H


def claude_limits(five, week, status="allowed", typ=None):
    return {"status": status, "type": typ, "overage": False,
            "windows": {"five_hour": {"used": five, "resets": NOW + H}, "seven_day": {"used": week, "resets": NOW + WEEK}}}


def codex_limits(five, week, status="allowed", five_at=H, week_at=3 * 24 * H, other=None):
    windows = {"five_hour": {"used": five, "resets": NOW + five_at}, "seven_day": {"used": week, "resets": NOW + week_at}}
    windows.update(other or {})
    return {"status": status, "type": None, "overage": False, "windows": windows, "engine": "codex", "plan": "plus"}


class Fold(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        owner = {"channel": "cli", "owner": True}
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **owner)
        self.ev("session.requested", "maps", cwd="/m", session_id="s-m", **owner)
        for n in ("hub", "maps"):
            self.ev("session.started", n, session_id=f"s-{n[0]}", source="startup")

    def ev(self, t, e=None, ts=None, **d):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or NOW + self.seq, "type": t,
                                                   "entity": e, "data": d})
        return why

    def mixed(self):
        self.ev("codex.host", None, on=True, channel="core", owner=False)   # HUB_CODEX=1: claude and codex/… sessions

    def whole(self):
        self.ev("codex.host", None, on=True, whole=True, channel="core", owner=False)   # "host": "codex"

    def test_a_mixed_hub_keeps_the_claude_row_for_the_hub(self):
        self.mixed()
        self.ev("account.limits", None, **claude_limits(0.3, 0.5))
        self.ev("account.limits", None, **codex_limits(0.9, 0.95))
        self.assertEqual(self.st["limits"]["windows"]["five_hour"]["used"], 0.3)   # the limits line: Claude's
        self.assertNotIn("engine", self.st["limits"])
        self.assertEqual(self.st["account"]["seen"][""]["windows"]["five_hour"]["used"], 0.3)   # the default login's
        self.assertEqual(self.st["codex"]["limits"]["windows"]["five_hour"]["used"], 0.9)        # Codex's own row
        self.assertEqual(budget.decide(self.st, None, 0.95, NOW + 100)["mode"], "normal")   # by Claude, not by Codex

    def test_a_codex_reading_before_any_claude_one_is_not_the_hub_row(self):
        self.mixed()
        self.ev("account.limits", None, **codex_limits(0.9, 0.95))
        self.assertEqual(self.st["limits"], {})
        self.assertEqual((self.st.get("account") or {}).get("seen") or {}, {})

    def test_a_hub_on_codex_shows_and_saves_by_codex(self):
        self.whole()
        self.ev("account.limits", None, **codex_limits(0.9, 0.5))
        self.assertEqual(self.st["limits"]["engine"], "codex")
        self.assertEqual(self.st["limits"]["windows"]["five_hour"]["used"], 0.9)
        self.assertEqual(budget.decide(self.st, None, 0.95, NOW + 100)["mode"], "economy")

    def test_switching_the_hub_engine_switches_the_row_at_once(self):
        self.mixed()
        self.ev("account.limits", None, **claude_limits(0.3, 0.5))
        self.ev("account.limits", None, **codex_limits(0.9, 0.95))
        self.whole()                                   # the hub moved onto Codex: its line is Codex's from now
        self.assertEqual(self.st["limits"]["engine"], "codex")
        self.ev("account.limits", None, **claude_limits(0.1, 0.1))   # a Claude reading left over: not the hub's row
        self.assertEqual(self.st["limits"]["engine"], "codex")
        self.mixed()                                   # back to claude next to codex: Claude's last known numbers
        self.assertNotIn("engine", self.st["limits"])
        self.assertEqual(self.st["limits"]["windows"]["five_hour"]["used"], 0.1)
        self.ev("codex.host", None, on=False, channel="core", owner=False)
        self.assertNotIn("engine", self.st["limits"])

    def cut(self, **extra):
        self.ev("message.received", None, to="maps", text="make a report", channel="web", owner=True)
        mid = list(self.st["messages"])[-1]
        self.ev("turn.started", "maps", marker=mid)
        self.ev("turn.result", "maps", text="Codex: usage limit reached (usageLimitExceeded)", is_error=True,
                engine="codex", limit=True, **extra)

    def test_a_codex_cut_on_a_mixed_hub_takes_no_reset_from_claude(self):
        self.mixed()
        self.ev("account.limits", None, **claude_limits(1.0, 0.5, status="rejected", typ="five_hour"))
        self.cut()   # the turn named no reset and there is no Codex reading: no continuation by the clock
        self.assertIsNone(self.st["sessions"]["maps"]["limit_cut"].get("resets"))

    def test_a_codex_cut_reads_the_codex_row(self):
        self.mixed()
        self.ev("account.limits", None, **claude_limits(0.1, 0.1))
        self.ev("account.limits", None, **codex_limits(1.0, 0.4, status="rejected"))
        self.cut()
        self.assertEqual(self.st["sessions"]["maps"]["limit_cut"]["resets"], NOW + H)

    def test_a_claude_cut_on_a_mixed_hub_is_as_on_main(self):
        self.mixed()
        self.ev("account.limits", None, **claude_limits(1.0, 0.5, status="rejected", typ="five_hour"))
        self.ev("account.limits", None, **codex_limits(0.1, 0.1))
        self.ev("message.received", None, to="maps", text="x", channel="web", owner=True)
        self.ev("turn.result", "maps", text="You've hit your limit · resets 5pm", is_error=True)
        self.assertEqual(self.st["sessions"]["maps"]["limit_cut"]["resets"], NOW + H)
        self.assertNotIn("engine", self.st["sessions"]["maps"]["limit_cut"])

    def test_the_economy_holds_until_the_reset_the_cut_turn_named(self):
        # the reading: refused, no window used up — the nearest reset is the 5 h one; the turn the limit cut said the week
        self.whole()
        self.ev("account.limits", None, **codex_limits(0.6, 0.9, status="rejected"))
        week = NOW + 3 * 24 * H
        self.cut(limit_resets=week)
        self.assertEqual(self.st["sessions"]["maps"]["limit_cut"]["resets"], week)
        later = NOW + 2 * H   # past the 5 h reset, the week still ahead
        out = budget.decide(self.st, None, 0.95, later)
        self.assertEqual((out["mode"], out["until"]), ("owner", week))
        # a fresh reading still refused keeps it (its own nearest reset passes at 3.5 h: only the held week keeps it
        # full at 4 h); so does one that says "allowed": hosts/codex_limits writes that on a sparse update once the 5 h
        # window renews, while the week still refuses
        self.ev("account.limits", None, ts=NOW + 3 * H, **codex_limits(0.0, 0.5, status="rejected", five_at=3 * H + 1800))
        self.assertEqual(budget.decide(self.st, None, 0.95, NOW + 4 * H)["mode"], "owner")
        self.ev("account.limits", None, ts=NOW + 5 * H, **codex_limits(0.0, 0.5, five_at=6 * H))
        self.assertEqual(budget.decide(self.st, None, 0.95, NOW + 5 * H)["mode"], "owner")
        # a Codex turn that went through: Codex lets in again — the held reset goes
        self.ev("turn.result", "maps", ts=NOW + 6 * H, text="done", is_error=False, engine="codex", turns=1)
        self.assertNotIn("held", self.st["codex"]["limits"])
        self.assertNotIn("held", self.st["limits"])
        self.assertEqual(budget.decide(self.st, None, 0.95, NOW + 6 * H)["mode"], "normal")

    def test_a_mixed_hub_from_before_this_gets_the_codex_reading_out_of_its_row(self):
        self.st["limits"] = {**codex_limits(1.0, 0.5, status="rejected"), "ts": NOW}
        self.st["codex"] = {"host": True}
        reducer.upgrade(self.st)
        self.assertEqual(self.st["limits"], {})
        self.assertEqual(self.st["codex"]["limits"]["windows"]["five_hour"]["used"], 1.0)
        self.assertEqual(reducer.upgrade(self.st)["codex"]["limits"]["windows"]["five_hour"]["used"], 1.0)   # once

    def test_a_turn_begun_before_the_cut_does_not_let_in(self):
        self.whole()
        self.ev("account.limits", None, **codex_limits(0.6, 0.9, status="rejected"))
        self.cut(limit_resets=NOW + 3 * 24 * H)
        cut_at = self.st["codex"]["limits"]["held_at"]
        # another session's turn, let in a minute before the cut, ends a second after it
        self.ev("turn.result", "hub", ts=cut_at + 1, text="done", is_error=False, engine="codex", turns=1,
                duration_ms=61_000)
        self.assertIn("held", self.st["codex"]["limits"])
        self.ev("turn.result", "hub", ts=cut_at + 120, text="done", is_error=False, engine="codex", turns=1,
                duration_ms=60_000)
        self.assertNotIn("held", self.st["codex"]["limits"])

    def test_a_read_that_says_codex_lets_in_drops_the_held_reset(self):
        from hosts import codex_limits as cl
        self.whole()
        self.ev("account.limits", None, **codex_limits(0.6, 0.9, status="rejected"))
        self.cut(limit_resets=NOW + 3 * 24 * H)
        snap = {"limitId": "codex", "primary": {"usedPercent": 5, "windowDurationMins": 300, "resetsAt": NOW + 6 * H},
                "secondary": {"usedPercent": 20, "windowDurationMins": 10080, "resetsAt": NOW + 6 * 24 * H}}
        sparse = {k: v for k, v in cl.limits_fact(snap).items() if not k.startswith("_")}
        self.assertNotIn("usage_allowed", sparse)   # a sparse update never says it
        self.ev("account.limits", None, ts=NOW + 5 * H, **sparse)
        self.assertIn("held", self.st["codex"]["limits"])
        read = {k: v for k, v in cl.limits_fact(snap, allowed=True).items() if not k.startswith("_")}
        self.ev("account.limits", None, ts=NOW + 5 * H + 1, **read)
        self.assertNotIn("held", self.st["codex"]["limits"])
        self.assertEqual(budget.decide(self.st, None, 0.95, NOW + 5 * H + 2)["mode"], "normal")

    def test_a_cut_before_any_reading_holds_with_its_time(self):
        self.whole()
        self.cut(limit_resets=NOW + 3 * H)
        self.assertEqual(self.st["limits"]["ts"], NOW + self.seq)
        out = budget.decide(self.st, None, 0.95, NOW + H)
        self.assertEqual((out["mode"], out["until"]), ("owner", NOW + 3 * H))

    def test_a_codex_row_from_before_this_is_taken_over(self):
        # a snapshot of a core before this slice: the Codex reading sits in st["limits"], no Codex row of its own
        self.st["limits"] = {**codex_limits(1.0, 0.5, status="rejected"), "ts": NOW}
        self.st["codex"] = {"host": True}
        reducer.upgrade(self.st)   # the snapshot's load
        self.whole()
        self.assertEqual(self.st["codex"]["limits"]["windows"]["five_hour"]["used"], 1.0)
        self.assertEqual(self.st["limits"]["windows"]["five_hour"]["used"], 1.0)
        self.assertEqual(budget.decide(self.st, None, 0.95, NOW + 100)["mode"], "owner")

    def test_a_held_reset_that_passed_holds_nothing(self):
        self.whole()
        self.ev("account.limits", None, **codex_limits(0.6, 0.5, status="rejected"))
        self.cut(limit_resets=NOW + 3 * H)
        self.assertEqual(budget.decide(self.st, None, 0.95, NOW + 2 * H)["mode"], "owner")
        self.assertEqual(budget.decide(self.st, None, 0.95, NOW + 4 * H)["mode"], "normal")

    def test_a_claude_hub_fold_is_as_on_main(self):
        # no Codex at all: no codex key, the limits row and the default login's as before
        self.ev("account.limits", None, **claude_limits(0.3, 0.5))
        self.assertNotIn("codex", self.st)
        self.assertEqual(self.st["limits"], {**claude_limits(0.3, 0.5), "ts": NOW + self.seq})
        self.assertEqual(self.st["account"]["seen"][""], {k: claude_limits(0.3, 0.5)[k] for k in
                                                          ("status", "type", "windows")} | {"ts": NOW + self.seq})


class Labels(unittest.TestCase):
    def test_a_codex_window_of_another_length_is_named_by_its_length(self):
        self.assertEqual(budget.window_name("codex_60m"), "1 h")
        self.assertEqual(budget.window_name("codex_30m"), "30 min")
        self.assertEqual(budget.window_name("codex_2880m"), "2 d")
        self.assertEqual(budget.window_name("five_hour"), "5 h")
        self.assertEqual(budget.window_name("seven_day"), "week")
        st = {"limits": {**codex_limits(0.1, 0.1, other={"codex_60m": {"used": 0.9, "resets": NOW + H}}), "ts": NOW},
              "account": {}}
        out = budget.decide(st, None, 0.95, NOW)
        self.assertEqual(out["window"], "codex_60m")
        # the reason is the core's phrase: the hub reads the English, the Russian owner the Russian (its marks ride in
        # the budget.mode fact, why_l10n — as hubd writes it into the journal)
        self.assertEqual(out["why"], "window 1 h 90%")
        data = json.loads(json.dumps(coretext.put({**out, "eta": NOW + 600, "resets": NOW + H}, "why", out["why"])))
        card = budget.card({"owner": {}, "runs": {}}, data, "UTC")
        self.assertIn("Limit economy: window 1 h 90%", card)
        self.assertNotIn("codex_60m", card)
        ru = i18n.tr("ru", "core.budget.why_economy_window", pct=90, window=i18n.tr("ru", "core.budget.window_hours", n=1))
        self.assertIn(ru, coretext.human(card, "ru"))
        self.assertNotIn("window 1 h", coretext.human(card, "ru"))


class Forecast(unittest.TestCase):
    """The economy card's forecast reads the series of the hub's engine, not of whichever fact came last."""

    def setUp(self):
        from core import usage
        self.dir = tempfile.mkdtemp(prefix="cxrows-")
        self.addCleanup(platforms.current.rmtree, self.dir)
        usage.reset()

    def write(self, points):
        from core import journal
        lines = [{"seq": i + 1, "ts": ts, "src": "host", "type": "account.limits", "entity": None, "data": d}
                 for i, (ts, d) in enumerate(points)]
        with open(os.path.join(self.dir, "journal.jsonl"), "w") as f:
            f.write("".join(journal._line(x) for x in lines))

    def test_a_mixed_hub_forecasts_by_claude_even_after_a_codex_reading(self):
        points = []
        for i, used in enumerate((0.1, 0.2, 0.3)):
            ts = NOW - (2 - i) * H
            points.append((ts, {**claude_limits(used, 0.1), "windows": {
                "five_hour": {"used": used, "resets": NOW + 3 * H}, "seven_day": {"used": 0.1, "resets": NOW + WEEK}}}))
        points.append((NOW - 60, codex_limits(0.9, 0.1, five_at=H)))
        self.write(points)
        eta = budget.forecast(self.dir, {}, None, "five_hour", NOW, engine=None)
        self.assertAlmostEqual(eta, NOW + 7 * H, delta=120)   # Claude: 10% an hour from 30%
        self.assertNotAlmostEqual(budget.forecast(self.dir, {}, None, "five_hour", NOW, engine="codex") or 0, eta,
                                  delta=120)


class Mark(unittest.TestCase):
    """The daemon's codex.host mark says whether the hub is entirely on Codex (whole) — the fold picks the hub row by it."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="cxrows-mark-")
        self.addCleanup(platforms.current.rmtree, self.state)

    def hub(self, host, cfg):
        from core.daemon import Hub
        conf = os.path.join(self.state, "hub.json")
        with open(conf, "w") as f:
            f.write(cfg)
        with mock.patch.dict(os.environ, {"HUB_CONFIG": conf}):
            os.environ.pop("HUB_CODEX", None)
            return Hub(self.state, host, registry_dir=os.path.join(self.state, "registry"))

    def marks(self, h):
        from core import journal
        return [e["data"] for e in journal.read(h.journal.path) if e["type"] == "codex.host"]

    def test_a_hub_on_codex_is_marked_whole_once(self):
        from hosts.codex import CodexHost
        h = self.hub(CodexHost(self.state, codex=(os.path.join(self.state, "no-codex"),), all_sessions=True),
                     '{"host": "codex"}')
        h.codex_mark()
        h.codex_mark()
        self.assertEqual([(m["on"], m.get("whole")) for m in self.marks(h)], [(True, True)])
        self.assertTrue(h.state["codex"]["whole"])

    def test_a_mixed_hub_is_not_whole(self):
        from hosts.codex import CodexHost, MuxHost
        host = MuxHost(types.SimpleNamespace(), CodexHost(self.state, codex=(os.path.join(self.state, "no-codex"),)))
        h = self.hub(host, "{}")
        h.codex_mark()
        self.assertEqual([(m["on"], bool(m.get("whole"))) for m in self.marks(h)], [(True, False)])
        self.assertNotIn("whole", h.state["codex"])

    def test_an_earlier_mark_without_whole_is_written_again(self):
        from hosts.codex import CodexHost
        h = self.hub(CodexHost(self.state, codex=(os.path.join(self.state, "no-codex"),), all_sessions=True),
                     '{"host": "codex"}')
        h.record("codex.host", None, {"on": True, "channel": "core", "owner": False})   # a core before this slice
        h.codex_mark()
        self.assertEqual([m.get("whole") for m in self.marks(h)], [None, True])


if __name__ == "__main__":
    unittest.main()
