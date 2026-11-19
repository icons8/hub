"""Quote of the owner's message on the page (2026-10-25): "the session replied to your message" with a quote shows
only for the first result of a task; later results of the same task (interim results of a mini-hub, session wake-ups)
are a line of the same session without the quote, with a short note on which task. As in Telegram
(modules/telegram/bot.py first_reply)."""
import tests  # noqa: F401
import unittest

from core import reducer, views

OWNER = {"channel": "web", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}


class ReplyQuoteOnce(unittest.TestCase):
    def setUp(self):
        self.reader, self.seq = views.Reader(), 0
        for n, kind in (("hub", "hub"), ("w", None), ("v", None)):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", kind=kind, **OWNER)
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.reader.feed({"seq": self.seq, "ts": 100 + self.seq, "type": type, "entity": entity, "data": data})
        return self.seq

    def task(self, to, words, text="do it"):
        m = self.ev("message.received", None, to="hub", text=words, **OWNER)
        d = self.ev("delegation.created", None, session=to, words=words, context=text, **{"for": f"m{m}"}, **HUB)
        return m, d

    def turn(self, name, marker, text):
        self.ev("turn.started", name, **({"marker": f"m{marker}"} if marker else {}))
        r = self.ev("turn.result", name, text=text)
        self.ev("turn.stopped", name)
        return r

    def replies(self):
        return [i for i in self.reader.timelines["hub"] if i["kind"] == "reply"]

    def test_only_the_first_result_of_a_task_quotes_the_message(self):
        _, d = self.task("w", "fix the site map")
        self.turn("w", d, "done: MR !31")
        self.turn("w", None, "more: checked prod")      # the session was woken by background work — same turn of the task
        first, again = self.replies()
        self.assertEqual((first["quote"], first["text"]), ("fix the site map", "done: MR !31"))
        self.assertNotIn("again", first)
        self.assertNotIn("quote", again)
        self.assertEqual((again["again"], again["from"], again["text"], again["task"]),
                         (True, "w", "more: checked prod", "fix the site map"))

    def test_each_task_has_its_own_first_reply(self):
        _, d1 = self.task("w", "first")
        _, d2 = self.task("v", "second")
        self.turn("w", d1, "answer w")
        self.turn("v", d2, "answer v")
        self.assertEqual([i.get("quote") for i in self.replies()], ["first", "second"])

    def test_the_task_label_is_short(self):
        _, d = self.task("w", "very long owner message text. " * 8)
        self.turn("w", d, "one")
        self.turn("w", None, "two")
        again = self.replies()[1]
        self.assertLessEqual(len(again["task"]), 31)
        self.assertTrue(again["task"].endswith("…"))

    def test_a_silent_result_is_not_a_reply(self):
        _, d = self.task("w", "check the map")
        self.turn("w", d, "—")                             # quiet check result: the page does not show it
        self.turn("w", None, "found: no /blog")
        (shown,) = [i for i in self.replies() if i["text"] != "—"]
        self.assertEqual(shown["quote"], "check the map")
        self.assertNotIn("again", shown)

    def test_a_second_task_for_the_same_message_does_not_quote_it_again(self):
        # as in Telegram (first_reply): the quote goes on the session's first reply to this message, not on each
        # of its tasks for it
        m, d1 = self.task("w", "check prod")
        self.turn("w", d1, "first check")
        d2 = self.ev("delegation.created", None, session="w", words="check prod", context="once more", channel="session",
                     owner=False, sender="hub", **{"for": f"m{m}"})
        self.turn("w", d2, "second check")
        first, second = self.replies()
        self.assertEqual((first.get("quote"), first.get("again")), ("check prod", None))
        self.assertEqual((second.get("quote"), second.get("again")), (None, True))

    def test_the_reducer_counts_the_results_of_a_task(self):
        st, seq = reducer.empty(), 0
        def ev(type, entity=None, **data):
            nonlocal st, seq
            seq += 1
            st, _, _ = reducer.reduce(st, {"seq": seq, "ts": 100 + seq, "type": type, "entity": entity, "data": data})
            return seq
        for n, kind in (("hub", "hub"), ("w", None)):
            ev("session.requested", n, cwd="/w", session_id=f"s-{n}", kind=kind, **OWNER)
            ev("session.started", n)
        m = ev("message.received", None, to="hub", text="x", **OWNER)
        d = ev("delegation.created", None, session="w", **{"for": f"m{m}"}, **HUB)
        for marker, text in ((d, "one"), (None, "two")):
            ev("turn.started", "w", **({"marker": f"m{marker}"} if marker else {}))
            ev("turn.result", "w", text=text)
            ev("turn.stopped", "w")
        self.assertEqual(st["delegations"][f"d{d}"]["replies"], 2)


if __name__ == "__main__":
    unittest.main()
