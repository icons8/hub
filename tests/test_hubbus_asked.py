"""An incoming item the liaison asked the owner about waits for the owner, not for the liaison: no «the liaison has not
decided in 30 minutes» while the question is open. The liaison asked by a form in its turn on that one item; the core
told the owner anyway that it had not decided (a question about a server another hub was closing). Once the owner
answers, the item is the liaison's again and the 30 minutes count from the answer."""
import tests  # noqa: F401 — keeps the machine's settings out
import json
import os
import unittest

from tests.test_hubbus import NOW, need_lib, need_ssh
from tests.test_hubbus_done import thanked
from tests import test_hubbus_s1 as base
from core import busd, busstate, liaison

Q = {"questions": [{"question": "Their hub asks where to take the codes for the old server. Answer them?",
                    "header": "Codes", "multiSelect": False,
                    "options": [{"label": "Yes", "description": "tell them"}, {"label": "No", "description": "decline"}]}]}


def notices(h):
    with open(os.path.join(h.h.state_dir, "journal.jsonl"), encoding="utf-8") as f:
        return [e for e in map(json.loads, f) if e["type"] == "owner.notify" and (e["data"] or {}).get("source") == "bus"]


def liaison_turn(a, *items):
    """The liaison's turn takes the messages of these items (the first one is its mark, the rest came with it)."""
    marks = [a.bus["inbox"][b]["message"] for b in items]
    a.rec("turn.started", {"marker": marks[0], "also": marks[1:]}, entity=liaison.NAME)


def form(a, rid="r-codes"):
    a.rec("permission.requested", {"request_id": rid, "tool": "AskUserQuestion", "interactive": True, "input": Q},
          entity=liaison.NAME)
    return rid


def tick(a, at):
    a.now = at
    with a.env():
        busd.tick(a.h)


def answer(a, rid, at):
    a.now = at
    q = Q["questions"][0]["question"]
    a.rec("permission.answered", {"request_id": rid, "behavior": "allow", "answers": {q: "Yes"},
                                  "channel": "web", "owner": True}, entity=liaison.NAME)


@need_lib
@need_ssh
class AskedOwner(unittest.TestCase):
    def test_a_form_on_one_item_holds_the_mark_while_the_question_is_open(self):
        a, _, _, bid = thanked()
        liaison_turn(a, bid)
        form(a)
        tick(a, NOW + busstate.STALE + 1)
        self.assertFalse(a.bus["inbox"][bid].get("stale"))
        self.assertEqual(notices(a), [])
        self.assertIsNone(a.bus["inbox"][bid]["outcome"])   # still the liaison's to close: done, pass, an answer

    def test_after_the_answer_the_thirty_minutes_count_from_it(self):
        a, _, _, bid = thanked()
        liaison_turn(a, bid)
        rid = form(a)
        answer(a, rid, NOW + busstate.STALE + 5)
        tick(a, NOW + busstate.STALE + 10)              # long after the item came, a moment after the answer
        self.assertFalse(a.bus["inbox"][bid].get("stale"))
        tick(a, NOW + 2 * busstate.STALE + 10)          # the liaison did nothing with the answer for 30 minutes
        self.assertTrue(a.bus["inbox"][bid].get("stale"))
        self.assertEqual(len(notices(a)), 1)

    def test_a_typed_answer_counts_too(self):
        # the owner answers in words, not by the card's buttons: the core takes them as the answer to the one open
        # question, and the 30 minutes count from them
        a, _, _, bid = thanked()
        liaison_turn(a, bid)
        form(a)
        a.now = NOW + busstate.STALE + 5
        a.rec("message.received", {"to": liaison.NAME, "text": "Yes, tell them", "channel": "web", "owner": True})
        self.assertEqual([q for q in a.st["sessions"][liaison.NAME].get("asks") or [] if not q.get("hubbus")], [])
        tick(a, NOW + busstate.STALE + 10)
        self.assertFalse(a.bus["inbox"][bid].get("stale"))
        tick(a, NOW + 2 * busstate.STALE + 10)
        self.assertTrue(a.bus["inbox"][bid].get("stale"))

    def test_a_second_question_counts_from_its_own_answer(self):
        a, _, _, bid = thanked()
        liaison_turn(a, bid)
        answer(a, form(a, "r-1"), NOW + 60)             # the first answer, by the card
        liaison_turn(a, bid)
        form(a, "r-2")
        a.now = NOW + 3 * busstate.STALE
        a.rec("message.received", {"to": liaison.NAME, "text": "Yes", "channel": "web", "owner": True})   # in words
        tick(a, NOW + 3 * busstate.STALE + 10)
        self.assertFalse(a.bus["inbox"][bid].get("stale"))

    def test_the_liaison_still_closes_the_item_after_asking(self):
        a, _, _, bid = thanked()
        liaison_turn(a, bid)
        rid = form(a)
        answer(a, rid, NOW + 60)
        _, verdict = a.as_liaison("bus.done_requested", {"inbox": bid})
        self.assertIsNone(verdict)
        self.assertEqual(a.bus["inbox"][bid]["outcome"], "read")

    def test_a_form_in_a_turn_on_two_items_marks_neither(self):
        a, b, wire, bid = thanked()
        _, other = base.sent_through(b, a, wire, "and one more thing", session="maps",
                                     contact=next(iter(b.bus["contacts"])))
        liaison_turn(a, bid, other)
        form(a)
        tick(a, NOW + busstate.STALE + 1)
        self.assertTrue(a.bus["inbox"][bid].get("stale"))     # which one the question is about is unknown
        self.assertTrue(a.bus["inbox"][other].get("stale"))

    def test_a_form_of_another_session_marks_nothing(self):
        a, _, _, bid = thanked()
        a.up("api")
        a.rec("turn.started", {"marker": a.bus["inbox"][bid]["message"]}, entity=liaison.NAME)
        a.rec("permission.requested", {"request_id": "r-api", "tool": "AskUserQuestion", "interactive": True,
                                       "input": Q}, entity="api")
        tick(a, NOW + busstate.STALE + 1)
        self.assertTrue(a.bus["inbox"][bid].get("stale"))


if __name__ == "__main__":
    unittest.main()
