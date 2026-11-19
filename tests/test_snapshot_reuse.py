"""An update in one session must not recopy every other session's history."""
import tests  # noqa: F401
import os
import tempfile
import unittest

from core import journal, views
from core.tail import Tail


class PublishedTimelines(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.journal = journal.Journal(os.path.join(self.home.name, "journal.jsonl"))
        for name in ("a", "b"):
            self.journal.append("session.requested", name, {"cwd": "/work", "session_id": name})
            self.journal.append("session.said", name, {"text": "Initial " + name})
        self.tail = Tail(self.journal.path)
        self.tail.poll()

    def test_live_publish_reuses_unchanged_history_and_detaches_changed_rows(self):
        old = self.tail.snapshot
        self.journal.append("session.said", "a", {"text": "Next answer"})
        self.tail.poll()
        new = self.tail.snapshot
        self.assertIs(new["timelines"]["b"], old["timelines"]["b"])
        self.assertIsNot(new["timelines"]["a"], old["timelines"]["a"])
        self.assertFalse(any(x.get("text") == "Next answer" for x in old["timelines"]["a"]))
        self.assertTrue(any(x.get("text") == "Next answer" for x in new["timelines"]["a"]))
        row = self.tail.reader.timelines["a"][-1]
        row["text"] = "Changed in place"
        self.tail.reader.touch("a", row)
        self.tail._publish(True)
        self.assertEqual(new["timelines"]["a"][-1]["text"], "Next answer")
        self.assertEqual(self.tail.snapshot["timelines"]["a"][-1]["text"], "Changed in place")

    def test_removed_history_is_not_retained(self):
        self.tail.reader.state["sessions"]["b"]["state"] = "removed"
        self.tail._publish(True)
        self.assertNotIn("b", self.tail.snapshot["timelines"])

    def test_new_reader_does_not_reuse_old_generation(self):
        old = self.tail.snapshot
        fresh = views.Reader()
        fresh.feed_all(journal.read(self.journal.path))
        self.tail.reader = fresh
        self.tail._publish(True)
        self.assertNotEqual(old["epoch"], self.tail.snapshot["epoch"])
        self.assertIsNot(old["timelines"]["a"], self.tail.snapshot["timelines"]["a"])

    def test_ordinary_snapshots_remain_independent(self):
        a = self.tail.reader.snapshot()
        b = self.tail.reader.snapshot()
        a["timelines"]["a"][-1]["text"] = "Caller edit"
        self.assertNotEqual(b["timelines"]["a"][-1]["text"], "Caller edit")
        self.assertNotEqual(self.tail.reader.timelines["a"][-1]["text"], "Caller edit")

    def test_reused_snapshot_has_identical_contents_to_a_fresh_copy(self):
        old = self.tail.snapshot
        self.journal.append("session.said", "b", {"text": "Another answer"})
        self.tail.poll()
        reader = self.tail.reader
        self.assertEqual(reader.snapshot(now=123, previous=old), reader.snapshot(now=123))

    def test_visible_history_and_nested_tool_edits_keep_snapshot_isolation(self):
        reader = self.tail.reader
        tool = {"kind": "tool", "k": 50, "input": {"args": ["before"]}}
        reader.timelines["a"].append(tool)
        reader.touch("a", tool)
        self.tail._publish(True)
        old = self.tail.snapshot
        tool["input"]["args"].append("after")
        reader.touch("a", tool)
        self.tail._publish(True)
        self.assertEqual(old["timelines"]["a"][-1]["input"]["args"], ["before"])
        self.assertEqual(self.tail.snapshot["timelines"]["a"][-1]["input"]["args"], ["before", "after"])
