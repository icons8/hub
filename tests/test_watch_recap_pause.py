"""The "what it is working on" line when the model fails: claude -p hit a limit — the watcher does not call it again
on every pass (every 2 s for each busy session), but waits with growing intervals; if the limit reset is known — until
then. A journal replay."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import tempfile
import unittest

from core import spool
from modules.watch import watch
from tests.test_watch import OWNER, base, journal

LIMIT = "claude -p: You've hit your limit · resets 3pm (Europe/Paris)"


class Recap:
    def __init__(self):
        self.calls, self.error = [], LIMIT

    def __call__(self, text):
        self.calls.append(text)
        if self.error:
            raise ValueError(self.error)
        return "fixes a test"


class Pause(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="watch-recap-")
        journal(self.state, base())
        self.recap, self.at = Recap(), []   # calls of the line model and their time
        self.w = watch.Watcher(self.state, {}, ask=lambda t: {"trouble": False, "kind": "ok"}, needs=None,
                               urgent=None, recap=self.recap)

    def run_passes(self, start, end):
        """Watcher passes every EVERY seconds from start to end; what it wrote to the box."""
        t = start
        while t < end:
            n = len(self.recap.calls)
            self.w.step(t)
            self.at += [t] * (len(self.recap.calls) - n)
            t += watch.EVERY
        box = os.path.join(self.state, "spool")
        return [ev for path, ev in spool.take(box) if not os.unlink(path)]

    def test_a_failing_model_is_called_with_growing_pauses_not_every_pass(self):
        self.run_passes(115, 115 + 3600)   # an hour: 1800 passes
        n = len(self.recap.calls)
        self.assertGreaterEqual(n, 2)
        self.assertLessEqual(n, 10, "the model is called on every pass")
        # pauses grow: each next one is no shorter than the previous
        gaps = [b - a for a, b in zip(self.at, self.at[1:])]
        self.assertEqual(gaps, sorted(gaps))
        self.assertGreater(gaps[-1], gaps[0])

    def test_after_the_model_answers_again_the_line_comes_and_the_pause_is_gone(self):
        self.run_passes(115, 400)
        self.recap.error = None
        got = self.run_passes(400, 400 + watch.RECAP_PAUSE_MAX + 10)
        self.assertEqual([(e["type"], e["data"]["text"]) for e in got if e["type"] == "session.recap"],
                         [("session.recap", "fixes a test")])
        self.assertEqual(self.w.recap_pause, {})
        journal(self.state, [(6, 2400, "turn.result", "a", {"text": "done"}),
                             (7, 2410, "turn.started", "a", {"marker": None, "prompt_head": "more"})])
        n = len(self.recap.calls)
        self.run_passes(2411, 2414)   # a new turn — the line at once, no pause
        self.assertEqual(len(self.recap.calls), n + 1)

    def test_a_known_reset_of_the_limit_is_waited_for(self):
        # the account refused until the window reset (core limits): do not call until the reset, right after — again
        journal(self.state, [(6, 112, "account.limits", None, {
            "status": "rejected", "type": "five_hour", "windows": {"five_hour": {"used": 1.0, "resets": 5000}},
            **OWNER})])
        self.run_passes(115, 2990)
        self.assertEqual(len(self.recap.calls), 1)
        # a new turn before the reset — waits too; a permission card mid-turn does not lift the pause
        journal(self.state, [(7, 3000, "turn.result", "a", {"text": "done"}),
                             (8, 3010, "turn.started", "a", {"marker": None, "prompt_head": "more"}),
                             (9, 3020, "permission.requested", "a", {"request_id": "r1", "tool": "Bash",
                                                                     "input": {"command": "ls"}})])
        self.run_passes(2990, 3025)
        self.assertEqual(self.w.state["sessions"]["a"]["state"], "dialog")
        journal(self.state, [(10, 3030, "permission.answered", "a", {"request_id": "r1", "behavior": "allow", **OWNER})])
        self.run_passes(3031, 4990)
        self.assertEqual(self.w.state["sessions"]["a"]["state"], "busy")
        self.assertEqual(len(self.recap.calls), 1)
        self.recap.error = None
        got = self.run_passes(4990, 5010)
        self.assertEqual(len(self.recap.calls), 2)
        self.assertEqual([e["data"]["text"] for e in got if e["type"] == "session.recap"], ["fixes a test"])

    def test_another_account_of_the_pool_is_tried_at_once(self):
        journal(self.state, [(6, 112, "account.limits", None, {
            "status": "rejected", "type": "five_hour", "windows": {"five_hour": {"used": 1.0, "resets": 50000}},
            **OWNER})])
        self.addCleanup(watch.POOL.update, {"active": None})
        self.run_passes(115, 200)
        self.assertEqual(len(self.recap.calls), 1)
        # the core moved the pool to another account — the old one's limit does not hold it
        journal(self.state, [(7, 200, "account.switched", None, {"to": "second", "channel": "core"})])
        self.recap.error = None
        self.run_passes(201, 205)
        self.assertEqual(len(self.recap.calls), 2)

    def test_the_wait_for_the_reset_ends_when_the_account_is_let_in_again(self):
        journal(self.state, [(6, 112, "account.limits", None, {
            "status": "rejected", "type": "five_hour", "windows": {"five_hour": {"used": 1.0, "resets": 50000}},
            **OWNER})])
        self.run_passes(115, 200)
        self.assertEqual(len(self.recap.calls), 1)
        journal(self.state, [(7, 200, "account.limits", None, {
            "status": "allowed", "type": "five_hour", "windows": {"five_hour": {"used": 0.2, "resets": 50000}},
            **OWNER})])
        self.recap.error = None
        self.run_passes(201, 205)
        self.assertEqual(len(self.recap.calls), 2)

    def test_a_failure_of_one_session_does_not_pause_the_others(self):
        # not a limit but a failure at this session's input — the pause is only for it; a limit — for all
        journal(self.state, [(6, 120, "session.requested", "b", {"cwd": "/b", "session_id": "s-b", **OWNER}),
                             (7, 120, "session.started", "b", {}),
                             (8, 121, "turn.started", "b", {"marker": None, "prompt_head": "build"})])
        self.recap.error = None
        bad = self.recap

        def recap(text):
            if "fix the test" in text:
                bad.calls.append(text)
                raise KeyError("now")
            return "builds"
        self.w.recap = recap
        got = self.run_passes(122, 130)
        self.assertEqual([e["entity"] for e in got if e["type"] == "session.recap"], ["b"])
        self.assertEqual(len(bad.calls), 1)
        self.assertEqual(list(self.w.recap_pause), ["a"])


class Turn(unittest.TestCase):
    """The "what it is working on" line is only about the running turn: a new turn clears it, and a late line from the
    previous turn (the next one started while the model was thinking) is not accepted. The page, plugins and Telegram
    read one core field."""

    def fold(self, events):
        from core import reducer
        st, why = reducer.empty(), []
        for i, (kind, ent, data) in enumerate(events, 1):
            st, _, w = reducer.reduce(st, {"seq": i, "ts": 100 + i, "type": kind, "entity": ent, "data": data})
            why.append(w)
        return st["sessions"]["a"], why

    START = [("session.requested", "a", {"cwd": "/w", "session_id": "s-a", **OWNER}),
             ("session.started", "a", {}), ("turn.started", "a", {"marker": None}),                  # turn 3
             ("session.recap", "a", {"text": "fixes a test", "by": "watch", "turn": 3, **OWNER}),
             ("turn.result", "a", {"text": "done"})]

    def test_a_new_turn_drops_the_line_of_the_last_one(self):
        sess, _ = self.fold(self.START)
        self.assertEqual(sess["recap"]["text"], "fixes a test")   # free: the previous turn's line stays (the Project view)
        sess, _ = self.fold(self.START + [("turn.started", "a", {"marker": None})])
        self.assertNotIn("recap", sess)

    def test_a_late_line_of_the_last_turn_is_refused_and_a_line_after_an_insert_is_taken(self):
        sess, why = self.fold(self.START + [("turn.started", "a", {"marker": None}),                 # turn 6
                                            ("session.recap", "a", {"text": "old", "by": "watch", "turn": 3,
                                                                    **OWNER})])
        self.assertIn("past turn", why[-1])
        self.assertNotIn("recap", sess)
        sess, why = self.fold(self.START + [("turn.started", "a", {"marker": None}),
                                            ("turn.started", "a", {"marker": None}),   # insertion mid-turn 6
                                            ("session.recap", "a", {"text": "new", "by": "watch", "turn": 7,
                                                                    **OWNER})])
        self.assertIsNone(why[-1])
        self.assertEqual(sess["recap"]["text"], "new")


if __name__ == "__main__":
    unittest.main()
