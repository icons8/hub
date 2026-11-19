"""The term of a recurring job (`--until`): the core used to move it to expired silently (30.10: a meetings watcher ended
and came back 60 h later, two Jira watchers stood 53 h, a Sentry watcher ended and was never renewed). Now whoever set
the job hears of it twice — two days before the term (a hubd timer, timer.fired sched:<id>) and when it ends
(schedule.ended) — each once per term; a renewal with a new term starts over. A session that set it gets it (a sleeping
one is woken); the owner, the core or a session that is gone or handed in — the hub. A system job (renewed by the core)
gets neither. `hub schedule rm` takes an expired job off the list ("not needed")."""
import tests  # noqa: F401 — hide the machine's settings
import unittest

from core import reducer, schedule

DAY = 86400
OWNER = {"channel": "cli", "owner": True}
T0 = 1000.0


def _as(sender):
    return {"channel": "session", "owner": False, "sender": sender}


class Hubd:
    """The fold plus hubd's timers (daemon.fire_timers): events at given times, ticks fire the due timers."""

    def __init__(self):
        self.st, self.seq, self.rejected = reducer.empty(), 0, []
        self.ev(0, "owner.settings", timezone="UTC")
        for name, kind in (("hub", "hub"), ("w", None), ("x", None)):
            self.ev(0, "session.requested", name, cwd="/w", session_id=f"sid-{name}", **({"kind": kind} if kind else OWNER))
            self.ev(1, "session.started", name, session_id=f"sid-{name}")

    def ev(self, t, type, entity=None, **data):
        self.seq += 1
        e = {"seq": self.seq, "ts": T0 + t, "src": "test", "type": type, "entity": entity, "data": data}
        self.st, _, why = reducer.reduce(self.st, e)
        if why:
            self.rejected.append((type, entity, why))
        return why

    def tick(self, t):
        for deadline, mid, attempt in reducer.pending_timers(self.st):
            if deadline <= T0 + t:
                self.ev(t, "timer.fired", mid, attempt=attempt, n=attempt)

    def run(self, until, step=3600):
        t = 0
        while t <= until:
            self.tick(t)
            t += step

    def notes(self, job):
        return [m for m in self.st["messages"].values() if m["text"].startswith(f"[⏰ schedule {job}]")
                and (" ends " in m["text"] or " ended " in m["text"])]


