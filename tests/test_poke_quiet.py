"""The core's reminders on open tasks ("open and you are free", delegations.poke step 0) of one session are spaced by
POKE_GAP instead of coming in a pile. Case: many owner tasks of one session were `replied` with one quiet mark, all
reached OPEN_STALE in one second, and the session got a reminder per task in a row."""
import tests  # noqa: F401 — ignore the machine's settings
import unittest

from core import delegations, forms
from tests import test_stuck_tasks as ts

MIN, T0 = ts.MIN, ts.T0
GAP = getattr(delegations, "POKE_GAP", 10 * MIN)


class Pile(ts.Ladder):
    def tasks(self, n):
        """n owner tasks of w, all replied with an interim result, all with one quiet mark."""
        dids = [self.did]
        for _ in range(n - 1):
            mid = self.ev("message.received", None, text="one more", channel="web", owner=True, to="hub")
            ev = self.ev("delegation.created", None, session="w", **{"for": mid}, channel="session", owner=False,
                         sender="hub")
            dids.append(f"d{ev}")
        self.now += MIN
        for did in dids:
            self.turn("w", self.st["delegations"][did]["message"], "interim: working")
        mark = self.now
        for did in dids:
            self.st["delegations"][did]["quiet_mark"] = self.st["delegations"][did]["answered_at"] = mark
        return dids, mark

    def pokes(self):
        return sorted((m for m in self.st["messages"].values()
                       if " is open, " in m["text"] and m["origin"] == forms.ORIGIN_CORE), key=lambda m: m["created"])

    def test_tasks_with_one_deadline_get_one_reminder_per_poke_gap(self):
        dids, mark = self.tasks(5)
        seen, end = [], mark + delegations.OPEN_STALE + 6 * GAP
        t = mark
        while t < end:
            t += 30
            self.at((t - T0) / MIN)
            for m in self.pokes():
                if m["id"] not in seen:
                    seen.append(m["id"])
                    self.turn("w", m["id"], "waiting for the build")   # the session answers at once
        stamps = [self.st["messages"][i]["created"] for i in seen]
        self.assertEqual(len(stamps), 5)
        self.assertTrue(all(b - a >= GAP for a, b in zip(stamps, stamps[1:])), stamps)

    def test_pending_counts_the_latest_reminders_once_and_agrees_with_quiet(self):
        dids, mark = self.tasks(3)
        self.at((mark - T0) / MIN + delegations.OPEN_NUDGE // MIN)
        (m,) = self.pokes()
        self.turn("w", m["id"], "waiting for the build")   # answered: the session is free again
        nudged_at = delegations.last_nudged(self.st)
        for did in dids:
            d = self.st["delegations"][did]
            self.assertEqual(delegations.quiet(self.st, d, now=self.now),
                             delegations.quiet(self.st, d, now=self.now, nudged_at=nudged_at))
        waiting = [d for d in dids if not self.st["delegations"][d].get("open_nudged")]
        due = {i: at for at, i, _ in delegations.pending(self.st, now=self.now)}
        self.assertTrue(all(due[d] >= nudged_at["w"] + GAP for d in waiting), due)


if __name__ == "__main__":
    unittest.main()
