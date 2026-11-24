"""The Mac between sleeps (macOS DarkWake: a window of 2-45 s every ~15 minutes, the network often silent in it). A
turn started in such a window sends its request, the Mac sleeps again, every next window burns one retry, and after
ten of them (3-4 hours) the turn fails with "Request timed out" (2026-11-22: nine such turns, six of them schedule
jobs). The core learns about the dark wake from a fact (core.dark, hubd asks the OS) and starts nothing new in it:
the queue is not typed, spawns and schedule commands wait, and all of it goes once at the full wake. The watchdog
does not count the sleep and the dark wake as waiting."""
import tests  # noqa: F401 — do not see this machine's settings
import os
import shutil
import tempfile
import time
import unittest

from core import journal, reducer, spool
from core.daemon import DARK_MAX, DARK_POLL, Hub
from modules.scheduler.clock import Clock
from modules.watch import watch
import platforms
from platforms import posix, windows

OWNER = {"channel": "cli", "owner": True}
SLEEP = 15 * 60
TIMED_OUT = "API Error: Request timed out."


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

    def idle(self):
        self.ev("session.requested", "a", cwd="/w", session_id="s-a", **OWNER)
        self.ev("session.started", "a")
        return self

    def dark(self, on):
        return self.ev("core.dark", on=on)

    def msgs(self):
        return list(self.st["messages"].values())


class Queue(unittest.TestCase):
    def test_a_schedule_job_in_a_dark_wake_waits_for_the_full_wake_and_goes_once(self):
        r = Replay().idle()
        r.ev("schedule.set", "x", session="a", every="30m", prompt="check the build", **OWNER)
        slot = r.st["schedule"]["x"]["start"]
        r.at(slot + 5).ev("core.slept", seconds=SLEEP)
        self.assertIsNone(r.dark(True))
        self.assertIsNone(r.ev("schedule.due", "x", slot=slot))
        (m,) = r.msgs()
        self.assertEqual((m["state"], r.typed), ("queued", []))       # the slot came, the message waits
        r.at(slot + 3 * SLEEP).ev("core.slept", seconds=2 * SLEEP)    # more windows: still nothing typed
        r.ev("session.requested", "b", cwd="/w", session_id="s-b", **OWNER)   # any other event pumps the queue
        self.assertEqual(r.typed, [])
        r.at(slot + 3 * SLEEP + 30).dark(False)
        self.assertEqual([t["message"] for t in r.typed], [m["id"]])   # the full wake: once
        r.ev("core.dark", on=False)
        r.ev("schedule.due", "x", slot=slot)                            # the clock repeats the slot: no second message
        self.assertEqual([t["message"] for t in r.typed], [m["id"]])
        self.assertEqual(len(r.msgs()), 1)

    def test_the_owner_s_message_in_a_dark_wake_waits_too(self):
        r = Replay().idle()
        r.dark(True)
        r.ev("message.received", None, to="a", text="round", **OWNER)
        self.assertEqual(r.typed, [])
        r.at(1100).dark(False)
        self.assertEqual(len(r.typed), 1)

    def test_a_typed_message_whose_term_runs_out_in_a_dark_wake_goes_back_to_the_queue(self):
        r = Replay().idle()
        r.ev("message.received", None, to="a", text="round", **OWNER)
        (m,) = r.msgs()
        self.assertEqual((m["state"], m["attempt"]), ("typing", 1))
        deadline = m["deadline"]
        r.at(deadline - 1).dark(True)
        r.at(deadline + 1).ev("timer.fired", m["id"], attempt=1)
        (m,) = r.msgs()
        self.assertEqual((m["state"], len(r.typed)), ("queued", 1))    # not retyped into the silent network
        r.at(deadline + 60).dark(False)
        (m,) = r.msgs()
        self.assertEqual((m["state"], len(r.typed)), ("typing", 2))    # once, at the full wake, not lost

    def test_an_urgent_message_to_a_busy_session_waits_too(self):
        r = Replay().idle()
        r.ev("message.received", None, to="a", text="round", **OWNER)
        r.ev("turn.started", "a", marker=r.msgs()[0]["id"])
        r.dark(True)
        r.ev("message.received", None, to="a", text="stop that", now=True, **OWNER)
        urgent = r.msgs()[1]
        self.assertEqual((urgent["state"], urgent.get("urgent")), ("queued", True))   # not into the hung turn
        r.at(1100).dark(False)
        self.assertEqual(r.msgs()[1]["state"], "delivered")                          # mid-turn at the full wake
        self.assertTrue(r.msgs()[1].get("injected"))

    def test_a_queue_without_a_dark_wake_types_at_once_as_before(self):
        r = Replay().idle()
        r.ev("core.slept", seconds=SLEEP)
        r.ev("message.received", None, to="a", text="round", **OWNER)
        self.assertEqual(len(r.typed), 1)

    def test_a_malformed_dark_fact_changes_nothing(self):
        r = Replay().idle()
        for bad in (None, "yes", 1, 0):
            r.ev("core.dark", on=bad)
        self.assertNotIn("dark", r.st)
        r.ev("message.received", None, to="a", text="round", **OWNER)
        self.assertEqual(len(r.typed), 1)