class Expiry(unittest.TestCase):
    def test_the_setter_hears_two_days_before_and_at_the_end_once_each(self):
        h = Hubd()
        h.ev(2, "schedule.set", "w-watch", session="w", every="1h", prompt="check", until=T0 + 5 * DAY, **_as("w"))
        h.run(3 * DAY - 7200)
        self.assertEqual(h.notes("w-watch"), [])   # more than two days left
        h.run(3 * DAY + 3600)
        h.run(5 * DAY - 60)
        (soon,) = h.notes("w-watch")
        self.assertEqual(soon["session"], "w")
        self.assertIn(" ends ", soon["text"])
        self.assertIn("hub schedule extend w-watch", soon["text"])
        self.assertIn("hub schedule rm w-watch", soon["text"])
        h.ev(5 * DAY + 60, "schedule.ended", "w-watch")
        h.run(6 * DAY)
        self.assertEqual(h.st["schedule"]["w-watch"]["state"], "expired")
        soon2, ended = h.notes("w-watch")
        self.assertEqual(soon2["id"], soon["id"])
        self.assertEqual(ended["session"], "w")
        self.assertIn(" ended ", ended["text"])
        self.assertIn("hub schedule extend w-watch", ended["text"])
        self.assertEqual(h.rejected, [])

    def test_a_job_set_by_the_hub_or_the_owner_is_told_to_the_hub(self):
        h = Hubd()
        h.ev(2, "schedule.set", "x-watch", session="x", cron="0 9 * * *", prompt="p", until=T0 + 10 * DAY, **_as("hub"))
        h.ev(2, "schedule.set", "own", session="x", every="2h", prompt="p", until=T0 + 10 * DAY, **OWNER)
        h.run(10 * DAY - 60)
        h.ev(10 * DAY + 60, "schedule.ended", "x-watch")
        h.ev(10 * DAY + 60, "schedule.ended", "own")
        for job in ("x-watch", "own"):
            self.assertEqual([m["session"] for m in h.notes(job)], ["hub", "hub"], job)
            self.assertIn("to session x", h.notes(job)[0]["text"])

    def test_a_renewal_moves_the_warning_and_ending_to_the_new_term(self):
        h = Hubd()
        spec = dict(session="w", every="1h", prompt="p", **_as("w"))
        h.ev(2, "schedule.set", "j", until=T0 + 5 * DAY, **spec)
        h.run(2 * DAY)
        h.ev(2 * DAY + 10, "schedule.set", "j", until=T0 + 12 * DAY, **spec)   # renewed before the warning
        h.run(9 * DAY)
        self.assertEqual(h.notes("j"), [])
        h.run(10 * DAY + 3600)
        self.assertEqual(len(h.notes("j")), 1)   # two days before the new term
        h.ev(10 * DAY + 7200, "schedule.set", "j", until=T0 + 20 * DAY, **spec)   # renewed after the warning: told again
        h.run(17 * DAY)
        self.assertEqual(len(h.notes("j")), 1)
        h.run(18 * DAY + 3600)
        self.assertEqual(len(h.notes("j")), 2)

    def test_a_renewal_of_an_expired_job_brings_it_back(self):
        h = Hubd()
        spec = dict(session="w", cron="0 9 * * *", prompt="p", **_as("w"))
        h.ev(2, "schedule.set", "j", until=T0 + 3 * DAY, **spec)
        h.run(3 * DAY)
        h.ev(3 * DAY + 60, "schedule.ended", "j")
        self.assertEqual(len(h.notes("j")), 2)
        h.ev(4 * DAY, "schedule.set", "j", until=T0 + 14 * DAY, **spec)
        self.assertEqual(h.st["schedule"]["j"]["state"], "active")
        h.run(11 * DAY)
        self.assertEqual(len(h.notes("j")), 2)
        h.run(12 * DAY + 3600)   # two days before the new term
        self.assertEqual(len(h.notes("j")), 3)

    def test_a_short_term_gets_no_warning_only_the_end(self):
        # set for a day: the setter has just chosen the term, a "two days left" note would be noise
        h = Hubd()
        h.ev(2, "schedule.set", "j", session="w", every="1h", prompt="p", until=T0 + DAY, **_as("w"))
        h.run(DAY - 60)
        self.assertEqual(h.notes("j"), [])
        self.assertEqual(reducer.pending_timers(h.st), [])
        h.ev(DAY + 60, "schedule.ended", "j")
        self.assertEqual(len(h.notes("j")), 1)

    def test_a_system_job_is_renewed_by_the_core_and_tells_nobody(self):
        h = Hubd()
        h.ev(2, "schedule.set", "sys", session="w", cron="0 11 * * *", prompt="p", until=T0 + 5 * DAY, channel="core",
             system=True)
        h.run(5 * DAY - 60)
        h.ev(5 * DAY + 60, "schedule.ended", "sys")
        self.assertEqual(h.st["schedule"]["sys"]["state"], "expired")
        self.assertEqual(h.notes("sys"), [])

    def test_a_command_with_a_term_is_told_and_one_without_never(self):
        h = Hubd()
        h.ev(2, "schedule.set", "mon", every="1h", command="true", until=T0 + 5 * DAY, **_as("w"))
        h.ev(2, "schedule.set", "forever", every="1h", command="true", **_as("w"))
        h.run(5 * DAY - 60)
        h.ev(5 * DAY + 60, "schedule.ended", "mon")
        self.assertEqual([m["session"] for m in h.notes("mon")], ["w", "w"])
        self.assertEqual(h.notes("forever"), [])

    def test_a_parked_setter_is_woken(self):
        # working or parked (an idle one whose process the core let go) — as the core's other own messages; one put
        # to sleep by a human is not woken by the core (the hub gets it, below)
        h = Hubd()
        h.ev(2, "schedule.set", "j", session="w", every="1h", prompt="p", until=T0 + 5 * DAY, **_as("w"))
        h.ev(3, "session.parked", "w", channel="core", why="restart")
        self.assertEqual(h.st["sessions"]["w"]["state"], "parked")
        h.run(3 * DAY + 3600)
        (soon,) = h.notes("j")
        self.assertEqual(soon["session"], "w")
        self.assertEqual(h.st["sessions"]["w"]["state"], "starting")

    def test_a_setter_that_is_gone_or_handed_in_gives_way_to_the_hub(self):
        for gone in ("asleep", "held", "removed", "retired", "closed", "failed"):
            with self.subTest(gone=gone):
                h = Hubd()
                h.ev(2, "schedule.set", "j", session="x", every="1h", prompt="p", until=T0 + 5 * DAY, **_as("w"))
                h.st["sessions"]["w"]["state"] = gone
                h.run(3 * DAY + 3600)
                (soon,) = h.notes("j")
                self.assertEqual(soon["session"], "hub")
                self.assertIn("set by session w", soon["text"])
                self.assertEqual(h.st["sessions"]["w"]["state"], gone)

    def test_a_late_timer_after_the_term_tells_nothing_and_stops(self):
        # hubd was down over the whole warning window: no "ends in…" after the term, the end note is enough;
        # and the timer does not fire on every tick after that
        h = Hubd()
        h.ev(2, "schedule.set", "j", session="w", every="1h", prompt="p", until=T0 + 5 * DAY, **_as("w"))
        h.tick(5 * DAY + 600)
        self.assertEqual(h.notes("j"), [])
        self.assertEqual(reducer.pending_timers(h.st), [])

    def test_a_stale_timer_is_a_fact_without_consequences(self):
        h = Hubd()
        h.ev(2, "schedule.set", "j", session="w", every="1h", prompt="p", until=T0 + 5 * DAY, **_as("w"))
        self.assertIsNone(h.ev(3 * DAY + 10, "timer.fired", "sched:j", attempt=T0 + 9 * DAY, n=1))   # another term
        self.assertIsNone(h.ev(3 * DAY + 10, "timer.fired", "sched:nope", attempt=T0 + 5 * DAY, n=1))
        self.assertEqual(h.notes("j"), [])

    def test_rm_takes_an_expired_job_off(self):
        h = Hubd()
        h.ev(2, "schedule.set", "j", session="w", every="1h", prompt="p", until=T0 + DAY, **_as("w"))
        h.ev(DAY + 60, "schedule.ended", "j")
        self.assertIsNone(h.ev(DAY + 120, "schedule.remove_requested", "j", **_as("w")))
        self.assertEqual(h.st["schedule"]["j"]["state"], "removed")


    def test_the_same_set_without_a_term_does_not_revive_an_expired_job(self):
        # it would come back with its passed term, end at once and tell its setter again — on every `apply`
        h = Hubd()
        spec = dict(session="w", every="1h", prompt="p")
        h.ev(2, "schedule.set", "j", until=T0 + DAY, **spec, **_as("w"))
        h.ev(DAY + 60, "schedule.ended", "j")
        h.ev(DAY + 120, "schedule.applied", jobs=[{"id": "j", **spec}], **_as("w"))
        self.assertEqual(h.st["schedule"]["j"]["state"], "expired")
        self.assertIn("hub schedule extend j", h.ev(DAY + 180, "schedule.set", "j", **spec, **_as("w")))
        self.assertEqual(h.st["schedule"]["j"]["state"], "expired")
        self.assertEqual(len(h.notes("j")), 1)
        # who may not edit the job hears that, not "renew it"
        self.assertIn("someone else's", h.ev(DAY + 200, "schedule.set", "j", **spec, **_as("x")) or "")
        # a set that leaves it out means "not needed"
        h.ev(DAY + 240, "schedule.applied", jobs=[], **_as("w"))
        self.assertEqual(h.st["schedule"]["j"]["state"], "removed")


