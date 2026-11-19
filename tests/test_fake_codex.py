"""The fake `codex app-server` (tests/fakes/fake_codex.py) behaves like codex-cli 0.162.1 where the hosts depend on it
(probe 2026-11-05 with an own CODEX_HOME and a dead model provider, no model call):
- an experimental method or field without capabilities.experimentalApi in initialize is refused with -32600 and the
  real text — a host that forgets the flag is red in tests, not only on a live Codex;
- thread/start is a new conversation: no history and no tokens of the thread before it."""
import tests  # noqa: F401 — keep the machine's settings out
import json
import os
import subprocess
import sys
import tempfile
import unittest

import platforms

FAKE = os.path.join(os.path.dirname(__file__), "fakes", "fake_codex.py")


class Fake:
    def __init__(self, home, experimental=False):
        env = {**os.environ, "CODEX_HOME": home, "FAKE_TURN_S": "0.05"}
        self.p = subprocess.Popen([sys.executable, FAKE], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
                                  text=True, encoding="utf-8")
        self.n = 0
        init = {"clientInfo": {"name": "t", "version": "0"}}
        if experimental:
            init["capabilities"] = {"experimentalApi": True}
        self.call("initialize", init)

    def call(self, method, params=None):
        self.n += 1
        self.p.stdin.write(json.dumps({"id": self.n, "method": method, "params": params or {}}) + "\n")
        self.p.stdin.flush()
        return self.wait(lambda m: m.get("id") == self.n)

    def wait(self, pred):
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise AssertionError("the fake exited")
            msg = json.loads(line)
            if pred(msg):
                return msg

    def turn(self, thread, text):
        self.call("turn/start", {"threadId": thread, "input": [{"type": "text", "text": text}]})
        done = self.wait(lambda m: m.get("method") == "turn/completed")
        return done["params"]["turn"]["items"][-1]["text"]

    def close(self):
        self.p.stdin.close()
        self.p.wait(5)


class FakeCodex(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="fakecx-")
        self.addCleanup(platforms.current.rmtree, self.home)

    def fake(self, experimental=False):
        f = Fake(self.home, experimental)
        self.addCleanup(f.close)
        return f

    def test_experimental_without_the_capability_is_refused_as_codex_does(self):
        f = self.fake()
        thread = f.call("thread/start", {"cwd": self.home})["result"]["thread"]["id"]
        got = f.call("thread/backgroundTerminals/list", {"threadId": thread})
        self.assertEqual(got.get("error"), {"code": -32600, "message":
                                            "thread/backgroundTerminals/list requires experimentalApi capability"})
        got = f.call("turn/start", {"threadId": thread, "input": [{"type": "text", "text": "x"}],
                                    "additionalContext": {"a": {"kind": "application", "value": "v"}}})
        self.assertEqual(got["error"]["message"], "turn/start.additionalContext requires experimentalApi capability")
        # null is not a value: Codex lets it through
        self.assertIn("result", f.call("thread/start", {"cwd": self.home, "dynamicTools": None}))

    def test_with_the_capability_it_goes(self):
        f = self.fake(experimental=True)
        thread = f.call("thread/start", {"cwd": self.home})["result"]["thread"]["id"]
        self.assertIn("result", f.call("thread/backgroundTerminals/list", {"threadId": thread}))
        self.assertIn("result", f.call("turn/start", {"threadId": thread, "input": [{"type": "text", "text": "x"}],
                                                      "additionalContext": {"a": {"kind": "application", "value": "v"}}}))

    def test_a_new_thread_has_nothing_of_the_previous_one(self):
        f = self.fake()
        one = f.call("thread/start", {"cwd": self.home})["result"]["thread"]["id"]
        f.turn(one, "one")
        self.assertIn("remembers", f.turn(one, "two"))   # the same thread remembers
        two = f.call("thread/start", {"cwd": self.home})["result"]["thread"]["id"]
        self.assertNotIn("remembers", f.turn(two, "three"))
        f.call("turn/start", {"threadId": two, "input": [{"type": "text", "text": "four"}]})
        usage = f.wait(lambda m: m.get("method") == "thread/tokenUsage/updated")["params"]["tokenUsage"]["total"]
        self.assertEqual(usage["inputTokens"], 100 + 110 + 100)   # the second thread's own turns only


if __name__ == "__main__":
    unittest.main()
