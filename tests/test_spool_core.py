"""Only the core writes `core.*` events (16.10): the inbox used to accept them from any process. hubd wrote a forged
`core.snapshot` without changing its own state, while journal readers (page, bot, history) replaced all their state
with it; readers took a forged `core.rejected` with `of` of the previous line for a refusal of an already accepted
event. The inbox rejects such a fact: the journal gets a stub without data and a rejection after it, like for any
core rejection."""
import tests  # noqa: F401
import json
import os
import shutil
import tempfile
import types
import unittest

from core import journal, reducer, spool
from core.daemon import Hub

OWNER = {"channel": "cli", "owner": True}


class CoreEventsFromTheSpool(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="spool-core-")
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        self.h = Hub(self.state, types.SimpleNamespace())
        self.h.execute = lambda action: None         # no spawning, no printing: the journal only
        self.h.record("session.requested", "maps", {"cwd": "/m", "session_id": "s-m", **OWNER})

    def events(self):
        return journal.read(self.h.journal.path)

    def reader(self):
        """State as journal readers (page, bot) see it: a fold of what is written, from scratch."""
        return reducer.replay(self.events())

    def test_a_forged_snapshot_does_not_replace_the_state_of_readers(self):
        spool.put(self.h.spool_dir, "core.snapshot", None,
                  {"why": "periodic", "state": {**reducer.empty(), "sessions": {}}}, src="cli")
        self.h.ingest()
        self.assertIn("maps", self.h.state["sessions"])
        self.assertIn("maps", self.reader()["sessions"])
        self.assertEqual([e for e in self.events() if e["type"] == "core.snapshot"], [])

    def test_a_forged_rejection_does_not_cancel_the_event_before_it(self):
        spool.put(self.h.spool_dir, "session.requested", "docs", {"cwd": "/d", "session_id": "s-d", **OWNER})
        self.h.ingest()
        before = self.events()[-1]["seq"]
        spool.put(self.h.spool_dir, "core.rejected", None, {"of": before, "type": "session.requested",
                                                            "reason": "no"}, src="cli")
        self.h.ingest()
        self.assertIn("docs", self.h.state["sessions"])
        self.assertIn("docs", self.reader()["sessions"])

    def test_the_command_that_put_it_sees_the_refusal_with_the_reason(self):
        spool.put(self.h.spool_dir, "core.started", None, {"pid": 1, "req": "r1", **OWNER}, src="cli")
        self.h.ingest()
        seq, why = journal.decided(self.h.journal.path, "r1", 0.3)
        self.assertIsNotNone(seq)
        self.assertIn("core.started", why or "")
        stand_in = next(e for e in self.events() if e["seq"] == seq)
        self.assertFalse(stand_in["type"].startswith("core."))
        self.assertNotIn("pid", stand_in["data"])   # the data of the forged fact does not reach the journal

    def test_the_cores_own_events_are_still_written(self):
        self.h.snapshot("periodic")
        self.h.record("core.pruned", data={"before": 0})
        self.assertEqual([e["type"] for e in self.events()][-2:], ["core.snapshot", "core.pruned"])
        self.assertIn("maps", self.reader()["sessions"])

    def test_the_spool_file_is_removed(self):
        spool.put(self.h.spool_dir, "core.snapshot", None, {"state": {}}, src="cli")
        self.h.ingest()
        self.assertEqual(spool.pending(self.h.spool_dir), 0)
        self.assertEqual([n for n in os.listdir(self.h.spool_dir) if n.endswith(".bad")], [])


if __name__ == "__main__":
    unittest.main()
