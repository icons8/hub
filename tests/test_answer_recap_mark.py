"""The core's retelling of an owner's action on a card is a core row, not the owner's words.

When the owner answers a question card, closes it or withdraws it, the core writes the session a message in his name
("The owner's answer to question qN: …"). Its feed row came out as an in row from the owner (sender :owner, from.kind
owner): a client that draws rows from the owner as his own bubbles showed the core's template as if the owner had typed
it. The row is now from the core and says on whose behalf it was written (on_behalf: owner); the answer itself stays a
single "answer" row."""
import unittest

from core import forms, views

OWNER = {"channel": "web", "owner": True}
ASK = {"questions": [{"question": "Which one?", "options": [{"label": "A"}, {"label": "B"}]}]}


def feed(tail):
    raw = [("session.requested", {"session_id": "s", "cwd": "/w", **OWNER}),
           ("session.started", {"session_id": "s"}),
           ("turn.started", {"marker": None}),
           ("permission.requested", {"request_id": "r1", "tool": "AskUserQuestion", "input": ASK, "interactive": True}),
           *tail]
    r = views.Reader()
    for seq, (type, data) in enumerate(raw, 1):
        r.feed({"seq": seq, "ts": 100 + seq, "src": "web", "entity": "pe", "type": type, "data": data})
    return r, len(raw)


class RetellingIsACoreRow(unittest.TestCase):
    def rows(self, tail):
        r, last = feed(tail)
        tl = r.timelines["pe"]
        return [i for i in tl if i["seq"] == last], r

    def assert_core(self, row):
        self.assertEqual(row["kind"], "in")
        self.assertEqual(row["sender"], views.CORE_SENDER)
        self.assertEqual(row["from"], {"kind": "core"})
        self.assertEqual(row.get("on_behalf"), "owner")
        self.assertEqual(row.get("answer_to"), "q4")

    def test_answer_retelling_is_from_the_core_and_the_answer_is_shown_once(self):
        rows, r = self.rows([("permission.answered", {"request_id": "r1", "behavior": "allow",
                                                      "answers": {"Which one?": "B"}, **OWNER})])
        self.assertEqual(sorted(i["kind"] for i in rows), ["answer", "in"])
        self.assert_core(next(i for i in rows if i["kind"] == "in"))
        # no row of this card reads as the owner's own words
        owner_words = [i for i in r.timelines["pe"] if i["kind"] == "in" and (i.get("from") or {}).get("kind") == "owner"]
        self.assertEqual(owner_words, [])

    def test_a_dismissed_card_note_is_from_the_core(self):
        rows, _ = self.rows([("question.dismissed", {"request_id": "r1", **OWNER})])
        self.assert_core(next(i for i in rows if i["kind"] == "in"))

    def test_a_card_withdrawn_by_the_owner_note_is_from_the_core(self):
        rows, _ = self.rows([("question.withdrawn", {"id": "q4", **OWNER})])
        row = next(i for i in rows if i["kind"] == "in")
        self.assertEqual((row["sender"], row["from"], row.get("on_behalf")), (views.CORE_SENDER, {"kind": "core"}, "owner"))

    def test_the_reducer_marks_the_notes_it_writes_in_the_owner_s_name(self):
        # the mark is set where the note is written, not guessed from its sender later: the archives' readers (_sent) see it
        for tail in ([("permission.answered", {"request_id": "r1", "behavior": "allow", "answers": {"Which one?": "B"},
                                               **OWNER})],
                     [("question.dismissed", {"request_id": "r1", **OWNER})],
                     [("question.withdrawn", {"id": "q4", **OWNER})]):
            with self.subTest(tail[0][0]):
                r, last = feed(tail)
                self.assertEqual(r.state["messages"][f"m{last}"].get("on_behalf"), "owner")

    def test_a_note_written_before_the_mark_keeps_its_old_row(self):
        # a message of the owner's that the core wrote before the reducer marked such notes: no on_behalf is guessed
        msg = {"id": "m9", "session": "pe", "text": "x", "origin": forms.ORIGIN_OWNER, "from": {"kind": "owner"},
               "from_owner": True, "answers_question": "q4"}
        row = views.core_row(msg, 9, 109, {})
        self.assertEqual((row["sender"], row["from"], row.get("answer_to")), (views.OWNER_SENDER, {"kind": "owner"}, "q4"))
        self.assertNotIn("on_behalf", row)

    def test_other_core_rows_keep_their_sender(self):
        # a note from the hub stays from the hub, without on_behalf
        msg = {"id": "m9", "session": "pe", "text": "x", "origin": forms.ORIGIN_HUB}
        row = views.core_row(msg, 9, 109, {})
        self.assertEqual((row["sender"], row["from"]), ("hub", {"kind": "hub"}))
        self.assertNotIn("on_behalf", row)


if __name__ == "__main__":
    unittest.main()
