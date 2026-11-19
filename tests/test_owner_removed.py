"""A removed session has no open questions to the owner: no timers, no reminders (its tool request stayed
"needs-owner" in the state, and the owner was reminded of a request that cannot be answered any more)."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from core import owner

REQ = {"tool": "x", "state": "needs-owner", "since": 1000.0, "why": ""}
CARD = {"request_id": "r1", "tool": "AskUserQuestion", "since": 1000.0, "input": {"questions": [{"question": "Which?"}]}}


def state(session_state, **extra):
    s = {"state": session_state, "asks": [], "pending": [], "requests": [], **extra}
    return {"owner": owner.initial(), "sessions": {"a": s}}


class RemovedSession(unittest.TestCase):
    def test_a_tool_request_of_a_removed_session_is_not_a_question(self):
        st = state("removed", requests=[dict(REQ)])
        self.assertEqual(owner.questions(st), [])
        self.assertEqual(owner.pending(st), [])
        self.assertIsNone(owner.remind(st, {"entity": "q:a:tool:x", "data": {"n": 1}}))
        self.assertEqual(st["owner"]["reminders"], {})

    def test_the_cards_of_a_removed_session_are_not_questions_either(self):
        st = state("removed", pending=[dict(CARD)], asks=[{**CARD, "request_id": "r2"}])
        self.assertEqual(owner.questions(st, 1001.0), [])
        self.assertEqual(owner.pending(st), [])

    def test_a_live_session_keeps_its_questions(self):
        st = state("idle", requests=[dict(REQ)], pending=[dict(CARD)])
        self.assertEqual({q[0] for q in owner.questions(st)}, {"q:a:tool:x", "q:a:r1"})
        self.assertEqual(len(owner.pending(st)), 2)
        self.assertIsNotNone(owner.remind(st, {"entity": "q:a:tool:x", "data": {"n": 1}}))


if __name__ == "__main__":
    unittest.main()
