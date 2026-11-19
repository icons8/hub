"""One broken extension does not bring hub-up down: a command that is not a list, a settings entry that is not an
object, a missing program, an unreadable hub.json: hub-up logs it and keeps the core, parts and other extensions
running. Before, the supervisor itself crashed, taking all its children with it, on every start."""
import tests  # noqa: F401 — keep this machine's settings out
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB_UP = os.path.join(REPO, "bin", "hub-up")
PART = "import sys, time\nopen(sys.argv[1], 'a').write(sys.argv[2] + '\\n')\ntime.sleep(60)\n"


def load_hub_up():
    loader = importlib.machinery.SourceFileLoader("hub_up_ext", HUB_UP)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("hub_up_ext", loader))
    loader.exec_module(mod)
    return mod


class Settings(unittest.TestCase):
    def setUp(self):
        self.cfg = os.path.join(tempfile.mkdtemp(), "hub.json")

    def test_bad_entries_are_skipped(self):
        folder = tempfile.mkdtemp()
        with open(self.cfg, "w") as f:
            json.dump({"extensions": {"null": None, "text": {"run": "python3 main.py", "path": folder},
                                      "mixed": {"run": ["python3", 7], "path": folder}, "list": ["x"],
                                      "good": {"run": ["python3", "main.py"], "path": folder}}}, f)
        hub_up = load_hub_up()   # the command of a good entry is as this platform gives it (python3 may be the hub's Python)
        with mock.patch.dict(os.environ, {"HUB_CONFIG": self.cfg}):
            self.assertEqual(hub_up.extensions(),
                             {"ext:good": {"cmd": hub_up.plat.ext_command(["python3", "main.py"]), "cwd": folder}})

    def test_a_python3_plugin_gets_the_hubs_python_on_windows_without_a_real_one(self):
        # run ["python3", ...]: on Windows python3 is often the Microsoft Store stub (exit 9009), and after a reboot the
        # plugin process stood dead for days; hub-up gives it the hub's Python, as it does for modules
        from platforms import windows
        folder = tempfile.mkdtemp()
        with open(self.cfg, "w") as f:
            json.dump({"extensions": {"py": {"run": ["python3", "main.py"], "path": folder}}}, f)
        hub_up = load_hub_up()
        with mock.patch.dict(os.environ, {"HUB_CONFIG": self.cfg}), mock.patch.object(hub_up, "plat", windows), \
                mock.patch.object(windows.shutil, "which", lambda name: None):
            cmd = hub_up.extensions()["ext:py"]["cmd"]
        self.assertEqual((cmd[0], cmd[-1]), (sys.executable, "main.py"))

    def test_unreadable_settings_are_not_no_extensions(self):
        # hub.json with a typo: hub-up used to crash on start; and treating it as "no extensions" would stop running ones on hub restart
        with open(self.cfg, "w") as f:
            f.write('{"extensions": {')
        with mock.patch.dict(os.environ, {"HUB_CONFIG": self.cfg}):
            self.assertIsNone(load_hub_up().extensions())


