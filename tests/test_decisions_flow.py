"""An owner decision told to an executor is not lost (spec §4): the executor's `hub decided` goes to whoever raised it,
and that session records it in its own CLAUDE.md («Owner decisions»)."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

from core import journal, reducer

OWNER = {"channel": "cli", "owner": True}


class Decisions(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "devops", cwd="/k", session_id="s-d", role="agent", about="DevOps", **OWNER)
        self.ev("session.started", "devops")
        self.ev("turn.started", "devops", marker=None, prompt_head="x")
        self.ev("session.requested", "devops-2", session_id="s-f", cwd="/k", executor=True, raised_by="devops", **OWNER)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def test_an_executors_decision_reaches_whoever_raised_it(self):
        self.ev("owner.decided", None, text="no tracking until the new analytics", channel="session", owner=False,
                sender="devops-2")
        notes = [m for m in self.st["messages"].values() if m["session"] == "devops" and "analytics" in m["text"]]
        self.assertEqual(len(notes), 1)
        self.assertIn("CLAUDE.md", notes[0]["text"])
        self.assertIn("devops-2", notes[0]["text"])

    def test_a_specialists_own_decision_stays_with_it(self):
        self.ev("owner.decided", None, text="deploys on Wednesdays", channel="session", owner=False, sender="devops")
        self.assertFalse([m for m in self.st["messages"].values() if "Wednesdays" in m["text"]])


if __name__ == "__main__":
    unittest.main()


class Revoke(Decisions):
    """`hub decided undo <seq>`: a decision recorded by mistake (`hub decided list` once made the decision "list") is
    taken back by a new fact, the journal only grows: its author, the hub or the owner withdraws it; the summary forgets
    it, the list marks it, whoever was told about it hears it is withdrawn."""

    def decide(self, text="list", sender="devops"):
        self.assertIsNone(self.ev("owner.decided", None, text=text, channel="session", owner=False, sender=sender))
        return self.seq

    def revoke(self, seq, **who):
        return self.ev("owner.decided_revoked", None, seq=seq, **who)

    def test_the_author_withdraws_it_and_the_summary_forgets_it(self):
        seq = self.decide()
        keep = self.decide("deploys on Wednesdays")
        self.assertIsNone(self.revoke(seq, channel="session", owner=False, sender="devops"))
        self.assertEqual([d["text"] for d in self.st["owner"]["decided"]], ["deploys on Wednesdays"])
        self.assertEqual({d["seq"]: bool(d.get("revoked")) for d in self.st["decisions"]}, {seq: True, keep: False})

    def test_the_hub_and_the_owner_withdraw_anyones_another_session_does_not(self):
        a, b, c = self.decide(), self.decide("x"), self.decide("y")
        self.assertEqual(self.revoke(a, channel="session", owner=False, sender="devops-2"), reducer.DECISION_WHO)
        self.assertIsNone(self.revoke(a, channel="session", owner=False, sender="hub"))
        self.assertIsNone(self.revoke(b, **OWNER))
        self.assertEqual([d["text"] for d in self.st["owner"]["decided"]], ["y"])
        self.assertTrue(c)

    def test_an_unknown_or_repeated_withdrawal_is_refused(self):
        seq = self.decide()
        self.assertEqual(self.revoke(999, **OWNER), reducer.DECISION_UNKNOWN.format(seq=999))
        self.assertIsNone(self.revoke(seq, **OWNER))
        self.assertEqual(self.revoke(seq, **OWNER), reducer.DECISION_GONE.format(seq=seq))

    def test_whoever_was_told_hears_it_is_withdrawn(self):
        seq = self.decide("no tracking until the new analytics", sender="devops-2")
        told = [m for m in self.st["messages"].values() if m["session"] == "devops"]
        self.assertIsNone(self.revoke(seq, channel="session", owner=False, sender="devops-2"))
        notes = [m for m in self.st["messages"].values() if m["session"] == "devops"][len(told):]
        self.assertEqual(len(notes), 1)
        self.assertIn(f"#{seq}", notes[0]["text"])
        self.assertIn("no tracking until the new analytics", notes[0]["text"])

    def test_only_the_last_ones_are_kept_and_a_rename_keeps_the_author(self):
        with unittest.mock.patch.object(reducer, "DECISIONS_KEPT", 3):
            first = self.decide("a", sender="devops-2")
            for t in "bcd":
                last = self.decide(t, sender="devops-2")
        self.assertEqual([d["text"] for d in self.st["decisions"]], ["b", "c", "d"])
        self.assertTrue(self.revoke(first, **OWNER))   # gone from the list: refused, not crashed
        self.st["sessions"]["devops-2"]["state"] = "asleep"   # a sleeping session is renamed at once
        self.assertIsNone(self.ev("session.renamed", "devops-2", to="ops-2", **OWNER))
        self.assertIn("ops-2", self.st["sessions"])
        self.assertEqual({d["by"] for d in self.st["decisions"]}, {"ops-2"})
        self.assertIsNone(self.revoke(last, channel="session", owner=False, sender="ops-2"))

    def test_the_feed_says_the_decision_was_withdrawn_once(self):
        from core import views
        events = [{"seq": 1, "ts": 1, "type": "session.requested", "entity": "devops",
                   "data": {"cwd": "/k", "session_id": "s", "role": "agent", **OWNER}},
                  {"seq": 2, "ts": 2, "type": "owner.decided", "entity": None,
                   "data": {"text": "list", "channel": "session", "owner": False, "sender": "devops"}}]
        for seq in (3, 4):   # the second one is refused: no second line
            events.append({"seq": seq, "ts": seq, "type": "owner.decided_revoked", "entity": None,
                           "data": {"seq": 2, "channel": "session", "owner": False, "sender": "devops"}})
        r = views.Reader()
        r.feed_all(events)
        lines = [it["text"] for it in r.timeline("devops")["items"] if it.get("kind") == "sys" and "#2" in it["text"]]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("list", lines[0])


class OldStateFile(unittest.TestCase):
    """A hub updated from a core without `hub decided list` starts from a state file without "decisions": hubd finds
    the decisions in the journal file at start, so the ones recorded before the update are listed and withdrawn."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="decided-old-")
        j = journal.Journal(os.path.join(self.dir, "journal.jsonl"))
        say = {"channel": "session", "owner": False, "sender": "hub"}
        j.append("session.requested", "hub", {"cwd": "/h", "session_id": "s", "kind": "hub", **OWNER})
        j.append("owner.decided", None, {"text": "first", **say})
        j.append("owner.decided", None, {"text": "list", **say})
        j.append("owner.decided_revoked", None, {"seq": 2, **say})
        old = reducer.replay(journal.read(os.path.join(self.dir, "journal.jsonl")))
        old.pop("decisions")   # the state file of the earlier core
        journal.save_state(self.dir, old)
        j.append("owner.decided", None, {"text": "after the state file", **say})

    def test_hubd_finds_them_at_start_and_they_can_be_withdrawn(self):
        st = journal.load_state(self.dir, decisions=True)
        self.assertEqual([(d["seq"], d["text"], bool(d.get("revoked"))) for d in st["decisions"]],
                         [(2, "first", True), (3, "list", False), (5, "after the state file", False)])
        st, _, why = reducer.reduce(st, {"seq": 6, "ts": 6, "type": "owner.decided_revoked", "entity": None,
                                         "data": {"seq": 3, **OWNER}})
        self.assertIsNone(why)
        self.assertTrue(st["decisions"][1]["revoked"])

    def test_a_hub_with_no_decisions_is_marked_done_and_not_scanned_again(self):
        d = tempfile.mkdtemp(prefix="decided-none-")
        j = journal.Journal(os.path.join(d, "journal.jsonl"))
        j.append("session.requested", "hub", {"cwd": "/h", "session_id": "s", "kind": "hub", **OWNER})
        old = reducer.replay(journal.read(os.path.join(d, "journal.jsonl")))
        old.pop("decisions", None)   # the state file of the earlier core
        journal.save_state(d, old)
        st = journal.load_state(d, decisions=True)
        self.assertEqual(st.get("decisions"), [])   # the key stays in the next state file: no scan at the next start
        journal.save_state(d, st)
        with unittest.mock.patch.object(journal, "decided_events", side_effect=AssertionError("scanned again")):
            self.assertEqual(journal.load_state(d, decisions=True)["decisions"], [])

    def test_commands_do_not_read_the_journal_for_it(self):
        st = journal.load_state(self.dir)
        self.assertEqual([d["seq"] for d in st["decisions"]], [5])

    def test_a_snapshot_in_the_journal_without_them_is_filled_on_replay(self):
        events = journal.read(os.path.join(self.dir, "journal.jsonl"))
        snap = reducer.replay(events[:4])
        snap.pop("decisions")
        events.insert(4, {"seq": 4, "ts": 4, "type": "core.snapshot", "entity": None, "data": {"state": snap}})
        events[5:] = [{**e, "seq": e["seq"] + 1} for e in events[5:]]
        st = reducer.replay(events)
        self.assertEqual([d["seq"] for d in st["decisions"]], [2, 3, 6])

    def test_the_command_lists_them_and_marks_the_withdrawn_one(self):
        journal.save_state(self.dir, journal.load_state(self.dir, decisions=True))   # as hubd saves it
        hub = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
        env = {k: v for k, v in os.environ.items() if k != "HUB_SESSION"}
        out = subprocess.run([sys.executable, hub, "decided", "list"], capture_output=True, text=True, encoding="utf-8",
                             env={**env, "HUB_STATE_DIR": self.dir, "HUB_CLI_WAIT": "0"}).stdout
        rows = [line for line in out.splitlines() if line.startswith("#")]
        self.assertEqual([r.split()[0] for r in rows], ["#2", "#3", "#5"])
        self.assertTrue(rows[0].endswith("]") and not rows[1].endswith("]"), rows)   # the withdrawn one is marked


