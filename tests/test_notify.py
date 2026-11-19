"""`hub notify` (owner decision 2026-09-30: monitors go on the schedule): a schedule command job decides itself what
to tell the owner — an owner.notify fact, which Telegram sends to the owner. A session cannot write this way: its word
to the owner is the turn result."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import subprocess
import sys
import tempfile
import unittest

from core import reducer, spool

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")


class Notify(unittest.TestCase):
    def cli(self, *args, **env):
        state = tempfile.mkdtemp(prefix="notify-")
        e = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0", **env}
        r = subprocess.run([sys.executable, HUB, "notify", *args], env=e, capture_output=True, text=True,
                           input="from stdin")
        return r, [ev for _, ev in spool.take(os.path.join(state, "spool"))]

    def test_notify_from_monitor(self):
        r, evs = self.cli("--urgent", "--title", "Shop", "the gate is closed", HUB_SCHEDULE_JOB="gate")
        self.assertEqual(r.returncode, 0, r.stderr)
        (ev,) = evs
        self.assertEqual(ev["type"], "owner.notify")
        self.assertEqual({k: ev["data"][k] for k in ("text", "title", "urgent", "source")},
                         {"text": "the gate is closed", "title": "Shop", "urgent": True, "source": "gate"})
        _, evs = self.cli("-")
        self.assertEqual(evs[0]["data"]["text"], "from stdin")

    def test_an_id_and_its_resolve_from_the_schedule(self):
        # a schedule job names a notification and clears it when the cause is gone (push spec, §2.6)
        _, evs = self.cli("--id", "gate", "--topic", "Gate", "--urgency", "now", "--due", "18:00", "closed",
                          HUB_SCHEDULE_JOB="gate")
        self.assertEqual({k: evs[0]["data"].get(k) for k in ("id", "topic", "urgency", "due", "text")},
                         {"id": "gate", "topic": "Gate", "urgency": "now", "due": "18:00", "text": "closed"})
        r, evs = self.cli("--resolve", "gate", HUB_SCHEDULE_JOB="gate")
        self.assertEqual(r.returncode, 0, r.stderr)
        (ev,) = evs
        self.assertEqual((ev["type"], ev["data"]["id"], ev["data"]["source"]), ("owner.notify_resolved", "gate", "gate"))

    def test_core_takes_notify_from_cli_not_from_session(self):
        st = reducer.empty()
        ok = {"text": "x", "channel": "cli", "owner": True}
        st, _, why = reducer.reduce(st, {"seq": 1, "ts": 1, "type": "owner.notify", "entity": None, "data": ok})
        self.assertIsNone(why)
        _, _, why = reducer.reduce(st, {"seq": 2, "ts": 2, "type": "owner.notify", "entity": None,
                                        "data": {"text": "x", "channel": "session", "sender": "w", "owner": False}})
        self.assertIsNotNone(why)


if __name__ == "__main__":
    unittest.main()
