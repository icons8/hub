"""While the owner is away the hub answers sessions' cards for them, except their own decisions (owner 17.11: "in away
every question except the critical ones the hub should do, right?", card "Decide more in away"; spec d2549241). A card a
session left open at the end of its turn goes to the hub (AWAY_CARD); the cards open when he leaves go in one message
(AWAY_OPEN). The hub answers with `hub answer <session> --card qN … --why`; the core refuses it while the owner is here,
on cards that are his whatever the hub thinks (🚨, budget, the bus, the liaison, the hub's own) and without a why. The
hub's answer is never the owner's word: the session cannot pass a gate with it. On return the summary has its own
section, and `hub decided undo` withdraws an answer."""
import tests  # noqa: F401 — keeps the machine's settings out
import os
import unittest

from core import journal, owner, reducer, tools
from tests.test_cli_edges import Base as CliBase

OWNER = {"channel": "web", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}
ASK = {"questions": [{"question": "Which file name: tiles.toml or tiles_config.toml?",
                      "options": [{"label": "tiles.toml (Recommended)", "description": "short"},
                                  {"label": "tiles_config.toml"}]}]}
URGENT = {"questions": [{"question": "🚨 prod is down: roll the release back?", "options": [{"label": "Yes"}]}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.now, self.actions = reducer.empty(), 0, 100, []
        for n, kind in (("hub", "hub"), ("maps", None), ("seo", None)):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", kind=kind, **OWNER)
            self.ev("session.started", n)

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.now = ts or self.now + 1
        self.st, self.actions, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.now, "type": type,
                                                              "entity": entity, "data": data})
        return why

    def ask(self, name="maps", rid="r1", form=ASK):
        self.ev("turn.started", name, marker=None)
        self.ev("permission.requested", name, request_id=rid, tool="AskUserQuestion", input=form, interactive=True)
        return f"q{self.seq}"

    def end(self, name="maps"):
        self.ev("turn.result", name, text="done for now")
        self.ev("turn.stopped", name)

    def to_hub(self):
        return [m for m in self.st["messages"].values() if m["session"] == "hub" and m.get("origin") == " from core"]

    def answer(self, qid, name="maps", rid="r1", why="short, like the files next to it · rename the file", **who):
        return self.ev("permission.answered", name, request_id=rid, behavior="allow", card=qid,
                       answers={ASK["questions"][0]["question"]: "tiles.toml"}, **({"why": why} if why else {}),
                       **(who or HUB))


