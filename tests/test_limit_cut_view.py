"""A turn cut by an account limit is in the session's row of the snapshot (the page and the clients) and in the sessions
slice of a module: {ts, resets}. `resets` is when the window that refused the turn resets, or null when no limits were
known before the refusal. The field goes away when the core has continued the turn (a new turn, the reset fact)."""
import tests  # noqa: F401 - ignore the machine's settings
import unittest

from core import modstate, reducer, views

OWNER = {"channel": "cli", "owner": True}
CORE = {"channel": "core"}
RESETS = 5000
LIMIT_TEXT = "You've hit your session limit · resets 6:20am (Asia/Almaty)"


class Cut(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.events = reducer.empty(), 0, []
        self.ev("session.requested", "maps", cwd="/home/x/maps", session_id="s-m", **OWNER)
        self.ev("session.started", "maps")
        self.ev("session.requested", "docs", cwd="/home/x/docs", session_id="s-d", **OWNER)
        self.ev("session.started", "docs")

    def ev(self, kind, entity=None, /, **data):
        self.seq += 1
        e = {"seq": self.seq, "ts": 100 + self.seq, "type": kind, "entity": entity, "data": data}
        self.events.append(e)
        self.st, _, why = reducer.reduce(self.st, e)
        self.assertIsNone(why, (kind, data))

    def cut(self, status="rejected", resets=RESETS):
        self.ev("message.received", None, to="maps", text="do it", channel="telegram", owner=True)
        self.ev("turn.started", "maps", marker=f"m{self.seq}")
        self.ev("account.limits", None, status=status, type="five_hour",
                windows={"five_hour": {"used": 1, "resets": resets}}, **OWNER)
        self.ev("turn.result", "maps", text=LIMIT_TEXT, is_error=True)

    def snap(self, name="maps"):
        r = views.Reader()
        r.feed_all(self.events)
        return {s["name"]: s for s in r.snapshot()["sessions"]}[name]

    def slice(self, name="maps"):
        return modstate.session_rows(self.st)["sessions"][name]

    def test_the_cut_is_in_the_snapshot_row_with_the_reset(self):
        self.cut()
        ts = self.st["sessions"]["maps"]["limit_cut"]["ts"]
        self.assertEqual(self.snap()["limit_cut"], {"ts": ts, "resets": RESETS})
        self.assertEqual(self.slice()["limit_cut"], {"ts": ts, "resets": RESETS})

    def test_a_cut_with_no_known_reset_has_null_resets(self):
        self.cut(status="allowed")
        ts = self.st["sessions"]["maps"]["limit_cut"]["ts"]
        self.assertEqual(self.snap()["limit_cut"], {"ts": ts, "resets": None})
        self.assertEqual(self.slice()["limit_cut"], {"ts": ts, "resets": None})

    def test_a_session_that_is_not_cut_has_null(self):
        self.cut()
        self.assertIsNone(self.snap("docs")["limit_cut"])
        self.assertIsNone(self.slice("docs")["limit_cut"])

    def test_it_goes_away_when_the_core_has_continued_the_turn(self):
        self.cut()
        self.ev("session.limit_reset", "maps", resets=RESETS, **CORE)
        self.assertIsNone(self.snap()["limit_cut"])
        self.assertIsNone(self.slice()["limit_cut"])

    def test_it_goes_away_with_a_new_turn(self):
        self.cut()
        self.ev("message.received", None, to="maps", text="go on", channel="web", owner=True)
        self.ev("turn.started", "maps", marker=f"m{self.seq}")
        self.assertIsNone(self.snap()["limit_cut"])
        self.assertIsNone(self.slice()["limit_cut"])

    def test_the_row_carries_nothing_but_the_time_and_the_reset(self):
        self.cut()
        self.st["sessions"]["maps"]["limit_cut"].update(account="a@x", gen=3, task="d9", marker="m1")
        self.assertEqual(set(self.slice()["limit_cut"]), {"ts", "resets"})


if __name__ == "__main__":
    unittest.main()
