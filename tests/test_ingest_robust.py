"""A broken spool file does not stop hubd: it is set aside as .bad and the files behind it are taken; a lone surrogate
in the text (tool output) is written to the journal instead of bringing the core down together with all sessions."""
import tests  # noqa: F401 — keeps this machine's settings out
import json
import os
import tempfile
import time
import types
import unittest
from unittest import mock

from core import journal, spool
from core.daemon import Hub

OWNER = {"channel": "cli", "owner": True}
BAD = [   # shapes hubd used to crash on (a type or entity of the wrong shape is rejected with a reason by the fold anyway)
    "[1, 2]",
    "5",
    json.dumps({"type": "message.received", "data": [1]}),
    json.dumps({"type": "x.y", "data": "s"}),
]


class Ingest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="ingest-")
        self.box = os.path.join(self.state, "spool")
        self.hub = Hub(self.state, types.SimpleNamespace(poll=lambda: []))
        self.hub.execute = lambda a: None
        spool._held.clear()

    def raw(self, name, body):
        os.makedirs(self.box, exist_ok=True)
        with open(os.path.join(self.box, name), "w", encoding="utf-8") as f:
            f.write(body)

    def malformed(self):
        for i, body in enumerate(BAD, 1):
            self.raw(f"{i:020d}-1-a.json", body)
        spool.put(self.box, "session.requested", "a", {"cwd": "/w", "session_id": "s-a", **OWNER})

    def left(self):
        names = sorted(os.listdir(self.box))
        return [n for n in names if n.endswith(".bad")], [n for n in names if n.endswith(".json")]

    def test_malformed_files_are_set_aside_and_the_rest_is_taken_at_start(self):
        self.malformed()
        self.hub.ingest()
        self.assertIn("a", self.hub.state["sessions"])
        bad, waiting = self.left()
        self.assertEqual((len(bad), waiting), (len(BAD), []))

    def test_malformed_files_are_set_aside_in_the_tick(self):
        self.malformed()
        self.hub.collect()
        self.assertIn("a", self.hub.state["sessions"])
        bad, waiting = self.left()
        self.assertEqual((len(bad), waiting), (len(BAD), []))

    def held(self, *names):
        """open() of these mailbox files refuses (PermissionError), as Windows does while a scanner holds a new file."""
        real = open

        def refusing(path, *args, **kw):
            if os.path.basename(str(path)) in names:
                raise PermissionError(13, "Permission denied", path)
            return real(path, *args, **kw)
        return mock.patch("builtins.open", refusing)

    def test_a_file_that_cannot_be_opened_now_waits_and_keeps_the_order(self):
        # Windows, a starved machine: a fact just renamed into the mailbox was held for a moment (PermissionError on
        # open) and take() moved the good fact to .bad - the command was lost (the page test saw "not recorded").
        # The files behind it wait too: two commands of one session must not swap
        first = spool.put(self.box, "message.received", None, {"text": "one", **OWNER}) + ".json"
        second = spool.put(self.box, "message.received", None, {"text": "two", **OWNER}) + ".json"
        with self.held(first):
            self.assertEqual(spool.take(self.box), [])   # not the second one before the first
        bad, waiting = self.left()
        self.assertEqual((bad, len(waiting)), ([], 2))
        self.assertEqual([e["data"]["text"] for _, e in spool.take(self.box)], ["one", "two"])   # the next pass

    def test_a_file_held_for_long_is_set_aside_and_the_queue_moves_on(self):
        # a file the OS never opens (another user's, on POSIX) must not stop the queue for ever
        first = spool.put(self.box, "message.received", None, {"text": "one", **OWNER}) + ".json"
        spool.put(self.box, "message.received", None, {"text": "two", **OWNER})
        path = os.path.join(self.box, first)
        with self.held(first):
            self.assertEqual(spool.take(self.box), [])   # the first refusal starts the count
            spool._held[path] = time.monotonic() - spool.HELD - 1   # ... which has gone on for over HELD seconds
            self.assertEqual([e["data"]["text"] for _, e in spool.take(self.box)], ["two"])
        bad, waiting = self.left()
        self.assertEqual((bad, len(waiting)), ([first + ".bad"], 1))
        self.assertNotIn(path, spool._held)

    def test_a_held_file_that_cannot_be_set_aside_does_not_stop_the_queue_again(self):
        # the rename to .bad is refused too (held): the file stays, and the next pass does not wait HELD seconds again
        first = spool.put(self.box, "message.received", None, {"text": "one", **OWNER}) + ".json"
        spool.put(self.box, "message.received", None, {"text": "two", **OWNER})
        path = os.path.join(self.box, first)
        with self.held(first), mock.patch("core.spool.os.replace", side_effect=PermissionError(13, "held")):
            spool.take(self.box)
            spool._held[path] = time.monotonic() - spool.HELD - 1
            for _ in range(2):
                self.assertEqual([e["data"]["text"] for _, e in spool.take(self.box)], ["two"])
        self.assertIn(path, spool._held)

    def test_the_hold_is_counted_from_the_refusal_not_from_the_age_of_the_file(self):
        # hubd was down (or the machine slept): the fact has lain in the mailbox for long, one refusal of the OS is
        # still a moment's hold. A mtime ahead of the clock changes nothing either
        name = spool.put(self.box, "message.received", None, {"text": "one", **OWNER}) + ".json"
        for stamp in (time.time() - 3600, time.time() + 3600):
            os.utime(os.path.join(self.box, name), (stamp, stamp))
            with self.held(name):
                self.assertEqual(spool.take(self.box), [])
            self.assertEqual(self.left()[0], [])
        self.assertEqual(len(spool.take(self.box)), 1)

    def test_a_refusal_that_passed_does_not_count_towards_the_next_one(self):
        name = spool.put(self.box, "message.received", None, {"text": "one", **OWNER}) + ".json"
        path = os.path.join(self.box, name)
        with self.held(name):
            spool.take(self.box)
        spool._held[path] -= spool.HELD + 1   # a long-ago refusal ...
        self.assertEqual(len(spool.take(self.box)), 1)   # ... that was followed by a successful read
        self.assertNotIn(path, spool._held)

    def test_a_held_file_taken_away_by_another_is_forgotten(self):
        name = spool.put(self.box, "message.received", None, {"text": "one", **OWNER}) + ".json"
        path = os.path.join(self.box, name)
        with self.held(name):
            spool.take(self.box)
        self.assertIn(path, spool._held)
        os.unlink(path)
        spool.take(self.box)
        self.assertNotIn(path, spool._held)

    def test_a_fact_of_an_odd_type_is_still_written_and_rejected(self):
        # not into .bad: a rejection with a reason, so the CLI (req) and a module learn that the core did not accept it
        self.raw("00000000000000000001-1-a.json", json.dumps({"type": 5, "data": {"req": "r1"}}))
        self.hub.ingest()
        types_ = [e["type"] for e in journal.read(self.hub.journal.path)]
        self.assertEqual(types_[-1], "core.rejected")
        self.assertEqual(self.left(), ([], []))

    def test_a_fact_without_a_type_or_with_an_unhashable_type_or_req_does_not_stop_hubd(self):
        # the "already written" check took item["type"] and put (type, req) into a set: KeyError and TypeError on every
        # start, and the file stayed in the spool
        # and after the write: the start gathered (type, req) of the journal tail into the same set
        forms = [{"data": {}}, {"type": "x.y", "data": {"req": [1]}}, {"type": ["x"], "data": {}},
                 {"type": {"a": 1}, "data": {"req": {"b": 2}}}, {"type": ["x"], "data": {"req": "r1"}}]
        for take in ("ingest", "collect"):
            for form in forms:
                with self.subTest(take=take, form=form):
                    self.hub = self.again()
                    self.raw("00000000000000000001-1-a.json", json.dumps(form))
                    getattr(self.hub, take)()
                    # for x.y without a channel the spool check also marks it (spool.flagged and the spool.checked count, core/admit.py), after the rejection
                    types_ = [e["type"] for e in journal.read(self.hub.journal.path)
                              if e["type"] not in ("spool.flagged", "spool.checked")]
                    self.assertEqual(types_[-1], "core.rejected")
                    self.assertEqual(self.left(), ([], []))

    def test_hubd_starts_on_a_journal_with_a_fact_of_an_odd_type_and_a_string_req(self):
        # such a fact is written and rejected; the next start gathered (type, req) of the tail into a set and crashed
        self.raw("00000000000000000001-1-a.json", json.dumps({"type": ["x"], "data": {"req": "r1"}}))
        self.hub.ingest()
        self.assertEqual(self.count("core.rejected"), 1)
        self.again().ingest()
        self.assertEqual(self.left(), ([], []))

    def test_start_counts_the_waiting_messages_past_a_malformed_file(self):
        self.raw("00000000000000000001-1-a.json", "[1, 2]")
        spool.put(self.box, "message.received", None, {"text": "hi", **OWNER})
        h = self.hub
        h.load_catalog = h.load_news = h.ingest = h.ensure_hub = h.ensure_dev = h.reap = h.reconcile = lambda: None
        try:
            h.start()
        except Exception:  # noqa: BLE001 — the test does not need anything past the start (host, modules): core.started is what matters
            pass
        (ev,) = [e for e in journal.read(h.journal.path) if e["type"] == "core.started"]
        self.assertEqual(ev["data"]["spooled"], 1)

    def again(self):
        hub = Hub(self.state, types.SimpleNamespace(poll=lambda: []))
        hub.execute = lambda a: None
        return hub

    def count(self, type):
        return len([e for e in journal.read(self.hub.journal.path) if e["type"] == type])

    def test_a_fact_the_journal_could_not_write_stays_in_the_spool(self):
        """Full disk: the write failed, so the core crashes; the fact waits in the spool and arrives after the restart, not in .bad."""
        from unittest import mock
        spool.put(self.box, "session.requested", "a", {"cwd": "/w", "session_id": "s-a", **OWNER})
        with mock.patch.object(journal.os, "fsync", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                self.hub.ingest()
        self.assertEqual([n[-5:] for n in os.listdir(self.box)], [".json"])
        self.hub = self.again()
        self.hub.ingest()
        self.assertIn("a", self.hub.state["sessions"])
        self.assertEqual(os.listdir(self.box), [])

    def test_a_fact_already_in_the_journal_is_not_taken_again_after_a_crash(self):
        """hubd died between writing the fact and deleting its file: the next start used to write it a second time."""
        spool.put(self.box, "message.received", None, {"text": "hi", "req": "r1", **OWNER})
        (path, item), = spool.take(self.box)
        self.hub.record(item["type"], item.get("entity"), item.get("data"), src="cli")
        self.hub = self.again()
        self.hub.ingest()
        self.assertEqual(self.count("message.received"), 1)
        self.assertEqual(os.listdir(self.box), [])

    def test_the_cli_giving_up_on_a_command_of_a_past_life_is_taken(self):
        """cli.gave_up carries the req of the command it waited for: by req alone it would be taken for the same command,
        and the session would never learn of the refusal."""
        self.hub.record("session.rename_requested", "nosuch", {"to": "x", "req": "r1", "channel": "session",
                                                                "owner": False, "sender": "w1"})
        spool.put(self.box, "cli.gave_up", None, {"req": "r1", "what": "x", "channel": "session", "owner": False,
                                                  "sender": "w1"}, src="session")
        self.hub = self.again()
        self.hub.ingest()
        self.assertEqual(self.count("cli.gave_up"), 1)
        self.assertEqual(os.listdir(self.box), [])

    def test_a_file_that_cannot_be_deleted_is_not_taken_twice(self):
        """Windows: an antivirus holds the file, so unlink crashed the tick and the next pass took the fact again."""
        from unittest import mock
        from core import daemon
        spool.put(self.box, "message.received", None, {"text": "hi", **OWNER})
        with mock.patch.object(daemon.os, "unlink", side_effect=PermissionError(13, "in use")):
            self.hub.ingest()
            self.hub.collect()
        self.hub.collect()
        self.assertEqual(self.count("message.received"), 1)
        self.assertEqual(os.listdir(self.box), [])

    def test_a_lone_surrogate_is_written_and_folded_the_same(self):
        spool.put(self.box, "session.requested", "a", {"cwd": "/w", "session_id": "s-a", **OWNER})
        self.raw("99999999999999999999-1-z.json",
                 '{"type": "message.received", "data": {"to": "a", "text": "out \\ud800 put", "channel": "cli", '
                 '"owner": true}}')
        self.hub.ingest()
        (msg,) = self.hub.state["messages"].values()
        self.assertEqual(msg["text"], "out ? put")
        (ev,) = [e for e in journal.read(self.hub.journal.path) if e["type"] == "message.received"]
        self.assertEqual(ev["data"]["text"], "out ? put")
        self.assertEqual(self.left(), ([], []))

    def test_a_surrogate_in_a_host_line_is_written(self):
        self.hub.record("session.said", "a", {"text": "x\udcff y"}, src="host")
        (ev,) = [e for e in journal.read(self.hub.journal.path) if e["type"] == "session.said"]
        self.assertEqual(ev["data"]["text"], "x? y")


if __name__ == "__main__":
    unittest.main()