class CardGoesToTheHub(Base):
    def test_a_card_left_open_at_the_end_of_the_turn_goes_to_the_hub_once(self):
        self.ev("owner.away", None, **OWNER)
        qid = self.ask()
        self.assertEqual(self.to_hub(), [])   # not while the session may still decide it itself
        self.end()
        (msg,) = self.to_hub()
        self.assertIn(qid, msg["text"])
        self.assertIn(f"hub answer maps --card {qid}", msg["text"])
        self.assertIn("tiles_config.toml", msg["text"])   # the options are in it: the hub decides without reading
        self.ev("turn.result", "maps", text="again")   # a second result of the same turn: no second message
        self.assertEqual(len(self.to_hub()), 1)

    def test_a_card_the_session_withdrew_in_its_turn_does_not_go(self):
        self.ev("owner.away", None, **OWNER)
        qid = self.ask()
        self.ev("question.withdrawn", "maps", id=qid, why="decided", channel="session", owner=False, sender="maps")
        self.end()
        self.assertEqual(self.to_hub(), [])

    def test_owner_here_nothing_goes(self):
        self.ask()
        self.end()
        self.assertEqual(self.to_hub(), [])

    def test_a_cut_turn_sends_nothing(self):
        self.ev("owner.away", None, **OWNER)
        self.ask()
        self.ev("turn.result", "maps", text="You've hit your limit · resets 3am", is_error=True)   # cut by the account
        self.ev("turn.stopped", "maps")
        self.assertEqual(self.to_hub(), [])   # the safe side: the card waits for the owner

    def test_owner_cards_do_not_go(self):
        self.ev("owner.away", None, **OWNER)
        self.ask("maps", "r1", URGENT)
        self.end("maps")
        self.ask("hub", "r2")
        self.end("hub")
        self.assertEqual(self.to_hub(), [])

    def test_tmux_host_sends_at_the_question(self):
        self.st["host"] = "tmux"   # no turn results there
        self.ev("owner.away", None, **OWNER)
        qid = self.ask()
        (msg,) = self.to_hub()
        self.assertIn(qid, msg["text"])

    def test_cards_open_when_he_leaves_go_in_one_message(self):
        q1 = self.ask("maps", "r1")
        self.end("maps")
        q2 = self.ask("seo", "r2", URGENT)
        self.end("seo")
        self.ev("owner.away", None, **OWNER)
        (msg,) = self.to_hub()
        self.assertIn(q1, msg["text"])
        self.assertNotIn(q2, msg["text"])
        self.ev("owner.away", None, hours=2, **OWNER)   # a new period while away: nothing again
        self.assertEqual(len(self.to_hub()), 1)

    def test_a_long_card_in_the_open_list_is_cut_with_a_mark(self):
        long = {"questions": [{"question": "Which file name? " + "context " * 80,
                               "options": [{"label": "tiles.toml"}, {"label": "tiles_config.toml"}]}]}
        self.ask("maps", "r1", long)
        self.end()
        self.ev("owner.away", None, **OWNER)
        (msg,) = self.to_hub()
        self.assertIn("…", msg["text"])   # the hub sees that the options were cut, not a short list

    def test_the_owner_coming_back_withdraws_a_card_the_hub_has_not_taken(self):
        # the hub was busy: the card stayed in its queue, and the owner came back. It would reach the hub after his
        # return, where its answer is refused (he is here) — one wasted hub turn; and the page shows "Sending" under
        # the hub's feed for good
        self.ev("owner.away", None, **OWNER)
        self.ev("turn.started", "hub", marker=None)
        qid = self.ask()
        self.end()
        (msg,) = self.to_hub()
        self.assertEqual(msg["state"], "queued")
        self.ev("owner.back", None, **OWNER)
        msg = self.st["messages"][msg["id"]]
        self.assertEqual((msg["state"], msg.get("reason")), ("failed", "the owner is back"))
        self.assertEqual([a["id"] for a in self.st["sessions"]["maps"]["asks"]], [qid])   # the card is his now
        self.assertNotIn("to_hub", self.st["sessions"]["maps"]["asks"][0])

    def test_coming_back_leaves_what_the_hub_already_has(self):
        # typed or delivered: it is the hub's already (its refusal on return is the answer to such a message)
        self.ev("owner.away", None, **OWNER)
        self.ask()
        self.end()
        (msg,) = self.to_hub()
        self.assertNotEqual(msg["state"], "queued")
        was = msg["state"]
        self.ev("owner.back", None, **OWNER)
        self.assertEqual(self.st["messages"][msg["id"]]["state"], was)

    def test_coming_back_leaves_the_other_messages_of_the_hub(self):
        self.ev("owner.away", None, **OWNER)
        self.ev("turn.started", "hub", marker=None)
        self.ev("message.received", None, to="hub", text="a task from a session", channel="session", owner=False,
                sender="seo")
        self.ev("owner.back", None, **OWNER)
        (msg,) = [m for m in self.st["messages"].values() if m["session"] == "hub"]
        self.assertEqual(msg["state"], "queued")

    def test_no_open_cards_no_message(self):
        self.ev("owner.away", None, **OWNER)
        self.assertEqual(self.to_hub(), [])


class Urgent(Base):
    def test_an_urgent_card_while_away_reaches_him_at_once(self):
        # owner 17.11, q2551190 "push at once": 🚨 is not held for his return
        self.ev("owner.away", None, **OWNER)
        qid = self.ask("maps", "r1", URGENT)
        self.assertNotIn("away", self.st["sessions"]["maps"]["asks"][0])
        self.assertEqual([a["card"] for a in owner.alerts(self.st, self.now) if a["kind"] == "question"], [qid])
        deny = next(a for a in self.actions if a["do"] == "respond")
        self.assertNotIn("when they are back", deny["response"]["message"])


