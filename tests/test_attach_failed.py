"""An attachment that failed to download (Telegram: over 20 MB, a Telegram error, network) is a service fact, not the
owner's words (feed review 2026-10-14: messages to agents looked as if the owner had written them). The bot puts it in
the attach_failed field of the message.received event; text keeps only the owner's words. The session sees the reason
as a note appended to the input — like "Attachments:"; the feed shows it as a row field, the owner's bubble without it."""
import tests  # noqa: F401 — do not see the machine's settings
import unittest

from core import reducer, views

TG = {"channel": "telegram", "owner": True}
FAILED = [{"name": "plan.pdf", "size": 26_500_000, "reason": "plan.pdf, 25.3 MB — over the 20 MB limit for bots"}]


class AttachFailed(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.acts, self.events = reducer.empty(), 0, [], []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", channel="web", owner=True)
        self.ev("session.started", "hub")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        ev = {"seq": self.seq, "ts": 1000 + self.seq, "src": "test", "type": type, "entity": entity, "data": data}
        self.events.append(ev)
        self.st, acts, why = reducer.reduce(self.st, ev)
        self.acts += acts
        return why

    def typed(self, mid):
        return [a for a in self.acts if a["do"] == "type" and a["message"] == mid]

    def test_the_session_reads_the_reason_apart_from_the_owner_words(self):
        self.assertIsNone(self.ev("message.received", None, text="estimate", attachments=[], attach_failed=FAILED, **TG))
        msg = self.st["messages"][f"m{self.seq}"]
        self.assertEqual(msg["text"], "estimate")
        (typed,) = self.typed(msg["id"])
        head, _, note = typed["text"].partition("estimate")
        self.assertIn("Not downloaded from Telegram", note)
        self.assertIn("plan.pdf, 25.3 MB — over the 20 MB limit for bots", note)

    def test_the_feed_keeps_the_owner_bubble_and_the_reason_as_a_field(self):
        self.ev("message.received", None, text="estimate", attachments=[], attach_failed=FAILED, **TG)
        r = views.Reader()
        for ev in self.events:
            r.feed(ev)
        (item,) = [i for i in r.timelines["hub"] if i.get("kind") == "in"]
        self.assertEqual(item["text"], "estimate")
        self.assertEqual(item["attach_failed"], [{"name": "plan.pdf", "size": 26_500_000,
                                                  "reason": "plan.pdf, 25.3 MB — over the 20 MB limit for bots"}])

    def test_malformed_entries_are_dropped_not_fatal(self):
        bad = [{"name": "a.txt", "reason": "a.txt — timeout"}, "string", {"name": 5}, {"reason": None}]
        self.assertIsNone(self.ev("message.received", None, text="here", attach_failed=bad, **TG))
        msg = self.st["messages"][f"m{self.seq}"]
        self.assertEqual(msg["attach_failed"], [{"name": "a.txt", "reason": "a.txt — timeout"}])
        self.assertIsNone(self.ev("message.received", None, text="so", attach_failed="junk", **TG))
        self.assertNotIn("attach_failed", self.st["messages"][f"m{self.seq}"])

    def test_a_plain_message_has_no_note(self):
        self.ev("message.received", None, text="hello", **TG)
        (typed,) = self.typed(f"m{self.seq}")
        self.assertNotIn("Not downloaded", typed["text"])


if __name__ == "__main__":
    unittest.main()
