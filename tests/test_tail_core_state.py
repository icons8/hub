"""The page takes its state from the core instead of recomputing history by the new rules (a fork was in
an unclear state; after queue changes the page replayed the old journal the new way, so in its version the fork was
alive and a message to it stayed "sending" forever, though the core had long delivered it to the agent). The page
still builds timelines from the journal, and takes its state at start from the core's state file, if that file is for
the same place in the journal. The journal is live: hubd writes events all the time, so "the same place" is the place
the page has read to, not the end of the journal at the moment it asks."""
import tests  # noqa: F401 — ignore the machine's settings
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from core import journal, reducer, specmode
from core.tail import ADOPT_EVERY, ADOPT_RETRY, ADOPT_TRIES, Tail
from tests.test_minihub import Delegating, by

OWNER = {"channel": "web", "owner": True}


class CoreStateWins(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tail-core-")
        self.path = os.path.join(self.dir, "journal.jsonl")
        j = journal.Journal(self.path)
        j.append("session.requested", "a", {"cwd": "/w", "session_id": "s-a", **OWNER})
        j.append("message.received", None, {"to": "a", "text": "hello", **OWNER})

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_page_state_is_the_cores_when_the_state_file_matches(self):
        core = reducer.replay(journal.read(self.path))
        mid = next(iter(core["messages"]))
        core["messages"][mid]["state"] = "delivered"          # "the core knows better" than a recomputation by the current code
        journal.save_state(self.dir, core)
        t = Tail(self.path)
        t.poll()
        self.assertEqual(t.reader.state["messages"][mid]["state"], "delivered")
        self.assertEqual(t.snapshot["messages"][mid]["state"], "delivered")
        self.assertTrue(any(i.get("kind") == "in" for i in t.reader.timelines.get("a", [])))   # timelines come from the journal

    def test_without_a_matching_state_file_the_replay_stands(self):
        t = Tail(self.path)
        t.poll()
        (m,) = t.reader.state["messages"].values()
        self.assertIn(m["state"], ("queued", "typing"))


class OlderCoreDecided(Delegating):
    """A stuck spec card (found 10.11): hubd had been running since before the rule "while an executor's spec is open
    the owner's words go to the executor" and relayed the owner's words to its mini-hub; whoever raised the executor
    accepted the spec with `--for` that message — hubd accepted. The page, restarted on the new code, recomputed the same
    journal by the new rules: the words went to the executor, the `--for` refused, the spec stayed "open" and the card
    "Spec is ready" hung with buttons the core refuses ("the task has a reply"), the owner's words — "Sending" forever.
    The page takes the core's state, not its own recomputation."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(prefix="tail-skew-")
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.path = os.path.join(self.dir, "journal.jsonl")
        self.minihub()
        self.ev("session.spec_set", "notes", on=True, **OWNER)
        self.run_task("notes-ci", text="design the export", spec=True)
        self.start("notes-ci")
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "notes-ci"]
        self.did = d["id"]
        self.ev("turn.started", "notes-ci", marker=d["message"])
        self.ev("turn.result", "notes-ci", text="The spec is ready.")
        self.ev("turn.stopped", "notes-ci")
        self.setup_events = len(self.events)

    def decided_by_the_older_core(self):
        """The owner's words and the acceptance as hubd of the previous code decided them: the words through the mini-hub."""
        with mock.patch.object(specmode, "direct", lambda *a: None):
            self.ev("message.received", None, to="notes-ci", text="tell them S1 is in", **OWNER)
            self.word = f"m{self.seq}"
            self.assertIsNone(self.ev("delegation.spec_accepted", self.did, sha256="ab" * 32, **{"for": self.word},
                                      pending=f"specs/{self.did}/.accepted-abababababab-00000000.md", **by("notes")))
        self.assertEqual(self.st["delegations"][self.did]["spec"]["state"], "accepted")

    def journal_of(self, events):
        j = journal.Journal(self.path)
        for e in events:
            self.assertEqual(j.append(e["type"], e["entity"], e["data"])["seq"], e["seq"])

    def page_row(self, t):
        return next(x for x in t.snapshot["delegations"] if x["id"] == self.did)

    def test_the_card_goes_when_the_page_starts_on_a_journal_that_is_still_written(self):
        self.decided_by_the_older_core()
        self.journal_of(self.events)
        journal.save_state(self.dir, self.st)
        t = Tail(self.path)
        real = journal.read_state_file

        def asked_while_hubd_writes(state_dir):   # an event comes in while the page loads the core's state file
            journal.Journal(self.path).append("session.recap", "notes", {"text": "idle", "by": "model"})
            return real(state_dir)
        with mock.patch.object(journal, "read_state_file", asked_while_hubd_writes):
            t.poll()
        self.assertEqual(self.page_row(t)["spec"]["state"], "accepted")
        self.assertEqual(self.page_row(t)["state"], "working")
        self.assertFalse(next(s for s in t.snapshot["sessions"] if s["name"] == "notes-ci").get("spec"))   # no card
        self.assertEqual(t.reader.state["messages"][self.word]["session"], "notes")   # the words went through the mini-hub

    def test_what_the_page_recomputed_by_the_new_rules_is_taken_back_from_the_core(self):
        self.journal_of(self.events[:self.setup_events])
        t = Tail(self.path)
        t.poll()
        self.assertEqual(self.page_row(t)["spec"]["state"], "open")
        self.decided_by_the_older_core()
        self.journal_of(self.events[self.setup_events:])
        journal.save_state(self.dir, self.st)
        t.adopt_next = 0   # the page's own clock: the time to compare with the core has come
        t.poll()
        self.assertEqual(self.page_row(t)["spec"]["state"], "accepted")
        self.assertEqual(t.reader.state["messages"][self.word]["session"], "notes")
        self.assertEqual((t.adopted, t.rev), (1, 1))   # the stream of a quiet hub sees the change (Web.stream_view rev)

    def test_the_checkpoint_of_the_reader_holds_the_core_state_it_took(self):
        self.journal_of(self.events[:self.setup_events])
        t = Tail(self.path, checkpoint=True)
        t.poll()
        self.decided_by_the_older_core()
        self.journal_of(self.events[self.setup_events:])
        journal.save_state(self.dir, self.st)
        t.adopt_next = 0
        t.checkpoint.next_write = 0   # a write is due: Tail.poll captures after the replacement, not before it
        t.poll()
        self.assertEqual(self.page_row(t)["spec"]["state"], "accepted")
        os.remove(os.path.join(self.dir, journal.STATE_FILE))   # a restart without the core's file: only the checkpoint
        again = Tail(self.path, checkpoint=True)
        again.poll()
        self.assertEqual(self.page_row(again)["spec"]["state"], "accepted")
        self.assertEqual(again.reader.state["messages"][self.word]["session"], "notes")

    def test_a_quiet_hub_is_compared_too_and_a_file_ahead_of_the_page_is_tried_again_soon(self):
        self.journal_of(self.events[:self.setup_events])
        t = Tail(self.path)
        t.poll()   # no state file yet: nothing to take, the next try is soon
        self.assertEqual(t.adopt_misses, 1)
        self.assertLessEqual(t.adopt_next - time.monotonic(), ADOPT_RETRY)
        self.decided_by_the_older_core()
        self.journal_of(self.events[self.setup_events:])
        journal.save_state(self.dir, self.st)
        t.adopt_next = 0
        with mock.patch.object(journal, "state_at", lambda *a: journal.AHEAD):   # the file is ahead of the page's place
            t.poll()
        self.assertEqual(self.page_row(t)["spec"]["state"], "open")
        self.assertEqual(t.adopt_misses, 0)   # the page will catch up: not a file that never fits
        self.assertLessEqual(t.adopt_next - time.monotonic(), ADOPT_RETRY)
        t.adopt_next = 0
        t.poll()   # nothing new in the journal: the hub is quiet, the page still compares
        self.assertEqual(self.page_row(t)["spec"]["state"], "accepted")
        self.assertGreater(t.adopt_next - time.monotonic(), ADOPT_RETRY)

    def test_a_state_file_that_never_fits_is_not_tried_every_few_seconds(self):
        self.journal_of(self.events[:self.setup_events])
        t = Tail(self.path)   # no state file at all
        t.poll()   # the first miss
        waits = []
        for _ in range(3):
            t.adopt_next = 0
            t.poll()
            waits.append(round(t.adopt_next - time.monotonic()))
        self.assertEqual(ADOPT_TRIES, 3)
        self.assertEqual(waits, [ADOPT_RETRY, ADOPT_EVERY, ADOPT_EVERY])

    def test_a_broken_state_file_does_not_stop_the_page(self):
        self.journal_of(self.events[:self.setup_events])
        t = Tail(self.path)
        t.poll()
        for broken in ('{"seq": 1, "offset": 0, "first": 1, "state": 5}', '[]', "{"):
            with open(os.path.join(self.dir, journal.STATE_FILE), "w") as f:
                f.write(broken)
            t.adopt_next = 0
            t.poll()   # no exception, the page keeps its own state
            self.assertGreater(t.adopt_next, 0)

    def test_a_state_file_ahead_of_the_page_waits_and_the_page_keeps_its_own(self):
        self.decided_by_the_older_core()
        self.journal_of(self.events)
        journal.save_state(self.dir, self.st)
        self.assertIs(journal.state_at(self.dir, self.seq - 1), journal.AHEAD)
        self.assertEqual(journal.state_at(self.dir, self.seq)["seq"], self.seq)


if __name__ == "__main__":
    unittest.main()
