"""Watchdog alerts for the hub are kept in st["watch"] under session-name keys — service records (concessions,
core failure, promise reminders) live separately, under keys containing ":" (a session name does not start with it):
a session named "surrender" or "core" does not share a record with a service one. State of an earlier hub (old
state.json, journal snapshot) moves to the new keys on read. The concession watchdog scans a turn without a window
of the latest steps."""
import tests  # noqa: F401 — hide the machine's settings
import json
import os
import tempfile
import unittest

from core import journal, reducer, spool
from modules.watch import watch

OWNER = {"channel": "web", "owner": True}


class Keys(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        for name in ("a", "surrender", "core"):
            self.ev("session.requested", name, cwd="/w", session_id=f"s-{name}", **OWNER)
            self.ev("session.started", name)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 10000 + 10 * self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def surrender(self, session="a"):
        self.ev("message.received", None, to=session, text="are you sure?",**OWNER)
        return self.ev("watch.surrender", session, message=f"m{self.seq}", channel="cli", owner=True)

    def alert(self, session, kind="loop"):
        return self.ev("watch.alert", session, kind=kind, reason="r", channel="cli", owner=True)

    def test_a_session_named_surrender_is_alerted_after_a_surrender_was_noted(self):
        self.assertIsNone(self.surrender())
        self.assertIsNone(self.alert("surrender"))
        self.assertIsNotNone(self.alert("surrender"))   # too early: it has already been reported

    def test_a_surrender_is_noted_after_the_session_named_surrender_was_alerted(self):
        self.assertIsNone(self.alert("surrender"))
        self.assertIsNone(self.surrender())

    def test_a_core_bug_does_not_share_the_pause_with_the_session_named_core(self):
        self.assertIsNone(self.ev("watch.alert", None, kind="corebug", reason="r", channel="cli", owner=True))
        self.assertIsNone(self.alert("core"))
        self.assertIsNotNone(self.ev("watch.alert", None, kind="corebug", reason="r", channel="cli", owner=True))

    def test_the_session_named_core_alerted_first_does_not_hold_back_a_core_bug(self):
        self.assertIsNone(self.alert("core"))
        self.assertIsNone(self.ev("watch.alert", None, kind="corebug", reason="r", channel="cli", owner=True))

    def test_the_service_keys_are_not_session_names(self):
        self.surrender()
        self.ev("watch.alert", None, kind="corebug", reason="r", channel="cli", owner=True)
        self.alert("a")
        self.assertEqual(sorted(k for k in self.st["watch"] if not k.startswith(":")), ["a"])
        self.assertTrue(all(not reducer.NAME.fullmatch(k) for k in self.st["watch"] if k.startswith(":")))


def old_layout():
    """State before the split: service keys next to session names."""
    st = reducer.empty()
    st["sessions"] = {"a": {"name": "a", "state": "idle"}}
    st["watch"] = {"surrender": ["m3", "m7"], "core": 5000, "a": 4000, "a:undelivered": 4100}
    return st


class Migration(unittest.TestCase):
    def check(self, st):
        self.assertEqual(st["watch"][reducer.SURRENDER_KEY], ["m3", "m7"])
        self.assertEqual(st["watch"][reducer.CORE_KEY], 5000)
        self.assertEqual((st["watch"]["a"], st["watch"]["a:undelivered"]), (4000, 4100))
        self.assertNotIn("surrender", st["watch"])
        self.assertNotIn("core", st["watch"])

    def test_a_snapshot_event_is_read_on_the_new_keys(self):
        self.check(reducer.restore({"seq": 9, "data": {"state": old_layout()}}))

    def test_the_state_file_is_read_on_the_new_keys(self):
        d = tempfile.mkdtemp(prefix="watch-keys-")
        with open(os.path.join(d, "journal.jsonl"), "w") as f:
            f.write(json.dumps({"seq": 1, "ts": 1, "type": "core.started", "data": {}, "src": "t"}) + "\n")
        st = old_layout()
        st["seq"] = 1
        journal.save_state(d, st)
        self.check(journal.load_state(d))

    def test_the_new_layout_is_left_as_it_is(self):
        st = reducer.restore({"seq": 9, "data": {"state": old_layout()}})
        again = reducer.restore({"seq": 9, "data": {"state": json.loads(json.dumps(st))}})
        self.assertEqual(again["watch"], st["watch"])

    def test_a_pause_of_the_session_named_core_is_not_taken_for_the_core_one(self):
        st = old_layout()
        st["sessions"]["core"] = {"name": "core", "state": "idle"}   # the old record is indistinguishable: do not take it from the session
        st = reducer.restore({"seq": 9, "data": {"state": st}})
        self.assertEqual(st["watch"]["core"], 5000)
        self.assertNotIn(reducer.CORE_KEY, st["watch"])

    def test_old_alerts_still_hold_back_the_repeat(self):
        st = reducer.restore({"seq": 9, "data": {"state": old_layout()}})
        st, _, why = reducer.reduce(st, {"seq": 10, "ts": 4500, "type": "watch.alert", "entity": "a",
                                         "data": {"kind": "loop", "reason": "r", "channel": "cli", "owner": True}})
        self.assertIsNotNone(why)


class Feed(unittest.TestCase):
    """A concession without a check is caught on a long turn too: the turn start and "did it check" do not depend
    on the steps window."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="surrender-long-")
        self.write([(1, 100, "session.requested", "hub", {"cwd": "/h", "session_id": "s-h", "kind": "hub", **OWNER}),
                    (2, 100, "session.requested", "a", {"cwd": "/w", "session_id": "s-a", **OWNER}),
                    (3, 101, "session.started", "hub", {}), (4, 101, "session.started", "a", {})])
        self.w = watch.Watcher(self.state, {"surrender": True}, ask=lambda t: {"trouble": False}, needs=None,
                               urgent=None, recap=lambda text: "")
        self.step(102)

    def write(self, events):
        with open(os.path.join(self.state, "journal.jsonl"), "a") as f:
            for seq, ts, type, entity, data in events:
                f.write(json.dumps({"seq": seq, "ts": ts, "type": type, "entity": entity, "data": data, "src": "t"},
                                   ensure_ascii=False) + "\n")

    def step(self, now):
        self.w.step(now)
        return [ev for path, ev in spool.take(os.path.join(self.state, "spool")) if not os.unlink(path)
                if ev["type"] == "watch.surrender"]

    def long_turn(self, tool, n=watch.WINDOW + 20):
        evs = [(10, 110, "message.received", None, {"to": "a", "text": "are you sure the port is 8080?", **OWNER}),
               (11, 111, "turn.started", "a", {"marker": "m10", "prompt_head": "are you sure the port is 8080?"})]
        evs += [(12 + i, 112, "turn.tool", "a", {"id": f"t{i}", **tool}) for i in range(n)]
        evs += [(12 + n, 120, "turn.result", "a", {"text": "You're right, changed it to 9090."}),
                (13 + n, 121, "turn.stopped", "a", {})]
        self.write(evs)

    def test_a_surrender_after_a_long_turn_without_checks_is_caught(self):
        self.long_turn({"name": "Edit", "input": {"file_path": "/w/conf.yml"}})
        (ev,) = self.step(125)
        self.assertEqual((ev["entity"], ev["data"]["message"]), ("a", "m10"))

    def test_a_check_at_the_start_of_a_long_turn_still_counts(self):
        self.write([(10, 110, "message.received", None, {"to": "a", "text": "are you sure the port is 8080?", **OWNER}),
                    (11, 111, "turn.started", "a", {"marker": "m10", "prompt_head": "are you sure the port is 8080?"}),
                    (12, 112, "turn.tool", "a", {"id": "r", "name": "Read", "input": {"file_path": "/w/conf.yml"}})])
        self.write([(13 + i, 113, "turn.tool", "a", {"id": f"t{i}", "name": "Edit", "input": {"file_path": "/w/c"}})
                    for i in range(watch.WINDOW + 20)])
        n = 13 + watch.WINDOW + 20
        self.write([(n, 120, "turn.result", "a", {"text": "You're right, changed it to 9090."}), (n + 1, 121, "turn.stopped", "a", {})])
        self.assertEqual(self.step(125), [])


if __name__ == "__main__":
    unittest.main()
