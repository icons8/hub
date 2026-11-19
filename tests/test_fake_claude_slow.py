"""The fake claude's long turn (/slow) on every OS. Earlier it waited for an interrupt with select() on stdin, which on
Windows works on sockets only — on the pipe it raised at once, the "20 s turn" ended at its start, and the live hubd
tests that need a busy session (test_stream_hubd, test_auto_executors_live) went red there under load: the core saw the
start and the end of the turn in one tick."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest

import platforms

FAKE = os.path.join(os.path.dirname(__file__), "fakes", "fake_claude_stream.py")


class SlowTurn(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="fake-slow-")
        self.addCleanup(platforms.current.rmtree, self.dir)
        env = {**os.environ, "HUB_STATE_DIR": self.dir, "FAKE_TURN_S": "0", "PYTHONUTF8": "1"}
        self.p = subprocess.Popen([sys.executable, FAKE, "--session-id", "sid-slow"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=self.dir)
        self.addCleanup(self.p.wait)
        self.addCleanup(lambda: self.p.poll() is None and self.p.kill())
        for f in (self.p.stdout, self.p.stderr):
            self.addCleanup(f.close)
        self.lines = queue.Queue()
        threading.Thread(target=self.pump, daemon=True).start()
        self.assertEqual(self.next_line(10)["subtype"], "init")

    def pump(self):
        for line in self.p.stdout:
            self.lines.put(json.loads(line))

    def next_line(self, timeout):
        try:
            return self.lines.get(timeout=timeout)
        except queue.Empty:
            return None

    def write(self, obj):
        self.p.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.p.stdin.flush()

    def test_the_turn_lasts_until_an_interrupt(self):
        self.write({"type": "user", "message": {"role": "user", "content": "/slow"}})
        early = self.next_line(2)   # a turn still going: nothing comes out
        self.assertIsNone(early, f"the /slow turn ended at once: {early}; stderr: "
                          + (self.p.stderr.read1(4000).decode("utf-8", "replace") if self.p.poll() is not None else ""))
        self.assertIsNone(self.p.poll())
        self.write({"type": "control_request", "request_id": "r1", "request": {"subtype": "interrupt"}})
        got = [self.next_line(10), self.next_line(10)]
        self.assertEqual(got[0]["type"], "control_response")
        self.assertEqual((got[1]["type"], got[1]["subtype"]), ("result", "error_during_execution"))
        self.p.stdin.close()   # stdin closed: the fake leaves by itself (SessionEnd)
        self.assertEqual(self.p.wait(10), 0)


if __name__ == "__main__":
    unittest.main()
