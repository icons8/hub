"""A module process and core-to-module calls (Module API, slice S2, core/modrun.py): the module crashes, stays silent or
answers garbage; the core gets None within the budget, writes module.error and carries on; three failures in 5 minutes
suspend the module and it is no longer called; a module that did not start or did not answer hello is blocked at once,
and so are its dependents."""
import tests  # noqa: F401 — keeps the machine's settings out
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from core import modrun, version
from tests.fakes.modstand import Stand


def answer_dict(res, ctx):
    if not isinstance(res, dict):
        raise modrun.BadResult("object")
    return res


# a point to check the call: the real points and their schemas come with later slices
POINT = {"doctor": {"budget": 1.0, "cap": "doctor", "check": answer_dict}}


@mock.patch.dict(modrun.POINTS, POINT)
class Calls(unittest.TestCase):
    def stand(self, **kw):
        s = Stand(self, points=["doctor"], caps=["doctor"], **kw)
        s.launch()
        s.until_ready()
        return s

    def call(self, s, payload=None):
        return s.hub.modules.mods.call("fake", "doctor", payload or {"x": 1})

    def test_hello_makes_the_module_ready_with_its_points(self):
        s = self.stand()
        ready = s.events("module.ready")
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["data"]["points"], ["doctor"])
        self.assertEqual(ready[0]["data"]["version"], "1.0.0")
        hello = s.calls("hello")[0]
        self.assertTrue(hello["secret_ok"])            # the core came with this launch's secret
        self.assertEqual(hello["body"]["api"], version.MODULE_API)
        # only the points this hub calls on a module of its version (api ^2.2: no 2.8 points)
        self.assertEqual(hello["body"]["points"], modrun.offered("^2.2"))
        self.assertNotIn("profile.token", hello["body"]["points"])
        self.assertIn("doctor", hello["body"]["points"])

    def test_an_answer_in_budget_comes_back(self):
        s = self.stand()
        s.update(doctor={"do": "ok", "answer": {"checks": []}})
        self.assertEqual(self.call(s, {"q": "hello"}), {"checks": []})
        self.assertEqual(s.calls("doctor")[0]["body"], {"q": "hello"})
        self.assertEqual(s.events("module.error"), [])

    def test_silence_garbage_errors_and_size_are_none_and_a_fact(self):
        s = self.stand()
        for do, kind in (("silent", "timeout"), ("garbage", "schema"), ("status", "status"), ("big", "size")):
            s.update(doctor={"do": do})
            began = time.monotonic()
            self.assertIsNone(self.call(s), do)
            self.assertLess(time.monotonic() - began, 1.0 + 0.8, do)   # no longer than the point's budget
            self.assertEqual(s.events("module.error")[-1]["data"]["kind"], kind, do)
            s.hub.state["modules"]["fake"].update(errors=[], state="ok", until=None, suspensions=0)
            s.hub.modules.mods.local.clear()

    def test_an_answer_out_of_schema_is_a_failure(self):
        s = self.stand()
        s.update(doctor={"do": "ok", "answer": ["not an object"]})
        self.assertIsNone(self.call(s))
        self.assertEqual(s.events("module.error")[-1]["data"]["kind"], "schema")

    def test_the_manifest_budget_is_shorter(self):
        s = Stand(self, points=["doctor"], caps=["doctor"], budget={"doctor": 0.2})
        s.launch()
        s.until_ready()
        s.update(doctor={"do": "slow", "delay": 0.6})
        began = time.monotonic()
        self.assertIsNone(self.call(s))
        self.assertLess(time.monotonic() - began, 0.55)

    def test_three_failures_suspend_the_module_and_it_is_not_called(self):
        s = self.stand()
        s.update(doctor={"do": "status"})
        for _ in range(3):
            self.assertIsNone(self.call(s))
        self.assertEqual(s.rec()["state"], "suspended")
        asked = len(s.calls("doctor"))
        s.update(doctor={"do": "ok", "answer": {}})
        self.assertIsNone(self.call(s))
        self.assertEqual(len(s.calls("doctor")), asked)    # a suspended module is not called
        notes = s.events("owner.notify")
        self.assertEqual(len(notes), 1)                    # one notification per transition
        self.assertIn("paused", notes[0]["data"]["text"])

    def test_one_slow_answer_between_good_ones_never_adds_up_to_a_pause(self):
        # a loaded machine: the point answers in time nine times out of ten and misses the budget once. Every miss
        # right after a good answer is "slow", not "stuck": it is written (hub modules shows it) and does not count
        s = Stand(self, points=["doctor"], caps=["doctor"], budget={"doctor": 0.2})
        s.launch()
        s.until_ready()
        for _ in range(4):
            s.update(doctor={"do": "ok", "answer": {}})
            self.assertEqual(self.call(s), {})
            s.update(doctor={"do": "slow", "delay": 0.6})
            self.assertIsNone(self.call(s))
        self.assertEqual([s.rec()["state"], s.rec()["suspensions"], s.rec()["errors"]], ["ok", 0, []])
        self.assertEqual([e["data"]["kind"] for e in s.events("module.error")], ["slow"] * 4)
        self.assertEqual(s.rec()["slow"]["hook"], "doctor")
        self.assertEqual(s.events("owner.notify"), [])

    def test_a_point_that_stays_silent_after_a_good_answer_still_pauses_the_module(self):
        s = Stand(self, points=["doctor"], caps=["doctor"], budget={"doctor": 0.2})
        s.launch()
        s.until_ready()
        s.update(doctor={"do": "ok", "answer": {}})
        self.assertEqual(self.call(s), {})
        s.update(doctor={"do": "slow", "delay": 0.6})
        for _ in range(4):   # the first miss is "slow" (the last answer was good), the next three are a stuck point
            self.assertIsNone(self.call(s))
        self.assertEqual(s.rec()["state"], "suspended")
        self.assertEqual([e["data"]["kind"] for e in s.events("module.error")], ["slow", "timeout", "timeout", "timeout"])

    def test_a_good_answer_forgives_only_its_own_point_launch_and_time(self):
        s = Stand(self, points=["doctor"], caps=["doctor"], budget={"doctor": 0.2})
        s.launch()
        s.until_ready()
        now, m = [1000.0], s.hub.modules.mods
        m.clock = lambda: now[0]
        m.good[("fake", "doctor")] = ("L1", now[0])
        self.assertTrue(m.fail("fake", "other", "timeout", "no answer", "L1"))     # another point: never answered
        self.assertEqual(s.events("module.error")[-1]["data"]["kind"], "timeout")
        m.good[("fake", "doctor")] = ("L1", now[0])
        m.fail("fake", "doctor", "timeout", "no answer", "L2")                      # a new launch of the module
        self.assertEqual(s.events("module.error")[-1]["data"]["kind"], "timeout")
        m.good[("fake", "doctor")] = ("L1", now[0])
        now[0] += modrun.SLOW_AFTER + 1                                              # an old answer
        m.fail("fake", "doctor", "timeout", "no answer", "L1")
        self.assertEqual(s.events("module.error")[-1]["data"]["kind"], "timeout")
        m.good[("fake", "doctor")] = ("L1", now[0])
        m.fail("fake", "doctor", "timeout", "no answer", "L1")
        self.assertEqual(s.events("module.error")[-1]["data"]["kind"], "slow")

    def test_a_slow_miss_is_kept_apart_and_hides_no_real_failure(self):
        rec = modrun.fold(None, {"hook": "doctor", "kind": "schema", "error": "not an object"}, 100)
        rec = modrun.fold(rec, {"hook": "profile.list", "kind": "slow", "error": "no answer within 2 s"}, 200)
        self.assertEqual((rec["last"]["kind"], rec["slow"]["hook"], rec["errors"]), ("schema", "profile.list", [100]))
        first = modrun.fold(None, {"hook": "profile.list", "kind": "slow", "error": "x"}, 300)
        self.assertEqual((first["last"]["kind"], first["state"]), ("slow", "ok"))   # status() reads last["ts"]

    def test_a_process_that_died_after_ready_is_a_failure_not_a_hang(self):
        s = self.stand()
        s.update(doctor={"do": "exit"})
        began = time.monotonic()
        self.assertIsNone(self.call(s))
        self.assertLess(time.monotonic() - began, 1.5)
        self.assertEqual(s.events("module.error")[-1]["data"]["kind"], "closed")

    def test_a_point_the_module_did_not_declare_is_not_called(self):
        s = Stand(self, points=[], caps=["doctor"])
        s.launch()
        s.until_ready()
        self.assertIsNone(self.call(s))
        self.assertEqual(s.calls("doctor"), [])
        self.assertEqual(s.events("module.error"), [])

    def test_a_module_that_is_not_ready_is_not_called(self):
        s = Stand(self, points=["doctor"], caps=["doctor"])
        self.assertIsNone(self.call(s))
        self.assertEqual(s.events("module.error"), [])

    def test_answering_lists_ready_modules_in_load_order(self):
        s = Stand(self, points=["doctor"], caps=["doctor"], extra=[{"name": "base", "caps": ["doctor"]}])
        # fake loads after base if it depends on it; here by name: base, fake
        s.launch("base")
        s.launch()
        s.until_ready("base")
        s.until_ready()
        self.assertEqual(s.hub.modules.mods.answering("doctor"), ["base", "fake"])

    def test_a_module_the_core_no_longer_keeps_is_not_called(self):
        # the module was switched off: hub-up stops the process, and calling it is no longer a failure
        s = self.stand()
        s.hub.modules.reload({"modules": {"paths": [s.root], "enabled": []}}, {})
        s.hub.modules.tick()
        self.assertIsNone(self.call(s))
        self.assertEqual(s.calls("doctor"), [])
        self.assertEqual(s.events("module.error"), [])

    def test_another_process_sees_readiness_after_the_last_state_file(self):
        # a hub command reads state.json and the journal tail: readiness written after state.json is visible
        s = Stand(self, points=["doctor"], caps=["doctor"])
        s.hub.record("core.started", data={"pid": 1})
        s.hub.save_state()
        s.launch()
        s.until_ready()
        s.update(doctor={"do": "ok", "answer": {"ok": True}})
        self.assertEqual(modrun.Modules(s.state_dir).call("fake", "doctor", {}), {"ok": True})

    def test_own_failures_count_only_until_the_journal_knows_them(self):
        now = [1000.0]
        recs = {"fake": {"state": "ok"}}
        m = modrun.Modules(tempfile.mkdtemp(), records=lambda: recs, emit=lambda *a: None, clock=lambda: now[0])
        for _ in range(3):
            m.fail("fake", "doctor", "timeout", "no answer")
        self.assertEqual(m.status("fake"), "suspended")
        now[0] += modrun.LOCAL_TTL + 1        # the journal already knows (and, say, the owner reset the module)
        self.assertEqual(m.status("fake"), "ok")

    def test_a_repeated_connection_failure_of_the_same_launch_is_not_written_again(self):
        sent, recs = [], {"fake": {"state": "ok"}}
        m = modrun.Modules(tempfile.mkdtemp(), records=lambda: recs, emit=lambda *a: sent.append(a))
        self.assertTrue(m.fail("fake", "fold", "connect", "no connection", "L1"))
        self.assertFalse(m.fail("fake", "http", "connect", "no connection", "L1"))
        self.assertTrue(m.fail("fake", "fold", "timeout", "no answer", "L1"))    # a different failure is written
        self.assertTrue(m.fail("fake", "fold", "connect", "no connection", "L2"))   # a new launch, a new crash
        self.assertEqual([a[2]["kind"] for a in sent], ["connect", "timeout", "connect"])
        now = [1000.0]
        hubd = modrun.Modules(tempfile.mkdtemp(), local=False, emit=lambda *a: sent.append(a), clock=lambda: now[0],
                              records=lambda: {"fake": {"state": "ok", "crash": {"launch": "L3", "ts": 990.0}}})
        self.assertFalse(hubd.fail("fake", "fold", "connect", "no connection", "L3"))   # the journal already knows
        now[0] += modrun.CRASH_SPAN
        self.assertTrue(hubd.fail("fake", "fold", "connect", "no connection", "L3"))    # a minute passed: a failure again

    def test_another_process_calls_through_the_spool(self):
        # a page or a hub command: reads its own state from state.json and reports a failure as a fact into the core's
        # spool; folds its own failure itself, so it does not call the module again without waiting for the journal
        s = self.stand()
        s.hub.save_state()
        other = modrun.Modules(s.state_dir)
        s.update(doctor={"do": "status"})
        for _ in range(4):
            other.call("fake", "doctor", {})
        self.assertEqual(len(s.calls("doctor")), 3)
        self.assertEqual([x["type"] for x in s.spooled()], ["module.error"] * 3)


