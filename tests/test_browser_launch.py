"""Mac reboot 02.10: the agent Chrome with the owner's profile took longer than 10–16 s to come up — the watchdog killed
its main process and started it again, over and over, leaving a pile of Chromes that nearly brought the system down.
Now a launch gets LAUNCH_GRACE, a second Chrome on the same profile is never started, and a hung one is killed whole."""
import tests  # noqa: F401 — ignore this machine's settings
import unittest

from modules.browser import run


class Proc:
    pid = 4242

    def __init__(self):
        self.alive = True

    def poll(self):
        return None if self.alive else 0


class SlowChrome(unittest.TestCase):
    def setUp(self):
        self.now, self.launched, self.killed, self.elsewhere = [0.0], [], [], [None]
        self.sup = run.Supervisor("/tmp/none", clock=lambda: self.now[0], probe=lambda url: None)
        self.sup.launch = lambda cfg, state_dir, name, b: self.launched.append(Proc()) or self.launched[-1]
        self.sup.kill_tree = lambda proc: self.killed.append(proc) or setattr(proc, "alive", False)
        self.sup.running = lambda profile: self.elsewhere[0]
        self.b = {"kind": "local", "url": "http://127.0.0.1:9", "port": 9, "risk": []}

    def at(self, t):
        self.now[0] = t
        return self.sup.check({}, "mac", self.b)

    def test_one_launch_while_chrome_starts_slowly(self):
        for t in (0, 5, 15, 30, 60, 100):
            self.assertEqual(self.at(t)["problem"], "starting")
        self.assertEqual(len(self.launched), 1)
        self.at(run.LAUNCH_GRACE + 1)                      # never answered — kill it whole and start once more
        self.assertEqual((len(self.killed), len(self.launched)), (1, 2))

    def test_chrome_left_from_before_is_not_doubled(self):
        self.elsewhere[0] = 777                             # a Chrome with this profile already exists (the hub's previous life)
        for t in (0, 30, run.LAUNCH_GRACE + 5):
            self.at(t)
        self.assertEqual(self.launched, [])
        self.assertIn("777", self.at(run.LAUNCH_GRACE + 10)["problem"])


if __name__ == "__main__":
    unittest.main()
