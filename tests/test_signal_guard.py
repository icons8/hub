"""Stopping a process never signals a pid that is not a real child's. A test that mocks subprocess.Popen hands out a
MagicMock whose pid turns into 1, and killpg(1, SIGKILL) is kill(-1, SIGKILL): every process of the user, the hub too.
os.kill and os.killpg are replaced here, so the test sends nothing even when the guard is missing."""
import tests  # noqa: F401 — keeps the machine's settings out
import os
import sys
import unittest
from unittest import mock

import platforms
from modules.browser import run as browser_run
from modules.telegram import mermaid
from modules.testchrome import chrome as testchrome

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

BAD = [mock.MagicMock(), 1, 0, -1, None, True]


@unittest.skipIf(os.name == "nt", "process groups and signals: macOS and Linux")
class NoSignalForAStandInPid(unittest.TestCase):
    def setUp(self):
        self.kill = mock.patch("os.kill").start()
        self.killpg = mock.patch("os.killpg").start()
        self.addCleanup(mock.patch.stopall)

    def assertNothingSent(self):
        self.kill.assert_not_called()
        self.killpg.assert_not_called()

    def proc(self, pid, returncode=None):
        p = mock.MagicMock()
        p.pid, p.returncode = pid, returncode
        return p

    def test_real_pid(self):
        self.assertTrue(platforms.real_pid(12345))
        for pid in BAD:
            self.assertFalse(platforms.real_pid(pid), pid)

    def test_mermaid_stop(self):
        for pid in BAD:
            mermaid.stop(self.proc(pid))
        mermaid.stop(mock.MagicMock())
        self.assertNothingSent()

    def test_kill_detached_and_test_chrome_stop(self):
        for pid in BAD:
            platforms.current.kill_detached(self.proc(pid))
            testchrome.stop(self.proc(pid))
        self.assertNothingSent()

    def test_browser_kill_pid(self):
        with mock.patch("os.getpgid", side_effect=lambda pid: pid):
            for pid in BAD:
                browser_run.kill_pid(pid)
        self.assertNothingSent()

    def test_posix_helpers(self):
        from platforms import posix
        for pid in BAD:
            self.assertFalse(posix.alive(pid))
            posix.tty_stop(pid, grace=0)
        self.assertNothingSent()

    def test_a_collected_child_whose_number_went_to_another_process_is_not_signalled(self):
        # the audit of 03.11: once wait/poll collected the child, and its group is gone, its number may lead another group
        import compat

        def every_stop(code):
            mermaid.stop(self.proc(424242, code))
            platforms.current.kill_detached(self.proc(424242, code))
            testchrome.stop(self.proc(424242, code))
            compat._kill(self.proc(424242, code))
        for code in (0, -9, 1):
            every_stop(code)   # os.kill(pid, 0) answers: a process lives under the number
        self.killpg.assert_not_called()
        self.assertTrue(all(c.args[1] == 0 for c in self.kill.call_args_list), self.kill.call_args_list)
        # no process under the number: what is left of the group is the child's own (POSIX does not reuse a number
        # while its group exists), so it is stopped, as before (review: helpers of Chrome and claude leaked)
        self.kill.side_effect = ProcessLookupError
        every_stop(0)
        self.assertEqual(self.killpg.call_count, 4)

    def test_a_real_pid_is_still_signalled(self):
        mermaid.stop(self.proc(424242))
        self.killpg.assert_called_once()


if __name__ == "__main__":
    unittest.main()
