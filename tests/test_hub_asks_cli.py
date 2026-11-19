"""`hub answer hub …` also answers a hub question handed over as a card: the card is not in `pending` but in
`asks` — the command used to say "hub is not asking anything"."""
import tests  # noqa: F401 — ignore this machine's settings
import os
import subprocess
import sys
import tempfile
import unittest

from core import journal, spool

HUB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")


class AnswerHubCard(unittest.TestCase):
    def test_answer_reaches_the_hub_card(self):
        state = tempfile.mkdtemp(prefix="asks-")
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        j.append("session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", "channel": "cli", "owner": True})
        j.append("session.started", "hub", {"session_id": "s-h"})
        j.append("turn.started", "hub", {"marker": None, "prompt_head": "x"})
        j.append("permission.requested", "hub", {"request_id": "h1", "tool": "AskUserQuestion", "interactive": True,
                                                 "input": {"questions": [{"question": "Merge?", "options": [{"label": "Yes"}]}]}})
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0"}
        env.pop("HUB_SESSION", None)
        r = subprocess.run([sys.executable, HUB, "answer", "hub", "Yes"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        evs = [ev for _, ev in spool.take(os.path.join(state, "spool"))]
        (ans,) = [e for e in evs if e["type"] == "permission.answered"]
        self.assertEqual((ans["data"]["request_id"], ans["data"]["answers"]), ("h1", {"Merge?": "Yes"}))


if __name__ == "__main__":
    unittest.main()
