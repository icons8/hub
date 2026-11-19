"""A hub on a machine with no screen: the browser was set up with a window (headless: false) and no DISPLAY, Chrome died
at every start ("Missing X server or $DISPLAY"), the supervisor started it again every minute for ever, and each start
left a 4 MB BrowserMetrics/*.pma in the profile (17 GB in three days). Now: no screen — a headless Chrome; a Chrome that
keeps dying at start is tried less and less often, the status says why, and what it left behind is removed."""
import tests  # noqa: F401 — ignore this machine's settings
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

import platforms
from modules.browser import browsers, run


class Proc:
    pid = 4242

    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


def window():
    return browsers.configured({"browsers": {"agents": {"kind": "local", "port": 9399, "headless": False}}})["agents"]


class NoScreen(unittest.TestCase):
    def on_linux(self, env):
        return mock.patch.object(browsers, "sys", types.SimpleNamespace(platform="linux")), \
            mock.patch.dict(os.environ, env, clear=True)

    def test_a_window_with_no_display_becomes_a_headless_chrome(self):
        a, b = self.on_linux({"PATH": "/usr/bin"})
        with a, b:
            got = window()
            self.assertTrue(got["headless"])
            cmd = browsers.chrome_command({"browser": {"chrome": "/bin/chrome"}}, "/st", "agents", got)
            self.assertIn("--headless=new", cmd)

    def test_a_display_anywhere_keeps_the_window(self):
        for env in ({"DISPLAY": ":0"}, {"WAYLAND_DISPLAY": "wayland-0"}):
            with self.subTest(env=env):
                a, b = self.on_linux(env)
                with a, b:
                    self.assertFalse(window()["headless"])
        a, b = self.on_linux({"PATH": "/usr/bin"})
        with a, b:   # the DISPLAY of the browser.env setting goes to Chrome: that is the display it sees
            got = browsers.configured({"browser": {"env": {"DISPLAY": ":99"}},
                                       "browsers": {"agents": {"kind": "local", "headless": False}}})["agents"]
            self.assertFalse(got["headless"])

    def test_other_systems_have_a_screen_of_their_own(self):
        with mock.patch.object(browsers, "sys", types.SimpleNamespace(platform="darwin")), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(window()["headless"])