class Readiness(unittest.TestCase):
    def test_garbage_hello_blocks_the_module_and_its_dependents(self):
        s = Stand(self, extra=[{"name": "user", "requires": {"fake": "^1.0"}}])
        s.hub.modules.tick()
        self.assertEqual(sorted(modrun.wants(s.state_dir)), ["fake", "user"])
        s.control(hello={"name": "fake", "version": "9.9.9"})
        s.launch()
        self.assertTrue(s.until(lambda: s.rec().get("state") == "blocked"))
        self.assertIn("hello", s.rec()["reason"])
        s.hub.modules.tick()
        self.assertEqual(modrun.wants(s.state_dir), {})          # hub-up stops both it and its dependent
        notes = [e["data"]["text"] for e in s.events("owner.notify")]
        self.assertEqual(len(notes), 1)
        self.assertIn("did not come up", notes[0])

    def test_hello_with_points_of_another_version_or_without_the_right_blocks(self):
        # a point not of this hub; a point without the right (points newer than the module's version: test_modapi_graph.Versions)
        for points, caps in ((["future.point"], []), (["doctor"], [])):
            with mock.patch.dict(modrun.POINTS, POINT):
                s = Stand(self, points=points, caps=caps)
                s.launch()
                self.assertTrue(s.until(lambda: s.rec().get("state") == "blocked"), points)

    def test_a_process_that_never_listens_is_blocked_after_the_wait(self):
        s = Stand(self)
        s.control(start="late", late_s=30)
        with mock.patch.object(modrun, "HELLO_WAIT", 0.5):
            s.launch()
            self.assertTrue(s.until(lambda: s.rec().get("state") == "blocked", timeout=10))
        self.assertEqual(s.events("module.error")[-1]["data"]["hook"], "hello")

    def test_a_new_start_of_the_hub_tries_a_blocked_module_again(self):
        s = Stand(self)
        s.control(hello={"name": "x", "version": "1.0.0"})
        s.launch()
        self.assertTrue(s.until(lambda: s.rec().get("state") == "blocked"))
        s.hub.modules.tick()
        self.assertEqual(modrun.wants(s.state_dir), {})
        s.hub.record("core.started", data={"pid": 1})
        s.hub.modules.tick()
        self.assertEqual(list(modrun.wants(s.state_dir)), ["fake"])

    def test_restart_of_a_blocked_module_part_is_a_retry(self):
        s = Stand(self)
        s.control(hello={"name": "x", "version": "1.0.0"})
        s.launch()
        self.assertTrue(s.until(lambda: s.rec().get("state") == "blocked"))
        s.hub.record("hub.restart_requested", data={"parts": ["mod:fake"], "channel": "cli", "owner": True})
        self.assertEqual(s.rec()["state"], "ok")
        self.assertFalse(s.hub.state.get("restart_parts"))    # no process: hub-up starts it by want
        s.hub.modules.tick()
        self.assertEqual(list(modrun.wants(s.state_dir)), ["fake"])

    def test_a_failed_first_read_of_modules_does_not_drop_their_processes(self):
        s = Stand(self)
        s.hub.modules.tick()
        fresh = modrun.Supervisor(s.state_dir, lambda: s.hub.state, s.hub.record)   # the core started anew
        with mock.patch.object(modrun.modules, "discover", side_effect=RuntimeError("failure")):
            fresh.reload(s.cfg, {})
        fresh.tick()
        self.assertEqual(list(modrun.wants(s.state_dir)), ["fake"])

    def test_a_new_version_on_disk_before_the_manifests_are_reread_is_ready_not_blocked(self):
        # git checkout in the module folder: module.json is already new, hubd still holds the old one (it rereads every
        # ~30 s); the process came up and answered with the new version — the module is ready with it, no blocked, no
        # notice to the owner
        s = Stand(self)
        s.hub.modules.tick()
        man = os.path.join(s.root, "fake", "module.json")
        with open(man) as f:
            m = json.load(f)
        with open(man, "w") as f:
            json.dump({**m, "version": "1.0.1"}, f)
        s.launch()
        self.assertTrue(s.until(lambda: s.rec().get("version") == "1.0.1"), s.rec())
        self.assertEqual(s.events("module.error"), [])
        self.assertEqual(s.events("owner.notify"), [])
        self.assertEqual(modrun.wants(s.state_dir)["fake"]["version"], "1.0.1")   # hub-up restarts it by the new want

    def test_a_version_that_is_not_on_disk_either_still_blocks(self):
        s = Stand(self)
        s.control(hello={"name": "fake", "version": "1.0.1"})
        s.launch()
        self.assertTrue(s.until(lambda: s.rec().get("state") == "blocked"))
        self.assertIn("1.0.1", s.rec()["reason"])

    def test_the_run_file_is_private(self):
        s = Stand(self)
        s.launch()
        if os.name != "nt":
            mode = os.stat(os.path.join(s.state_dir, "modrun", "fake.run.json")).st_mode & 0o777
            self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
