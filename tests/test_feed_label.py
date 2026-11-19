"""A short feed line instead of the service text of an owner message: a button press and an answer to a card — not the
full «Answer to question …». The message text does not change (delivery and «Send again» go by it)."""
import tests  # noqa: F401
import unittest

from core import views

OWNER = {"channel": "web", "owner": True}


class FeedLabel(unittest.TestCase):
    def test_label_helper(self):
        self.assertEqual(views._feed_label({"action": True, "text": "Install"}), "you pressed “Install”")
        self.assertEqual(views._feed_label({"answers_question": "q1", "answer_body": "yes, option 2",
                                            "text": "Answer to question q1 («…»): yes, option 2"}), "answer: yes, option 2")
        self.assertIsNone(views._feed_label({"text": "an ordinary message"}))
        self.assertIsNone(views._feed_label({"answers_question": "q1", "text": "no body"}))  # no answer_body — leave it alone

    def test_button_press_in_feed_has_label_and_raw_text(self):
        r = views.Reader()
        raw = [("session.requested", "hub", {"session_id": "h", "cwd": "/w", "kind": "hub", **OWNER}),
               ("session.started", "hub", {}),
               ("message.received", None, {"to": "hub", "text": "Install", "action": True, **OWNER})]
        for i, (t, ent, d) in enumerate(raw, 1):
            r.feed({"seq": i, "ts": 100 + i, "src": "test", "type": t, "entity": ent, "data": d})
        it = [x for x in r.snapshot()["timelines"]["hub"] if x["kind"] == "in"][-1]
        self.assertEqual(it["label"], "you pressed “Install”")
        self.assertEqual(it["text"], "Install")   # raw text — for «Send again»

    def test_a_press_that_carries_a_task_shows_the_button_not_the_task(self):
        # "Fix" on the core's own edits: the session gets the whole task, the timeline shows only the pressed button —
        # not the task as if the owner wrote it (11-07)
        task = "The owner pressed \"Fix\" on the red band. Steps: …"
        r = views.Reader()
        raw = [("session.requested", "dev", {"session_id": "d", "cwd": "/w", "kind": "agent", **OWNER}),
               ("session.started", "dev", {}),
               ("message.received", None, {"to": "dev", "text": task, "action": True, "button": "Fix", **OWNER})]
        for i, (t, ent, d) in enumerate(raw, 1):
            r.feed({"seq": i, "ts": 100 + i, "src": "test", "type": t, "entity": ent, "data": d})
        it = [x for x in r.snapshot()["timelines"]["dev"] if x["kind"] == "in"][-1]
        self.assertEqual(it["label"], "you pressed “Fix”")
        self.assertEqual(it["text"], task)   # the session's words are unchanged

    def test_a_button_caption_that_is_not_short_words_falls_back_to_the_text(self):
        for bad in ("", "   ", 5, "x" * 81):
            self.assertEqual(views._feed_label({"action": True, "text": "Install"}, {"button": bad}),
                             "you pressed “Install”", repr(bad))
        # without action the field means nothing
        self.assertIsNone(views._feed_label({"text": "an ordinary one"}, {"button": "Fix"}))


if __name__ == "__main__":
    unittest.main()
