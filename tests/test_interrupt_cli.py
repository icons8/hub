"""Abort the current turn of any session from the command line, like Stop on the page (Esc in Claude Code): the session
stays. `hub session stop` only stops an executor; a standing agent waiting on a background subagent is not interrupted by
reload or --now, so without this command the only way left was a signal to its process."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from core import reducer, spool

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")
FROM_HUB = {"channel": "session", "owner": False, "sender": "hub"}


class Cli(unittest.TestCase):
    def test_command_asks_core_to_interrupt(self):
        state = tempfile.mkdtemp(prefix="interrupt-")
        self.addCleanup(shutil.rmtree, state, True)
        e = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0"}
        e.pop("HUB_SESSION", None)
        r = subprocess.run([sys.executable, HUB, "session", "interrupt", "agent", "other"], env=e,
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = [(ev["type"], ev["entity"]) for _, ev in spool.take(os.path.join(state, "spool"))]
        self.assertEqual(got, [("session.interrupt_requested", "agent"), ("session.interrupt_requested", "other")])


class Core(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", channel="cli", owner=True)
        self.ev("session.requested", "agent", cwd="/a", session_id="s-a", **FROM_HUB)
        for n in ("hub", "agent"):
            self.ev("session.started", n)
        self.ev("turn.started", "agent", marker="m1")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, actions, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.seq, "type": type,
                                                         "entity": entity, "data": data})
        return actions, why

    def test_hub_interrupts_a_busy_agent_and_it_stays(self):
        actions, why = self.ev("session.interrupt_requested", "agent", **FROM_HUB)
        self.assertIsNone(why)
        self.assertEqual([a["do"] for a in actions if a.get("session") == "agent"], ["interrupt"])
        self.assertNotIn(self.st["sessions"]["agent"]["state"], ("asleep", "closed", "removed", "retired"))

    def test_stop_of_an_agent_points_to_interrupt(self):
        _, why = self.ev("session.stop_requested", "agent", **FROM_HUB)
        self.assertIn("hub session interrupt", why)


if __name__ == "__main__":
    unittest.main()