class NetCut(unittest.TestCase):
    """The turn the network cut is continued by the core (net_cut); in a dark wake the continuation waits."""

    def busy(self):
        r = Replay().idle()
        r.ev("message.received", None, to="a", text="round", **OWNER)
        r.ev("turn.started", "a", marker=r.msgs()[0]["id"])
        return r

    def test_a_turn_cut_while_the_mac_slept_continues_once_after_the_full_wake(self):
        r = self.busy()
        r.at(1000 + SLEEP).ev("core.slept", seconds=SLEEP)          # it fell asleep mid-turn
        r.dark(True)
        r.at(1000 + 4 * SLEEP).ev("turn.result", "a", text=TIMED_OUT, is_error=True, turns=3)
        cut = r.st["sessions"]["a"]["net_cut"]
        r.at(cut["due"] + 1).ev("timer.fired", reducer.NET_TIMER + "a")   # the probe passed in a window
        go = [m for m in r.msgs() if m["text"].startswith(reducer.NET_CUT[:30])]
        self.assertEqual(len(go), 1)
        self.assertEqual((go[0]["state"], len(r.typed)), ("queued", 1))   # not typed into the next hang
        r.at(cut["due"] + 600).dark(False)
        self.assertEqual([t["message"] for t in r.typed[1:]], [go[0]["id"]])
        r.ev("timer.fired", reducer.NET_TIMER + "a")                     # a stale timer: no second continuation
        self.assertEqual(len([m for m in r.msgs() if m["text"].startswith(reducer.NET_CUT[:30])]), 1)

    def test_a_timeout_without_a_sleep_continues_as_before(self):
        r = self.busy()
        r.at(1100).ev("turn.result", "a", text=TIMED_OUT, is_error=True, turns=3)
        cut = r.st["sessions"]["a"]["net_cut"]
        r.at(cut["due"] + 1).ev("timer.fired", reducer.NET_TIMER + "a")
        self.assertEqual(len(r.typed), 2)                               # the continuation is typed at once


class Paused(unittest.TestCase):
    def test_a_night_of_sleeps_and_dark_wakes_is_one_pause(self):
        r = Replay()
        r.at(2000).ev("core.slept", seconds=900)                          # [1100, 2000]
        r.dark(True)                                                      # 2000 …
        for k in range(1, 4):
            r.at(2000 + k * 900).ev("core.slept", seconds=880)            # inside the open dark span
        self.assertEqual(reducer.paused(r.st, 1000, 5000), 5000 - 1100)   # still dark: until now
        r.at(4800).dark(False)
        self.assertEqual(r.st["pauses"], [[1100, 4800]])
        self.assertEqual(reducer.paused(r.st, 1000, 6000), 4800 - 1100)
        self.assertEqual(reducer.paused(r.st, 3000, 6000), 4800 - 3000)   # only the part after since
        self.assertEqual(reducer.paused(r.st, 5000, 6000), 0)

    def test_old_pauses_are_forgotten(self):
        r = Replay()
        r.at(2000).ev("core.slept", seconds=900)
        r.at(2000 + reducer.PAUSES_KEEP + 5000).ev("core.slept", seconds=900)
        self.assertEqual(len(r.st["pauses"]), 1)


