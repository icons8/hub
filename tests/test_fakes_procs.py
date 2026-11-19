"""Cleanup of processes left by tests (tests/fakes/procs.py) on POSIX without /proc — macOS. After signal 0,
alive() read /proc/<pid>/stat, which does not exist on macOS — every live process came out "dead", kill() put out
nothing, and module processes from tests (fake_module.py, main.py) piled up in the test account on the Mac for days,
while the full run hung there."""
import tests  # noqa: F401 — do not see the machine's settings
import builtins
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests.fakes import procs

POSIX = unittest.skipIf(os.name == "nt", "POSIX: on Windows it is the process exit code, /proc has nothing to do with it")


def without_proc():
    """As on macOS: no /proc at all."""
    real_open, real_isdir = builtins.open, os.path.isdir

    def no_proc_open(path, *a, **kw):
        if str(path).startswith("/proc/"):
            raise FileNotFoundError(path)
        return real_open(path, *a, **kw)

    def no_proc_isdir(path):
        return False if str(path).startswith("/proc") else real_isdir(path)
    return mock.patch.multiple(builtins, open=no_proc_open), mock.patch.object(os.path, "isdir", no_proc_isdir)


@POSIX
class WithoutProc(unittest.TestCase):
    def sleeper(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL)
        self.addCleanup(p.wait)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def test_a_live_process_is_alive_and_kill_takes_it(self):
        p = self.sleeper()
        a, b = without_proc()
        with a, b:
            self.assertTrue(procs.alive(p.pid))
            procs.kill(p.pid)
        self.assertIsNotNone(p.wait(10), "kill() without /proc did not stop the process")

    def test_a_zombie_is_dead(self):
        p = subprocess.Popen([sys.executable, "-c", ""], stdin=subprocess.DEVNULL)
        self.addCleanup(p.wait)
        end = time.time() + 10
        a, b = without_proc()
        with a, b:
            while procs.alive(p.pid) and time.time() < end:   # exited but not reaped by wait — a zombie
                time.sleep(0.05)
            self.assertFalse(procs.alive(p.pid))

    def test_a_gone_process_is_dead(self):
        p = subprocess.Popen([sys.executable, "-c", ""], stdin=subprocess.DEVNULL)
        p.wait()
        a, b = without_proc()
        with a, b:
            self.assertFalse(procs.alive(p.pid))


@unittest.skipUnless(os.name == "nt", "Windows: the number of an exited process is soon given to another")
class ForeignNumber(unittest.TestCase):
    def test_a_process_started_after_the_record_is_left_alone(self):
        # cleanup kills by a pid read from a journal with taskkill /T; the recorded process had already exited, and
        # its number could belong to another process — it died with its whole tree. "Another" here is our own sleeper,
        # started after the record. kill() waits for the process it stopped to be gone: poll() right after
        # TerminateProcess could still see it running, and the test stayed green without the check
        recorded = time.time()
        time.sleep(0.1)   # process start times are rounded to the system tick
        p = subprocess.Popen([sys.executable, "-c", "import time; print('up', flush=True); time.sleep(60)"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.stdout.close)
        self.addCleanup(p.wait, 10)
        self.addCleanup(p.kill)
        procs.kill(p.pid, recorded)
        self.assertIsNone(p.poll())
        self.assertEqual(p.stdout.readline().strip(), "up")
        procs.kill(p.pid, time.time())   # recorded after it said it runs (its Python start is far beyond a tick): it is the one
        p.wait(10)
        self.assertFalse(procs.alive(p.pid))


if __name__ == "__main__":
    unittest.main()
