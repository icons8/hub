"""A plugin process that keeps exiting is seen on the plugins page: hub-up restarts it with a growing pause, as before, and
now also keeps its last exit in hub-up.json (code, how many times in a row, when); the page's plugin list carries it.
Before, the only trace was a line in hub-up.log every minute: the plugin looked installed and fine while its process
crash-looped for days (a plugin that imported an optional add-on missing on a clean hub)."""
import tests  # noqa: F401 — keep this machine's settings out
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

import platforms
from tests import test_hub_up_extensions as hub_up_tests, test_web

CRASH = "import sys\nsys.exit(3)\n"


class Exits(unittest.TestCase):
    setUp, run_hub_up, starts, until = (hub_up_tests.Supervisor.setUp, hub_up_tests.Supervisor.run_hub_up,
                                        hub_up_tests.Supervisor.starts, hub_up_tests.Supervisor.until)

    def exits(self):
        try:
            with open(os.path.join(self.state, "hub-up.json")) as f:
                return json.load(f).get("exits") or {}
        except (OSError, ValueError):
            return {}

    def test_a_crashing_plugin_is_recorded_and_a_running_one_is_not(self):
        crash = os.path.join(self.state, "crash.py")
        with open(crash, "w") as f:
            f.write(CRASH)
        exts = {"ext:crash": {"cmd": [sys.executable, crash], "cwd": self.state},
                "ext:good": {"cmd": [sys.executable, self.part, self.log, "good"], "cwd": self.state}}
        self.run_hub_up(HUB_UP_EXT=json.dumps(exts))
        self.until(lambda: self.exits().get("ext:crash", {}).get("count", 0) >= 2)
        got = self.exits()
        self.assertEqual(got["ext:crash"]["code"], 3)
        self.assertLessEqual(abs(got["ext:crash"]["at"] - time.time()), 60)
        self.assertNotIn("ext:good", got)
        self.assertIn("good", self.starts())

    def test_a_restart_asked_for_starts_the_count_again(self):
        # `hub restart ext:<name>` (by hand or after an update installed a fix): an older crash no longer stands
        crash = os.path.join(self.state, "crash.py")
        with open(crash, "w") as f:
            f.write(CRASH)
        self.run_hub_up(HUB_UP_EXT=json.dumps({"ext:crash": {"cmd": [sys.executable, crash], "cwd": self.state}}))
        self.until(lambda: self.exits().get("ext:crash", {}).get("count", 0) >= 3, timeout=30)
        with open(os.path.join(self.state, "restart"), "w") as f:
            json.dump({"parts": ["ext:crash"]}, f)
        self.until(lambda: self.exits().get("ext:crash", {}).get("count", 0) < 3, timeout=10)

    def test_a_plugin_that_is_gone_leaves_no_record(self):
        crash = os.path.join(self.state, "crash.py")
        with open(crash, "w") as f:
            f.write(CRASH)
        exts = os.path.join(self.state, "exts.json")
        with open(exts, "w") as f:
            json.dump({"ext:crash": {"cmd": [sys.executable, crash], "cwd": self.state}}, f)
        self.run_hub_up(HUB_UP_EXT_FILE=exts)
        self.until(lambda: "ext:crash" in self.exits())
        with open(exts, "w") as f:
            json.dump({}, f)   # removed (or turned off), then hub restart ext:crash
        with open(os.path.join(self.state, "restart"), "w") as f:
            json.dump({"parts": ["ext:crash"]}, f)
        self.until(lambda: "ext:crash" not in self.exits(), timeout=10)


class Page(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-exits-")
        self.addCleanup(platforms.current.rmtree, self.tmp)
        plugins = {}
        for name in ("alice:owl", "alice:fox"):
            folder = os.path.join(self.tmp, name.replace(":", "-"))
            os.makedirs(folder)
            with open(os.path.join(folder, "hub-plugin.json"), "w") as f:
                json.dump({"name": name.split(":")[1], "version": "1.0.0", "hub": "^1.0", "run": ["python3", "main.py"]}, f)
            plugins[name] = {"path": folder, "run": ["python3", "main.py"], "source": "alice/x", "hub": "^1.0"}
        cfg = os.path.join(self.tmp, "hub.json")
        with open(cfg, "w") as f:
            json.dump({"dev": {"enabled": False}, "update": {"enabled": False}, "extensions": plugins}, f)
        env = mock.patch.dict(os.environ, {"HUB_CONFIG": cfg})
        env.start()
        self.addCleanup(env.stop)
        self.h = test_web.WebTest("test_page_and_snapshot")
        self.h.setUp()
        self.addCleanup(self.h.tearDown)

    def rows(self):
        return {p["name"]: p for p in self.h.snap()["plugins"]}

    def test_the_plugin_list_carries_the_last_exit(self):
        with open(os.path.join(self.h.state, "hub-up.json"), "w") as f:
            json.dump({"pid": os.getpid(), "exits": {"ext:alice:owl": {"code": 3, "count": 7, "at": 1000.0}}}, f)
        rows = self.rows()
        self.assertEqual(rows["alice:owl"]["exits"], {"code": 3, "count": 7, "at": 1000.0})
        self.assertIsNone(rows["alice:fox"]["exits"])

    def test_no_or_odd_record_is_no_exit(self):
        self.assertIsNone(self.rows()["alice:owl"]["exits"])
        for odd in ("{", json.dumps([]), json.dumps({"exits": None}), json.dumps({"exits": {"ext:alice:owl": "x"}}),
                    json.dumps({"exits": {"ext:alice:owl": {"code": "3", "count": None}}})):
            with open(os.path.join(self.h.state, "hub-up.json"), "w") as f:
                f.write(odd)
            self.assertIsNone(self.rows()["alice:owl"]["exits"], odd)

    def test_the_page_shows_it(self):
        with open(os.path.join(os.path.dirname(test_web.__file__), "..", "modules", "web", "static", "js", "plugins.js"),
                  encoding="utf-8") as f:
            page = f.read()
        self.assertIn("p.exits", page)
        self.assertIn("web.plugins.process_exits", page)
        for lang in ("en", "ru"):
            with open(os.path.join(os.path.dirname(test_web.__file__), "..", "i18n", f"{lang}.json"), encoding="utf-8") as f:
                self.assertIn("web.plugins.process_exits", json.load(f), lang)


if __name__ == "__main__":
    unittest.main()
