import json
import os
import tempfile
import unittest

import platforms
from core import journal, spool


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "journal.jsonl")

    def test_append_read_and_seq_continues(self):
        j = journal.Journal(self.path)
        j.append("a", "x", {"k": 1}, ts=1)
        j.append("b", ts=2)
        j2 = journal.Journal(self.path)
        self.assertEqual(j2.append("c", ts=3)["seq"], 3)
        self.assertEqual([e["type"] for e in journal.read(self.path)], ["a", "b", "c"])

    def test_torn_tail_is_dropped_and_repaired(self):
        """Power loss in mid-write: the tail is cut off and numbering continues from the last whole event."""
        j = journal.Journal(self.path)
        j.append("a", ts=1)
        with open(self.path, "ab") as f:
            f.write(b'{"seq":2,"ts":2,"ty')
        self.assertEqual(len(journal.read(self.path)), 1)
        j2 = journal.Journal(self.path)
        self.assertEqual(j2.append("b", ts=3)["seq"], 2)
        self.assertEqual([e["type"] for e in journal.read(self.path)], ["a", "b"])

    def test_a_piece_of_a_failed_write_is_not_glued_to_the_next_event(self):
        """A full disk left a line without a newline; the next event stuck to it and readers stalled."""
        j = journal.Journal(self.path)
        j.append("a", ts=1)
        with open(self.path, "ab") as f:
            f.write(b'{"seq":2,"ts":2,"ty')
        j.append("b", ts=3)
        self.assertEqual([e["type"] for e in journal.read(self.path)], ["a", "b"])

    def test_a_failed_write_leaves_nothing(self):
        """A fact and its rejection are one record: all or nothing (a fragment of a fact without its rejection would be taken by the CLI as accepted)."""
        from unittest import mock
        j = journal.Journal(self.path)
        j.append("a", ts=1)
        before = open(self.path, "rb").read()
        with mock.patch.object(journal.os, "fsync", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                j.write(j.event("b", ts=2), j.event("core.rejected", ts=2))
        self.assertEqual(open(self.path, "rb").read(), before)

    def test_repair_keeps_what_it_cuts(self):
        """A corrupt line in the middle cut off all events after it, with no copy kept."""
        j = journal.Journal(self.path)
        j.append("a", ts=1)
        with open(self.path, "ab") as f:
            f.write(b'{"seq":2,"ts":2,"ty{"seq":3,"ts":3,"src":"core","type":"b","entity":null,"data":{}}\n')
            f.write(b'{"seq":4,"ts":4,"src":"core","type":"c","entity":null,"data":{}}\n')
        journal.Journal(self.path)
        self.assertEqual([e["type"] for e in journal.read(self.path)], ["a"])
        cut = os.path.join(self.dir, journal.ARCHIVE)
        (name,) = os.listdir(cut)
        self.assertTrue(name.startswith("cut-"), name)
        self.assertEqual(journal.archives(self.dir), [])   # not a journal archive: neither the feed nor the "Flow" reads it
        with open(os.path.join(cut, name), "rb") as f:
            self.assertIn(b'"type":"c"', f.read())

    def test_repair_of_a_torn_last_line_keeps_no_copy(self):
        j = journal.Journal(self.path)
        j.append("a", ts=1)
        with open(self.path, "ab") as f:
            f.write(b'{"seq":2,"ts":2,"ty')
        journal.Journal(self.path)
        self.assertFalse(os.path.exists(os.path.join(self.dir, journal.ARCHIVE)))


class LastStartedTest(unittest.TestCase):
    """journal.last_started: the core's start from the journal's end, without folding the journal."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, self.dir)
        self.path = os.path.join(self.dir, "journal.jsonl")

    def put(self, *lines):
        with open(self.path, "ab") as f:
            for line in lines:
                f.write(line if isinstance(line, bytes) else json.dumps(line, separators=(",", ":")).encode())
                f.write(b"\n")

    def ev(self, kind, ts, **data):
        return {"seq": 1, "ts": ts, "src": "core", "type": kind, "entity": None, "data": data}

    def test_the_last_start_wins_and_no_journal_is_none(self):
        self.assertIsNone(journal.last_started(self.path))
        self.put(self.ev("core.started", 10.5), self.ev("message.received", 11, text="hi"), self.ev("core.started", 20.25),
                 self.ev("message.received", 21, text="after"))
        self.assertEqual(journal.last_started(self.path), 20.25)

    def test_words_in_a_message_are_not_a_start(self):
        # a message that holds the marker, a raw line that only looks like it, and a last line cut before its newline
        self.put(self.ev("core.started", 5.0), self.ev("message.received", 6, text='"type":"core.started"'),
                 b'{"type":"core.started","ts":"x"}')
        with open(self.path, "ab") as f:
            f.write(b'{"seq":9,"ts":99.5,"src":"core","type":"core.started","entity":nu')
        self.assertEqual(journal.last_started(self.path), 5.0)

    def test_every_place_of_the_start_in_the_blocks_is_found(self):
        for pad in range(0, 70):
            self.put(self.ev("message.received", 1, text="y" * pad), self.ev("core.started", 40.0 + pad),
                     self.ev("message.received", 2, text="z" * (pad % 7)))
            self.assertEqual(journal.last_started(self.path, block=64), 40.0 + pad, pad)
        os.unlink(self.path)
        self.put(self.ev("core.started", 3.0))   # the first line of the file
        self.assertEqual(journal.last_started(self.path, block=16), 3.0)

    def test_a_start_out_of_reach_is_none(self):
        self.put(self.ev("core.started", 5.0), *[self.ev("message.received", 6, text="x" * 200) for _ in range(40)])
        self.assertIsNone(journal.last_started(self.path, max_bytes=4000))
        self.assertEqual(journal.last_started(self.path), 5.0)

    def test_a_cut_right_after_a_newline_keeps_the_line(self):
        start, after = self.ev("core.started", 7.5), self.ev("message.received", 8, text="x")
        self.put(self.ev("message.received", 1, text="before"), start, after)
        tail = len(json.dumps(start, separators=(",", ":"))) + len(json.dumps(after, separators=(",", ":"))) + 2
        self.assertEqual(journal.last_started(self.path, max_bytes=tail), 7.5)   # the window begins at the start's line
        self.assertIsNone(journal.last_started(self.path, max_bytes=tail - 1))   # one byte less: the line is cut


class SpoolTest(unittest.TestCase):
    def test_put_take_in_order_and_bad_file_skipped(self):
        d = tempfile.mkdtemp()
        spool.put(d, "one")
        with open(os.path.join(d, "00000000000000000001-bad.json"), "w") as f:
            f.write("{not json")
        spool.put(d, "two", "s", {"x": 1})
        items = spool.take(d)
        self.assertEqual([e["type"] for _, e in items], ["one", "two"])
        self.assertTrue(any(n.endswith(".bad") for n in os.listdir(d)))

    def test_order_holds_within_one_clock_tick(self):
        # 08.10, Windows: the clock there ticks in millisecond steps, so two facts of one process got the same time in
        # the name and a random tail decided the order (owner.notify after update.checked)
        from unittest import mock
        d = tempfile.mkdtemp()
        with mock.patch.object(spool.time, "time_ns", return_value=1_000_000):
            for i in range(30):
                spool.put(d, f"e{i}")
        self.assertEqual([e["type"] for _, e in spool.take(d)], [f"e{i}" for i in range(30)])


if __name__ == "__main__":
    unittest.main()


class RawStartIsTheFold(unittest.TestCase):
    """What last_started reads from the journal's end is what the page's fold holds as the core's start."""

    def test_the_same_as_the_state_after_a_whole_pass(self):
        from core.tail import Tail
        d = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, d)
        path = os.path.join(d, "journal.jsonl")
        j = journal.Journal(path)
        j.append("core.started", None, {"pid": 1})
        j.append("message.received", None, {"text": '"type":"core.started"\n{"seq":1,"ts":1.5,"src":"core","type":"core.started"'})
        j.append("core.started", None, {"pid": 2})
        j.append("message.received", None, {"text": "x" * 70000})
        tail = Tail(path, page=False)
        tail.poll()
        self.assertEqual(journal.last_started(path), tail.started)
        self.assertEqual(tail.core_started(), tail.started)
