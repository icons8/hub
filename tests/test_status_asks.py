"""Open cards to the owner (qN) are visible in `hub status` (13.10: an executor was compiling a summary of what waits
for the owner and found the card only in the journal; `hub question show` refused it — the hub shows a card in its own
chat)."""
import tests  # noqa: F401 — keep the machine's settings out
import os
import subprocess
import sys
import tempfile
import unittest

from core import journal

HUB_CLI = os.path.join(os.path.dirname(__file__), "..", "bin", "hub")
OWNER = {"channel": "cli", "owner": True}
Q = {"questions": [{"question": "Which tiles to use?", "options": [{"label": "Raster"}, {"label": "Vector"}]}]}


class StatusAsks(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="st-asks-")
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        j.append("session.requested", "hub", {"cwd": "/h", "session_id": "s-hub", "kind": "hub", **OWNER})
        j.append("session.requested", "maps", {"cwd": "/m", "session_id": "s-m", **OWNER})
        j.append("session.started", "hub", {"session_id": "s-hub"})
        j.append("session.started", "maps", {"session_id": "s-m"})
        j.append("turn.started", "maps", {"marker": None, "prompt_head": "x"})
        j.append("permission.requested", "maps", {"request_id": "r1", "tool": "AskUserQuestion", "interactive": True,
                                                  "input": Q})

    def hub(self, *args, me=None):
        env = {**os.environ, "HUB_STATE_DIR": self.state}
        env.pop("HUB_SESSION", None)
        if me:
            env["HUB_SESSION"] = me
        return subprocess.run([sys.executable, HUB_CLI, *args], capture_output=True, text=True, env=env)

    def qid(self):
        import json
        st = json.loads(self.hub("status", "--json").stdout)
        (ask,) = st["sessions"]["maps"]["asks"]
        return ask["id"]

    def test_an_open_card_is_listed_under_its_session(self):
        out = self.hub("status").stdout
        line = next((ln for ln in out.splitlines() if self.qid() in ln), None)
        self.assertIsNotNone(line, out)
        self.assertIn("Which tiles to use?", line)
        self.assertIn("waits for the owner", line)

    def test_show_from_a_non_hub_session_points_to_status(self):
        r = self.hub("question", "show", self.qid(), me="maps")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("hub status", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