class HubAnswers(Base):
    def test_the_hub_answers_and_it_is_not_the_owners_word(self):
        self.ev("owner.away", None, **OWNER)
        qid = self.ask()
        self.end()
        self.assertIsNone(self.answer(qid))
        self.assertEqual(self.st["sessions"]["maps"]["asks"], [])
        (msg,) = [m for m in self.st["messages"].values() if m.get("hub_answers") == qid]
        self.assertEqual(msg["session"], "maps")
        self.assertIn("tiles.toml", msg["text"])
        self.assertIn("rename the file", msg["text"])
        for key in ("from_owner", "answers_question", "re", "on_behalf", "card_declined"):
            self.assertNotIn(key, msg)
        self.assertIsNone(tools.card_answer(self.st, qid))   # `--for qN` finds no answer of his
        self.assertFalse(reducer._his_words([msg]))
        self.assertFalse(reducer._heard_it(self.st, "maps", f"as {qid} says"))
        rec = self.st["decisions"][-1]
        self.assertEqual((rec["by"], rec["told"], rec["card"]), ("hub", "maps", qid))
        (d,) = self.st["owner"]["decided"]
        self.assertEqual((d["session"], d["text"], d["ts"], d["hub"], d["asker"]), ("hub", rec["text"], rec["ts"], True,
                                                                                     "maps"))
        self.assertEqual(self.st["sessions"]["maps"]["answered"][-1]["by"], "hub")

    def test_refused_while_the_owner_is_here(self):
        qid = self.ask()
        self.end()
        self.assertIsNotNone(self.answer(qid))
        self.assertEqual([a["id"] for a in self.st["sessions"]["maps"]["asks"]], [qid])

    def test_refused_after_he_came_back(self):
        self.ev("owner.away", None, **OWNER)
        qid = self.ask()
        self.end()
        self.ev("owner.back", None, **OWNER)
        self.assertIsNotNone(self.answer(qid))
        self.assertEqual(len(self.st["sessions"]["maps"]["asks"]), 1)

    def test_refused_without_why_and_from_others(self):
        self.ev("owner.away", None, **OWNER)
        qid = self.ask()
        self.end()
        self.assertIsNotNone(self.answer(qid, why=""))
        self.assertIsNotNone(self.answer(qid, channel="session", owner=False, sender="seo"))
        self.assertIsNotNone(self.answer(qid, channel="plugin", owner=False, sender="mod:x"))
        self.assertEqual(len(self.st["sessions"]["maps"]["asks"]), 1)

    def test_refused_on_the_owners_cards(self):
        self.ev("owner.away", None, **OWNER)
        q1 = self.ask("maps", "r1", URGENT)
        self.end("maps")
        self.assertIsNotNone(self.answer(q1))
        q2 = self.ask("hub", "r2")
        self.end("hub")
        self.assertIsNotNone(self.answer(q2, name="hub", rid="r2"))
        self.st["sessions"]["seo"]["liaison"] = True
        self.st["sessions"]["liaison"] = {**self.st["sessions"].pop("seo"), "name": "liaison"}
        q3 = self.ask("liaison", "r3")
        self.end("liaison")
        self.assertIsNotNone(self.answer(q3, name="liaison", rid="r3"))
        self.assertEqual(sum(len(s.get("asks") or []) for s in self.st["sessions"].values()), 3)

    def test_the_hub_line_goes_only_with_a_card_the_hub_may_answer(self):
        self.ev("owner.away", None, **OWNER)
        self.ask("maps", "r1")
        told = next(a for a in self.actions if a["do"] == "respond")["response"]["message"]
        self.assertIn("the hub looks at", told)
        self.ask("hub", "r2")
        told = next(a for a in self.actions if a["do"] == "respond")["response"]["message"]
        self.assertNotIn("the hub looks at", told)
        self.st["sessions"]["seo"]["liaison"] = True   # the liaison's card is the owner's: the hub never gets it
        self.st["sessions"]["liaison"] = {**self.st["sessions"].pop("seo"), "name": "liaison"}
        self.ask("liaison", "r3")
        told = next(a for a in self.actions if a["do"] == "respond")["response"]["message"]
        self.assertNotIn("the hub looks at", told)

    def test_a_closed_sessions_card_stays_for_the_summary(self):
        qid = self.ask()
        self.end()
        self.st["sessions"]["maps"]["state"] = "closed"   # closed with the card open
        self.ev("owner.away", None, **OWNER)
        self.assertEqual(self.to_hub(), [])
        self.assertIsNotNone(self.answer(qid))
        self.ev("owner.back", None, **OWNER)
        self.assertEqual([w["session"] for w in self.st["owner"]["summary"]["waiting"]], ["maps"])
        self.assertNotIn("hub", self.st["owner"]["summary"])

    def test_the_owner_still_answers_as_before(self):
        self.ev("owner.away", None, **OWNER)
        qid = self.ask()
        self.end()
        self.assertIsNone(self.answer(qid, why="", **OWNER))
        self.assertIsNotNone(tools.card_answer(self.st, qid))

    def test_the_hub_cannot_pass_the_card_off_as_his_words(self):
        self.ev("message.received", None, to="hub", text="good night", **OWNER)
        word = f"m{self.seq}"
        self.ev("owner.away", None, **OWNER)
        qid = self.ask()
        self.end()
        self.assertIsNotNone(self.ev("message.received", None, to="maps", text="tiles.toml", card=qid, **{"for": word},
                                     **HUB))
        self.assertEqual(len(self.st["sessions"]["maps"]["asks"]), 1)