class RotatedJournal(unittest.TestCase):
    """10.11: hubd started on a rotated journal whose last line is a snapshot of the whole state (tens of MB), with the
    records in the archive before it: the list came out empty and the key was left as "searched", so a withdrawal was
    refused for good."""

    SAY = {"channel": "session", "owner": False, "sender": "harbor"}

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="decided-rot-")
        self.path = os.path.join(self.dir, "journal.jsonl")
        j = journal.Journal(self.path)
        j.append("session.requested", "harbor", {"cwd": "/h", "session_id": "s", "role": "agent", **OWNER})
        self.old = j.append("owner.decided", None, {"text": "old", **self.SAY})["seq"]
        self.bad = j.append("owner.decided_revoked", None, {"seq": self.old, **self.SAY})["seq"]
        j.append("core.rejected", None, {"of": self.bad, "type": "owner.decided_revoked", "reason": "unknown event type"})
        for i in range(4):
            j.append("session.said", "harbor", {"text": f"t{i}"})
        # an older hubd: no records in its state; rotation carries the last events only, the archive keeps the rest
        state = reducer.replay(journal.read(self.path))
        state.pop("decisions")
        journal.rotate(self.dir, state, keep=2, per_session=0)
        self.assertNotIn(self.old, [e["seq"] for e in journal.read(self.path)])
        self.j = journal.Journal(self.path)
        self.new = self.j.append("owner.decided", None, {"text": "new", **self.SAY})["seq"]

    def state_without_records(self, partial=False):
        state = reducer.replay(journal.read(self.path))   # the list of the carried part alone: the new one
        if not partial:
            state.pop("decisions")
        journal.save_state(self.dir, state)

    def test_the_records_of_the_archive_are_found_and_the_rejected_withdrawal_counts_for_nothing(self):
        self.state_without_records()
        st = journal.load_state(self.dir, decisions=True)
        self.assertEqual([(d["seq"], bool(d.get("revoked"))) for d in st["decisions"]],
                         [(self.old, False), (self.new, False)])
        st, _, why = reducer.reduce(st, {"seq": st["seq"] + 1, "ts": 9, "type": "owner.decided_revoked", "entity": None,
                                         "data": {"seq": self.old, **OWNER}})
        self.assertIsNone(why)
        self.assertTrue(st["decisions"][0]["revoked"])

    def test_a_snapshot_as_big_as_the_state_is_no_wall_in_front_of_them(self):
        self.j.append("core.snapshot", None, {"why": "stop", "state": {"pad": "x" * (33 << 20)}})
        self.state_without_records()
        st = journal.load_state(self.dir, decisions=True)
        self.assertEqual([d["seq"] for d in st["decisions"]], [self.old, self.new])

    def test_a_state_file_that_is_not_of_this_journal_falls_back_to_the_replay_and_still_finds_them(self):
        st = journal.load_state(self.dir, decisions=True)   # no state file at all
        self.assertEqual([d["seq"] for d in st["decisions"]], [self.old, self.new])

    def test_a_list_the_replay_built_from_the_carried_part_alone_is_searched_once_and_marked(self):
        self.state_without_records(partial=True)
        st = journal.load_state(self.dir, decisions=True)
        self.assertEqual([d["seq"] for d in st["decisions"]], [self.old, self.new])
        journal.save_state(self.dir, st)
        with unittest.mock.patch.object(journal, "decided_events", side_effect=AssertionError("scanned again")):
            self.assertEqual([d["seq"] for d in journal.load_state(self.dir, decisions=True)["decisions"]],
                             [self.old, self.new])

    def test_a_hub_with_none_is_searched_once_too(self):
        d = tempfile.mkdtemp(prefix="decided-none-")
        j = journal.Journal(os.path.join(d, "journal.jsonl"))
        j.append("session.requested", "hub", {"cwd": "/h", "session_id": "s", "kind": "hub", **OWNER})
        state = reducer.replay(journal.read(os.path.join(d, "journal.jsonl")))
        state.pop("decisions", None)
        journal.save_state(d, state)
        st = journal.load_state(d, decisions=True)
        self.assertEqual((st["decisions"], st["decisions_searched"]), ([], True))
        journal.save_state(d, st)
        with unittest.mock.patch.object(journal, "decided_events", side_effect=AssertionError("scanned again")):
            journal.load_state(d, decisions=True)

    def test_a_list_the_state_has_keeps_its_slug_and_told_when_the_search_runs(self):
        state = reducer.replay(journal.read(self.path))
        state["decisions"] = [{"seq": self.new, "by": "harbor", "text": "new", "ts": 1, "slug": "dir", "told": "boss"}]
        journal.save_state(self.dir, state)
        st = journal.load_state(self.dir, decisions=True)
        self.assertEqual([d["seq"] for d in st["decisions"]], [self.old, self.new])
        self.assertEqual((st["decisions"][1]["slug"], st["decisions"][1]["told"]), ("dir", "boss"))
        self.assertTrue(st["decisions_searched"])

    def test_a_rejected_record_does_not_end_the_search_early(self):
        with unittest.mock.patch.object(journal, "DECISIONS_ARCHIVES", 4):
            events = journal.decided_events(self.dir, 2)
        self.assertEqual([e["seq"] for e in events if e["type"] == "owner.decided"], [self.old, self.new])

    def test_a_journal_never_rotated_does_not_read_the_archives(self):
        d = tempfile.mkdtemp(prefix="decided-plain-")
        j = journal.Journal(os.path.join(d, "journal.jsonl"))
        j.append("session.requested", "hub", {"cwd": "/h", "session_id": "s", "kind": "hub", **OWNER})
        j.append("owner.decided", None, {"text": "x", **self.SAY})
        state = reducer.replay(journal.read(os.path.join(d, "journal.jsonl")))
        state.pop("decisions")
        journal.save_state(d, state)
        with unittest.mock.patch.object(journal, "archives", side_effect=AssertionError("archives read")):
            self.assertEqual([x["seq"] for x in journal.load_state(d, decisions=True)["decisions"]], [2])
