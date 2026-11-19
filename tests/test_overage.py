"""The weekly window that includes overage (seven_day_overage_included, "7d_oi" of the gateway) and the overage flag of
account.limits: the limit economy counts the window like the other two, a refusal on it makes the account full until its
reset, and the hub hears once when an account starts to be billed as overage."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from core import budget, reducer

OI = "seven_day_overage_included"
FAR = 10 ** 10


def reading(status="allowed", type="five_hour", oi=0.1, oi_at=FAR, five=0.1, week=0.1):
    return {"status": status, "type": type,
            "windows": {"five_hour": {"used": five, "resets": FAR}, "seven_day": {"used": week, "resets": FAR},
                        OI: {"used": oi, "resets": oi_at}}}


def state(limits=None, accounts=None, blocked=None):
    st = reducer.empty()
    st["account"] = {"seen": accounts or {}, "blocked": blocked or {}}
    st["limits"] = limits or {}
    return st


class Window(unittest.TestCase):
    def test_the_window_counts_in_the_fill_level(self):
        self.assertEqual(budget.fill(reading(oi=0.9), 1000), (0.9, OI))
        self.assertEqual(budget.fill(reading(five=0.2, oi=0.1), 1000), (0.2, "five_hour"))

    def test_a_window_past_its_reset_is_zero(self):
        self.assertEqual(budget.fill(reading(oi=0.9, oi_at=500), 1000), (0.1, "five_hour"))

    def test_a_refusal_on_it_means_full_until_its_reset(self):
        seen = reading("rejected", OI, oi=0.5, oi_at=9000)
        self.assertEqual(budget.fill(seen, 1000), (1, OI))
        self.assertEqual(budget.fill(seen, 9000), (0.1, "five_hour"))

    def test_the_reset_of_the_window_frees_the_account(self):
        seen = reading("rejected", OI, oi=1, oi_at=9000)
        self.assertEqual(budget.decide(state(seen), None, 0.95, 1000)["until"], 9000)

    def test_a_single_login_refused_on_it_is_the_owners_word(self):
        out = budget.decide(state(reading("rejected", OI, oi=0.5, oi_at=9000)), None, 0.95, 1000)
        self.assertEqual((out["mode"], out["window"]), ("owner", OI))

    def test_the_pool_moves_off_an_account_refused_on_it(self):
        accounts = {"a": reading("rejected", OI, oi=0.5, oi_at=9000), "b": reading(five=0.3)}
        out = budget.decide(state(accounts=accounts), ["a", "b"], 0.95, 1000)
        self.assertEqual((out["mode"], out["account"]), ("normal", "b"))
        out = budget.decide(state(accounts={"a": accounts["a"]}), ["a"], 0.95, 1000)
        self.assertEqual(out["mode"], "owner")

    def test_its_share_over_the_threshold_turns_the_economy_on(self):
        out = budget.decide(state(reading(oi=0.87)), None, 0.95, 1000)
        self.assertEqual((out["mode"], out["window"]), ("economy", OI))
        self.assertEqual(budget.decide(state(reading(oi=0.84)), None, 0.95, 1000)["mode"], "normal")
        out = budget.decide(state(reading(oi=0.97)), None, 0.95, 1000)
        self.assertEqual(out["mode"], "owner")

    def test_the_reason_names_the_window_for_a_person(self):
        out = budget.decide(state(reading(oi=0.87)), None, 0.95, 1000)
        self.assertNotIn(OI, budget.why_of(out))
        self.assertTrue(budget.window_name(OI))
        self.assertNotEqual(budget.window_name(OI), OI)


class Carry(unittest.TestCase):
    """The gateway sends the overage window in some readings only: the last value, with its reset ahead, stands."""

    def setUp(self):
        self.st, self.seq = reducer.empty(), 0

    def read(self, ts, windows, account="main"):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts, "type": "account.limits", "entity": None,
                                                   "data": {"status": "allowed", "type": "five_hour", "overage": False,
                                                            "account": account, "windows": windows}})
        self.assertIsNone(why)
        return self.st["account"]["seen"][account]

    def full(self, five=0.2, oi=0.9, oi_at=9000):
        return {"five_hour": {"used": five, "resets": 5000}, "seven_day": {"used": 0.3, "resets": 8000},
                OI: {"used": oi, "resets": oi_at}}

    def test_a_reading_without_the_window_keeps_the_last_one(self):
        self.read(1000, self.full())
        seen = self.read(1010, {k: v for k, v in self.full(five=0.3).items() if k != OI})
        self.assertEqual(seen["windows"][OI], {"used": 0.9, "resets": 9000})
        self.assertEqual(seen["windows"]["five_hour"]["used"], 0.3)
        self.assertEqual(budget.fill(seen, 1010), (0.9, OI))

    def test_the_fill_level_does_not_jump_between_readings(self):
        fills = []
        for i in range(4):
            windows = self.full() if i % 2 == 0 else {k: v for k, v in self.full().items() if k != OI}
            fills.append(budget.fill(self.read(1000 + i, windows), 1000 + i))
        self.assertEqual(set(fills), {(0.9, OI)})

    def test_a_new_value_replaces_it(self):
        self.read(1000, self.full())
        seen = self.read(1010, self.full(oi=0.95))
        self.assertEqual(seen["windows"][OI]["used"], 0.95)

    def test_a_window_past_its_reset_is_not_kept(self):
        self.read(1000, self.full(oi_at=1500))
        seen = self.read(1600, {"five_hour": {"used": 0.2, "resets": 5000}})
        self.assertNotIn(OI, seen["windows"])

    def test_the_active_limits_row_keeps_it_too(self):
        self.read(1000, self.full())
        self.st["account"]["active"] = "main"
        self.read(1010, {"five_hour": {"used": 0.3, "resets": 5000}})
        self.assertEqual(self.st["limits"]["windows"][OI]["used"], 0.9)

    def test_an_account_does_not_take_anothers_window(self):
        self.read(1000, self.full(), account="a")
        seen = self.read(1010, {"five_hour": {"used": 0.3, "resets": 5000}}, account="b")
        self.assertNotIn(OI, seen["windows"])


class Notice(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.now = reducer.empty(), 0, 100
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", channel="cli", owner=True)
        self.ev("session.started", "hub")

    def ev(self, name, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.now + self.seq, "type": name,
                                                   "entity": entity, "data": data})
        self.assertIsNone(why)

    def limits(self, overage, resets=FAR, **extra):
        self.ev("account.limits", None, status="allowed", type="five_hour", overage=overage,
                windows={"five_hour": {"used": 1.0, "resets": resets}}, **extra)

    def told(self):
        return [m for m in self.st["messages"].values() if m["session"] == "hub" and "overage" in m["text"]]

    def test_the_hub_hears_once_when_overage_starts(self):
        self.limits(False)
        self.assertEqual(self.told(), [])
        self.limits(True)
        self.assertEqual(len(self.told()), 1)
        for _ in range(3):
            self.limits(True)
        self.assertEqual(len(self.told()), 1)

    def test_the_end_of_overage_is_quiet(self):
        self.limits(True)
        self.limits(False)
        self.now += reducer.OVERAGE_OFF + 10
        for _ in range(3):
            self.limits(False)
        self.assertEqual(len(self.told()), 1)
        self.assertNotIn("overage", self.st["account"])

    def test_a_late_process_with_old_headers_does_not_end_the_episode(self):
        self.limits(True)
        self.limits(False)
        self.limits(True)
        self.assertEqual(len(self.told()), 1)
        for _ in range(3):   # readings out of order: false, true, false, true
            self.limits(False)
            self.limits(True)
        self.assertEqual(len(self.told()), 1)

    def test_readings_without_the_flag_for_long_end_the_episode(self):
        self.limits(True)
        self.limits(False)
        self.now += reducer.OVERAGE_OFF - 10
        self.limits(False)
        self.limits(True)
        self.assertEqual(len(self.told()), 1)   # not yet steady: the true one cancels it
        self.limits(False)
        self.now += reducer.OVERAGE_OFF + 10
        self.limits(False)
        self.limits(True)
        self.assertEqual(len(self.told()), 2)

    def test_the_reset_of_the_window_ends_the_episode(self):
        self.limits(True, resets=self.now + 500)
        self.limits(True, resets=self.now + 500)
        self.assertEqual(len(self.told()), 1)
        self.now += 1000
        self.limits(True, resets=self.now + 500)
        self.assertEqual(len(self.told()), 2)

    def test_an_episode_with_no_used_up_window_ends_when_its_windows_reset(self):
        # the pool moved off the account and nothing reads it: the next overage after its windows reset is news again
        self.ev("account.limits", None, status="allowed", type="five_hour", overage=True, account="a",
                windows={"five_hour": {"used": 0.5, "resets": self.now + 500}})
        self.assertEqual(len(self.told()), 1)
        self.now += 1000
        self.ev("account.limits", None, status="allowed", type="five_hour", overage=True, account="a",
                windows={"five_hour": {"used": 0.5, "resets": self.now + 500}})
        self.assertEqual(len(self.told()), 2)

    def test_no_overage_no_note_and_no_key_in_the_account_row(self):
        for _ in range(3):
            self.limits(False)
        self.assertEqual(self.told(), [])
        self.assertNotIn("overage", self.st["account"])

    def test_a_pool_account_has_its_own_episode_and_is_named(self):
        self.limits(True, account="main")
        self.limits(True, account="spare")
        self.limits(True, account="main")
        notes = self.told()
        self.assertEqual(len(notes), 2)
        self.assertTrue(any("main" in m["text"] for m in notes))
        self.assertTrue(any("spare" in m["text"] for m in notes))

    def test_a_fresh_token_of_an_account_still_in_overage_is_not_a_new_episode(self):
        self.ev("account.switched", None, to="a", channel="cli", owner=True)
        self.limits(True, account="a")
        self.ev("account.switched", None, to="a", **{"from": "a"}, refresh=True, restart=True, channel="core")
        self.limits(True, account="a")
        self.assertEqual(len(self.told()), 1)

    def test_a_process_of_an_old_token_does_not_start_an_episode(self):
        self.ev("account.switched", None, to="a", channel="cli", owner=True)
        self.ev("account.switched", None, to="a", **{"from": "a"}, refresh=True, restart=True, channel="core")
        gen = self.st["account"]["refreshed"]["a"]
        self.limits(True, account="a", gen=gen - 1)
        self.assertEqual(self.told(), [])

    def test_the_flag_stays_in_the_limits_row(self):
        self.limits(True)
        self.assertIs(self.st["limits"]["overage"], True)


if __name__ == "__main__":
    unittest.main()
