"""hub status: "not yet collected in the spool" counts only ready facts (2026-10-09: an empty .tmp from a put killed
halfway gave "hubd is not running?" while hubd was alive)."""
import tests  # noqa: F401 — hide the machine's settings
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from core import journal, lock, spool


class Pending(unittest.TestCase):
    def test_counts_ready_facts_only(self):
        d = os.path.join(tempfile.mkdtemp(), "spool")
        self.assertEqual(spool.pending(d), 0)   # there is no spool yet
        spool.put(d, "a.b")
        spool.put(d, "a.c")
        open(os.path.join(d, ".0001-1-abcdef.tmp"), "w").close()   # put died before replace
        self.assertEqual(spool.pending(d), 2)


class Stalled(unittest.TestCase):
    """`hub status` right after a restart said "not yet taken from the mailbox (is hubd not running?)" while hubd was starting."""
    def setUp(self):
        self.state = tempfile.mkdtemp()
        self.spool = os.path.join(self.state, "spool")

    def put_aged(self, seconds):
        """A waiting fact whose name carries the time `seconds` ago."""
        os.makedirs(self.spool, exist_ok=True)
        stamp = int((time.time() - seconds) * 1e9)
        with open(os.path.join(self.spool, f"{stamp:020d}-1-abcdef.json"), "w") as f:
            f.write("{}")

    def test_an_empty_mailbox_and_a_young_fact_are_not_a_warning(self):
        self.assertIsNone(spool.stalled(self.spool, self.state))
        self.put_aged(2)
        self.assertIsNone(spool.stalled(self.spool, self.state))   # hubd is between two runs: seconds

    def pulse(self, ago):
        """hubd's pulse file (core/daemon.py ALIVE) touched `ago` seconds ago."""
        path = os.path.join(self.state, spool.PULSE)
        open(path, "w").close()
        os.utime(path, (time.time() - ago,) * 2)

    def test_the_pulse_name_is_the_one_hubd_writes(self):
        from core import daemon
        self.assertEqual(spool.PULSE, daemon.ALIVE)

    def test_an_old_fact_with_no_pulse_or_a_stale_one_is_down(self):
        self.put_aged(spool.RESTART_GAP + 5)
        self.assertEqual(spool.stalled(self.spool, self.state), "down")   # hubd never ran here
        self.pulse(spool.PULSE_FRESH + 5)
        self.assertEqual(spool.stalled(self.spool, self.state), "down")

    def test_a_file_not_named_by_us_is_not_hiding_the_warning(self):
        self.put_aged(1)
        open(os.path.join(self.spool, "a-stray.json"), "w").close()   # no time in its name
        self.assertEqual(spool.stalled(self.spool, self.state), "down")

    def test_with_a_fresh_pulse_it_is_only_slow_after_a_minute(self):
        self.pulse(1)
        self.put_aged(spool.RESTART_GAP + 5)
        self.assertIsNone(spool.stalled(self.spool, self.state))
        os.remove(os.path.join(self.spool, os.listdir(self.spool)[0]))
        self.put_aged(spool.SLOW_START + 5)
        self.assertEqual(spool.stalled(self.spool, self.state), "slow")

    def test_the_status_never_takes_the_lock_of_hubd(self):
        # a probe that held hubd.lock could make a hubd starting at that moment think another one runs (it exits)
        self.put_aged(spool.SLOW_START + 5)
        self.pulse(1)
        with mock.patch.object(lock, "acquire", side_effect=AssertionError("hubd.lock taken")), \
                mock.patch.object(lock, "hold", side_effect=AssertionError("hubd.lock taken")):
            spool.stalled(self.spool, self.state)
        self.assertEqual(os.listdir(self.state).count("hubd.lock"), 0)   # and not even created
        self.hub_status()
        self.assertEqual(os.listdir(self.state).count("hubd.lock"), 0)

    def hub_status(self):
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_LANG": "en"}
        env.pop("HUB_SESSION", None)
        return subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), "..", "bin", "hub"), "status"],
                              capture_output=True, text=True, env=env).stdout

    def test_hub_status_warns_about_an_old_fact_not_about_a_fresh_one(self):
        journal.Journal(os.path.join(self.state, "journal.jsonl")).append("session.requested", "hub", {
            "cwd": "/h", "session_id": "s-hub", "kind": "hub", "channel": "cli", "owner": True})
        self.put_aged(1)
        self.assertNotIn("mailbox", self.hub_status())
        os.remove(os.path.join(self.spool, os.listdir(self.spool)[0]))
        self.put_aged(120)
        self.assertIn("not yet taken from the mailbox: 1 (is hubd not running?)", self.hub_status())


if __name__ == "__main__":
    unittest.main()
