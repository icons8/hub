"""A card to a deputy through their hub on the bus (spec d2679763, A7), on two hubs of the stand: the core sends the
card in a thread of its own (route = qN), with who asks, on what and why them; the answer in that thread comes back to
the session that asked in the deputy's frame — never the owner's word. Two cards to one deputy — two threads, each
answer to its own card. A message that does not go returns the card to the owner; a hub with a liaison's rule about
it is not asked."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from tests.test_hubbus import need_lib, need_ssh
from tests.test_hubbus_s1 import acquainted

PERSON = {"id": "c0000a0b1", "name": "Pat Lee"}
FORM = {"questions": [{"question": "Which file name: tiles.toml or tiles_config.toml?",
                       "options": [{"label": "tiles.toml (Recommended)", "description": "short"},
                                   {"label": "tiles_config.toml"}]}]}
SESSION = {"channel": "session", "owner": False}
SECRET = "key sk-ant-api03-" + "A1b2C3d4" * 6   # an invented key shape only


@need_lib
@need_ssh
class ThroughTheirHub(unittest.TestCase):
    def setUp(self):
        self.a, self.b, self.wire = acquainted()
        self.name_at_b = next(iter(self.b.bus["contacts"]))
        a = self.a
        a.ask("bus.contact_requested", {"op": "link", "name": "bee", "person": PERSON})
        ev = a.ask("contact.deputy_set", {"person": PERSON, "names": ["Pat"], "zone": "map tiles",
                                          "path": {"kind": "bus", "contact": "bee"}})
        self.assertIsNone(a.verdict(ev))
        a.ask("owner.away", {"deputies": True})
        a.up("maps")
        self.n = 0

    def card(self, form=FORM):
        self.n += 1
        a = self.a
        a.rec("turn.started", {"marker": None}, entity="maps")
        ev = a.rec("permission.requested", {"request_id": f"r{self.n}", "tool": "AskUserQuestion", "input": form,
                                            "interactive": True}, entity="maps")
        return f"q{ev['seq']}"

    def route(self, qid, note=None):
        ev = self.a.rec("question.routed", {"id": qid, "person": "Pat", **({"note": note} if note else {}),
                                            "sender": "maps", **SESSION}, entity="maps")
        return self.a.verdict(ev)

    def ask_of(self, qid):
        return next((x for x in self.a.st["sessions"]["maps"].get("asks") or [] if x["id"] == qid), None)

    def theirs(self, qid):
        """B's inbox item of the card's message."""
        out = self.a.bus["out"][self.ask_of(qid)["deputy"]["out"]]
        return next(x for x in self.b.bus["inbox"].values() if x.get("thread") == out["thread"])

    def b_answers(self, item, text):
        _, why = self.b.as_liaison("bus.send_requested", {"contact": self.name_at_b, "thread": item["thread"],
                                                          "text": text})
        self.assertIsNone(why)
        for c in self.b.cards():
            self.b.answer(c, "send")
        self.wire.settle()

    def replies(self, qid):
        return [m for m in self.a.messages_to("maps") if m.get("hubbus") and f"Reply on {qid}" in m["text"]]

    def test_the_card_goes_in_its_own_thread_and_the_answer_comes_back_as_the_deputys_word(self):
        qid = self.card()
        self.assertIsNone(self.route(qid, note="the folder has tiles.json"))
        out = self.a.bus["out"][self.ask_of(qid)["deputy"]["out"]]
        self.assertEqual((out["route"], out["source"], out["direct"]), (qid, "maps", True))
        self.wire.settle()
        item = self.theirs(qid)
        for part in ("For Pat Lee to decide, not for their hub: please show it to them", "«map tiles»",
                     "From the session maps", "tiles_config.toml", "the folder has tiles.json"):
            self.assertIn(part, item["text"])
        self.assertEqual(self.a.bus["threads"][out["thread"]]["route"], qid)
        self.b_answers(item, "tiles.toml, like the rest")
        (msg,) = self.replies(qid)
        self.assertIn("«tiles.toml, like the rest»", msg["text"])
        self.assertIn("through their hub bee", msg["text"])
        self.assertIn(f"--thread {out['thread']}", msg["text"])
        for k in ("from_owner", "answers_question", "re", "on_behalf"):
            self.assertFalse(msg.get(k), k)
        self.assertEqual(self.ask_of(qid)["deputy"]["state"], "answered")

    def test_two_cards_two_threads_answers_in_reverse_order(self):
        q1 = self.card()
        q2 = self.card({"questions": [{"question": "Cache the tiles for a day?", "options": [{"label": "Yes"}]}]})
        self.route(q1)
        self.route(q2)
        self.wire.settle()
        i1, i2 = self.theirs(q1), self.theirs(q2)
        self.assertNotEqual(i1["thread"], i2["thread"])
        self.b_answers(i2, "yes, a day")
        self.b_answers(i1, "tiles.toml")
        self.assertIn("«yes, a day»", self.replies(q2)[0]["text"])
        self.assertIn("«tiles.toml»", self.replies(q1)[0]["text"])

    def test_a_late_answer_after_the_owner_answered_is_an_ordinary_one(self):
        qid = self.card()
        self.route(qid)
        self.wire.settle()
        item = self.theirs(qid)
        self.a.rec("permission.answered", {"request_id": "r1", "behavior": "allow", "channel": "web", "owner": True,
                                           "answers": {FORM["questions"][0]["question"]: "tiles.toml"}},
                   entity="maps")
        self.b_answers(item, "tiles_config.toml")
        self.assertEqual(self.replies(qid), [])
        self.assertTrue([m for m in self.a.messages_to("maps") if m.get("hubbus") and "tiles_config.toml" in m["text"]])

    def test_a_message_that_does_not_go_returns_the_card(self):
        qid = self.card()
        self.assertIsNone(self.route(qid, note="here " + SECRET))
        dep = self.ask_of(qid)["deputy"]
        self.assertEqual(self.a.bus["out"][dep["out"]]["code"], "secret")
        self.assertEqual(dep["state"], "returned")
        self.assertTrue([m for m in self.a.messages_to("maps") if f"Card {qid} did not reach Pat Lee" in m["text"]])

    def test_a_hub_with_a_liaison_rule_is_not_asked(self):
        ev = self.a.ask("bus.rule_requested", {"op": "add", "contact": "bee", "text": "ask me before writing to bee"})
        self.assertIsNone(self.a.verdict(ev))
        qid = self.card()
        self.assertIn("the liaison has a rule about the hub bee", self.route(qid) or "")
        self.assertIsNone(self.ask_of(qid).get("deputy"))


if __name__ == "__main__":
    unittest.main()
