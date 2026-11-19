"""The state file (state.json) is only a cache of the journal: a failed write of it must not crash hubd. 2026-11-03: on Windows
a reader held state.json past the 5 s of replace retries, PermissionError went up through record() and hubd died — nine
times a day, each time the whole fleet was brought up again. Readers read the file whole and parse it after closing, so
the window in which they hold it is the read, not the parse. The journal rotation replaces journal.jsonl the same way and
must not crash hubd either."""
import tests  # noqa: F401
import builtins
import contextlib
import importlib.util
import io
import json
import os
import tempfile
import types
import unittest
from unittest import mock

import platforms
from core import daemon, i18n, journal, modules, scrub, tail
from core.daemon import Hub
from modules.browser.run import Supervisor
from core import cutout  # noqa: E402 — the open flavor's run has no stats report (core/cutout.py)
stats = None
if cutout.present("stats"):
    from modules.stats import stats
from modules.watch import watch

OWN = {"channel": "cli", "owner": True}
STOPS_AUDIT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "modules", "agent_dev",
                           "stops-audit.py")


def held(src, dst):
    raise PermissionError(5, "Access is denied", dst)


def held_journal():
    """Refuses only the replace of journal.jsonl: the rest (the state file) goes through as usual."""
    real = platforms.current.replace

    def replace(src, dst):
        return held(src, dst) if os.path.basename(dst) == "journal.jsonl" else real(src, dst)
    return replace


def hub(**kw):
    h = Hub(tempfile.mkdtemp(prefix="statesave-"), types.SimpleNamespace(), **kw)
    h.execute = lambda a: None
    return h


def events(h, n, start=0):
    for i in range(start, start + n):
        h.record("session.requested", f"s-{i}", {"cwd": f"/home/x/s{i}", "session_id": f"id-{i}", **OWN})


class StateSave(unittest.TestCase):
    def test_a_held_state_file_does_not_crash_the_core(self):
        h = hub()
        with mock.patch.object(platforms.current, "replace", held):
            events(h, daemon.STATE_EVERY + 1)   # the write falls due inside and fails
        self.assertEqual(len(h.state["sessions"]), daemon.STATE_EVERY + 1)
        events(h, daemon.STATE_EVERY, start=daemon.STATE_EVERY + 1)   # the next due write goes through
        self.assertGreater(journal.read_state_file(h.state_dir)["seq"], 0)

    def test_after_a_failed_write_commands_still_read_the_whole_state(self):
        h = hub()
        events(h, daemon.STATE_EVERY)   # a good state file
        with mock.patch.object(platforms.current, "replace", held):
            events(h, daemon.STATE_EVERY, start=daemon.STATE_EVERY)   # the next one fails: the old file stays
        got = journal.load_state(h.state_dir)
        self.assertEqual(got["seq"], h.state["seq"])
        self.assertEqual(sorted(got["sessions"]), sorted(h.state["sessions"]))

    def test_a_failed_write_leaves_no_temporary_file(self):
        """The .tmp holds the whole state (tens of MB, and what `hub journal scrub` has just cleaned) — not left behind."""
        h = hub()
        with mock.patch.object(platforms.current, "replace", held):
            events(h, daemon.STATE_EVERY)
        self.assertNotIn(journal.STATE_FILE + ".tmp", os.listdir(h.state_dir))

    def test_a_failed_write_is_retried_on_time_not_only_on_events(self):
        """A quiet hub gets few events: the retry comes from the tick after RETRY_EVERY seconds, not after 50 events —
        otherwise a state file `hub journal scrub` did not get to clean could live on for hours."""
        now = [1000.0]
        h = hub(clock=lambda: now[0])
        with mock.patch.object(platforms.current, "replace", held):
            events(h, daemon.STATE_EVERY)
        target = os.path.join(h.state_dir, journal.STATE_FILE)
        h.retry_state()
        self.assertFalse(os.path.exists(target))   # not yet: a held file is not retried on every tick
        now[0] += daemon.RETRY_EVERY
        h.retry_state()
        self.assertEqual(journal.read_state_file(h.state_dir)["seq"], h.state["seq"])
        self.assertIn(h.retry_state.__name__, Hub.tick.__code__.co_names)   # a step of the tick


