"""The machine sleeps (macOS Maintenance Sleep / DarkWake: a window of 10-50 s every ~15 minutes). The hub process is
frozen, the wall clock runs on, and every wall-clock term of the core expires without the process having lived through
it (2026-11-05: /compact of a session took 1904 s instead of ~55 s; the guard of a compaction in progress
(COMMAND_CONFIRM_SECONDS) ran out, a schedule message was typed behind the compaction, its delivery timer counted
attempts 1..4 across the sleep, the watchdog raised "not delivered", and a reload of the session cut the compaction).
The core learns about the sleep from a fact (core.slept: seconds the wall clock ran ahead of the awake clock) and
moves the terms that count the time of a living process: the compaction guard and the delivery deadline."""
import tests  # noqa: F401 — do not see this machine's settings
import tempfile
import unittest

from core import reducer
from core.daemon import Hub
from core.machines import COMMAND_CONFIRM_SECONDS

OWNER = {"channel": "cli", "owner": True}
SLEEP = 15 * 60   # one maintenance sleep: the longer part of the 15 minutes between wake windows


class Replay:
    def __init__(self):
        self.st, self.seq, self.ts, self.typed = reducer.empty(), 0, 1000.0, []

    def at(self, ts):
        self.ts = ts
        return self

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, actions, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.ts, "type": type, "entity": entity,
                                                         "data": data})
        self.typed += [a for a in actions if a.get("do") == "type"]
        return why

    def msg(self):
        return next(iter(self.st["messages"].values()))

    def sess(self):
        return self.st["sessions"]["a"]


def compacting():
    """a is free, /compact was sent at t=1000 and has not answered."""
    r = Replay()
    r.ev("session.requested", "a", cwd="/w", session_id="s-a", **OWNER)
    r.ev("session.started", "a")
    r.ev("turn.result", "a", text="x", turns=1, context=250000)
    r.ev("session.compact_requested", "a", channel="core", why="idle")
    r.ev("host.done", "a", do="compact", result="sent", message=None, attempt=None)
    assert r.sess()["compacting"] == 1000.0
    return r


class Slept(unittest.TestCase):
    def test_a_message_arriving_after_a_sleep_still_waits_for_the_compaction(self):
        r = compacting()
        r.at(1000 + SLEEP)
        self.assertIsNone(r.ev("core.slept", seconds=SLEEP))         # the process lived 10 s of the 900
        r.ev("message.received", None, to="a", text="round", **OWNER)
        self.assertEqual(r.typed, [])                                # not typed behind /compact
        self.assertEqual(r.msg()["state"], "queued")
        r.at(1000 + SLEEP + 50)
        r.ev("session.compacted", "a", ok=True, before=250000, after=11000)
        self.assertEqual([t["message"] for t in r.typed], [r.msg()["id"]])   # the compaction ended: now it is typed

    def test_the_guard_still_runs_out_for_a_compaction_that_never_answers(self):
        r = compacting()
        r.at(1000 + SLEEP)
        r.ev("core.slept", seconds=SLEEP)
        r.at(1000 + SLEEP + COMMAND_CONFIRM_SECONDS + 1)             # awake time passes: hung for real
        r.ev("message.received", None, to="a", text="round", **OWNER)
        self.assertEqual(len(r.typed), 1)

    def test_a_sleep_does_not_spend_the_attempts_of_a_message_being_typed(self):
        r = Replay()
        r.ev("session.requested", "a", cwd="/w", session_id="s-a", **OWNER)
        r.ev("session.started", "a")
        r.ev("message.received", None, to="a", text="round", **OWNER)
        self.assertEqual(r.msg()["state"], "typing")
        deadline = r.msg()["deadline"]
        r.at(1000 + SLEEP)
        r.ev("core.slept", seconds=SLEEP)
        self.assertEqual(r.msg()["deadline"], deadline + SLEEP)
        self.assertEqual([t for t in reducer.pending_timers(r.st, r.ts) if t[0] <= r.ts], [])   # no timer is due on wake
        self.assertEqual(r.msg()["attempt"], 1)

    def test_a_malformed_sleep_changes_nothing(self):
        r = compacting()
        r.at(2000)
        for bad in (None, "x", -5, True, float("nan")):
            r.ev("core.slept", seconds=bad)
        self.assertEqual(r.sess()["compacting"], 1000.0)