class Timeline(Base):
    def test_the_hubs_answer_is_marked_as_the_hubs(self):
        from core import views
        self.ev("owner.away", None, **OWNER)
        qid = self.ask()
        self.end()
        self.answer(qid)
        ev = {"seq": self.seq, "ts": self.now, "type": "permission.answered", "entity": "maps",
              "data": {"request_id": "r1", "behavior": "allow", "why": "x", **HUB}}
        (row,) = [it for _, it in views._items(self.st, ev)]
        self.assertEqual(row["channel"], "hub")


class Return(Base):
    def test_the_summary_has_the_hubs_answers_apart(self):
        self.ev("owner.away", None, **OWNER)
        self.ev("owner.decided", None, text="named the branch fix-tiles", channel="session", owner=False,
                sender="seo")
        qid = self.ask()
        self.end()
        self.answer(qid)
        self.ev("owner.back", None, **OWNER)
        sm = self.st["owner"]["summary"]
        self.assertEqual([d["text"] for d in sm["decided"]], ["named the branch fix-tiles"])
        (row,) = sm["hub"]
        self.assertEqual(row["session"], "maps")
        self.assertIn(qid, row["text"])
        self.assertIn("tiles.toml", row["text"])

    def test_undo_takes_it_out_and_tells_the_session(self):
        self.ev("owner.away", None, **OWNER)
        qid = self.ask()
        self.end()
        self.answer(qid)
        seq = self.st["decisions"][-1]["seq"]
        self.assertIsNone(self.ev("owner.decided_revoked", None, seq=seq, **OWNER))
        self.assertEqual(self.st["owner"]["decided"], [])
        last = [m for m in self.st["messages"].values() if m["session"] == "maps"][-1]
        self.assertIn(qid, last["text"])
        self.assertIn("withdrawn", last["text"])
        self.assertIn("ask it once with the form", last["text"])   # no word comes — the session does not stall


class Cli(CliBase):
    """`hub answer <session> --card qN <answer> --why …`: the hub names the card and the why; the flags are not answers."""

    def setUp(self):
        super().setUp()
        self.cli = self
        j = self.j
        j.append("session.requested", "hub", {"cwd": self.state, "session_id": "s-hub", "kind": "hub", **OWNER})
        j.append("session.started", "hub", {"session_id": "s-hub"})
        j.append("owner.away", None, OWNER)
        j.append("turn.started", "a", {"marker": None})
        j.append("permission.requested", "a", {"request_id": "r1", "tool": "AskUserQuestion", "input": ASK,
                                               "interactive": True})
        self.qid = f"q{journal.read(os.path.join(self.state, 'journal.jsonl'))[-1]['seq']}"

    def test_the_hub_names_the_card_and_the_why(self):
        r = self.cli.hub("answer", "a", "--card", self.qid, "tiles.toml", "--why", "short · rename", me="hub")
        self.assertEqual(r.returncode, 0, r.stderr)
        (ev,) = [e for e in self.cli.spooled() if e["type"] == "permission.answered"]
        self.assertEqual((ev["entity"], ev["data"]["request_id"], ev["data"]["card"], ev["data"]["why"]),
                         ("a", "r1", self.qid, "short · rename"))
        self.assertEqual(ev["data"]["answers"], {ASK["questions"][0]["question"]: "tiles.toml"})

    def test_the_hub_without_card_or_why_is_refused(self):
        self.cli.refused(self.cli.hub("answer", "a", "tiles.toml", "--why", "x", me="hub"))
        self.cli.refused(self.cli.hub("answer", "a", "--card", self.qid, "tiles.toml", me="hub"))
        self.cli.refused(self.cli.hub("answer", "a", "--card", "q1", "tiles.toml", "--why", "x", me="hub"))


if __name__ == "__main__":
    unittest.main()