class Rotation(unittest.TestCase):
    def test_a_refused_rotation_does_not_crash_the_core(self):
        """journal.jsonl held past the replace retries (a reader, an antivirus): a line in the log, the journal stays as
        it was, the rotation is tried again after RETRY_EVERY seconds."""
        now = [1000.0]
        h = hub(clock=lambda: now[0], rotate_bytes=4096)
        events(h, 40)
        path = os.path.join(h.state_dir, "journal.jsonl")
        before = journal.read(path)
        with mock.patch.object(platforms.current, "replace", held):
            h.rotate_if_big()
        self.assertEqual(journal.read(path), before)
        self.assertNotIn("journal.jsonl.rotate", os.listdir(h.state_dir))
        events(h, 1, start=40)   # the core goes on writing to the same journal
        h.rotate_if_big()
        self.assertNotEqual(journal.read(path)[-1]["type"], "core.snapshot")   # not on every tick
        now[0] += daemon.RETRY_EVERY
        h.rotate_if_big()
        last = journal.read(path)[-1]
        self.assertEqual((last["type"], last["data"]["why"]), ("core.snapshot", "rotate"))
        self.assertEqual(len(journal.load_state(h.state_dir)["sessions"]), 41)

    def test_a_failure_after_the_replace_is_not_a_refused_rotation(self):
        """The journal was already replaced and a later step failed (the folder sync here): hubd must not go on as if
        nothing happened — its journal object still counts seq of the old file, and the next event would repeat the seq
        of the "rotate" snapshot. It fails as before the retry existed and is brought up on the new journal."""
        def sync_dir(path):
            if path == h.state_dir:
                raise OSError(5, "I/O error", path)
        for at_start in (False, True):
            with self.subTest(at_start=at_start):
                h = hub(rotate_bytes=4096)
                events(h, 40)
                with mock.patch.object(platforms.current, "sync_dir", sync_dir), self.assertRaises(RuntimeError):
                    if at_start:
                        Hub(h.state_dir, types.SimpleNamespace(), rotate_bytes=4096)
                    else:
                        h.rotate_if_big()
                path = os.path.join(h.state_dir, "journal.jsonl")
                last = journal.read(path)[-1]
                self.assertEqual((last["type"], last["data"]["why"]), ("core.snapshot", "rotate"))
                again = Hub(h.state_dir, types.SimpleNamespace(), rotate_bytes=4096)   # brought up on the new journal
                again.execute = lambda a: None
                events(again, 1, start=40)
                seqs = [e["seq"] for e in journal.read(path)]
                self.assertEqual(seqs[-1], last["seq"] + 1)
                self.assertEqual(len(journal.load_state(h.state_dir)["sessions"]), 41)

    def test_a_refused_rotation_leaves_one_archive(self):
        """The archive is a hard link to the journal made before the replace: a refused try drops it — otherwise it is a
        second name of the live journal, grows with it, and the retry links a third (journal-1-40 and journal-1-41, one
        file): the spending, the statistics and the history counted twice (15.10)."""
        now = [1000.0]
        h = hub(clock=lambda: now[0], rotate_bytes=4096)
        events(h, 40)
        path = os.path.join(h.state_dir, "journal.jsonl")
        with mock.patch.object(platforms.current, "replace", held):
            h.rotate_if_big()
        self.assertEqual(journal.archives(h.state_dir), [])
        self.assertEqual(os.listdir(os.path.join(h.state_dir, journal.ARCHIVE)), [])
        events(h, 1, start=40)
        whole = journal.read(path)
        now[0] += daemon.RETRY_EVERY
        h.rotate_if_big()
        self.assert_one_archive(h, whole)

    def test_a_link_the_reader_kept_is_not_a_second_archive(self):
        """Windows: the reader that refused the replace refuses the unlink of the link as well. Until the retry the name is
        the live journal — not an archive; after it, the left name is dropped and one archive stays."""
        now = [1000.0]
        h = hub(clock=lambda: now[0], rotate_bytes=4096)
        events(h, 40)
        path = os.path.join(h.state_dir, "journal.jsonl")
        archive_dir = os.path.join(h.state_dir, journal.ARCHIVE)
        real_unlink = os.unlink

        def unlink(p, *a, **kw):
            if os.path.dirname(os.path.abspath(p)) == os.path.abspath(archive_dir):
                raise PermissionError(32, "The process cannot access the file", p)
            return real_unlink(p, *a, **kw)
        with mock.patch.object(platforms.current, "replace", held), mock.patch("os.unlink", unlink):
            h.rotate_if_big()
        self.assertEqual(len(os.listdir(archive_dir)), 1)   # the kept name
        self.assertEqual(journal.archives(h.state_dir), [])
        events(h, 1, start=40)
        self.assertEqual(journal.archives(h.state_dir), [])
        whole = journal.read(path)
        now[0] += daemon.RETRY_EVERY
        h.rotate_if_big()
        self.assert_one_archive(h, whole)

    def test_two_journals_with_one_first_number_are_two_archives(self):
        """A rotation that carried a whole small journal starts the next one with the same first number: names of one
        number are dropped as duplicates only when they are one file."""
        d = tempfile.mkdtemp(prefix="statesave-")
        os.makedirs(os.path.join(d, journal.ARCHIVE))
        for name in ("journal-1-5.jsonl", "journal-1-9.jsonl"):
            with open(os.path.join(d, journal.ARCHIVE, name), "w", encoding="utf-8") as f:
                f.write(name)
        self.assertEqual([os.path.basename(p) for p in journal.archives(d)], ["journal-1-5.jsonl", "journal-1-9.jsonl"])

    def test_of_the_names_of_one_file_the_newest_is_the_archive(self):
        """By the file, not against the newest name of the first number only: X; Y under two names; a name of the live
        journal — X and Y once each."""
        d = tempfile.mkdtemp(prefix="statesave-")
        folder = os.path.join(d, journal.ARCHIVE)
        os.makedirs(folder)
        for name, text in (("journal-0-45.jsonl", "x"), ("journal-0-60.jsonl", "y"), ("journal.jsonl", "live")):
            with open(os.path.join(d if name == "journal.jsonl" else folder, name), "w", encoding="utf-8") as f:
                f.write(text)
        os.link(os.path.join(folder, "journal-0-60.jsonl"), os.path.join(folder, "journal-0-70.jsonl"))
        os.link(os.path.join(d, "journal.jsonl"), os.path.join(folder, "journal-0-80.jsonl"))
        self.assertEqual([os.path.basename(p) for p in journal.archives(d)], ["journal-0-45.jsonl", "journal-0-70.jsonl"])

    def test_a_rotation_leaves_one_name_of_each_archive(self):
        """Names a refused try could not drop go with the next rotation, whatever their first number — before the file
        gets a new inode (compact, scrub) and the old name turns into an archive of its own, uncompacted."""
        h = hub(rotate_bytes=4096)
        events(h, 40)
        folder = os.path.join(h.state_dir, journal.ARCHIVE)
        os.makedirs(folder)
        with open(os.path.join(folder, "journal-0-5.jsonl"), "w", encoding="utf-8") as f:
            f.write("old")
        os.link(os.path.join(folder, "journal-0-5.jsonl"), os.path.join(folder, "journal-0-4.jsonl"))
        with contextlib.redirect_stdout(io.StringIO()) as log:
            h.rotate_if_big()
        self.assertNotIn("journal-0-4.jsonl", os.listdir(folder))
        self.assertIn("journal-0-4.jsonl", log.getvalue())   # the sweep says what it dropped
        with open(os.path.join(folder, "new"), "w", encoding="utf-8") as f:
            f.write("compacted")
        os.replace(os.path.join(folder, "new"), os.path.join(folder, "journal-0-5.jsonl"))   # as compact does
        self.assertEqual([os.path.basename(p) for p in journal.archives(h.state_dir)][:1], ["journal-0-5.jsonl"])
        self.assertEqual(len(journal.archives(h.state_dir)), 2)

    def assert_one_archive(self, h, whole):
        names = os.listdir(os.path.join(h.state_dir, journal.ARCHIVE))
        self.assertEqual(names, [f"journal-{whole[0]['seq']}-{whole[-1]['seq']}.jsonl"])
        self.assertEqual(journal.archives(h.state_dir), [os.path.join(h.state_dir, journal.ARCHIVE, names[0])])
        self.assertEqual(journal.read(journal.archives(h.state_dir)[0]), whole)

    def test_a_refused_rotation_at_start_does_not_stop_the_core(self):
        """The journal grew past the threshold while hubd was down: the rotation at start is refused — hubd still starts on
        the journal as it was, and the tick rotates it after RETRY_EVERY seconds."""
        now = [1000.0]
        h = hub(clock=lambda: now[0])
        events(h, 40)
        path = os.path.join(h.state_dir, "journal.jsonl")
        before = journal.read(path)
        with mock.patch.object(platforms.current, "replace", held_journal()):   # the state file and the rest go through
            h = Hub(h.state_dir, types.SimpleNamespace(), clock=lambda: now[0], rotate_bytes=4096)
        h.execute = lambda a: None
        self.assertEqual(journal.read(path), before)
        self.assertNotIn("journal.jsonl.rotate", os.listdir(h.state_dir))
        h.rotate_if_big()
        self.assertEqual(journal.read(path), before)   # not on the first tick
        now[0] += daemon.RETRY_EVERY
        h.rotate_if_big()
        last = journal.read(path)[-1]
        self.assertEqual((last["type"], last["data"]["why"]), ("core.snapshot", "rotate"))
        self.assertEqual(len(journal.load_state(h.state_dir)["sessions"]), 40)

    def test_a_refused_scrub_rotation_is_not_forgotten(self):
        """`hub journal scrub --apply` asked for a rotation with scrubbing: a refused one stays due, and the retry is a
        scrubbing one; holding the waiting input again on it is harmless."""
        now = [1000.0]
        h = hub(clock=lambda: now[0])
        events(h, 3)
        h.scrub_due = True
        held_inputs, real_hold = [], h.hold_pending
        h.hold_pending = lambda: (real_hold(), held_inputs.append(dict(h.inputs)))
        with mock.patch.object(platforms.current, "replace", held):
            h.rotate_if_big()
        self.assertTrue(h.scrub_due)
        now[0] += daemon.RETRY_EVERY
        h.rotate_if_big()
        self.assertFalse(h.scrub_due)
        first = journal.read(os.path.join(h.state_dir, "journal.jsonl"))[0]
        self.assertEqual((first["type"], first["data"]["why"], first["data"].get("scrub")), ("core.snapshot", "rotate-start", True))
        self.assertEqual(len(held_inputs), 2)
        self.assertEqual(held_inputs[0], held_inputs[1])

    def test_a_refused_backfill_rotation_at_start_is_retried(self):
        """At start the journal is rotated also when it is small but began without session history (needs_backfill): a
        refused one is retried by the tick as well, not only one over the size."""
        now = [1000.0]
        h = hub(clock=lambda: now[0])
        events(h, 3)
        with mock.patch.object(journal, "needs_backfill", side_effect=[True, False]), \
                mock.patch.object(platforms.current, "replace", held_journal()):   # the start asks, then rotate itself
            h = Hub(h.state_dir, types.SimpleNamespace(), clock=lambda: now[0])
        h.execute = lambda a: None
        path = os.path.join(h.state_dir, "journal.jsonl")
        now[0] += daemon.RETRY_EVERY
        h.rotate_if_big()
        last = journal.read(path)[-1]
        self.assertEqual((last["type"], last["data"]["why"]), ("core.snapshot", "rotate"))
        self.assertIsNone(h.rotate_retry)

    def test_no_snapshot_while_the_rotation_is_refused(self):
        """A snapshot that would push the journal over the threshold is left to the rotation ("rotate"); while a refused
        rotation waits for its retry none is written either (31.10: such snapshots were most of the archive); a crashed
        hubd folds from state.json."""
        h = hub(rotate_bytes=4096)
        events(h, 40)
        h.since_snapshot = daemon.SNAPSHOT_EVERY + 1
        h.snapshot_end = 0
        self.assertEqual(h.snapshot_due(), "rotate")
        h.rotate_retry = h.clock() + daemon.RETRY_EVERY
        self.assertIsNone(h.snapshot_due())


