"""State snapshot: new code does not reread old events by new rules (failure of 29.09 — after the sleep state machine
changed, old "put to sleep" events were read as "hand over your work", and the core woke six sleeping sessions)."""
import os
import shutil
import tempfile
import unittest

import tests  # noqa: F401
from core import journal, reducer, views
from core.daemon import Hub


class NullHost:
    screen_dialogs = False

    def __init__(self):
        self.tool_flags = lambda **_: []

    def alive(self, name):
        return False

    def poll(self):
        return []

    def info(self, name):
        return None


class SnapshotTest(unittest.TestCase):
    def test_replay_starts_from_last_snapshot(self):
        asleep = {**reducer.empty(), "sessions": {"a": {"name": "a", "state": "asleep", "kind": "work"}}}
        events = [
            {"seq": 1, "ts": 0, "type": "session.requested", "entity": "zz", "data": {"session_id": "x"}},
            {"seq": 2, "ts": 1, "type": "core.snapshot", "entity": None, "data": {"why": "stop", "state": asleep}},
            {"seq": 3, "ts": 2, "type": "turn.stopped", "entity": "a", "data": {}},
        ]
        st = reducer.replay(events)
        self.assertEqual(list(st["sessions"]), ["a"])  # nothing before the snapshot is reread
        self.assertEqual(st["sessions"]["a"]["state"], "asleep")
        self.assertEqual(st["seq"], 3)
        r = views.Reader()
        for e in events:
            r.feed(e)
        self.assertEqual([s["name"] for s in r.snapshot()["sessions"]], ["a"])

    def test_hubd_writes_snapshot_and_restart_uses_it(self):
        d = tempfile.mkdtemp()
        try:
            hub = Hub(d, NullHost(), registry_dir=d)
            hub.record("session.requested", "a", {"session_id": "s", "cwd": "/w"})
            hub.snapshot("stop")
            hub.record("turn.stopped", "zz")  # will be rejected — but after the snapshot
            again = Hub(d, NullHost(), registry_dir=d)
            self.assertEqual(again.state["sessions"]["a"]["state"], "starting")
            kinds = [e["type"] for e in journal.read(os.path.join(d, "journal.jsonl"))]
            self.assertIn("core.snapshot", kinds)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
