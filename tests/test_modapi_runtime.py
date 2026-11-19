"""The state of the core's modules — a fold of module.* facts (core/modrun.py): one for all processes, one notification
per transition; a hub without modules keeps the same state as before; the core does not start on a journal with
core.write records it does not understand (Module API spec, C1)."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest

from core import modrun, modules, reducer
from core.daemon import Hub

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = {"channel": "cli", "owner": True}


class Fold(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []

    def put(self, event, ts, **data):
        self.seq += 1
        self.st, actions, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts, "type": event, "entity": None,
                                                         "data": data})
        self.actions += actions
        return why

    def error(self, ts, **extra):
        return self.put("module.error", ts, **{"module": "m", "hook": "doctor", "kind": "timeout",
                                               "error": "no answer", **extra})

    def rec(self):
        return self.st["modules"]["m"]

    def notices(self):
        return [a for a in self.actions if a["do"] == "owner_notice"]

    def test_three_failures_in_five_minutes_pause_for_ten(self):
        self.error(100)
        self.error(200)
        self.assertEqual(self.rec()["state"], "ok")
        self.error(300)
        self.assertEqual(self.rec()["state"], "suspended")
        self.assertEqual(self.rec()["until"], 300 + modrun.PAUSE)
        self.assertEqual(modrun.status(self.rec(), 500), "suspended")
        self.assertEqual(modrun.status(self.rec(), 300 + modrun.PAUSE), "ok")
        self.assertEqual(len(self.notices()), 1)

    def crash(self, ts, launch, calls=4):
        """The launch process crashed: calls before hub-up noticed the exit are connection failures, then exit."""
        for i in range(calls):
            self.error(ts + i * 0.2, hook="http", kind="connect", error="the process does not accept connections",
                      launch=launch)
        self.error(ts + 1, hook="process", kind="exit", error="the process exited with code 1", launch=launch, uptime=60)

    def test_one_crash_is_one_failure_however_many_calls_hit_it(self):
        self.crash(100, "L1")
        self.assertEqual((self.rec()["state"], len(self.rec()["errors"])), ("ok", 1))
        self.assertEqual(self.rec()["last"]["kind"], "exit")   # what came last is visible, as before

    def test_three_crashes_of_three_launches_still_pause(self):
        for i, launch in enumerate(("L1", "L2", "L3")):
            self.crash(100 + 10 * i, launch)
        self.assertEqual((self.rec()["state"], self.rec()["suspensions"]), ("suspended", 1))

    def test_other_failures_of_the_same_launch_still_count(self):
        self.crash(100, "L1", calls=1)
        self.error(110, launch="L1")
        self.error(120, launch="L1")
        self.assertEqual(self.rec()["state"], "suspended")

    def test_a_call_cut_by_the_crash_is_the_same_failure_but_a_live_process_cutting_calls_counts(self):
        self.error(99, hook="fold", kind="closed", error="the connection dropped", launch="L1")   # crashed in the middle of a call
        self.crash(100, "L1")
        self.assertEqual((self.rec()["state"], len(self.rec()["errors"])), ("ok", 1))
        for ts in (200, 201, 202):   # a live process cuts calls off without crashing: each one is a failure
            self.error(ts, hook="fold", kind="closed", error="the process closed the connection without a reply", launch="L2")
        self.assertEqual(self.rec()["state"], "suspended")

    def test_a_live_process_that_stopped_accepting_is_paused_within_minutes(self):
        # called once every 5 s while the process is alive (hub-up sees no exit): a failure once per CRASH_SPAN, a pause — beyond WINDOW
        for t in range(100, 100 + modrun.WINDOW, 5):
            self.error(t, hook="fold", kind="connect", error="the process does not accept connections", launch="L1")
        self.assertEqual(self.rec()["state"], "suspended")

    def test_an_old_cut_call_does_not_swallow_a_later_crash_of_the_same_launch(self):
        self.error(100, hook="fold", kind="closed", error="the connection dropped", launch="L1")
        self.crash(100 + modrun.CRASH_SPAN, "L1")
        self.assertEqual(len(self.rec()["errors"]), 2)

    def test_failures_without_a_launch_count_as_before(self):
        for ts in (100, 101, 102):
            self.error(ts, hook="http", kind="connect", error="no connection")
        self.assertEqual(self.rec()["state"], "suspended")

    def test_failures_far_apart_do_not_add_up(self):
        for ts in (0, 400, 800, 1200):
            self.error(ts)
        self.assertEqual(self.rec()["state"], "ok")

    def test_calls_in_flight_during_a_pause_do_not_count(self):
        for ts in (0, 1, 2):
            self.error(ts)
        for ts in (3, 4, 5):
            self.error(ts)
        self.assertEqual(self.rec()["suspensions"], 1)

    def test_the_fifth_pause_in_a_row_stops_it_until_reset(self):
        t = 0
        for _ in range(modrun.MAX_SUSPENSIONS):
            for _ in range(3):
                self.error(t)
                t += 1
            t += modrun.PAUSE
        self.assertEqual(self.rec()["state"], "error")
        self.assertEqual(modrun.status(self.rec(), t + 10 ** 6), "error")
        texts = [a["text"] for a in self.notices()]
        self.assertEqual(len(texts), 2)                     # the first pause and the stop — not every pause
        self.assertIn("hub modules reset m", texts[-1])
        self.assertIsNotNone(self.put("module.reset", t, module="m", channel="session", owner=False, sender="x"))
        self.assertIsNotNone(self.put("module.reset", t, module="m", channel="plugin", owner=False, sender="mod:m"))
        self.assertIsNone(self.put("module.reset", t, module="m", **OWNER))
        self.assertEqual(self.rec()["state"], "ok")
        self.assertEqual(self.rec()["suspensions"], 0)

    def test_boot_hiccups_hours_apart_never_add_up_to_a_stop(self):
        # a slow minute at every hub start: 3 failures, a pause, a quiet hour or two, the next start. Five of them in a
        # day are not "five pauses in a row": the stop (until reset) is for a module that fails again right after each pause
        t = 0
        for _ in range(modrun.MAX_SUSPENSIONS + 1):
            for i in range(3):
                self.error(t + i)
            self.assertEqual((self.rec()["state"], self.rec()["suspensions"]), ("suspended", 1))
            t += modrun.PAUSE + 2 * 3600
        self.assertEqual(modrun.status(self.rec(), t), "ok")

    def test_failures_right_after_each_pause_still_stop_it(self):
        t = 0
        for _ in range(modrun.MAX_SUSPENSIONS):
            for i in range(3):
                self.error(t + i)
            t += modrun.PAUSE + modrun.CALM // 2   # a quiet spell shorter than CALM does not break the row
        self.assertEqual(self.rec()["state"], "error")

    def test_a_process_that_exits_before_it_is_ready_is_blocked_at_once(self):
        self.error(10, kind="exit", hook="process", launch="L1", uptime=0.5)
        self.assertEqual(self.rec()["state"], "blocked")
        self.assertIn("did not start", self.rec()["reason"])
        self.assertEqual(len(self.notices()), 1)

    def test_a_ready_process_that_exits_is_a_failure_and_not_ready(self):
        self.assertIsNone(self.put("module.ready", 5, module="m", version="1.0.0", launch="L1", points=["cli"],
                                   channel="core"))
        self.error(10, kind="exit", hook="process", launch="L1", uptime=500)
        self.assertEqual(self.rec()["state"], "ok")
        self.assertNotIn("ready", self.rec())
        self.assertEqual(len(self.rec()["errors"]), 1)

    def test_ready_comes_only_from_the_core_and_clears_a_block(self):
        self.assertIsNotNone(self.put("module.ready", 1, module="m", version="1", launch="L", channel="cli"))
        self.error(2, kind="hello", hook="hello")
        self.assertEqual(self.rec()["state"], "blocked")
        self.put("module.ready", 3, module="m", version="1.0.0", launch="L2", points=[], channel="core")
        self.assertEqual(self.rec()["state"], "ok")
        self.assertNotIn("reason", self.rec())

    def test_bad_names_and_types_are_rejected(self):
        self.assertIsNotNone(self.put("module.error", 1, module="../x", kind="timeout"))
        self.assertIsNotNone(self.put("module.whatever", 1, module="m"))
        self.assertIsNotNone(self.put("module.reset", 1, module="nope", **OWNER))
        self.assertNotIn("modules", self.st)

    def test_a_modules_own_facts_are_recorded_and_change_nothing(self):
        self.assertIsNotNone(self.put("mod.hello.seen", 1, n=2))   # did not answer hello — not loaded
        self.put("module.ready", 1, module="hello", version="1.0.0", launch="L", points=[], channel="core")
        before = reducer.copy_state(self.st)
        self.assertIsNone(self.put("mod.hello.seen", 1, n=2))
        self.assertEqual({k: v for k, v in self.st.items() if k != "seq"}, {k: v for k, v in before.items() if k != "seq"})
        self.assertIsNotNone(self.put("mod.hello", 1))
        self.assertIsNotNone(self.put("mod.Bad_Name.x", 1))

    def test_restart_of_a_module_part(self):
        # a module that never started (or an unknown one) and one stopped after failures — a refusal with a hint, not an empty «accepted»
        self.assertIn("hub modules", self.put("hub.restart_requested", 1, parts=["mod:m"], **OWNER))
        self.put("module.ready", 2, module="m", version="1.0.0", launch="L", points=[], channel="core")
        self.assertIsNone(self.put("hub.restart_requested", 3, parts=["mod:m"], **OWNER))
        self.assertEqual(self.st["restart_parts"], ["mod:m"])
        self.st["modules"]["m"]["state"] = "error"
        self.assertIn("hub modules reset m", self.put("hub.restart_requested", 4, parts=["mod:m"], **OWNER))


class WithoutModules(unittest.TestCase):
    def test_a_hub_without_the_section_keeps_its_state_and_writes_nothing_new(self):
        state = tempfile.mkdtemp(prefix="nomods-")
        h = Hub(state, types.SimpleNamespace(), registry_dir=state)
        h.execute = lambda a: None
        h.record("session.requested", "a", {"cwd": "/w", "session_id": "s-a", **OWNER})
        h.load_catalog()
        for _ in range(3):
            h.modules.tick()
        h.record("core.started", data={"pid": 1})
        h.save_state()
        self.assertFalse(os.path.exists(os.path.join(state, "modrun")))
        self.assertNotIn("modules", h.state)
        self.assertNotIn("core_written", h.state)
        with open(os.path.join(state, "journal.jsonl"), encoding="utf-8") as f:
            types_ = [json.loads(x)["type"] for x in f if x.strip()]
        self.assertFalse([t for t in types_ if t.startswith(("module.", "mod."))], types_)


class CoreWritten(unittest.TestCase):
    def test_marks_this_code_does_not_understand(self):
        d = tempfile.mkdtemp(prefix="cw-")
        self.assertIsNone(modules.core_write_refusal(d, reducer.empty()))
        # format 1 — the core.write records of this code (core/corewrite.py); a newer one — a refusal
        self.assertIsNone(modules.core_write_refusal(d, {"core_written": {"format": 1, "modules": {"m": {}}}}))
        self.assertIsNotNone(modules.core_write_refusal(d, {"core_written": {"format": 2, "modules": {"m": {}}}}))
        self.assertIsNotNone(modules.core_write_refusal(d, {"core_written": {"m": {"first": 1, "last": 2}}}))
        self.assertIsNone(modules.core_write_refusal(d, {"core_written": {"format": 0}}))
        with open(os.path.join(d, modules.CORE_WRITTEN), "w") as f:
            json.dump({"format": 1, "modules": {"m": {"first_seq": 1}}}, f)
        self.assertIsNone(modules.core_write_refusal(d))
        with open(os.path.join(d, modules.CORE_WRITTEN), "w") as f:
            json.dump({"format": 2, "modules": {"m": {"first_seq": 1}}}, f)
        why = modules.core_write_refusal(d)
        self.assertIn("core.write", why)
        self.assertIn("(m)", why)
        with open(os.path.join(d, modules.CORE_WRITTEN), "w") as f:
            f.write("garbage")
        self.assertIsNotNone(modules.core_write_refusal(d))   # an unreadable file — also a refusal

    def test_hubd_refuses_to_start(self):
        d = tempfile.mkdtemp(prefix="cw-hubd-")
        with open(os.path.join(d, "state.json"), "w") as f:
            json.dump({"seq": 0, "offset": 0, "first": None, "state": {"core_written": {"m": {"first": 1}}}}, f)
        out = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "hubd")], env={**os.environ,
                             "HUB_STATE_DIR": d}, capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("core.write", out.stderr)
        self.assertFalse(os.path.exists(os.path.join(d, "journal.jsonl")))


if __name__ == "__main__":
    unittest.main()