class Supervisor(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubup-ext-")
        self.log, self.out = os.path.join(self.state, "starts.log"), os.path.join(self.state, "out.log")
        self.part = os.path.join(self.state, "part.py")
        with open(self.part, "w") as f:
            f.write(PART)

    def run_hub_up(self, **env):
        env = {**os.environ, "HUB_STATE_DIR": self.state,
               "HUB_UP_PARTS": json.dumps({"hubd": [self.part, self.log, "hubd"]}), **env}
        with open(self.out, "w") as o:
            p = subprocess.Popen([sys.executable, HUB_UP], env=env, stdout=o, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: (p.terminate(), p.wait(20)))
        return p

    def starts(self):
        return open(self.log).read().split() if os.path.exists(self.log) else []

    def text(self):
        with open(self.out, encoding="utf-8", errors="replace") as f:
            return f.read()

    def until(self, pred, timeout=20.0):
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return
            time.sleep(0.1)
        self.fail("timed out")

    def test_a_missing_binary_does_not_stop_the_rest(self):
        exts = {"ext:broken": {"cmd": [os.path.join(self.state, "no-such-binary")], "cwd": self.state},
                "ext:good": {"cmd": [sys.executable, self.part, self.log, "good"], "cwd": self.state}}
        p = self.run_hub_up(HUB_UP_EXT=json.dumps(exts))
        self.until(lambda: sorted(self.starts()) == ["good", "hubd"])
        time.sleep(1.5)   # a supervisor cycle or two later it is still alive
        self.assertIsNone(p.poll())
        self.assertIn("ext:broken", self.text())

    def test_a_plugin_started_from_a_session_is_not_that_session(self):
        # hub-up started from a hub session: otherwise bin/hub in a plugin process would write channel session in its name
        probe = os.path.join(self.state, "probe.py")
        with open(probe, "w") as f:
            f.write("import os, sys, time\nopen(sys.argv[1], 'a').write("
                    "os.environ.get('HUB_SESSION', '-') + ':' + os.environ.get('HUB_EXTENSION', '-') + '\\n')\n"
                    "time.sleep(60)\n")
        exts = {"ext:good": {"cmd": [sys.executable, probe, self.log], "cwd": self.state}}
        self.run_hub_up(HUB_UP_EXT=json.dumps(exts), HUB_SESSION="hub")
        self.until(lambda: "-:good" in self.starts() or "hub:good" in self.starts())
        self.assertIn("-:good", self.starts())

    def test_the_core_started_from_a_session_is_not_that_session(self):
        # a session's process reads English (core/i18n): the hub's parts speak to the owner, in his language
        probe = os.path.join(self.state, "probe.py")
        with open(probe, "w") as f:
            f.write("import os, sys, time\nopen(sys.argv[1], 'a').write('core:' + os.environ.get('HUB_SESSION', '-') + "
                    "'\\n')\ntime.sleep(60)\n")
        self.run_hub_up(HUB_UP_PARTS=json.dumps({"hubd": [probe, self.log]}), HUB_SESSION="maps")
        self.until(lambda: self.starts())
        self.assertEqual(self.starts()[:1], ["core:-"])

    def test_a_plugin_gets_no_claude_login_keys(self):
        # as for modules (core/modrun.launch): the service's login keys do not reach the plugin, the rest of the environment is hub-up's
        probe = os.path.join(self.state, "probe.py")
        with open(probe, "w") as f:
            f.write("import os, sys, time\nopen(sys.argv[1], 'a').write(','.join(k for k in sorted(os.environ) if k in "
                    "('CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY', 'ANTHROPIC_CUSTOM_HEADERS', 'MAPS_FLAG')) + '\\n')\n"
                    "time.sleep(60)\n")
        exts = {"ext:good": {"cmd": [sys.executable, probe, self.log], "cwd": self.state}}
        self.run_hub_up(HUB_UP_EXT=json.dumps(exts), CLAUDE_CODE_OAUTH_TOKEN="t", ANTHROPIC_API_KEY="k",
                        ANTHROPIC_CUSTOM_HEADERS="x-api-key: h", MAPS_FLAG="1")
        self.until(lambda: any("MAPS_FLAG" in s for s in self.starts()))
        self.assertIn("MAPS_FLAG", self.starts())

    def test_a_restart_with_unreadable_settings_keeps_the_extension(self):
        exts = os.path.join(self.state, "exts.json")
        with open(exts, "w") as f:
            json.dump({"ext:good": {"cmd": [sys.executable, self.part, self.log, "good"], "cwd": self.state}}, f)
        p = self.run_hub_up(HUB_UP_EXT_FILE=exts)
        self.until(lambda: sorted(self.starts()) == ["good", "hubd"])
        with open(exts, "w") as f:
            f.write('{"ext:good": ')   # a typo, then hub restart ext:good
        with open(os.path.join(self.state, "restart"), "w") as f:
            json.dump({"parts": ["ext:good"]}, f)
        self.until(lambda: sorted(self.starts()) == ["good", "good", "hubd"])   # restarted with the same command
        self.assertIsNone(p.poll())
        self.assertNotIn("is off", self.text())


if __name__ == "__main__":
    unittest.main()
