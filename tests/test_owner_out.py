"""owner_out: the "new" badge of a session shows only when its result went to the owner, not on a REPLIED to the
hub or another session (2026-10-14: a client chimed/highlighted "new" on any result, 191 of 210 REPLIEDs in a day
were about tasks between sessions). last_out stays (compatibility); "unread" is computed from owner_out."""
import tests  # noqa: F401
import unittest

from core import reducer

OWNER = {"channel": "web", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}


class OwnerOut(unittest.TestCase):
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

    def out(self, name):
        s = self.st["sessions"][name]
        return s.get("last_out") or 0, s.get("owner_out") or 0

    def test_hub_answering_owner_sets_owner_out(self):
        m = self.ev("message.received", None, to="hub", text="what is there?", **OWNER)
        self.ev("turn.started", "hub", marker=f"m{m}")
        self.ev("turn.result", "hub", text="all is going")
        last, own = self.out("hub")
        self.assertTrue(last and own and last == own)   # a result for the owner sets both last_out and owner_out

    def test_executor_answering_owner_task_sets_owner_out(self):
        tg = self.ev("message.received", None, to="hub", text="fix the map", **OWNER)
        did = self.ev("delegation.created", None, session="seo", **{"for": f"m{tg}"}, **HUB)
        self.ev("turn.started", "seo", marker=f"m{did}")
        self.ev("turn.result", "seo", text="done")
        last, own = self.out("seo")
        self.assertTrue(last and own)                   # a reply to the owner's task — owner_out is set

    def test_reply_to_hub_task_does_not_set_owner_out(self):
        # the hub itself sets the task (for_session=hub); the executor's result is REPLIED to the hub, not the owner
        did = self.ev("delegation.created", None, session="w", for_session="hub", context="collect the log", **HUB)
        self.ev("turn.started", "w", marker=f"m{did}")
        self.ev("turn.result", "w", text="log collected")
        last, own = self.out("w")
        self.assertTrue(last)                           # there was output
        self.assertEqual(own, 0)                        # but not addressed to the owner — no "new" badge

    def test_a_task_injected_into_the_owners_turn_keeps_the_owners_mark(self):
        # a turn on the owner's word, and in the middle the hub urgently (--now) inserted its own task: the "owner's
        # turn" mark must not be replaced by the insertion mark — the result answers the owner too, so the "new"
        # badge is needed
        m = self.ev("message.received", None, to="w", text="what about the map?", **OWNER)
        self.ev("turn.started", "w", marker=f"m{m}")
        did = self.ev("delegation.created", None, session="w", for_session="hub", context="and collect the log too", now=True, **HUB)
        self.assertTrue(self.st["messages"][f"m{did}"].get("injected"))
        self.ev("turn.started", "w", marker=f"m{did}")
        self.ev("turn.result", "w", text="map fixed, log collected")
        last, own = self.out("w")
        self.assertTrue(last and own)

    def test_a_notice_without_marker_mid_turn_keeps_the_owners_mark(self):
        # a background notification in the middle of a turn is a turn.started without a mark; the "owner's turn"
        # mark would stay cleared
        m = self.ev("message.received", None, to="w", text="what about the map?", **OWNER)
        self.ev("turn.started", "w", marker=f"m{m}")
        did = self.ev("delegation.created", None, session="w", for_session="hub", context="and the log", now=True, **HUB)
        self.ev("turn.started", "w", marker=None)
        self.ev("turn.started", "w", marker=f"m{did}")
        self.ev("turn.result", "w", text="done")
        self.assertTrue(self.out("w")[1])

    def test_a_new_chain_mid_turn_does_not_keep_the_owners_mark(self):
        # a hub message not inserted mid-turn starts a new chain (turn_root): a result to the hub does not light
        # "new" for the owner
        m = self.ev("message.received", None, to="w", text="what about the map?", **OWNER)
        self.ev("turn.started", "w", marker=f"m{m}")
        did = self.ev("delegation.created", None, session="w", for_session="hub", context="log", **HUB)
        self.ev("turn.started", "w", marker=f"m{did}")
        self.ev("turn.result", "w", text="log collected")
        self.assertEqual(self.out("w")[1], 0)

    def test_owner_message_taken_in_also_marks_the_turn(self):
        h = self.ev("message.received", None, to="w", text="collect the log", **HUB)
        m = self.ev("message.received", None, to="w", text="and tell me", **OWNER)
        did = self.ev("delegation.created", None, session="w", for_session="hub", context="log", **HUB)
        self.ev("turn.started", "w", marker=f"m{did}", also=[f"m{m}"])
        self.ev("turn.result", "w", text="log collected")
        self.assertTrue(self.out("w")[1])

    def test_unread_in_snapshot_follows_owner_out(self):
        # the same events through views.Reader: "unread" of an executor that replied only to the hub is False;
        # of one that replied to the owner, True
        from core import views
        r = views.Reader()
        raw = [("session.requested", "hub", {"session_id": "h", "cwd": "/w", "kind": "hub", **OWNER}),
               ("session.started", "hub", {}),
               ("session.requested", "w", {"session_id": "sw", "cwd": "/w", **OWNER}),
               ("session.started", "w", {}),
               ("session.requested", "seo", {"session_id": "ss", "cwd": "/w", **OWNER}),
               ("session.started", "seo", {})]
        raw += [("delegation.created", None, {"session": "w", "for_session": "hub", "context": "x", **HUB}),  # seq 7
                ("turn.started", "w", {"marker": "m7"}),
                ("turn.result", "w", {"text": "done for the hub"}),
                ("message.received", None, {"to": "hub", "text": "fix the map", **OWNER}),                   # seq 10
                ("delegation.created", None, {"session": "seo", "for": "m10", **HUB}),                        # seq 11
                ("turn.started", "seo", {"marker": "m11"}),
                ("turn.result", "seo", {"text": "done for the owner"})]
        for i, (t, ent, d) in enumerate(raw, 1):
            r.feed({"seq": i, "ts": 100 + i, "src": "test", "type": t, "entity": ent, "data": d})
        rows = {s["name"]: s for s in r.snapshot()["sessions"]}
        self.assertFalse(rows["w"]["unread"])    # a reply only to the hub is not "new"
        self.assertTrue(rows["seo"]["unread"])   # a reply to the owner is "new"


if __name__ == "__main__":
    unittest.main()