class Daemon(unittest.TestCase):
    def setUp(self):
        self.wall, self.awake = [1000.0], [50.0]
        self.h = Hub(tempfile.mkdtemp(prefix="slept-"), type("H", (), {"alive": lambda s, n: True})(),
                     registry_dir=tempfile.mkdtemp(), clock=lambda: self.wall[0], mono=lambda: self.awake[0])

    def pass_(self, wall, awake):
        self.wall[0] += wall
        self.awake[0] += awake
        self.h.check_slept()

    def test_the_wall_clock_runs_ahead_of_the_awake_clock_is_a_sleep(self):
        events = []
        real = self.h.record
        self.h.record = lambda type, *a, **k: events.append((type, k.get("data"))) or real(type, *a, **k)
        self.pass_(0.3, 0.3)                       # a normal pass: the baseline
        self.pass_(900, 10)                        # woke up from a 15-minute sleep
        self.pass_(0.3, 0.3)
        self.assertEqual([(t, d) for t, d in events if t == "core.slept"], [("core.slept", {"seconds": 890})])

    def test_a_long_tick_is_not_a_sleep(self):
        events = []
        real = self.h.record
        self.h.record = lambda type, *a, **k: events.append(type) or real(type, *a, **k)
        self.pass_(0.3, 0.3)
        self.pass_(300, 300)                       # git took five minutes: both clocks ran
        self.assertNotIn("core.slept", events)

    def test_the_first_pass_has_no_baseline_and_says_nothing(self):
        events = []
        real = self.h.record
        self.h.record = lambda type, *a, **k: events.append(type) or real(type, *a, **k)
        self.h.check_slept()
        self.assertNotIn("core.slept", events)


class InATick(unittest.TestCase):
    def test_a_sleep_inside_a_step_is_known_before_the_next_step_judges_a_term(self):
        # review: the machine can fall asleep inside a long step of the tick (git up to 30 s, a module, an MCP status
        # call); the steps after it (fire_timers: the delivery deadline) must see the sleep, not spend an attempt on it
        import os
        import platforms
        from core import journal
        from hosts.streamjson import StreamHost
        state = tempfile.mkdtemp(prefix="slept-tick-")
        self.addCleanup(platforms.current.rmtree, state)
        wall, awake = [1000.0], [50.0]
        host = StreamHost(state, claude=("claude-not-run",))
        host.clock = lambda: wall[0]
        h = Hub(state, host, registry_dir=os.path.join(state, "registry"), clock=lambda: wall[0], mono=lambda: awake[0])
        h.execute = lambda action: None             # no spawn, no typing: only the journal
        h.record("session.requested", "a", {"cwd": state, "session_id": "s-a", **OWNER})
        h.record("session.started", "a", {"session_id": "s-a"})
        h.record("message.received", None, {"to": "a", "text": "round", **OWNER})
        mid = next(iter(h.state["messages"]))
        self.assertEqual(h.state["messages"][mid]["state"], "typing")

        def asleep():                                # this step is where the machine slept
            wall[0] += SLEEP
            awake[0] += 10
        h.check_mcp = asleep
        h.observe = lambda: None                     # no process behind the fake session: observe would see it gone
        h.tick()
        kinds = [e["type"] for e in journal.read(h.journal.path)]
        self.assertIn("core.slept", kinds)
        self.assertNotIn("timer.fired", kinds)      # the deadline moved before fire_timers looked at it
        self.assertEqual(h.state["messages"][mid]["attempt"], 1)


if __name__ == "__main__":
    unittest.main()
