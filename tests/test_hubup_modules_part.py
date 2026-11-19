"""hub-up --only: the core's modules start only when the part `modules` is named (it is a name, not a process).
A trimmed list of parts (a stand) keeps today's behaviour; a service that wants the modules names them."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

import platforms
from core import modrun
from tests.fakes.modstand import FAKE
from tests.fakes.procs import kill

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# a part that says it is up (its file appears), then lives
PART = ("import os, sys, time\n"
        "open(os.path.join(sys.argv[1], 'part-up'), 'w').close()\n"
        "time.sleep(300)\n")
# the core's only job here: mark the want files as checked by this life of the core (core/modrun.py PLAN)
CORE = ("import json, os, sys, time\n"
        "d = os.path.join(sys.argv[1], 'modrun')\n"
        "os.makedirs(d, exist_ok=True)\n"
        "open(os.path.join(d, 'plan.json'), 'w').write(json.dumps({'core': os.environ.get('HUB_UP_CORE')}))\n"
        "open(os.path.join(sys.argv[1], 'core-up'), 'w').close()\n"
        "time.sleep(300)\n")


class OnlyAndTheModulesPart(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="modpart-")
        self.addCleanup(platforms.current.rmtree, self.state)
        mod = os.path.join(self.state, "fake")
        os.makedirs(mod)
        with open(os.path.join(mod, "module.json"), "w") as f:
            json.dump({"name": "fake", "version": "1.0.0", "api": "^2.0", "run": ["x"]}, f)
        ctl = os.path.join(self.state, "ctl.json")
        with open(ctl, "w") as f:
            json.dump({}, f)
        files = {}
        for name, body in (("part", PART), ("core", CORE)):
            files[name] = os.path.join(self.state, name + ".py")
            with open(files[name], "w") as f:
                f.write(body)
        os.makedirs(modrun.run_dir(self.state), exist_ok=True)
        modrun._write(os.path.join(modrun.run_dir(self.state), "fake.want.json"),
                      {"name": "fake", "cmd": [sys.executable, FAKE, mod], "cwd": mod, "version": "1.0.0", "order": 0,
                       "budget": {}, "capabilities": [], "key": "k1"})
        self.env = {k: v for k, v in os.environ.items() if k not in ("HUB_SERVICE", "INVOCATION_ID", "XPC_SERVICE_NAME")}
        self.env.update(HUB_STATE_DIR=self.state, FAKE_MODULE_CTL=ctl,
                        HUB_UP_PARTS=json.dumps({"hubd": [files["core"], self.state], "web": [files["part"], self.state]}))
        self.p = None
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.p and self.p.poll() is None:
            self.p.terminate()
            self.p.wait(30)
        run = modrun.run_of(self.state, "fake")
        if run and run.get("pid"):
            kill(run["pid"])

    def hub_up(self, only):
        self.p = subprocess.Popen([sys.executable, os.path.join(ROOT, "bin", "hub-up"), "--only", only], env=self.env,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def until(self, check, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            got = check()
            if got:
                return got
            time.sleep(0.1)
        return None

    def part_is_up(self):
        return os.path.exists(os.path.join(self.state, "part-up"))

    def module(self):
        run = modrun.run_of(self.state, "fake")
        return run if run and run.get("pid") else None

    def test_the_modules_part_starts_the_core_modules(self):
        self.hub_up("hubd,web,modules")
        self.assertTrue(self.until(self.module, 30), "hub-up --only hubd,web,modules did not start the module")
        self.assertTrue(self.until(self.part_is_up, 30), "the part `modules` must not stop the real parts from starting")

    def test_a_trimmed_list_without_the_part_leaves_the_modules_alone(self):
        self.hub_up("hubd,web")
        self.assertTrue(self.until(self.part_is_up, 30))
        # the modules are looked at right after the parts start: a module would be launched within the next turns
        self.assertIsNone(self.until(self.module, 4))

    def test_the_modules_part_without_the_core_takes_the_want_files_as_they_are(self):
        self.hub_up("web,modules")
        self.assertTrue(self.until(self.module, 30))

    def test_modules_is_a_name_not_a_process_it_may_stand_first(self):
        self.hub_up("modules,hubd,web")
        self.assertTrue(self.until(self.module, 30))
        self.assertTrue(self.until(self.part_is_up, 30))
        self.assertIsNone(self.p.poll(), "hub-up must not exit on the name `modules`")

    def test_modules_alone_is_refused_before_anything_starts(self):
        # `modules` is not a part: with nothing else in the list there is no part to start — a plain refusal, not a crash
        r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "hub-up"), "--only", "modules"], env=self.env,
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no part to start", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertIsNone(self.module())


if __name__ == "__main__":
    unittest.main()
