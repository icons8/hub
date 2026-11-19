"""A task set by the hub itself (for_session=hub): on its result the hub got TWO messages — the result text
("answer to task dN") and a separate REPLIED "from the core" about the same thing (10-14: 9 pairs in a day, 4 of them two
separate hub turns). When the full result already went to the hub in the same turn, a separate REPLIED is not needed. For
another recipient (a different session, the owner) the hub keeps REPLIED — it is its signal about the task."""
import tests  # noqa: F401
import unittest

from core import forms, reducer

OWNER = {"channel": "web", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}


class DedupHub(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        for n, kind in (("hub", "hub"), ("w", None), ("seo", None)):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", kind=kind, **OWNER)
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, _ = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                 "entity": entity, "data": data})
        return self.seq

    def to_hub(self):
        return [m for m in self.st["messages"].values() if m["session"] == "hub"]

    def reply(self, executor, for_session=None, for_owner=None):
        kw = {"session": executor}
        if for_session:
            kw["for_session"] = for_session
        if for_owner:
            kw["for"] = for_owner
        did = self.ev("delegation.created", None, **kw, **HUB)
        self.ev("turn.started", executor, marker=f"m{did}")
        self.ev("turn.result", executor, text="done, a detailed result with a tail of important details")
        return did

    def test_hub_task_gets_one_message_not_two(self):
        before = len(self.to_hub())
        self.reply("w", for_session="hub")
        got = self.to_hub()[before:]
        self.assertEqual(len(got), 1)                                  # one message, not two
        self.assertIn("reply to task", got[0]["origin"])            # this is the full result, not a truncated REPLIED
        self.assertFalse(any(o.get("origin") == forms.ORIGIN_CORE and "REPLIED" in (o.get("text") or "") for o in got))

    def test_task_for_another_session_still_notifies_hub(self):
        hub_before, seo_before = len(self.to_hub()), len([m for m in self.st["messages"].values() if m["session"] == "seo"])
        self.reply("w", for_session="seo")
        seo_now = [m for m in self.st["messages"].values() if m["session"] == "seo"]
        hub_now = self.to_hub()
        self.assertEqual(len(seo_now) - seo_before, 1)                 # the recipient gets the result
        self.assertTrue(any("REPLIED" in (m.get("text") or "") for m in hub_now[hub_before:]))   # the hub gets REPLIED

    def test_owner_task_still_notifies_hub(self):
        tg = self.ev("message.received", None, to="hub", text="fix it", **OWNER)
        hub_before = len(self.to_hub())
        self.reply("seo", for_owner=f"m{tg}")
        self.assertTrue(any("REPLIED" in (m.get("text") or "") for m in self.to_hub()[hub_before:]))


if __name__ == "__main__":
    unittest.main()
