"""The record of a closed card in the session's `answered` says how it was closed: how (answered | dismissed | withdrawn),
the card's id (qN, none for a permission form), ts, by (who: "owner" or the name of the session / the hub that closed it)
and why (the words a withdrawal came with). Before this the owner's × left only a hash, and a card a session or the hub
withdrew left nothing: for a client the card on the screen just vanished. The record keeps the 16 latest per session."""
import tests  # noqa: F401 - ignore the machine's settings
import unittest

from core import owner, reducer, views

OWNER = {"channel": "web", "owner": True}
Q = {"questions": [{"question": "Which?", "options": [{"label": "A"}, {"label": "B"}]}]}


class Closed(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.events = reducer.empty(), 0, []
        for name, kind in (("hub", "hub"), ("maps", None)):
            self.ev("session.requested", name, cwd=f"/home/x/{name}", session_id=f"s-{name}",
                    **({"kind": kind} if kind else {}), **OWNER)
            self.ev("session.started", name)

    def ev(self, kind, entity=None, /, **data):
        self.seq += 1
        e = {"seq": self.seq, "ts": 1000 + self.seq, "type": kind, "entity": entity, "data": data}
        self.events.append(e)
        self.st, _, why = reducer.reduce(self.st, e)
        return why

    def ask(self, rid):
        self.ev("turn.started", "maps", marker=None, prompt_head="x")
        self.ev("permission.requested", "maps", request_id=rid, tool="AskUserQuestion", interactive=True, input=Q)
        self.ev("turn.result", "maps", text="asked")
        self.ev("turn.stopped", "maps")
        return self.st["sessions"]["maps"]["asks"][-1]["id"]

    def box(self):
        return self.st["sessions"]["maps"].get("answered", [])

    def test_the_owners_answer(self):
        q = self.ask("r1")
        self.assertIsNone(self.ev("permission.answered", "maps", request_id="r1", behavior="allow",
                                  answers={"Which?": "B"}, client_id="c-1", **OWNER))
        (rec,) = self.box()
        self.assertEqual({k: rec[k] for k in ("request_id", "how", "id", "by", "why", "ts", "client_id")},
                         {"request_id": "r1", "how": "answered", "id": q, "by": "owner", "why": None,
                          "ts": rec["ts"], "client_id": "c-1"})
        self.assertEqual((rec["seq"], rec["sig"]), (self.seq, owner.answer_sig("allow", {"Which?": "B"})))

    def test_the_owners_cross(self):
        q = self.ask("r1")
        self.assertIsNone(self.ev("question.dismissed", "maps", request_id="r1", **OWNER))
        (rec,) = self.box()
        self.assertEqual((rec["how"], rec["id"], rec["by"], rec["why"]), ("dismissed", q, "owner", None))
        self.assertEqual(rec["sig"], owner.answer_sig("dismissed"))   # what the page matches a repeat by is as before

    def test_a_deny_is_still_an_answer(self):
        self.ask("r1")
        self.ev("permission.answered", "maps", request_id="r1", behavior="deny", **OWNER)
        self.assertEqual(self.box()[0]["how"], "answered")

    def test_a_permission_form_has_no_card_id(self):
        self.ev("turn.started", "maps", marker=None, prompt_head="x")
        self.ev("permission.requested", "maps", request_id="p1", tool="Bash", input={"command": "ls"})
        self.ev("permission.answered", "maps", request_id="p1", behavior="allow", **OWNER)
        (rec,) = self.box()
        self.assertEqual((rec["how"], rec["id"], rec["by"]), ("answered", None, "owner"))

    def test_a_session_withdraws_its_own_card(self):
        q = self.ask("r1")
        self.assertIsNone(self.ev("question.withdrawn", "maps", id=q, why="settled by itself", sender="maps",
                                  channel="session", owner=False))
        (rec,) = self.box()
        self.assertEqual((rec["request_id"], rec["how"], rec["id"], rec["by"], rec["why"]),
                         ("r1", "withdrawn", q, "maps", "settled by itself"))
        self.assertEqual(rec["seq"], self.seq)
        self.assertGreater(rec["ts"], 0)
        self.assertEqual(self.st["sessions"]["maps"]["asks"], [])

    def test_the_hub_and_the_owner_withdraw_a_card_too(self):
        a, b = self.ask("r1"), self.ask("r2")
        self.assertIsNone(self.ev("question.withdrawn", "maps", id=a, why="same as q1", sender="hub",
                                  channel="session", owner=False))
        self.assertIsNone(self.ev("question.withdrawn", "maps", id=b, **OWNER))
        first, second = self.box()
        self.assertEqual((first["how"], first["by"], first["why"]), ("withdrawn", "hub", "same as q1"))
        self.assertEqual((second["how"], second["by"], second["why"]), ("withdrawn", "owner", None))

    def test_a_withdrawal_that_is_refused_writes_nothing(self):
        q = self.ask("r1")
        self.assertIsNotNone(self.ev("question.withdrawn", "maps", id=q, sender="other", channel="session", owner=False))
        self.assertEqual(self.box(), [])
        self.assertEqual(len(self.st["sessions"]["maps"]["asks"]), 1)

    def test_a_withdrawal_takes_no_client_id_and_an_answer_takes_no_why(self):
        # a reader that matches a repeat by client_id must never take a withdrawal for the client's own answer
        a, b = self.ask("r1"), self.ask("r2")
        self.ev("question.withdrawn", "maps", id=a, client_id="c-9", sender="maps", channel="session", owner=False)
        self.ev("permission.answered", "maps", request_id="r2", behavior="allow", answers={"Which?": "A"},
                client_id="c-9", why="x", **OWNER)
        first, second = self.box()
        self.assertNotIn("client_id", first)
        self.assertIsNone(first["sig"])
        self.assertEqual((second["client_id"], second["why"]), ("c-9", None))

    def test_a_long_why_is_cut(self):
        q = self.ask("r1")
        self.ev("question.withdrawn", "maps", id=q, why="x" * 5000, sender="maps", channel="session", owner=False)
        self.assertLessEqual(len(self.box()[0]["why"]), 300)

    def test_sixteen_latest_stay(self):
        for i in range(20):
            q = self.ask(f"r{i}")
            self.ev("question.withdrawn", "maps", id=q, sender="maps", channel="session", owner=False)
        self.assertEqual([r["request_id"] for r in self.box()], [f"r{i}" for i in range(4, 20)])

    def test_an_old_record_without_the_new_fields_is_kept_and_read(self):
        self.ask("r0")
        self.st["sessions"]["maps"]["answered"] = [{"request_id": "old", "seq": 3, "sig": "abc"}]   # the state of an older core
        self.ev("question.dismissed", "maps", request_id="r0", **OWNER)
        old, new = self.box()
        self.assertEqual(old, {"request_id": "old", "seq": 3, "sig": "abc"})
        self.assertEqual(new["how"], "dismissed")

    def test_the_snapshot_has_them(self):
        q = self.ask("r1")
        self.ev("question.withdrawn", "maps", id=q, why="gone", sender="maps", channel="session", owner=False)
        r = views.Reader()
        r.feed_all(self.events)
        (row,) = [s for s in r.snapshot()["sessions"] if s["name"] == "maps"]
        (rec,) = row["answered"]
        self.assertEqual((rec["how"], rec["id"], rec["by"], rec["why"]), ("withdrawn", q, "maps", "gone"))


if __name__ == "__main__":
    unittest.main()
