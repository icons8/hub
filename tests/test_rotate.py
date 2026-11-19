"""The journal must not slow the core down as it grows (hub 01.10: 211 MB, hub status 5.3 s, hub show over 2 minutes;
190 MB of that was full state snapshots inside the journal). The old journal goes to the archive whole, as is (owner
decision 2026-10-01: keep the old journal, evals need it); the new one starts with a snapshot and a tail of recent
events so the page timeline does not go empty. The snapshot for commands is a separate file with a position in the
journal: a command reads only the tail."""
import tests  # noqa: F401 — ignore the machine's settings
import filecmp
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from core import journal, reducer, views
from core.tail import Tail
from tests.test_snapshot import NullHost
from core.daemon import Hub

OWNER = {"channel": "web", "owner": True}


class Rotate(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rotate-")
        self.path = os.path.join(self.dir, "journal.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def hub(self):
        return Hub(self.dir, NullHost(), registry_dir=self.dir)

    def fill(self, hub, n):
        hub.record("session.requested", "a", {"session_id": "s-a", "cwd": "/w", **OWNER})
        hub.record("session.started", "a", {})
        for i in range(n):
            hub.record("message.received", None, {"to": "a", "text": f"task {i}", **OWNER})
            hub.record("turn.started", "a", {"marker": f"m{hub.state['seq']}"})
            hub.record("turn.result", "a", {"text": f"result {i}"})
            hub.record("turn.stopped", "a", {})

    def test_old_journal_goes_to_the_archive_whole_and_the_new_one_is_small(self):
        hub = self.hub()
        self.fill(hub, 60)
        hub.snapshot("stop")
        before = reducer.replay(journal.read(self.path))
        shutil.copy(self.path, self.path + ".orig")
        archived = journal.rotate(self.dir, before, keep=40, per_session=0)
        self.assertTrue(filecmp.cmp(archived, self.path + ".orig", shallow=False))    # archive is byte for byte
        self.assertEqual(os.path.dirname(archived), os.path.join(self.dir, journal.ARCHIVE))
        events = journal.read(self.path)
        self.assertLessEqual(len(events), 40 + 2)
        same = lambda st: {k: v for k, v in st.items() if k != "seq"}  # noqa: E731
        self.assertEqual(same(reducer.replay(events)), same(before))                    # same state
        # timeline: tail events are visible, as before
        r = views.Reader()
        for e in events:
            r.feed(e)
        texts = [it.get("text") for it in r.timelines["a"]]
        self.assertIn("task 59", texts)
        self.assertIn("task 51", texts)
        # the journal then continues from the same number
        again = self.hub()
        self.assertEqual(again.journal.seq, events[-1]["seq"])
        again.record("message.received", None, {"to": "a", "text": "after", **OWNER})
        self.assertEqual(reducer.replay(journal.read(self.path))["seq"], events[-1]["seq"] + 1)

    def test_rotation_that_did_not_replace_the_journal_can_be_repeated(self):
        # PR #18 review: the archive is a hard link, then the journal is replaced; the replacement failed (Windows:
        # a reader has the journal open), so an archive with the same name exists and every next start crashed in
        # os.link with FileExistsError
        hub = self.hub()
        self.fill(hub, 20)
        hub.snapshot("stop")
        state = reducer.replay(journal.read(self.path))
        events = journal.read(self.path)
        os.makedirs(os.path.join(self.dir, journal.ARCHIVE), exist_ok=True)
        stale = os.path.join(self.dir, journal.ARCHIVE, f"journal-{events[0]['seq']}-{events[-1]['seq']}.jsonl")
        shutil.copy(self.path, stale)
        archived = journal.rotate(self.dir, state, keep=10, per_session=0)
        self.assertTrue(filecmp.cmp(archived, stale, shallow=False))
        self.assertLess(len(journal.read(self.path)), len(events))

    def test_a_crash_before_a_rotation_on_start_is_still_a_crash(self):
        # the "rotate" snapshot ends the new journal: the start took it for the snapshot of a clean shutdown
        hub = self.hub()
        self.fill(hub, 3)
        hub.record("message.received", None, {"to": "a", "text": "long work", **OWNER})
        hub.record("turn.started", "a", {"marker": f"m{hub.state['seq']}"})
        clean = tempfile.mkdtemp(prefix="rotate-clean-")   # inside the run's temp folder: removed with the run
        shutil.copytree(self.dir, clean, dirs_exist_ok=True)
        for state, stop in ((self.dir, False), (clean, True)):   # the same hub after a clean shutdown: no crash
            if stop:
                Hub(state, NullHost(), registry_dir=state).snapshot("stop")
            Hub(state, NullHost(), registry_dir=state, rotate_bytes=1).start()
            self.assertEqual(len(os.listdir(os.path.join(state, journal.ARCHIVE))), 1)
            events = journal.read(os.path.join(state, "journal.jsonl"))
            (lost,) = [e for e in events if e["type"] == "session.exited" and e["entity"] == "a"]
            self.assertEqual(lost["data"].get("crash"), None if stop else True, state)
            # the "rotate" snapshot is already this start's state: no second whole copy of it ("start") after a crash
            self.assertEqual([e["data"]["why"] for e in events if e["type"] == "core.snapshot"], ["rotate-start", "rotate"])

    def test_hubd_rotates_a_big_journal_on_start(self):
        hub = self.hub()
        self.fill(hub, 30)
        hub.snapshot("stop")
        st = hub.state
        old = os.path.getsize(self.path)
        keep, journal.KEEP, per, journal.PER_SESSION = journal.KEEP, 20, journal.PER_SESSION, 5
        try:
            again = Hub(self.dir, NullHost(), registry_dir=self.dir, rotate_bytes=old // 2)
        finally:
            journal.KEEP, journal.PER_SESSION = keep, per
        self.assertEqual(len(os.listdir(os.path.join(self.dir, journal.ARCHIVE))), 1)
        self.assertLess(os.path.getsize(self.path), old)
        self.assertEqual(again.state["sessions"], st["sessions"])
        self.assertEqual(again.state["messages"], st["messages"])

    def test_down_since_after_a_rotation_on_start_is_when_the_old_hubd_stopped(self):
        # the "rotate" snapshot is stamped with the time of the start: core.started named the start as the stop
        hub = self.hub()
        self.fill(hub, 3)
        hub.snapshot("stop")
        stopped = journal.read(self.path)[-1]["ts"]
        beat = stopped - 600
        open(os.path.join(self.dir, "hubd.alive"), "a").close()
        os.utime(os.path.join(self.dir, "hubd.alive"), (beat, beat))
        later = time.time() + 3600
        with mock.patch.object(journal.time, "time", lambda: later):
            again = Hub(self.dir, NullHost(), registry_dir=self.dir, rotate_bytes=1)
        self.assertEqual(len(os.listdir(os.path.join(self.dir, journal.ARCHIVE))), 1)
        again.start()
        (started,) = [e for e in journal.read(self.path) if e["type"] == "core.started"]
        self.assertEqual(started["data"]["down_since"], stopped)

    def test_an_unread_last_line_before_a_rotation_on_start_is_not_taken_from_the_new_journal(self):
        # the last line of the old journal could not be read (an OSError: a reader holds it): the "rotate" snapshot of the
        # new journal is not a stand-in for it — it reads as a clean stop at the time of this start
        hub = self.hub()
        self.fill(hub, 3)
        beat = journal.read(self.path)[-1]["ts"] - 600
        open(os.path.join(self.dir, "hubd.alive"), "a").close()
        os.utime(os.path.join(self.dir, "hubd.alive"), (beat, beat))
        real = Hub.last_head
        later = time.time() + 3600
        with mock.patch.object(Hub, "last_head", lambda self, path=None: b"" if path else real(self)), \
                mock.patch.object(journal.time, "time", lambda: later):
            again = Hub(self.dir, NullHost(), registry_dir=self.dir, rotate_bytes=1)
        self.assertEqual(len(os.listdir(os.path.join(self.dir, journal.ARCHIVE))), 1)
        again.start()
        (started,) = [e for e in journal.read(self.path) if e["type"] == "core.started"]
        self.assertEqual(started["data"]["down_since"], beat)   # the pulse, not the rotation

    def test_a_crash_with_a_rotation_at_start_that_moved_nothing_still_writes_the_start_snapshot(self):
        # journal.rotate moves nothing when it reads no whole event: no "rotate" snapshot, the old journal stays — the
        # start after a crash reads its last line and writes its own snapshot
        hub = self.hub()
        self.fill(hub, 3)
        with mock.patch.object(journal, "rotate", return_value=None):
            again = Hub(self.dir, NullHost(), registry_dir=self.dir, rotate_bytes=1)
        again.start()
        self.assertEqual([e["data"]["why"] for e in journal.read(self.path) if e["type"] == "core.snapshot"], ["start"])
        (lost,) = [e for e in journal.read(self.path) if e["type"] == "session.exited" and e["entity"] == "a"]
        self.assertTrue(lost["data"].get("crash"))

    def test_a_running_hubd_rotates_without_a_restart(self):
        # the journal was already slow at 61 MB, but rotation ran only at hubd start and from 64 MB
        self.assertLessEqual(journal.ROTATE_BYTES, 50 << 20)   # 10.10: journal readers take ~3 s to parse 64 MB
        hub = self.hub()
        self.fill(hub, 30)
        for i in range(400):   # as in real life: the journal grows by session steps, not by state
            hub.record("turn.tool", "a", {"id": f"t{i}", "name": "Bash", "input": {"command": "x" * 300}})
        st, old = hub.state, os.path.getsize(self.path)
        reader = Tail(self.path)
        reader.poll()
        keep, journal.KEEP, per, journal.PER_SESSION = journal.KEEP, 20, journal.PER_SESSION, 5
        try:
            hub.rotate_bytes = old // 2
            hub.rotate_if_big()
        finally:
            journal.KEEP, journal.PER_SESSION = keep, per
        self.assertEqual(len(os.listdir(os.path.join(self.dir, journal.ARCHIVE))), 1)
        self.assertLess(os.path.getsize(self.path), old)
        self.assertEqual((hub.state["sessions"], hub.state["messages"]), (st["sessions"], st["messages"]))
        hub.record("message.received", None, {"to": "a", "text": "after rotation", **OWNER})
        self.assertIn("after rotation", [m["text"] for m in reducer.replay(journal.read(self.path))["messages"].values()])
        self.assertTrue(reader.poll())                             # a page that read the old journal reads the new one
        hub.rotate_if_big()                                        # small one: left alone
        self.assertEqual(len(os.listdir(os.path.join(self.dir, journal.ARCHIVE))), 1)

    def test_a_state_file_held_by_a_reader_does_not_stop_the_rotation(self):
        # Windows: rotation failed on os.unlink(state.json) with "file in use by another process": `hub` reads it
        hub = self.hub()
        self.fill(hub, 30)
        hub.save_state()
        st, real = hub.state, os.unlink

        def held(path, *a, **kw):
            if os.path.basename(path) == journal.STATE_FILE:
                raise PermissionError(32, "file is busy with another process", path)
            return real(path, *a, **kw)
        journal.os.unlink = held
        try:
            hub.rotate_bytes = 1
            hub.rotate_if_big()
        finally:
            journal.os.unlink = real
        self.assertEqual(len(os.listdir(os.path.join(self.dir, journal.ARCHIVE))), 1)
        self.assertEqual((hub.state["sessions"], hub.state["messages"]), (st["sessions"], st["messages"]))
        hub.record("message.received", None, {"to": "a", "text": "after rotation", **OWNER})
        self.assertEqual(journal.load_state(self.dir), reducer.replay(journal.read(self.path)))

    def test_the_rotation_size_comes_from_the_settings(self):
        # the 50 MB journal limit had no setting; it is now in Settings → General → Advanced
        # (journal.rotate_mb in hub.json); the core picks it up on the fly, without a restart
        hub = self.hub()
        self.assertEqual(hub.limit(), journal.ROTATE_BYTES)
        hub.config = {**hub.config, "journal": {"rotate_mb": 12}}
        self.assertEqual(hub.limit(), 12 << 20)
        self.assertTrue(hub.too_big(13 << 20))
        self.assertFalse(hub.too_big(11 << 20))
        hub.config = {**hub.config, "journal": {"rotate_mb": "many"}}   # broken by hand: the default limit applies
        self.assertEqual(hub.limit(), journal.ROTATE_BYTES)

    def test_commands_read_the_state_file_and_only_the_tail(self):
        hub = self.hub()
        self.fill(hub, 20)
        hub.save_state()
        self.fill(hub, 3)                                         # more events after the state file
        full = reducer.replay(journal.read(self.path))
        self.assertEqual(journal.load_state(self.dir), full)
        # a state file from another journal (after a rotation) is not trusted: read the journal
        with open(os.path.join(self.dir, journal.STATE_FILE), "r+") as f:
            text = f.read().replace('"offset": ', '"offset": 1')
            f.seek(0)
            f.write(text)
        self.assertEqual(journal.load_state(self.dir), full)

    def test_readers_notice_a_new_journal(self):
        hub = self.hub()
        self.fill(hub, 20)
        tail = Tail(self.path)
        tail.poll()
        journal.rotate(self.dir, hub.state, keep=10, per_session=0)
        hub = self.hub()
        hub.record("message.received", None, {"to": "a", "text": "new", **OWNER})
        tail.poll()
        self.assertEqual(tail.reader.state["messages"], hub.state["messages"])
        texts = [it.get("text") for it in tail.reader.timelines["a"]]
        self.assertEqual(len(texts), len(set(texts)))       # the carried part is not read twice
        self.assertEqual(texts[-1], "new")

    def test_a_reader_goes_on_after_a_rotation_instead_of_reading_the_carry_again(self):
        # 22.10: after a rotation the page re-read the new journal from the start, and the carried tail landed on a
        # snapshot taken before a week-old tail, which lacks a session born later: its file went into the hub
        # timeline. The archive is the same file (hard link): the reader finishes it from its own position and
        # goes on after the carry
        hub = self.hub()
        hub.record("session.requested", "quiet", {"session_id": "s-q", "cwd": "/w", **OWNER})
        hub.record("session.started", "quiet", {})
        hub.record("message.received", None, {"to": "quiet", "text": "old order", **OWNER})
        self.fill(hub, 5)                                    # "a" was born after the quiet one: its request is not carried
        for i in range(8):
            hub.record("session.said", "a", {"text": f"earlier {i}"})
        tail = Tail(self.path)
        tail.poll()
        epoch = tail.reader.epoch
        hub.record("message.received", None, {"to": "a", "text": "before rotation, not read yet", **OWNER})
        hub.record("owner.file", None, {"path": "/w/shot.png", "caption": None, "channel": "session", "owner": False,
                                        "sender": "a"})
        self.rotate_on_the_fly(hub, keep=3, per_session=2)
        self.assertNotIn("a", journal.read(self.path)[0]["data"]["state"]["sessions"])   # precondition of the case
        hub = self.hub()
        hub.record("message.received", None, {"to": "a", "text": "after rotation", **OWNER})
        tail.poll()
        self.assertEqual(tail.reader.epoch, epoch)          # same timelines: the page need not redraw everything
        line = tail.reader.timelines["a"]
        texts = [it.get("text") for it in line if it.get("text")]
        self.assertEqual(texts.count("earlier 0"), 1)
        self.assertIn("before rotation, not read yet", texts)
        self.assertEqual(texts[-1], "after rotation")
        self.assertEqual([it["path"] for it in line if it["kind"] == "file"], ["/w/shot.png"])
        self.assertEqual(len({it["k"] for it in line}), len(line))
        self.assertEqual(tail.reader.state["sessions"].keys(), hub.state["sessions"].keys())
        self.assertEqual(tail.reader.state["seq"], hub.state["seq"])

    def test_a_reader_of_another_file_reads_the_new_journal_from_the_start(self):
        # the archive is not the file that was read (two rotations between reads, a copy instead of a link): as
        # before, read from the start
        hub = self.hub()
        self.fill(hub, 5)
        tail = Tail(self.path)
        tail.poll()
        epoch = tail.reader.epoch
        journal.rotate(self.dir, hub.state, keep=3, per_session=0)
        hub = self.hub()
        self.fill(hub, 1)
        journal.rotate(self.dir, hub.state, keep=3, per_session=0)
        hub = self.hub()
        hub.record("message.received", None, {"to": "a", "text": "new", **OWNER})
        tail.poll()
        self.assertNotEqual(tail.reader.epoch, epoch)
        self.assertEqual(tail.reader.state["messages"], hub.state["messages"])

    def test_an_empty_carry_is_the_rotate_snapshot_itself(self):
        # review 22.10: a rotation without a carry leaves a journal of one "rotate" snapshot; the carried part is all of it
        hub = self.hub()
        self.fill(hub, 3)
        journal.rotate(self.dir, hub.state, keep=0, per_session=0)
        self.assertEqual(journal.carried_bytes(self.path), os.path.getsize(self.path))

    def test_a_reader_going_on_drops_the_feeds_of_removed_sessions(self):
        # review 22.10: the page no longer restarts timelines on every rotation, so removed ones do not pile up
        hub = self.hub()
        hub.record("session.requested", "gone", {"session_id": "s-g", "cwd": "/w", **OWNER})
        hub.record("session.started", "gone", {})
        hub.record("message.received", None, {"to": "gone", "text": "old order", **OWNER})
        self.fill(hub, 3)
        tail = Tail(self.path)
        tail.poll()
        self.assertIn("gone", tail.reader.timelines)
        hub.record("session.ended", "gone", {"reason": "prompt_input_exit"})
        hub.record("session.remove_requested", "gone", OWNER)
        journal.rotate(self.dir, hub.state, keep=3, per_session=2)
        epoch = tail.reader.epoch
        self.hub().record("message.received", None, {"to": "a", "text": "new", **OWNER})
        tail.poll()
        self.assertEqual(tail.reader.epoch, epoch)
        self.assertNotIn("gone", tail.reader.timelines)
        self.assertIn("a", tail.reader.timelines)

    def test_an_archive_whose_end_does_not_match_its_name_is_not_gone_on(self):
        # review 22.10: the end of the archive is checked against its name and the "rotate" snapshot, also when the
        # unread tail is empty
        hub = self.hub()
        self.fill(hub, 5)
        tail = Tail(self.path)
        tail.poll()
        epoch = tail.reader.epoch
        archived = journal.rotate(self.dir, hub.state, keep=3, per_session=0)
        with open(archived, "a", encoding="utf-8") as f:   # appended to the archive after the rotation: wrong end
            f.write(json.dumps({"seq": 10 ** 6, "ts": 1, "type": "x", "entity": None, "data": {}}) + "\n")
        hub = self.hub()
        hub.record("message.received", None, {"to": "a", "text": "new", **OWNER})
        tail.poll()
        self.assertNotEqual(tail.reader.epoch, epoch)
        self.assertEqual(tail.reader.state["messages"], hub.state["messages"])

    def rotate_on_the_fly(self, hub, keep, per_session):
        """As a running hubd rotates (rotate_if_big): the archive ends with an ordinary event, not a snapshot (31.10)."""
        saved = journal.KEEP, journal.PER_SESSION, hub.rotate_bytes
        journal.KEEP, journal.PER_SESSION, hub.rotate_bytes, hub.carried = keep, per_session, 1, 0
        try:
            hub.rotate_if_big()
        finally:
            journal.KEEP, journal.PER_SESSION, hub.rotate_bytes = saved
        (archived,) = journal.archives(self.dir)
        self.assertNotEqual(journal.read(archived)[-1]["type"], "core.snapshot")   # precondition of the case

    def rotate_mid_read(self, read, gone=False):
        """A reader reads the journal, more is appended, a rotation with a carry happens, then new events follow.
        read() is its read. gone is one more session, removed before the rotation."""
        hub = self.hub()
        if gone:
            hub.record("session.requested", "gone", {"session_id": "s-g", "cwd": "/w", **OWNER})
            hub.record("session.started", "gone", {})
            hub.record("session.said", "gone", {"text": "was"})
        self.fill(hub, 4)
        for i in range(6):
            hub.record("session.said", "a", {"text": f"earlier {i}"})
        read()
        hub.record("session.said", "a", {"text": "before rotation, not read yet"})
        if gone:
            hub.record("session.ended", "gone", {"reason": "prompt_input_exit"})
            hub.record("session.remove_requested", "gone", OWNER)
        self.rotate_on_the_fly(hub, keep=10, per_session=4)
        hub = self.hub()
        hub.record("session.said", "a", {"text": "after rotation"})
        read()
        return hub

    def test_the_watch_goes_on_after_a_rotation_instead_of_feeding_the_carry_again(self):
        # 22.10: after a rotation the watcher fed itself the carried tail again, so the session step window and the
        # step counter doubled (it uses them to spot "going in circles" and to decide on the "working on" line)
        import time
        from modules.watch import watch
        w = watch.Watcher(self.dir, {}, ask=None, needs=None, urgent=None, recap=lambda text: "")

        def read():
            w.refresh(time.time())
            w.started = True
        hub = self.rotate_mid_read(read)
        seqs = [e["seq"] for e in w.steps["a"]]
        self.assertEqual(len(seqs), len(set(seqs)))
        self.assertEqual(w.steps["a"][-1]["data"]["text"], "after rotation")
        self.assertIn("before rotation, not read yet", [e["data"].get("text") for e in w.steps["a"]])
        self.assertEqual(w.counted["a"], 4 + 6 + 1 + 1)   # turn.started x4, the older ones x6, before and after the rotation
        self.assertEqual(w.state["sessions"], hub.state["sessions"])
        self.assertTrue(w.started)

    def test_the_telegram_bot_goes_on_after_a_rotation_instead_of_reading_the_carry_again(self):
        from modules.telegram import bot as tg
        b = tg.Bot(self.dir, tg.Api("1:x", base="http://127.0.0.1:9"))
        got = []

        def read():
            got.append([e["seq"] for e, _ in b.verdicts(b.read_journal())])
        hub = self.rotate_mid_read(read, gone=True)
        first, after = got
        self.assertTrue(after and min(after) > max(first))   # the carried part is not read a second time
        self.assertIn("a", b.reader.timelines)
        self.assertNotIn("gone", b.reader.timelines)          # the reader lives on: timelines of removed sessions do not pile up
        self.assertEqual(b.reader.state["sessions"], hub.state["sessions"])
        self.assertEqual(b.reader.state["seq"], hub.state["seq"])

    def test_show_reads_from_a_recent_snapshot(self):
        hub = self.hub()
        self.fill(hub, 30)
        hub.snapshot("periodic")
        self.fill(hub, 2)
        events = journal.read(self.path)
        part = views.recent(events, keep=5)
        self.assertEqual(part[0]["type"], "core.snapshot")
        self.assertLess(len(part), 15)
        self.assertEqual(views.recent(events, keep=10 ** 6), events)

    def test_old_delivered_messages_leave_the_state(self):
        # the state is copied on every event: 1336 delivered messages in two days were 1 MB of 1.3
        hub = self.hub()
        self.fill(hub, 5)
        first = min(hub.state["messages"], key=lambda m: hub.state["messages"][m]["created"])
        hub.record("delegation.created", None, {"session": "a", "for": first, "context": "x", **OWNER})
        old_ids = set(hub.state["messages"])
        hub.clock = lambda: 10 ** 10
        hub.prune()
        kept = set(hub.state["messages"])
        task = next(d for d in hub.state["delegations"].values())
        self.assertIn(task["message"], kept)            # a task in progress keeps its messages
        self.assertIn(first, kept)
        self.assertLess(len(kept & old_ids), len(old_ids))
        self.assertTrue(all(hub.state["messages"][m]["state"] not in ("delivered", "failed") or m in (first, task["message"])
                            for m in kept))
        seq = hub.state["seq"]
        hub.prune()                                      # nothing to remove: no event
        self.assertEqual(hub.state["seq"], seq)
        self.assertEqual(reducer.replay(journal.read(self.path))["messages"], hub.state["messages"])

    def test_copy_is_deep(self):
        st = {"a": [{"b": 1}], "c": {"d": [1, 2]}}
        cp = reducer.copy_state(st)
        cp["a"][0]["b"] = 2
        cp["c"]["d"].append(3)
        self.assertEqual(st, {"a": [{"b": 1}], "c": {"d": [1, 2]}})

    def test_readers_fold_in_place_to_the_same_state(self):
        # the outcome of an event is known from the next line: accepted, no copy; rejected, skipped; same result
        hub = self.hub()
        self.fill(hub, 10)
        hub.record("turn.stopped", "nobody")                       # will be rejected
        hub.record("delegation.created", None, {"session": "zz", **OWNER})   # will be rejected
        self.fill(hub, 3)
        events = journal.read(self.path)
        slow = reducer.empty()
        for e in events:
            slow, _, _ = reducer.reduce(slow, e)
        self.assertEqual(reducer.fold(reducer.empty(), events), slow)
        a, b = views.Reader(), views.Reader()
        for e in events:
            a.feed(e)
        b.feed_all(events)
        same = lambda r: ({k: v for k, v in r.snapshot(now=1).items() if k != "epoch"}, r.timelines)  # noqa: E731
        self.assertEqual(same(a), same(b))  # epoch is per reader (page: timelines restart)
        self.assertIn("core.rejected", [e["type"] for e in events])

    def test_a_quiet_session_keeps_its_history(self):
        # a session showed no messages because it had been quiet for longer than the last 3000 events
        hub = self.hub()
        hub.record("session.requested", "quiet", {"session_id": "s-q", "cwd": "/w", **OWNER})
        hub.record("session.started", "quiet", {})
        hub.record("message.received", None, {"to": "quiet", "text": "old order", **OWNER})
        self.fill(hub, 30)                                   # then only "a" talks for a long time
        journal.rotate(self.dir, hub.state, keep=10)
        r = views.Reader()
        r.feed_all(journal.read(self.path))
        self.assertIn("old order", [it.get("text") for it in r.timelines.get("quiet", [])])

    def agent_steps(self, hub, n):
        """A session waits for a helper agent: its steps go to the journal but not to the session timeline."""
        for i in range(n):
            hub.record("turn.tool", "busy", {"id": f"x{i}", "name": "Bash", "input": {}, "parent": "agent1"})
            hub.record("turn.tool_result", "busy", {"id": f"x{i}", "text": "ok"})
            hub.record("session.said", "busy", {"text": f"step {i}", "parent": "agent1"})

    def busy_history(self, hub):
        hub.record("session.requested", "busy", {"session_id": "s-b", "cwd": "/w", **OWNER})
        hub.record("session.started", "busy", {})
        hub.record("message.received", None, {"to": "busy", "text": "old order", **OWNER})
        hub.record("turn.started", "busy", {"marker": "m-busy"})
        hub.record("turn.tool", "busy", {"id": "t1", "name": "Bash", "input": {"command": "ls"}})
        hub.record("turn.tool_result", "busy", {"id": "t1", "text": "done"})
        hub.record("session.said", "busy", {"text": "own answer"})

    def test_agent_steps_do_not_push_the_sessions_own_lines_out_of_the_carry(self):
        # a session's timeline history vanished. While it waited for an agent, its last 200 events were
        # the agent's steps (they do not go to the session timeline), so after a rotation the timeline was empty
        hub = self.hub()
        self.busy_history(hub)
        self.agent_steps(hub, 150)
        journal.rotate(self.dir, hub.state, keep=10, per_session=100)
        r = views.Reader()
        r.feed_all(journal.read(self.path))
        texts = [it.get("text") or it.get("name") for it in r.timelines.get("busy", [])]
        self.assertIn("old order", texts)
        self.assertIn("own answer", texts)
        self.assertFalse(any(str(t).startswith("step ") for t in texts))

    def test_an_empty_live_feed_of_an_old_session_still_offers_the_archive(self):
        # the session timeline is empty in the live journal but had entries before: the page must show "Show earlier"
        hub = self.hub()
        self.busy_history(hub)
        self.fill(hub, 30)
        journal.rotate(self.dir, hub.state, keep=10, per_session=0)
        hub = self.hub()
        hub.record("session.requested", "fresh", {"session_id": "s-f", "cwd": "/w", **OWNER})
        r = views.Reader()
        r.feed_all(journal.read(self.path))
        self.assertNotIn("busy", r.timelines)
        self.assertTrue(r.timeline("busy")["older"])
        self.assertFalse(r.timeline("fresh")["older"])   # born in this journal: nothing earlier

    def test_a_removed_session_history_is_not_carried(self):
        # 10.10: 112 of 140 sessions in the state were removed, yet the rotation carried PER_SESSION events for each:
        # a 16 MB tail out of 21 MB
        hub = self.hub()
        hub.record("session.requested", "gone", {"session_id": "s-g", "cwd": "/w", **OWNER})
        hub.record("session.started", "gone", {})
        hub.record("message.received", None, {"to": "gone", "text": "old order", **OWNER})
        hub.record("session.ended", "gone", {"reason": "prompt_input_exit"})
        hub.record("session.remove_requested", "gone", OWNER)
        self.assertEqual(hub.state["sessions"]["gone"]["state"], "removed")
        self.fill(hub, 30)
        journal.rotate(self.dir, hub.state, keep=10)
        r = views.Reader()
        r.feed_all(journal.read(self.path))
        self.assertNotIn("old order", [it.get("text") for it in r.timelines.get("gone", [])])
        self.assertEqual(reducer.replay(journal.read(self.path))["sessions"].keys(), hub.state["sessions"].keys())

    def test_a_rotation_that_cannot_shrink_the_journal_does_not_repeat(self):
        # 10.10: right after a rotation the new journal was 21.4 MB with a 20 MiB limit; every next event rotated it
        # again, and in 13 hours ~5000 identical copies, 120 GB, landed in the archive
        hub = self.hub()
        self.fill(hub, 30)
        for i in range(200):
            hub.record("turn.tool", "a", {"id": f"t{i}", "name": "Bash", "input": {"command": "x" * 300}})
        limit = os.path.getsize(self.path) // 2           # the whole journal is the tail: after rotation it is over the limit
        archive = os.path.join(self.dir, journal.ARCHIVE)
        hub.rotate_bytes = limit
        hub.rotate_if_big()
        self.assertEqual(len(os.listdir(archive)), 1)
        self.assertGreater(os.path.getsize(self.path), limit)
        hub.record("message.received", None, {"to": "a", "text": "more", **OWNER})
        hub.rotate_if_big()
        self.assertEqual(len(os.listdir(archive)), 1)     # on the fly: no repeat
        again = Hub(self.dir, NullHost(), registry_dir=self.dir, rotate_bytes=limit)
        self.assertEqual(len(os.listdir(archive)), 1)     # nor at start
        for i in range(200):                              # the journal grew by over half the limit: allowed again
            again.record("turn.tool", "a", {"id": f"u{i}", "name": "Bash", "input": {"command": "y" * 300}})
        again.rotate_if_big()
        self.assertEqual(len(os.listdir(archive)), 2)

    def test_a_big_state_does_not_fill_the_archive_with_snapshots(self):
        # 31.10: the state was 23 MB (thousands of removed sessions) and a snapshot went in every 2000 events (~3 MB): an
        # archive was 82 MB, of it 73 MB three snapshots and 3 MB new events — the periodic snapshot itself pushed the
        # journal over the threshold (carry + "rotate" snapshot 30 MB, + half the limit), and the rotation wrote one more
        # snapshot into the old journal before leaving: 37 rotations a day, 3 GB of archive
        from core import daemon
        hub = self.hub()
        for i in range(60):   # a state far heavier than a run of events between snapshots
            hub.record("session.requested", f"s{i}", {"session_id": f"id-{i}", "cwd": "/w/" + "x" * 2000, **OWNER})
        self.fill(hub, 2)
        every, daemon.SNAPSHOT_EVERY = daemon.SNAPSHOT_EVERY, 20
        keep, journal.KEEP = journal.KEEP, 10
        try:
            hub.rotate_bytes = 4 * os.path.getsize(journal.os.path.join(self.dir, journal.STATE_FILE))
            hub.carried = 0
            for i in range(3000):   # as in real life: session steps between the ticks
                hub.record("turn.tool", "a", {"id": f"t{i}", "name": "Bash", "input": {"command": "x" * 300}})
                hub.rotate_if_big()
        finally:
            daemon.SNAPSHOT_EVERY, journal.KEEP = every, keep
        archives = journal.archives(self.dir)
        self.assertGreater(len(archives), 1)
        own = []
        for path in archives:
            events = journal.read(path)
            at = next((i for i, e in enumerate(events) if (e.get("data") or {}).get("why") == "rotate"), -1)
            full = [e["data"]["why"] for e in events if e["type"] == "core.snapshot" and e["data"]["why"] != "rotate-start"]
            self.assertLessEqual(len(full), 1, (path, full))   # one full snapshot per archive
            own += [e["seq"] for e in events[at + 1:]]
        live = journal.read(self.path)
        at = next(i for i, e in enumerate(live) if (e.get("data") or {}).get("why") == "rotate")
        own += [e["seq"] for e in live[at + 1:] if e["type"] != "core.snapshot"]
        self.assertEqual(len(own), len(set(own)))   # every event once past the carries
        self.assertEqual(reducer.replay(live)["sessions"].keys(), hub.state["sessions"].keys())

    def test_a_small_state_is_still_snapshotted_every_snapshot_every_events(self):
        # the fold of a restart after a crash starts no further back than SNAPSHOT_EVERY events while snapshots are cheap
        from core import daemon
        hub = self.hub()
        self.fill(hub, 2)
        every, daemon.SNAPSHOT_EVERY = daemon.SNAPSHOT_EVERY, 20
        try:
            for i in range(50):
                hub.record("turn.tool", "a", {"id": f"t{i}", "name": "Bash", "input": {"command": "x" * 300}})
        finally:
            daemon.SNAPSHOT_EVERY = every
        whys = [e["data"]["why"] for e in journal.read(self.path) if e["type"] == "core.snapshot"]
        self.assertEqual(whys, ["periodic", "periodic"])

    def test_a_rotation_that_cannot_shrink_the_journal_is_logged_on_start_too(self):
        import contextlib
        import io
        hub = self.hub()
        self.fill(hub, 30)
        limit = os.path.getsize(self.path) // 2           # the whole journal is the tail: after rotation it is over the limit
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            Hub(self.dir, NullHost(), registry_dir=self.dir, rotate_bytes=limit)
        self.assertEqual(len(os.listdir(os.path.join(self.dir, journal.ARCHIVE))), 1)
        self.assertIn("above the threshold", out.getvalue())

    def test_tail_replay_does_not_leave_a_phantom_in_another_session_queue(self):
        # 09.10: the hub page showed "Queued · 15" with "no session backend", though that session was alive and busy.
        # After a rotation the journal tail lands on an old base (a snapshot before the first carried event) that does
        # not have this session yet: a scheduled check in the timeline fold became a "no such session" message to the
        # hub, with the same number as the real one in the session queue; after a real snapshot the hub row looked
        # "queued" and was never cleared
        import time
        hub = self.hub()
        hub.record("session.requested", "hub", {"session_id": "s-h", "cwd": "/h", "kind": "hub", **OWNER})
        hub.record("session.started", "hub", {})
        hub.record("session.requested", "quiet", {"session_id": "s-q", "cwd": "/w", **OWNER})
        hub.record("session.started", "quiet", {})
        hub.record("message.received", None, {"to": "quiet", "text": "old order", **OWNER})
        self.fill(hub, 30)                                   # "a" appeared after the timeline base and talked a lot
        hub.record("turn.started", "a", {"marker": None})    # busy: the check goes to its queue
        at = round(time.time()) + 30
        hub.record("schedule.set", "a-wait-ab12-1", {"session": "a", "at": at, "prompt": "[waiting for reply] x",
                                                     "channel": "session", "owner": False, "sender": "a"})
        hub.record("schedule.due", "a-wait-ab12-1", {"slot": at})
        (mid,) = [k for k, m in hub.state["messages"].items() if m.get("schedule")]
        self.assertEqual(hub.state["messages"][mid]["session"], "a")
        journal.rotate(self.dir, hub.state, keep=3, per_session=3)
        r = views.Reader()
        r.feed_all(journal.read(self.path))
        self.assertEqual([it["id"] for it in r.timelines.get("hub", []) if it.get("id") == mid], [])
        self.assertEqual(r.state["messages"][mid]["state"], "queued")

    def test_an_old_rotation_is_backfilled_from_its_archive(self):
        hub = self.hub()
        hub.record("session.requested", "quiet", {"session_id": "s-q", "cwd": "/w", **OWNER})
        hub.record("session.started", "quiet", {})
        hub.record("message.received", None, {"to": "quiet", "text": "old order", **OWNER})
        self.fill(hub, 30)
        journal.rotate(self.dir, hub.state, keep=10, per_session=0)
        with open(self.path) as f:                           # like the 01.10 rotation: no session history and no marker
            lines = f.readlines()
        first = json.loads(lines[0])
        del first["data"]["per_session"]
        with open(self.path, "w") as f:
            f.writelines([json.dumps(first, ensure_ascii=False) + "\n"] + lines[1:])
        self.assertTrue(journal.needs_backfill(self.dir))
        tail = Tail(self.path)
        tail.poll()
        self.assertNotIn("old order", [it.get("text") for it in tail.reader.timelines.get("quiet", [])])
        again = self.hub()                                   # hubd takes it from the archive at start
        self.assertFalse(journal.needs_backfill(self.dir))
        tail.poll()   # the carry also came from an older archive the page never read: do not continue, start over
        self.assertIn("old order", [it.get("text") for it in tail.reader.timelines.get("quiet", [])])
        r = views.Reader()
        r.feed_all(journal.read(self.path))
        self.assertIn("old order", [it.get("text") for it in r.timelines.get("quiet", [])])
        self.assertEqual(again.state["sessions"].keys(), hub.state["sessions"].keys())
        self.assertEqual(len(os.listdir(os.path.join(self.dir, journal.ARCHIVE))), 2)   # both journals are in the archive


if __name__ == "__main__":
    unittest.main()