class Loop(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="crashloop-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.now, self.launched, self.up, self.elsewhere = [0.0], [], [None], [None]
        self.sup = run.Supervisor(self.state, clock=lambda: self.now[0], probe=lambda url: self.up[0])
        self.sup.launch = self.launch
        self.sup.running = lambda profile: self.elsewhere[0]
        self.cfg = {"browser": {"chrome": "/bin/chrome"}}
        self.b = {"kind": "local", "url": "http://127.0.0.1:9", "port": 9, "risk": [], "start": "always"}
        os.makedirs(os.path.join(self.state, "browsers"))
        self.profile = browsers.profile_dir(self.state, "agents", self.b)

    def age(self, path, seconds=60):
        with open(path, "wb") as f:
            f.write(b"x" * 1024)
        old = time.time() - seconds
        os.utime(path, (old, old))

    def launch(self, cfg, state_dir, name, b):
        """A Chrome that dies at once, leaving what the real one left."""
        self.launched.append(self.now[0])
        os.makedirs(os.path.join(self.profile, "BrowserMetrics"), exist_ok=True)
        self.age(os.path.join(self.profile, "BrowserMetrics", f"BrowserMetrics-{len(self.launched)}.pma"))
        with open(os.path.join(self.state, "browsers", f"{name}.log"), "a") as f:
            f.write("[1:1:1112/121609.5:ERROR:ui/ozone/platform/x11/ozone_platform_x11.cc:257] Missing X server or $DISPLAY\n"
                    "[1:1:1112/121609.5:ERROR:ui/aura/env.cc:253] The platform failed to initialize.  Exiting.\n")
        return Proc(1)

    def tick(self, seconds=run.EVERY):
        self.now[0] += seconds
        return self.sup.check(self.cfg, "agents", self.b)

    def hour(self):
        out = None
        while self.now[0] < 3600:
            out = self.tick()
        return out

    def test_a_chrome_that_dies_at_every_start_is_not_started_every_minute_for_ever(self):
        self.hour()
        # before: a start every MAX_BACKOFF (60 s) — 60 in the hour
        self.assertLessEqual(len(self.launched), 10, self.launched)
        gaps = [b - a for a, b in zip(self.launched, self.launched[1:])]
        self.assertEqual(gaps, sorted(gaps), "the pause only grows")
        self.assertGreater(gaps[-1], 2 * run.MAX_BACKOFF)

    def test_the_status_says_why_and_when(self):
        status = self.hour()
        text = status["problem"]
        self.assertIn("Missing X server", text)
        self.assertRegex(text, r"\d+ min")
        self.assertNotIn("starting", text)

    def test_it_is_tried_again_at_once_when_the_settings_change(self):
        self.hour()
        n = len(self.launched)
        self.b["args"] = ["--no-sandbox"]            # another command line: the cause may be gone
        self.tick()
        self.assertEqual(len(self.launched), n + 1)

    def test_and_it_is_kept_when_it_comes_up(self):
        self.hour()
        self.up[0] = "Chrome/156"
        self.assertTrue(self.tick()["up"])
        self.assertEqual((self.sup.crashes, self.sup.backoff), ({}, {}))

    def test_a_chrome_that_was_up_and_died_is_not_a_failed_start(self):
        self.up[0] = "Chrome/156"
        self.sup.procs["agents"], self.sup.started["agents"] = Proc(None), 0
        self.tick()                                   # it answers: no start is being watched any more
        self.up[0] = None
        self.sup.procs["agents"] = Proc(137)          # killed later, after weeks
        self.tick()
        self.assertEqual(self.sup.crashes, {})
        self.assertEqual(len(self.launched), 1)       # an ordinary restart

    def test_what_a_failed_start_leaves_in_the_profile_is_removed(self):
        for _ in range(8):
            self.tick()
        left = os.listdir(os.path.join(self.profile, "BrowserMetrics"))
        self.assertLessEqual(len(left), 1, left)       # only the one of the start still being watched

    def test_a_running_chrome_of_this_profile_keeps_its_files(self):
        self.elsewhere[0] = 777
        self.sup.procs["agents"] = Proc(1)
        self.sup.started["agents"] = 0
        os.makedirs(os.path.join(self.profile, "BrowserMetrics"))
        path = os.path.join(self.profile, "BrowserMetrics", "BrowserMetrics-live.pma")
        self.age(path)
        self.tick()
        self.assertTrue(os.path.exists(path))
        self.assertEqual(self.sup.crashes, {})

    def test_the_reason_is_from_this_start_not_from_an_older_one(self):
        with open(os.path.join(self.state, "browsers", "agents.log"), "w") as f:
            f.write("[1:1:1112/121609.5:ERROR:old.cc:1] Missing X server or $DISPLAY\n")
        self.sup.launch = lambda *a: Proc(137)   # dies this time without a word
        for _ in range(3 * run.CRASH_FREE):
            self.tick(60)
        self.assertNotIn("Missing X server", self.sup.reported.get("agents", {}).get("problem", "") or
                         self.sup.crashes["agents"]["problem"])
        self.assertIn("exit code 137", self.sup.crashes["agents"]["problem"])

    def test_the_reason_is_the_line_after_the_noise(self):
        with open(os.path.join(self.state, "browsers", "agents.log"), "w") as f:
            f.write("[1:1:1112/1.5:ERROR:bus.cc:1] Failed to connect to the bus\n"
                    "[1:1:1112/1.5:ERROR:x11.cc:257] Missing X server or $DISPLAY\n"
                    "[1:1:1112/1.5:ERROR:env.cc:253] The platform failed to initialize.  Exiting.\n")
        self.assertEqual(self.sup.why_died("agents", Proc(1)),
                         "Missing X server or $DISPLAY; The platform failed to initialize.  Exiting.")

    def test_the_window_that_became_a_headless_chrome_says_so_once(self):
        self.b["no_screen"] = True
        with mock.patch("builtins.print") as out:
            for _ in range(3):
                self.tick(100)
        lines = [c.args[0] for c in out.call_args_list if "no screen" in c.args[0]]
        self.assertEqual(len(lines), 1, lines)

    def test_an_on_demand_launch_that_dies_leaves_no_files_either(self):
        os.makedirs(os.path.join(self.profile, "BrowserMetrics"))
        self.age(os.path.join(self.profile, "BrowserMetrics", "BrowserMetrics-1.pma"))
        run.ensure_browser(self.cfg, self.state, "agents", self.b, probe=lambda url: None, launch=lambda *a: None,
                           running=lambda p: None, clock=iter(range(0, 1000, 50)).__next__, sleep=lambda s: None, wait=100)
        self.assertEqual(os.listdir(os.path.join(self.profile, "BrowserMetrics")), [])

    def test_the_files_go_and_nothing_else(self):
        folder = os.path.join(self.profile, "BrowserMetrics")
        os.makedirs(folder)
        for n in ("BrowserMetrics-1.pma", "other.txt"):
            self.age(os.path.join(folder, n))
        self.age(os.path.join(self.profile, "BrowserMetrics-spare.pma"))
        self.age(os.path.join(folder, "BrowserMetrics-fresh.pma"), seconds=0)   # a Chrome started this moment by someone else
        open(os.path.join(self.profile, "Preferences"), "wb").close()
        self.assertEqual(browsers.clean_metrics(self.profile), 2)
        self.assertEqual((sorted(os.listdir(folder)), sorted(os.listdir(self.profile))),
                         (["BrowserMetrics-fresh.pma", "other.txt"], ["BrowserMetrics", "Preferences"]))


if __name__ == "__main__":
    unittest.main()
