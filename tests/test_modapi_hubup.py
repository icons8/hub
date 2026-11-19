"""hub-up keeps the core's module processes (core/modrun.py): part mod:<name> — driven by the want file from hubd, with
a port and a new secret on every start; no file — the process is stopped; changed — started anew; the process exited
by itself — a module.error fact in the core's mailbox and the start file removed."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from core import modrun, restart
from tests.fakes.modstand import FAKE
from tests.fakes.procs import alive, kill

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PART = "import time\ntime.sleep(300)\n"


def until(pred, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        got = pred()
        if got:
            return got
        time.sleep(0.1)
    return None


class HubUpRunsModules(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="modup-")
        self.mod = os.path.join(self.state, "fake")
        os.makedirs(self.mod)
        with open(os.path.join(self.mod, "module.json"), "w") as f:
            json.dump({"name": "fake", "version": "1.0.0", "api": "^2.0", "run": ["x"]}, f)
        self.ctl = os.path.join(self.state, "ctl.json")
        self.control()
        part = os.path.join(self.state, "part.py")
        with open(part, "w") as f:
            f.write(PART)
        self.part = part
        self.want("k1")
        self.pids = []
        self.p = self.hub_up()
        self.addCleanup(self.cleanup)

    def hub_up(self, **env):
        base = {k: v for k, v in os.environ.items() if k not in ("HUB_SERVICE", "INVOCATION_ID", "XPC_SERVICE_NAME")}
        base.update(HUB_STATE_DIR=self.state, HUB_UP_PARTS=json.dumps({"web": [self.part]}), FAKE_MODULE_CTL=self.ctl,
                    **env)
        return subprocess.Popen([sys.executable, os.path.join(ROOT, "bin", "hub-up")], env=base,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def cleanup(self):
        if self.p.poll() is None:
            self.p.terminate()
            self.p.wait(30)
        for pid, seen in self.pids:
            kill(pid, seen)

    def control(self, **c):
        with open(self.ctl, "w") as f:
            json.dump(c, f)

    def want(self, key, cmd=None, state=None):
        state = state or self.state
        os.makedirs(modrun.run_dir(state), exist_ok=True)
        modrun._write(os.path.join(modrun.run_dir(state), "fake.want.json"),
                      {"name": "fake", "cmd": cmd or [sys.executable, FAKE, self.mod], "cwd": self.mod,
                       "version": "1.0.0", "order": 0, "budget": {}, "capabilities": [], "key": key})

    def run_file(self, other_than=None):
        run = modrun.run_of(self.state, "fake")
        if run and run.get("pid") and run.get("launch") != other_than:
            self.pids.append((run["pid"], time.time()))   # hub-up drops the file when the process exits
            return run
        return None

    def hello(self, run, secret=None):
        return modrun.post(run["port"], secret or run["secret"], "/hook/hello", {}, 2.0)

    def test_a_module_part_runs_with_its_own_port_and_secret(self):
        run = until(self.run_file)
        self.assertTrue(run, "the module process did not come up")
        kind, got = until(lambda: (lambda r: r if r[0] != "connect" else None)(self.hello(run)), 10) or ("?", None)
        self.assertIsNone(kind, got)
        self.assertEqual(got["name"], "fake")
        self.assertEqual(self.hello(run, secret="foreign")[0], "status")   # without the start secret the module does not answer

    def test_a_changed_want_restarts_and_a_removed_one_stops(self):
        first = until(self.run_file)
        self.want("k2")
        second = until(lambda: self.run_file(other_than=first["launch"]))
        self.assertTrue(second, "the module was not restarted on the new want")
        self.assertNotEqual(second["secret"], first["secret"])
        self.assertTrue(until(lambda: not alive(first["pid"]), 20))
        os.unlink(os.path.join(modrun.run_dir(self.state), "fake.want.json"))
        self.assertTrue(until(lambda: not alive(second["pid"]), 20), "the module process was not stopped")
        time.sleep(3)   # and not started again: no new launch record (not the pid: Windows soon gives it to another process)
        self.assertIsNone(modrun.run_of(self.state, "fake"))
        self.assertFalse([e for e in self.spooled() if e["type"] == "module.error"])   # stopped by the core — not a failure

    def test_a_module_hub_up_stops_on_hub_stop_leaves_no_launch_file(self):
        # a whole-hub restart and `hub stop` stop the module processes too: a launch file left behind named the dead process
        # as ready, and the next core life called it before hub-up raised the module again - a "connect" failure of the
        # module on every hub restart, three within five minutes a pause
        run = until(self.run_file)
        self.assertTrue(run, "the module process did not come up")
        self.assertTrue(until(lambda: self.hello(run)[0] is None, 10))
        open(os.path.join(self.state, restart.STOP), "w").close()
        self.p.wait(60)
        self.assertIsNone(modrun.run_of(self.state, "fake"))
        self.assertFalse([e for e in self.spooled() if e["type"] == "module.error"])   # stopped by hub-up - not a failure

    def test_python3_in_run_is_the_hubs_own_python_even_when_the_system_has_none(self):
        # on Windows `python3` is often a Store stub, on a minimal server it is absent: the module runs on the hub's Python
        self.p.terminate()
        self.p.wait(30)
        nowhere = os.path.join(self.state, "empty-path")
        os.makedirs(nowhere)
        last = (modrun.run_of(self.state, "fake") or {}).get("launch")
        for word in ("python3", "python"):
            with self.subTest(word=word):
                self.want("k-" + word, cmd=[word, FAKE, self.mod])
                self.p = self.hub_up(PATH=nowhere)
                run = until(lambda: self.run_file(other_than=last))
                last = (run or {}).get("launch")
                self.assertTrue(run, "the module process did not come up")
                kind, got = until(lambda: (lambda r: r if r[0] != "connect" else None)(self.hello(run)), 10) or ("?", None)
                self.assertIsNone(kind, got)
                self.assertEqual(got["name"], "fake")
                self.assertFalse([e for e in self.spooled() if e["type"] == "module.error"])
                self.p.terminate()
                self.p.wait(30)
                self.assertTrue(until(lambda: not alive(run["pid"]), 20))

    def spooled(self):
        d = os.path.join(self.state, "spool")
        out = []
        for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
            if n.endswith(".json"):
                with open(os.path.join(d, n), encoding="utf-8") as f:
                    out.append(json.load(f))
        return out

    def test_a_module_that_cannot_be_started_is_a_fact_not_a_fall_of_the_hub(self):
        self.want("k2", cmd=[os.path.join(self.state, "no-such-program")])
        got = until(lambda: [e for e in self.spooled() if e["type"] == "module.error"])
        self.assertTrue(got, "no fact that the module did not start")
        self.assertIn("did not start", got[0]["data"]["error"])
        self.assertIsNone(self.p.poll())                       # hub-up is alive

    def test_a_process_that_exits_is_a_fact_for_the_core(self):
        self.control(start="exit")
        got = until(lambda: [e for e in self.spooled() if e["type"] == "module.error"])
        self.assertTrue(got, "no fact about the process exit")
        data = got[0]["data"]
        self.assertEqual((data["module"], data["kind"]), ("fake", "exit"))
        self.assertTrue(data["launch"])
        self.assertLess(data["uptime"], modrun.START_WINDOW)
        self.assertIn("3", data["error"])
        run = modrun.run_of(self.state, "fake")   # nobody to call: the start file is removed (or it is already a new start)
        self.assertTrue(run is None or run["launch"] != data["launch"])


# a stub core: reconciles the want files (writes PLAN with its own liveness mark) when the test puts down the go file
FAKE_CORE = ("import json, os, sys, time\n"
             "d = os.environ['HUB_STATE_DIR']\n"
             "while not os.path.exists(os.path.join(d, 'go')):\n"
             "    time.sleep(0.1)\n"
             "json.dump({'core': os.environ['HUB_UP_CORE']}, open(os.path.join(d, 'modrun', 'plan.json'), 'w'))\n"
             "time.sleep(300)\n")


class OnlyWhatTheCurrentCoreKeeps(unittest.TestCase):
    def test_want_files_from_before_are_not_run_until_the_core_checks_them(self):
        # the module was turned off while the hub was down, or the core does not come up — a want file from last time starts nothing
        state = tempfile.mkdtemp(prefix="modup-core-")
        core = os.path.join(state, "core.py")
        with open(core, "w") as f:
            f.write(FAKE_CORE)
        helper = HubUpRunsModules("test_a_module_part_runs_with_its_own_port_and_secret")
        helper.mod = os.path.join(state, "fake")
        os.makedirs(helper.mod)
        helper.want("k1", state=state)
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_SERVICE", "INVOCATION_ID", "XPC_SERVICE_NAME")}
        env.update(HUB_STATE_DIR=state, HUB_UP_PARTS=json.dumps({"hubd": [core]}))
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "bin", "hub-up")], env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def stop():
            p.terminate()
            p.wait(30)
            run = modrun.run_of(state, "fake")
            if run and run.get("pid"):
                kill(run["pid"], time.time())   # just read: it started by then
        self.addCleanup(stop)
        time.sleep(3)
        self.assertIsNone(modrun.run_of(state, "fake"))
        open(os.path.join(state, "go"), "w").close()
        self.assertTrue(until(lambda: (modrun.run_of(state, "fake") or {}).get("pid")), "the module was not brought up after the reconcile")


if __name__ == "__main__":
    unittest.main()