class Extend(unittest.TestCase):
    """`hub schedule extend` builds the job as it is with a new term (core/schedule.extend_spec)."""

    def job(self, **kw):
        return {"id": "j", "state": "active", "kind": "every", "every": 3600, "session": "w", "prompt": "p",
                "until": T0 + 5 * DAY, **kw}

    def test_by_default_fourteen_more_days_from_the_term_never_shorter(self):
        spec = schedule.extend_spec(self.job(until=T0 + 25 * DAY), None, T0, "UTC")
        self.assertEqual(spec["until"], "1970-01-30")   # 25 + 14 days would pass the 30-day limit: its last full day
        spec = schedule.extend_spec(self.job(), None, T0, "UTC")
        self.assertEqual(spec["until"], "1970-01-20")   # the term (day 5) + 14
        spec = schedule.extend_spec(self.job(state="expired", until=T0 - DAY), None, T0, "UTC")
        self.assertEqual(spec["until"], "1970-01-15")   # passed: from today
        self.assertEqual({k: spec[k] for k in ("every", "session", "prompt")}, {"every": 3600, "session": "w", "prompt": "p"})

    def test_a_term_already_at_the_limit_says_so(self):
        with self.assertRaises(schedule.Invalid):
            schedule.extend_spec(self.job(until=T0 + 29.5 * DAY), None, T0, "UTC")

    def test_what_is_not_extended(self):
        for job in (self.job(until=None, command="true", session=""), self.job(kind="at", at=T0), self.job(system=True),
                    self.job(state="removed"), self.job(state="paused"), None):
            with self.subTest(job=job):
                with self.assertRaises(schedule.Invalid):
                    schedule.extend_spec(job, None, T0, "UTC")

    def test_an_extended_job_is_accepted_by_the_core(self):
        h = Hubd()
        h.ev(2, "schedule.set", "j", session="w", every="1h", prompt="p", check="true", until=T0 + DAY, **_as("w"))
        h.ev(DAY + 60, "schedule.ended", "j")
        spec = schedule.extend_spec(h.st["schedule"]["j"], None, T0 + DAY + 100, "UTC")
        self.assertIsNone(h.ev(DAY + 120, "schedule.set", spec.pop("id"), **spec, **_as("w")))
        job = h.st["schedule"]["j"]
        self.assertEqual((job["state"], job["check"]), ("active", "true"))


class Shown(unittest.TestCase):
    def test_list_line_says_when_it_ended(self):
        h = Hubd()
        h.ev(2, "schedule.set", "j", session="w", every="1h", prompt="p", until=T0 + DAY, **_as("w"))
        h.ev(DAY + 60, "schedule.ended", "j")
        line = schedule.state_text(h.st["schedule"]["j"], T0 + DAY + 100, "UTC", "en")
        self.assertIn("ended", line)
        self.assertIn("hub schedule extend j", line)


if __name__ == "__main__":
    unittest.main()
