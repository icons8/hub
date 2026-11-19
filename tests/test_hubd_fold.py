"""hubd folds a fact in place, without a deep copy of the whole state per event (2026-10-22: on a hub with 1500 removed
sessions the copy took ~60 ms of ~62 per event, session commands waited seconds for a verdict). A rejection or a fold
failure is rolled back with a spare state: the live one after a rejection is as before the event, as with a copy."""
import tests  # noqa: F401
import json
import tempfile
import types
import unittest
from unittest import mock

from core import journal, reducer
from core.daemon import Hub

OWN = {"channel": "cli", "owner": True}


def removed(i):
    return {"name": f"old-{i}", "state": "removed", "cwd": f"/home/x/old-{i}", "session_id": f"s-old-{i}",
            "tools": ["a", "b"], "requests": [{"id": f"r{i}", "tool": "Bash"}], "info": {"model": "m"}}


class FoldInPlace(unittest.TestCase):
    def hub(self, gone=0):
        h = Hub(tempfile.mkdtemp(prefix="fold-"), types.SimpleNamespace())
        h.execute = lambda a: None
        for i in range(gone):
            h.state["sessions"][f"old-{i}"] = removed(i)
        return h

    def copies(self, h):
        """A counter of deep copies of the whole state (a dict with sessions and seq) — that is what the fold cost."""
        n = [0]
        real = reducer.copy_state

        def counting(o):
            if type(o) is dict and "sessions" in o and "seq" in o:
                n[0] += 1
            return real(o)
        return n, mock.patch("core.reducer.copy_state", counting)

    def next_line(self, h, ev):
        return next(e for e in journal.read(h.journal.path) if e["seq"] == ev["seq"] + 1)

    def reference(self, state, ev):
        """How hubd folded before 2026-10-22: a copy per event, a rejection — the previous state with the event number."""
        new, actions, why = reducer.reduce(state, ev)
        return new, why

    def test_accepted_events_do_not_copy_the_whole_state(self):
        h = self.hub(gone=1500)
        h.record("session.requested", "maps", {"cwd": "/home/x/m", "session_id": "s-m", **OWN})   # the spare is here
        n, patch = self.copies(h)
        with patch:
            for i in range(20):
                h.record("session.requested", f"maps-{i}", {"cwd": "/home/x/m", "session_id": f"s-{i}", **OWN})
        self.assertEqual(n[0], 0, "folding an accepted fact copies the whole state")
        self.assertEqual(len([s for s in h.state["sessions"].values() if s["state"] != "removed"]), 21)

    def test_state_after_each_event_is_the_same_as_with_a_copy(self):
        h = self.hub(gone=5)
        ref = reducer.copy_state(h.state)
        steps = [("session.requested", "maps", {"cwd": "/home/x/m", "session_id": "s-m", **OWN}),
                 ("session.started", "nobody", {}),                                   # rejection: no such session
                 ("session.started", "maps", {}),
                 ("session.requested", "maps", {"cwd": "/home/x/m", "session_id": "s-2", **OWN}),   # rejection: name taken
                 ("message.received", None, {"to": "maps", "text": "hello", **OWN}),
                 ("session.renamed", "maps", {"to": "maps-2", **OWN})]
        for type_, entity, data in steps:
            ev = h.record(type_, entity, data)
            ref, why = self.reference(ref, ev)
            if why:
                ref, _ = self.reference(ref, self.next_line(h, ev))
            self.assertEqual(h.state, ref, f"after {type_}")

    def partial(self, err):
        """A fold that managed to change the state and then failed (a rejection or a code failure) — on session maps."""
        real = reducer._apply

        def apply(st, ev, actions):
            if ev["type"] == "session.started" and ev.get("entity") == "maps":
                st["sessions"]["maps"]["state"] = "busy"
                st["sessions"]["maps"]["tools"].append("junk")
                st["junk"] = 1
                raise err
            return real(st, ev, actions)
        return mock.patch("core.reducer._apply", apply)

    def check_rolls_back(self, err):
        h = self.hub(gone=3)
        h.record("session.requested", "maps", {"cwd": "/home/x/m", "session_id": "s-m", "tools": ["t"], **OWN})
        before = reducer.copy_state(h.state)
        with self.partial(err):
            ev = h.record("session.started", "maps", {})
            rej = self.next_line(h, ev)
            want, _ = self.reference(before, ev)
            want, _ = self.reference(want, rej)
        self.assertEqual(rej["type"], "core.rejected")
        self.assertEqual(h.state, want)
        self.assertEqual(json.dumps(h.state, ensure_ascii=False), json.dumps(want, ensure_ascii=False))   # and the key order
        self.assertNotIn("junk", h.state)
        self.assertNotIn("junk", h.state["sessions"]["maps"].get("tools") or [])
        h.record("session.started", "maps", {})   # the next one folds on the whole state
        self.assertEqual(h.state["sessions"]["maps"]["state"], "idle")

    def test_a_rejection_after_a_partial_change_leaves_the_state_as_before(self):
        self.check_rolls_back(reducer.Rejected("late refusal"))

    def test_a_fold_failure_after_a_partial_change_leaves_the_state_as_before(self):
        self.check_rolls_back(KeyError("x"))

    def test_a_burst_of_rejections_costs_a_copy_each_and_no_journal_reread(self):
        h = self.hub(gone=100)
        h.record("session.requested", "maps", {"cwd": "/home/x/m", "session_id": "s-m", **OWN})
        n, patch = self.copies(h)
        with patch, mock.patch("core.journal.load_state", side_effect=AssertionError("journal re-read")):
            for _ in range(10):
                h.record("session.started", "nobody", {})
        self.assertLessEqual(n[0], 10)
        self.assertEqual(sorted(h.state["sessions"])[-1], "old-99")

    def test_a_state_replaced_outside_the_fold_is_what_a_rejection_rolls_back_to(self):
        # corewrite (h.state = new), rotation and start (load_state) replace the whole state — rollback goes to the replaced one
        h = self.hub()
        h.record("session.requested", "maps", {"cwd": "/home/x/m", "session_id": "s-m", **OWN})
        new = reducer.copy_state(h.state)
        new["sessions"]["other"] = {**new["sessions"]["maps"], "name": "other"}
        h.state = new
        h.record("session.started", "nobody", {})
        self.assertIn("other", h.state["sessions"])

    def test_actions_do_not_change_with_later_events(self):
        # a deferred action (self.delayed) waits while other events go on: editing the state in place does not change it
        h = self.hub()
        got = []
        h.execute = got.append
        h.record("session.requested", "maps", {"cwd": "/home/x/m", "session_id": "s-m", "tools": ["t"], **OWN})
        spawn = next(a for a in got if a["do"] == "spawn")
        frozen = reducer.copy_state(spawn)
        h.record("session.started", "maps", {})
        h.record("tools.granted", "maps", {"tool": "t2", **OWN})
        self.assertEqual(spawn, frozen)


if __name__ == "__main__":
    unittest.main()
