"""Web restart resumes a complete reader; stale or damaged checkpoints replay normally."""
import tests  # noqa: F401 - isolate machine settings
import hashlib
import hmac
import io
import os
import pickle
import tempfile
import unittest
from unittest import mock

from core import i18n, journal, reader_cache, reducer, views
from core.tail import Tail, _cache_key

OWNER = {"channel": "web", "owner": True}


def save(tail):
    """A due write right now, the way Tail.poll does it (capture under its lock, write after)."""
    tail.checkpoint.next_write = 0
    with tail.reading:
        job = tail.checkpoint.capture(tail.reader, tail.offset, tail.ino)
    if job:
        tail.checkpoint.write(job)


class ReaderCheckpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="web-checkpoint-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "journal.jsonl")
        self.j = journal.Journal(self.path)
        self.j.append("session.requested", "a", {"cwd": "/w", "session_id": "s-a", **OWNER})
        self.j.append("message.received", None, {"to": "a", "text": "Earlier message", **OWNER})

    def saved(self):
        tail = Tail(self.path, checkpoint=True)
        self.assertTrue(tail.poll())
        return tail

    def equal_reader(self, first, second):
        a, b = first.reader.snapshot(now=100), second.reader.snapshot(now=100)
        self.assertNotEqual(a.pop("epoch"), b.pop("epoch"))
        self.assertEqual(a, b)

    def test_restart_does_not_refold_checkpointed_events_and_keeps_all_histories(self):
        first = self.saved()
        second = Tail(self.path, checkpoint=True)
        with mock.patch.object(views.Reader, "feed_all", side_effect=AssertionError("replayed checkpoint")):
            self.assertTrue(second.poll())
        self.assertTrue(second.ready)
        self.assertEqual(second.offset, first.offset)
        self.equal_reader(first, second)
        self.assertEqual(first.snapshot["timelines"], second.snapshot["timelines"])

    def test_only_new_tail_is_folded_and_history_matches_uninterrupted_reader(self):
        first = self.saved()
        self.j.append("message.received", None, {"to": "a", "text": "New message", **OWNER})
        first.poll()
        second = Tail(self.path, checkpoint=True)
        original, counts = views.Reader.feed_all, []

        def feed(reader, events):
            counts.append(len(events))
            return original(reader, events)
        with mock.patch.object(views.Reader, "feed_all", feed):
            second.poll()
        self.assertEqual(counts, [1])
        self.equal_reader(first, second)

    def test_published_plugin_posts_survive_restore_tail_and_rotation(self):
        self.j.append("ext.post", None, {"plugin": "inbox", "text": "First", **OWNER})
        first = self.saved()
        restored = Tail(self.path, checkpoint=True)
        with mock.patch.object(views.Reader, "feed_all", side_effect=AssertionError("replayed checkpoint")):
            restored.poll()
        self.assertEqual(restored.feed_posts, first.feed_posts)
        self.j.append("ext.post", None, {"plugin": "inbox", "text": "Second", **OWNER})
        restored.poll()
        self.assertEqual(list(restored.feed_posts["inbox"]), restored.reader.feed_since("inbox", 0))
        journal.rotate(self.tmp.name, restored.reader.state)
        restored.poll()
        fresh = Tail(self.path)
        fresh.poll()
        self.assertEqual(restored.feed_posts, fresh.feed_posts)
        self.assertEqual([p["text"] for p in fresh.feed_posts["inbox"]], ["First", "Second"])

    def test_partial_line_is_not_checkpointed_or_lost(self):
        first = self.saved()
        with open(self.path, "ab") as f:
            f.write(b'{"seq":3,"ts":100,"type":"owner.seen",')
        first.poll()
        second = Tail(self.path, checkpoint=True)
        second.poll()
        self.assertEqual(second.offset, first.offset)
        with open(self.path, "ab") as f:
            f.write(b'"entity":"a","data":{"channel":"web","owner":true}}\n')
        second.poll()
        self.assertEqual(second.reader.state["seq"], 3)

    def test_checkpoint_is_opt_in(self):
        Tail(self.path).poll()
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "cache", "web-reader.pickle")))

    def test_cache_miss_does_not_hash_sources_or_create_a_key(self):
        cache = reader_cache.Checkpoint(self.path)
        with mock.patch.object(reader_cache, "code_signature") as signature, \
                mock.patch("core.tail._cache_key") as key:
            self.assertIsNone(cache.restore())
        signature.assert_not_called()
        key.assert_not_called()
        self.assertFalse(os.path.exists(os.path.dirname(cache.store)))

    def test_corrupt_signature_never_unpickles_and_falls_back(self):
        first = self.saved()
        with open(first.checkpoint.store, "r+b") as f:
            f.write(b"x" * 32)
        second = Tail(self.path, checkpoint=True)
        with mock.patch.object(reader_cache.pickle, "loads", side_effect=AssertionError("unsigned pickle")) as loads:
            second.poll()
        loads.assert_not_called()
        self.equal_reader(first, second)

    def test_changed_code_and_language_invalidate_checkpoint(self):
        first = self.saved()
        with mock.patch.object(reader_cache, "code_signature", return_value="new code"):
            self.assertIsNone(reader_cache.Checkpoint(self.path).restore())
        with mock.patch.object(i18n, "current", return_value="different-language"):
            self.assertIsNone(reader_cache.Checkpoint(self.path).restore())
        self.assertIsNotNone(first.checkpoint.restore())

    def test_in_place_journal_edit_invalidates_checkpoint(self):
        first = self.saved()
        with open(self.path, "r+b") as f:
            data = f.read().replace(b"Earlier message", b"Changed message")
            f.seek(0)
            f.write(data)
        self.assertIsNone(first.checkpoint.restore())
        second = Tail(self.path, checkpoint=True)
        second.poll()
        self.assertIn("Changed message", str(second.snapshot["timelines"]))

    def test_truncated_or_replaced_journal_invalidates_checkpoint(self):
        first = self.saved()
        with open(self.path, "rb") as f:
            data = f.read()
        with open(self.path, "wb") as f:
            f.write(data[:20])
        self.assertIsNone(first.checkpoint.restore())
        replacement = self.path + ".new"
        with open(replacement, "wb") as f:
            f.write(data)
        os.replace(replacement, self.path)
        self.assertIsNone(first.checkpoint.restore())

    def test_reader_created_before_source_update_does_not_save_new_signature(self):
        first = self.saved()
        with open(first.checkpoint.store, "rb") as f:
            before = f.read()
        with mock.patch.object(reader_cache, "code_signature", return_value="updated on disk"):
            save(first)
        with open(first.checkpoint.store, "rb") as f:
            self.assertEqual(before, f.read())

    def test_cache_write_failure_does_not_prevent_publication(self):
        tail = Tail(self.path, checkpoint=True)
        with mock.patch.object(reader_cache.tempfile, "mkstemp", side_effect=OSError("read only")), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertTrue(tail.poll())
        self.assertTrue(tail.ready)
        self.assertTrue(tail.snapshot["timelines"])
        self.assertIn("reader checkpoint not saved - OSError: read only", err.getvalue())   # a line in the page log

    def test_only_pickling_holds_the_read_lock(self):
        tail = Tail(self.path, checkpoint=True)
        held = {}
        capture, write = reader_cache.Checkpoint.capture, reader_cache.Checkpoint.write

        def spy(name, original):
            def call(cache, *args):
                held[name] = tail.reading.locked()
                return original(cache, *args)
            return call
        with mock.patch.object(reader_cache.Checkpoint, "capture", spy("capture", capture)), \
                mock.patch.object(reader_cache.Checkpoint, "write", spy("write", write)):
            tail.poll()
        self.assertEqual(held, {"capture": True, "write": False})
        self.assertIsNotNone(reader_cache.Checkpoint(self.path).restore())

    def test_restored_checkpoint_is_not_rewritten_before_the_interval(self):
        first = self.saved()
        before = os.stat(first.checkpoint.store).st_ino
        second = Tail(self.path, checkpoint=True)
        second.poll()
        self.j.append("message.received", None, {"to": "a", "text": "New message", **OWNER})
        second.poll()
        self.assertEqual(os.stat(first.checkpoint.store).st_ino, before)   # os.replace of a new write gives a new file
        save(second)
        self.assertNotEqual(os.stat(first.checkpoint.store).st_ino, before)

    def test_file_of_the_first_format_is_dropped_without_unpickling(self):
        first = self.saved()
        blob = pickle.dumps({"format": 1, "code": first.checkpoint.code, "lang": i18n.current(), "reader": first.reader})
        with open(first.checkpoint.store, "wb") as f:   # signed with this hub's key, the earlier layout
            f.write(hmac.new(_cache_key(self.tmp.name), blob, hashlib.sha256).digest() + blob)
        with mock.patch.object(reader_cache.pickle, "loads", side_effect=AssertionError("old layout unpickled")) as loads:
            self.assertIsNone(reader_cache.Checkpoint(self.path).restore())
        loads.assert_not_called()

    def test_matching_core_state_still_wins_after_restore(self):
        first = self.saved()
        state = first.reader.state
        mid = next(iter(state["messages"]))
        state["messages"][mid]["state"] = "delivered"
        journal.save_state(self.tmp.name, state)
        second = Tail(self.path, checkpoint=True)
        second.poll()
        self.assertEqual(second.snapshot["messages"][mid]["state"], "delivered")
        self.assertEqual(first.snapshot["timelines"], second.snapshot["timelines"])

    def test_advanced_core_state_is_adopted_after_checkpoint_suffix_is_folded(self):
        self.saved()
        self.j.append("message.received", None, {"to": "a", "text": "After checkpoint", **OWNER})
        state = reducer.replay(journal.read(self.path))
        latest = max(state["messages"].values(), key=lambda m: m["created"])
        latest["state"] = "delivered"
        journal.save_state(self.tmp.name, state)
        restored = Tail(self.path, checkpoint=True)
        restored.poll()
        self.assertEqual(restored.reader.state["seq"], state["seq"])
        self.assertEqual(restored.reader.state["messages"][latest["id"]]["state"], "delivered")
        self.assertIn("Earlier message", str(restored.snapshot["timelines"]))
        self.assertIn("After checkpoint", str(restored.snapshot["timelines"]))

    def test_scrub_rotation_rereads_the_cleaned_journal_instead_of_writing_the_old_feeds_back(self):
        key = "ghp_" + "Q" * 36
        self.j.append("session.started", "a", {})
        with open(self.path, "a", encoding="utf-8") as f:   # as an earlier hub wrote it: without the mask
            f.write(journal._line({"seq": self.j.seq + 1, "ts": 100, "src": "host", "type": "turn.tool", "entity": "a",
                                   "data": {"id": "t1", "name": "Bash", "input": {"command": f"GH_TOKEN={key} true"}}}))
        self.j = journal.Journal(self.path)
        for scrub in (False, True):
            with self.subTest(scrub=scrub):
                live = self.saved()
                reader = live.reader
                journal.rotate(self.tmp.name, reducer.replay(journal.read(self.path)), keep=2, per_session=0, scrub=scrub)
                self.j.append("session.said", "a", {"text": "after the rotation"})
                live.poll()
                save(live)
                with open(live.checkpoint.store, "rb") as f:
                    stored = f.read()
                # an ordinary rotation: the feeds go on; the scrub one: read anew from the cleaned journal, and so written
                self.assertEqual(live.reader is reader, not scrub)
                self.assertEqual(key.encode() in pickle.dumps(live.reader), not scrub)
                self.assertEqual(key.encode() in stored, not scrub)
                self.assertIn("after the rotation", str(live.snapshot["timelines"]))

    def test_checkpoint_written_across_a_rotation_is_removed(self):
        tail = self.saved()
        tail.checkpoint.next_write = 0
        with tail.reading:
            job = tail.checkpoint.capture(tail.reader, tail.offset, tail.ino)
        replace = os.replace

        def rotated_meanwhile(src, dst):
            replace(src, dst)
            if dst == tail.checkpoint.store:
                journal.rotate(self.tmp.name, tail.reader.state, scrub=True)
        with mock.patch.object(reader_cache.os, "replace", rotated_meanwhile):
            tail.checkpoint.write(job)
        self.assertFalse(os.path.exists(tail.checkpoint.store))

    def test_rotation_in_the_middle_of_a_write_is_a_silent_skip(self):
        tail = self.saved()
        tail.checkpoint.next_write = 0
        with tail.reading:
            job = tail.checkpoint.capture(tail.reader, tail.offset, tail.ino)
        before = os.stat(tail.checkpoint.store).st_ino
        with mock.patch.object(reader_cache, "prefix", side_effect=ValueError("Different journal")), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            tail.checkpoint.write(job)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(os.stat(tail.checkpoint.store).st_ino, before)

    def test_rotation_falls_back_to_the_same_reader_as_a_fresh_start(self):
        first = self.saved()
        journal.rotate(self.tmp.name, first.reader.state)
        self.assertIsNone(first.checkpoint.restore())
        cached, fresh = Tail(self.path, checkpoint=True), Tail(self.path)
        cached.poll()
        fresh.poll()
        self.equal_reader(cached, fresh)

    def test_restored_reader_retains_refusals_and_inflight_tool_links(self):
        self.j.append("session.started", "a", {})
        self.j.append("turn.tool", "a", {"id": "tool-1", "name": "Bash", "input": {"command": "true"}})
        first = self.saved()
        self.assertIn("tool-1", first.reader.tools)
        first.reader.refused[("a", "client-1")] = {"error": "Synthetic refusal", "seq": 4}
        save(first)
        second = Tail(self.path, checkpoint=True)
        second.poll()
        self.assertEqual(second.reader.refusal("a", "client-1"), first.reader.refusal("a", "client-1"))
        self.j.append("turn.tool_result", "a", {"id": "tool-1", "text": "Synthetic result"})
        first.poll()
        second.poll()
        self.equal_reader(first, second)
        self.assertNotIn("tool-1", second.reader.tools)


if __name__ == "__main__":
    unittest.main()