class StateFile(unittest.TestCase):
    def test_the_state_file_is_text_as_before(self):
        """Decoded as UTF-8 while it is read (one full-size buffer, not bytes and then their text); a BOM is an error, as
        it was with json.load on a UTF-8 file."""
        d = tempfile.mkdtemp(prefix="statesave-")
        target = os.path.join(d, journal.STATE_FILE)
        with open(target, "wb") as f:
            f.write(b'\xef\xbb\xbf{"seq": 1}')
        with self.assertRaises(ValueError):
            journal.read_state_file(d)
        with open(target, "w", encoding="utf-8") as f:
            json.dump({"seq": 2, "state": {"x": "é"}}, f, ensure_ascii=False)
        real, given = json.loads, []
        with mock.patch("json.loads", lambda s, *a, **kw: (given.append(type(s)), real(s, *a, **kw))[1]):
            self.assertEqual(journal.read_state_file(d)["state"]["x"], "é")
        self.assertEqual(given, [str])


class Readers(unittest.TestCase):
    """Every reader of state.json has closed it before parsing: on Windows a reader holding the file refuses hubd the
    replace, and parsing tens of MB takes far longer than reading them. Checked on any platform: the file object must be
    closed by the time json parses."""

    @classmethod
    def setUpClass(cls):
        h = hub()
        events(h, daemon.STATE_EVERY)
        cls.dir = h.state_dir
        cls.target = os.path.normcase(os.path.abspath(os.path.join(h.state_dir, journal.STATE_FILE)))

    def check(self, read):
        real_open, real_loads, real_load = builtins.open, json.loads, json.load
        opened, wrong = [], []

        def opening(file, *a, **kw):
            f = real_open(file, *a, **kw)
            if isinstance(file, (str, os.PathLike)) and os.path.normcase(os.path.abspath(file)) == self.target:
                opened.append(f)
            return f

        def loads(s, *a, **kw):
            if any(not f.closed for f in opened):
                wrong.append("parsed while the file is open")
            return real_loads(s, *a, **kw)

        def load(fp, *a, **kw):
            if fp in opened:
                wrong.append("parsed from the open file")
            return real_load(fp, *a, **kw)
        with mock.patch("builtins.open", opening), mock.patch("json.loads", loads), mock.patch("json.load", load):
            read()
        self.assertTrue(opened, "the reader did not read state.json")
        self.assertEqual(wrong, [])

    def test_load_state(self):
        self.check(lambda: journal.load_state(self.dir))

    def test_i18n(self):
        self.check(lambda: i18n.saved_state(self.dir))

    def test_tail_known(self):
        self.check(lambda: tail.known(self.dir, "s-0"))

    def test_scrub_live(self):
        self.check(lambda: scrub.live(self.dir))

    def test_modules_refusal(self):
        self.check(lambda: modules.state_file_refusal(self.dir))

    def test_browser_busy(self):
        self.check(lambda: Supervisor.busy(types.SimpleNamespace(state_dir=self.dir), "x"))

    def test_watch_core_waits(self):
        self.check(lambda: watch.Watcher.core_waits(types.SimpleNamespace(state_dir=self.dir), "m1"))

    def test_watch_core_waits_on_an_old_core(self):
        """The v1.4.0 page server imports watch during the update's self-check with its own core.journal in memory —
        one without read_state_file: the core's answer is unknown, as when the file is unreadable — yes."""
        reader = types.SimpleNamespace(state_dir=self.dir)
        self.assertFalse(watch.Watcher.core_waits(reader, "m1"))   # this core knows: m1 is older than the state
        with mock.patch.object(watch, "journal", types.SimpleNamespace()):
            self.assertTrue(watch.Watcher.core_waits(reader, "m1"))

    @unittest.skipUnless(stats, "the stats report is cut out of this tree")
    def test_stats(self):
        self.check(lambda: stats._core_state(self.dir))

    def test_stops_audit(self):
        spec = importlib.util.spec_from_file_location("stops_audit_reader", STOPS_AUDIT)
        audit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(audit)
        with mock.patch.dict(os.environ, {"HUB_STATE_DIR": self.dir}):
            self.check(audit.ledger_path)


if __name__ == "__main__":
    unittest.main()
