"""2026-10-09: on Windows the e2e tests' stop_chrome called os.killpg (absent there), cleanup failed and the browser
(four processes) stayed alive - a full run ate 20 GB. Stopping now kills the test's whole process tree."""
import tests  # noqa: F401
import os
import subprocess
import sys
import tempfile
import time
import unittest

import platforms
from modules.testchrome import chrome

PARENT = ("import subprocess, sys, time\n"
          "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
          "open(sys.argv[1], 'w').write(str(c.pid))\n"
          "time.sleep(60)\n")


def alive(pid):
    return platforms.current.alive(pid)


class StopTree(unittest.TestCase):
    def test_stop_kills_the_child_too(self):
        d = tempfile.mkdtemp(prefix="stop-tree-")
        mark = os.path.join(d, "child.pid")
        proc = platforms.current.popen_detached([sys.executable, "-c", PARENT, mark], stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL)
        end = time.time() + 20
        while not os.path.exists(mark) or not open(mark).read():
            self.assertLess(time.time(), end, "the descendant did not start")
            time.sleep(0.1)
        child = int(open(mark).read())
        self.assertTrue(alive(child))
        chrome.stop(proc)
        end = time.time() + 10
        while alive(child) and time.time() < end:
            time.sleep(0.1)
        self.assertFalse(alive(child))

    def test_stopping_an_exited_process_is_quiet(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        chrome.stop(proc)


if __name__ == "__main__":
    unittest.main()