class Daemon(unittest.TestCase):
    def hub(self, dark):
        self.wall, self.awake = [1000.0], [50.0]
        h = Hub(tempfile.mkdtemp(prefix="dark-"), type("H", (), {"alive": lambda s, n: True})(),
                registry_dir=tempfile.mkdtemp(), clock=lambda: self.wall[0], mono=lambda: self.awake[0], dark=dark)
        self.events = []
        real = h.record
        h.record = lambda type, *a, **k: self.events.append((type, k.get("data"))) or real(type, *a, **k)
        return h

    def pass_(self, h, wall, awake):
        self.wall[0] += wall
        self.awake[0] += awake
        h.check_slept()

    def darks(self):
        return [d["on"] for t, d in self.events if t == "core.dark"]

    def test_a_dark_wake_is_recorded_after_a_sleep_and_its_end_once_the_person_wakes_it(self):
        os_dark, asked = [False], []
        h = self.hub(lambda: asked.append(1) or os_dark[0])
        self.pass_(h, 0.3, 0.3)                     # the first pass asks once
        self.pass_(h, 0.3, 0.3)
        self.assertEqual((self.darks(), len(asked)), ([], 1))   # awake: not asked again
        os_dark[0] = True
        self.pass_(h, SLEEP, 5)                      # woke in a dark window
        self.assertEqual(self.darks(), [True])
        self.assertIn("dark", h.state)
        n = len(asked)
        self.pass_(h, 1, 1)
        self.assertEqual(len(asked), n)              # not every pass
        os_dark[0] = False
        self.pass_(h, DARK_POLL, DARK_POLL)          # the person opened the lid without a sleep in between
        self.assertEqual(self.darks(), [True, False])
        self.assertNotIn("dark", h.state)

    def test_a_dark_wake_longer_than_any_is_taken_as_awake(self):
        # a Mac whose full wake reports no graphics would otherwise stand still: nothing typed, spawned or launched
        h = self.hub(lambda: True)
        self.pass_(h, 0.3, 0.3)
        self.assertEqual(self.darks(), [True])
        for _ in range(DARK_MAX // DARK_POLL + 1):
            self.pass_(h, DARK_POLL, DARK_POLL)
        self.assertEqual(self.darks(), [True, False])
        self.pass_(h, DARK_POLL, DARK_POLL)
        self.assertEqual(self.darks(), [True, False])   # not asked again while awake
        self.pass_(h, SLEEP, 5)                           # a real sleep: asked anew, dark again for a while
        self.assertEqual(self.darks(), [True, False, True])

    def test_a_night_of_short_windows_is_not_one_long_dark_wake(self):
        # review: the awake seconds of separate windows add up past DARK_MAX over a weekend; each window counts alone
        h = self.hub(lambda: True)
        self.pass_(h, 0.3, 0.3)
        for _ in range(3 * DARK_MAX // 45):
            self.pass_(h, SLEEP, 40)                       # a sleep, then a 45 s window polled on the way
            self.pass_(h, 5, 5)
        self.assertEqual(self.darks(), [True])

    def test_a_failing_question_is_awake(self):
        h = self.hub(lambda: 1 / 0)
        self.pass_(h, 0.3, 0.3)
        self.pass_(h, SLEEP, 5)
        self.assertEqual(self.darks(), [])

    def test_a_hub_on_substituted_clocks_never_asks_the_os(self):
        h = Hub(tempfile.mkdtemp(prefix="dark-"), type("H", (), {"alive": lambda s, n: True})(),
                registry_dir=tempfile.mkdtemp(), clock=lambda: 1000.0, mono=lambda: 50.0)
        self.assertFalse(h.dark_wake())

    def test_linux_and_windows_have_no_dark_wake_and_record_nothing(self):
        ran = []

        def run(*a, **k):
            ran.append(a)
            raise AssertionError("no OS question off a Mac")
        for dark in (lambda: posix.dark_wake(plat="linux", run=run), lambda: windows.dark_wake(plat="win32", run=run)):
            h = self.hub(dark)
            for _ in range(3):
                self.pass_(h, 0.3, 0.3)
                self.pass_(h, SLEEP, 5)
            self.assertEqual(self.darks(), [])
            self.assertEqual([t for t, _ in self.events if t == "core.slept"], ["core.slept"] * 3)   # sleeps as before
        self.assertEqual(ran, [])


class Spawn(unittest.TestCase):
    def test_a_spawn_in_a_dark_wake_waits_and_goes_once_at_the_full_wake(self):
        state = tempfile.mkdtemp(prefix="dark-spawn-")
        self.addCleanup(platforms.current.rmtree, state)
        spawned = []

        class Host:
            def alive(self, name):
                return False

            def spawn(self, name, **kw):
                spawned.append((name, kw.get("cwd")))
                assert "claude_token" not in kw, kw
                return "spawned"
        wall = [1000.0]
        h = Hub(state, Host(), registry_dir=os.path.join(state, "registry"), clock=lambda: wall[0],
                mono=lambda: wall[0], dark=lambda: True)
        h.record("core.dark", data={"on": True})
        h.record("session.requested", "a", {"cwd": state, "session_id": "s-a", **OWNER})
        self.assertEqual(h.state["sessions"]["a"]["state"], "starting")
        self.assertEqual(spawned, [])
        self.assertIn("a", h.waiting())
        for _ in range(3):
            wall[0] += 10
            h.run_delayed()
        self.assertEqual(spawned, [])
        (held,) = [a for _, a in h.delayed if a["session"] == "a"]
        later = {k: v for k, v in held.items() if k != "claude_token"}
        h.execute({**later, "cwd": state + "/later"})   # a later spawn of the same session (an account, a reload)
        wall[0] += 10
        h.run_delayed()                                 # the older one comes due first: it is dropped, not the later
        self.assertEqual([a["cwd"] for _, a in h.delayed if a["session"] == "a"], [state + "/later"])
        h.record("core.dark", data={"on": False})
        wall[0] += 10
        h.run_delayed()
        self.assertEqual(spawned, [("a", state + "/later")])
        wall[0] += 10
        h.run_delayed()
        self.assertEqual(len(spawned), 1)


class InATick(unittest.TestCase):
    def test_a_dark_wake_is_known_before_the_tick_takes_in_what_came_during_the_sleep(self):
        # the order of the tick: check_slept (and the OS question) before every step, the inbox in observe after it —
        # a schedule message spooled at the wake is held, not typed into the silent network
        from hosts.streamjson import StreamHost
        state = tempfile.mkdtemp(prefix="dark-tick-")
        self.addCleanup(platforms.current.rmtree, state)
        wall, awake, os_dark = [1000.0], [50.0], [False]
        host = StreamHost(state, claude=("claude-not-run",))
        host.clock = lambda: wall[0]
        h = Hub(state, host, registry_dir=os.path.join(state, "registry"), clock=lambda: wall[0],
                mono=lambda: awake[0], dark=lambda: os_dark[0])
        done = []
        h.execute = lambda action: done.append(action["do"])
        h.record("session.requested", "a", {"cwd": state, "session_id": "s-a", **OWNER})
        h.record("session.started", "a", {"session_id": "s-a"})
        h.check_slept()                              # the baseline pass
        done.clear()

        def asleep():                                # the machine slept inside this step and woke in a dark window
            wall[0] += SLEEP
            awake[0] += 10
            os_dark[0] = True
            spool.put(h.spool_dir, "message.received", None, {"to": "a", "text": "round", **OWNER}, src="t")
        h.beat = asleep                              # the step right before observe takes in the inbox
        h.observe = h.collect                        # no process behind the fake session: observe would see it gone
        h.tick()
        kinds = [e["type"] for e in journal.read(h.journal.path)]
        self.assertLess(kinds.index("core.dark"), kinds.index("message.received"))
        (m,) = h.state["messages"].values()
        self.assertEqual((m["state"], [d for d in done if d == "type"]), ("queued", []))
        os_dark[0] = False
        awake[0] += DARK_POLL
        h.tick()
        self.assertEqual(h.state["messages"][m["id"]]["state"], "typing")
        self.assertEqual([d for d in done if d == "type"], ["type"])


@unittest.skipIf(os.name == "nt" and not shutil.which("sh"), "a command job needs a shell")
class ClockTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="dark-sched-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.now = 1_800_000_000.0
        self.clock = Clock(self.state, clock=lambda: self.now, awake=lambda: self.now)
        self.j.append("owner.settings", data={"timezone": "UTC"}, ts=self.now)

    def beat(self):
        path = os.path.join(self.state, "hubd.alive")
        open(path, "w").close()
        os.utime(path, (self.now, self.now))

    def spooled(self):
        d = os.path.join(self.state, "spool")
        items = [e for _, e in spool.take(d)]
        for n in os.listdir(d) if os.path.isdir(d) else []:
            os.unlink(os.path.join(d, n))
        return items

    def test_a_command_and_a_pre_check_wait_for_the_full_wake(self):
        self.j.append("schedule.set", "gate", {"every": "5m", "command": "true", **OWNER}, ts=self.now)
        self.j.append("session.requested", "a", {"cwd": self.state, "session_id": "s-a", **OWNER}, ts=self.now)
        self.j.append("schedule.set", "w", {"session": "a", "every": "5m", "prompt": "p", "check": "true", **OWNER},
                      ts=self.now)
        self.now += 300
        self.beat()
        self.j.append("core.dark", data={"on": True}, ts=self.now)
        self.j.append("schedule.due", "gate", {"slot": self.now}, ts=self.now)   # the slot came: the core marked a launch
        for _ in range(3):
            self.clock.step()
        self.assertEqual(self.clock.running, {})
        self.assertEqual([e["type"] for e in self.spooled() if e["type"] != "schedule.due"], [])   # no start, no check
        self.j.append("core.dark", data={"on": False}, ts=self.now + 30)
        self.now += 30
        self.beat()
        self.clock.step()
        got = sorted((e["type"], e["entity"]) for e in self.spooled())
        self.assertIn(("schedule.started", "gate"), got)                   # the command goes now
        self.assertIn("w", self.clock.running)                             # and the pre-check
        for _ in range(100):
            if not self.clock.running:
                break
            time.sleep(0.05)
            self.clock.step()


class Watchdog(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="dark-watch-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.j.append("session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **OWNER}, ts=100)
        self.j.append("session.requested", "a", {"cwd": "/w", "session_id": "s-a", **OWNER}, ts=100)
        self.j.append("session.started", "hub", {}, ts=101)
        self.j.append("session.started", "a", {}, ts=101)
        self.w = watch.Watcher(self.state, {}, ask=lambda t: {"trouble": False}, needs=None, urgent=None,
                               recap=lambda text: "")
        self.assertEqual(self.step(110), [])

    def step(self, now, awake=None):
        self.w.step(now) if awake is None else self.w.step(now, awake)
        return [e["data"]["kind"] for path, e in spool.take(os.path.join(self.state, "spool"))
                if not os.unlink(path) and e.get("type") == "watch.alert"]

    def test_a_message_held_over_a_night_of_sleeps_is_not_undelivered(self):
        self.j.append("core.dark", data={"on": True}, ts=200)
        self.j.append("message.received", None, {"to": "a", "text": "round", **OWNER}, ts=210)
        self.assertEqual(self.step(210 + 5 * SLEEP), [])                  # dark: the queue waits on purpose
        self.j.append("core.slept", data={"seconds": SLEEP}, ts=210 + 6 * SLEEP)
        self.j.append("core.dark", data={"on": False}, ts=210 + 6 * SLEEP + 5)
        self.assertEqual(self.step(210 + 6 * SLEEP + 6), [])               # the full wake: the hub has not waited
        self.assertEqual(self.step(210 + 6 * SLEEP + 5 + watch.UNDELIVERED + 1), ["undelivered"])   # then it does

    def test_a_sleep_without_a_dark_wake_is_not_waiting_either(self):
        self.j.append("message.received", None, {"to": "a", "text": "round", **OWNER}, ts=200)
        self.j.append("core.slept", data={"seconds": SLEEP}, ts=200 + SLEEP + 10)
        self.assertEqual(self.step(200 + SLEEP + 20), [])                  # 16 min on the wall, 20 s of the hub's

    def test_the_first_pass_after_a_sleep_judges_nothing(self):
        self.step(120, awake=50)
        self.j.append("message.received", None, {"to": "a", "text": "round", **OWNER}, ts=130)
        # woke up: hubd's core.slept is not in the journal yet
        self.assertEqual(self.step(130 + SLEEP, awake=60), [])
        self.assertEqual(self.step(150 + SLEEP, awake=80), [])     # hubd is still in a long step (git)
        self.j.append("core.slept", data={"seconds": SLEEP - 10}, ts=150 + SLEEP)
        self.assertEqual(self.step(152 + SLEEP, awake=82), [])
        self.assertEqual(self.step(130 + SLEEP + 70 + watch.UNDELIVERED, awake=130 + watch.UNDELIVERED),
                         ["undelivered"])                             # its own waiting still counts

    def test_nothing_is_judged_nor_asked_in_a_dark_wake(self):
        asked = []
        self.w.ask = lambda t: asked.append(t) or {"trouble": False}
        self.j.append("core.dark", data={"on": True}, ts=200)
        self.j.append("turn.started", "a", {"marker": None, "prompt_head": "fix the test"}, ts=201)
        for k in range(40):
            self.j.append("turn.tool", "a", {"id": f"t{k}", "name": "Bash", "input": {"command": "npm test"}}, ts=202 + k)
        self.assertEqual(self.step(202 + watch.WINDOW_SECONDS + 60), [])
        self.assertEqual(asked, [])


if __name__ == "__main__":
    unittest.main()
