"""The "you are back" summary showed "Done: nothing", while overnight one session brought v1 to prod, another
redeployed, and a third checked the chat four times (owner report 2026-10-03). "Done" was built only from answers to
delegations, but the work went on without delegations. Now it is the last result of each working session during the
absence."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from core import owner, reducer

OWNER = {"channel": "web", "owner": True}


class AwaySummary(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        for n, kind in (("hub", "hub"), ("pe", None), ("seo", None)):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", kind=kind, **OWNER)
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def result(self, name, text):
        self.ev("turn.started", name, marker=None)
        self.ev("turn.result", name, text=text)
        self.ev("turn.stopped", name)

    def test_work_without_tasks_is_in_done(self):
        self.result("pe", "before leaving, does not count")
        self.ev("owner.away", None, **OWNER)
        self.result("pe", "Slice 12 is in review")
        self.result("pe", "v1 is ready and works in prod")
        self.result("seo", "Chat re-read, nothing new")
        self.result("seo", "—")                                 # a silent result does not count
        self.result("hub", "Noted that you are away")             # the hub itself talks to the owner: not in "done"
        self.ev("owner.back", None, **OWNER)
        done = {d["session"]: d["text"] for d in self.st["owner"]["summary"]["done"]}
        self.assertEqual(done, {"pe": "v1 is ready and works in prod", "seo": "Chat re-read, nothing new"})

    def test_a_handover_result_is_in_done_without_the_core_mark(self):
        # HANDOFF-DONE is for the core (it puts the session to sleep): the return summary lists the work after it
        self.ev("owner.away", None, **OWNER)
        self.result("pe", "HANDOFF-DONE\nDone: v1 is in prod")
        self.ev("owner.back", None, **OWNER)
        self.assertEqual([d["text"] for d in self.st["owner"]["summary"]["done"]], ["Done: v1 is in prod"])

    def test_the_owner_writing_brings_him_back_and_sessions_do_not(self):
        self.ev("owner.away", None, **OWNER)
        self.ev("message.received", None, to="pe", text="from a session", channel="session", owner=False, sender="seo")
        self.ev("message.received", None, to="pe", text="from the hub on his word", channel="cli", owner=True)
        self.assertEqual(self.st["owner"]["state"], "away")
        self.ev("message.received", None, to="pe", text="I am here", channel="telegram", owner=True)
        self.assertEqual(self.st["owner"]["state"], "present")
        self.assertEqual(self.st["owner"]["summary"]["seq"], self.seq)
        self.ev("owner.away", None, **OWNER)
        self.ev("message.received", None, text="from the page", **OWNER)
        self.assertEqual(self.st["owner"]["state"], "present")

    def test_hours_out_of_range_are_refused_by_the_core(self):
        # hub away --hours nan|inf|-1 went straight into the state: the owner's timer got a NaN or infinite deadline,
        # or one already past; the page and Telegram allow only 0 < hours <= 720, and the core refuses the same way
        # from any channel
        for bad in (float("nan"), float("inf"), -1, 721, "2", True):
            with self.subTest(bad):
                self.assertIsNotNone(self.ev("owner.away", None, hours=bad, **OWNER))
                self.assertEqual((self.st["owner"]["state"], self.st["owner"].get("until")), ("present", None))
        self.assertIsNone(self.ev("owner.away", None, hours=720, **OWNER))
        self.assertEqual(self.st["owner"]["until"], 100 + self.seq + 720 * 3600)
        self.ev("owner.back", None, **OWNER)
        self.assertIsNone(self.ev("owner.away", None, hours=None, **OWNER))   # no hours: until he returns
        self.assertIsNone(self.st["owner"]["until"])

    def test_a_repeated_away_extends_the_absence_and_keeps_what_piled_up(self):
        # "away" while already away (the page, Telegram /away or `hub away` again, e.g. to set a new period) cleared
        # the decisions, results and deferred questions gathered since he left: the summary on return lost them
        self.ev("owner.away", None, hours=2, **OWNER)
        since = self.st["owner"]["away_since"]
        self.ev("owner.decided", None, text="We paint it blue", channel="session", owner=False, sender="pe")
        self.result("seo", "Chat re-read, nothing new")
        self.st["owner"]["deferred"].append({"session": "pe", "text": "Which option?", "ts": 100 + self.seq})
        self.assertIsNone(self.ev("owner.away", None, hours=5, **OWNER))
        o = self.st["owner"]
        self.assertEqual((o["state"], o["away_since"], o["until"]), ("away", since, 100 + self.seq + 5 * 3600))
        self.assertIsNone(self.ev("owner.away", None, hours=None, **OWNER))   # no hours now: until he returns
        o = self.st["owner"]
        self.assertEqual((o["away_since"], o["until"]), (since, None))
        self.ev("owner.back", None, **OWNER)
        sm = self.st["owner"]["summary"]
        self.assertEqual(sm["from"], since)
        self.assertEqual([d["text"] for d in sm["decided"]], ["We paint it blue"])
        self.assertEqual([d["text"] for d in sm["done"]], ["Chat re-read, nothing new"])
        self.assertEqual([w["text"] for w in sm["waiting"]], ["Which option?"])

    def test_the_page_sees_the_away_mode_and_the_saved_questions(self):
        from core import views
        r = views.Reader()
        r.feed({"seq": 1, "ts": 100, "type": "owner.away", "entity": None, "data": OWNER})
        r.state["owner"]["deferred"].append({"session": "pe", "text": "Paint it?", "ts": 101})
        o = r.snapshot()["owner"]
        self.assertEqual((o["state"], o["away_since"], o["deferred"]), ("away", 100, 1))


ASK = {"questions": [{"question": "Which option?", "options": [{"label": "A"}, {"label": "B"}]}]}


class AwayQuestionIsACard(unittest.TestCase):
    """After "away" there were no questions (owner report 2026-10-21): a form question during the absence was rejected by
    the core as "saved" and only its text went into the summary; there was no card with buttons, neither during nor
    after, so it could not be answered. Now the question is the same card as when the owner is present: while he is away,
    no push and no Telegram; on return it is open with a deadline counted from the return, and the summary links to it."""

    def setUp(self):
        self.st, self.seq, self.now, self.actions = reducer.empty(), 0, 100, []
        for n, kind in (("hub", "hub"), ("pe", None), ("seo", None)):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", kind=kind, **OWNER)
            self.ev("session.started", n)

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.now = ts or self.now + 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.now, "type": type,
                                                      "entity": entity, "data": data})
        self.actions = acts
        return why

    def ask(self, name="pe", rid="r1"):
        self.ev("turn.started", name, marker=None)
        self.ev("permission.requested", name, request_id=rid, tool="AskUserQuestion", input=ASK, interactive=True)
        return f"q{self.seq}"

    def test_question_while_away_is_an_open_card_after_return(self):
        self.ev("owner.away", None, **OWNER)
        qid = self.ask()
        asked = self.now
        deny = next(a for a in self.actions if a["do"] == "respond")
        self.assertEqual(deny["response"]["behavior"], "deny")
        self.assertIn(qid, deny["response"]["message"])   # the session gets the card number: the answer comes as a message
        self.ev("turn.stopped", "pe")
        self.ev("owner.back", None, ts=100 + 30 * 3600, **OWNER)   # past the card's deadline: the deadline counts from the return
        o, sess = self.st["owner"], self.st["sessions"]["pe"]
        self.assertEqual([(a["id"], a["request_id"]) for a in owner.open_asks(sess, 100 + 30 * 3600)], [(qid, "r1")])
        self.assertEqual((sess["asks"][0]["since"], sess["asks"][0]["back_at"]), (asked, 100 + 30 * 3600))   # remembers when it was asked
        self.assertEqual(o["summary"]["waiting"], [{"session": "pe", "text": "Which option?", "card": qid}])
        self.assertEqual(o["deferred"], [])
        self.ev("permission.answered", "pe", request_id="r1", behavior="allow", answers={"Which option?": "B"},
                **OWNER)
        self.assertEqual(self.st["sessions"]["pe"]["asks"], [])
        msg = next(m for m in self.st["messages"].values() if m.get("answers_question") == qid)
        self.assertEqual((msg["session"], msg["text"]), ("pe", f"The user's answer to question {qid}:\n— Which option?: B"))

    def test_while_away_the_card_does_not_ping_and_does_not_expire(self):
        self.ev("owner.away", None, **OWNER)
        self.ask()
        later = 100 + 40 * 3600
        self.assertEqual(len(owner.open_asks(self.st["sessions"]["pe"], later)), 1)
        self.assertEqual([a for a in owner.alerts(self.st, later) if a["kind"] == "question"], [])
        self.assertEqual(owner.pending(self.st), [])
        from core import views
        r = views.Reader()
        r.state = self.st
        self.assertEqual(r.snapshot()["owner"]["deferred"], 1)   # "away, questions: 1" on the page
        self.ev("owner.back", None, ts=later, **OWNER)
        self.assertEqual([a["card"] for a in owner.alerts(self.st, later) if a["kind"] == "question"],
                         [self.st["sessions"]["pe"]["asks"][0]["id"]])

    def test_the_session_that_left_or_withdrew_has_no_card(self):
        self.ev("owner.away", None, **OWNER)
        self.ask("pe", "r1")
        q2 = self.ask("seo", "r2")
        self.ask("hub", "r3")
        self.st["sessions"]["pe"]["state"] = "closed"   # closed while he was away (how does not matter: the rule goes by state)
        self.ev("session.remove_requested", "pe", **OWNER)
        self.st["sessions"]["hub"]["state"] = "failed"   # failed for good
        self.ev("question.withdrawn", "seo", id=q2, why="settled", channel="session", owner=False, sender="seo")
        self.ev("owner.back", None, **OWNER)
        self.assertEqual([a for s in self.st["sessions"].values() for a in s.get("asks") or []], [])
        # no card, but what the departed sessions asked is in the summary as text; what the session itself withdrew is nowhere
        self.assertEqual([(w["session"], "card" in w) for w in self.st["owner"]["summary"]["waiting"]],
                         [("hub", False), ("pe", False)])

    def test_the_word_that_brings_him_back_does_not_answer_an_unseen_card(self):
        # review: the owner's first word after "away" ("I'm back") used to resolve a card asked without him as its answer
        self.ev("owner.away", None, **OWNER)
        qid = self.ask()
        self.ev("turn.stopped", "pe")
        self.ev("message.received", None, to="pe", text="I am back, what is here", channel="telegram", owner=True)
        self.assertEqual(self.st["owner"]["state"], "present")
        self.assertEqual([a["id"] for a in self.st["sessions"]["pe"]["asks"]], [qid])
        self.ev("message.received", None, to="pe", text="B", channel="web", owner=True)   # he has seen it now: an answer
        self.assertEqual(self.st["sessions"]["pe"]["asks"], [])
        self.assertTrue(any(m.get("answers_question") == qid for m in self.st["messages"].values()))

    def test_answered_on_the_page_while_away_is_not_stale(self):
        self.ev("owner.away", None, **OWNER)
        qid = self.ask()
        self.ev("permission.answered", "pe", ts=self.now + 30 * 3600, request_id="r1", behavior="allow",
                answers={"Which option?": "A"}, **OWNER)
        msg = next(m for m in self.st["messages"].values() if m.get("answers_question") == qid)
        self.assertNotIn("stale", msg["text"])

    def test_owner_present_cards_are_untouched_by_return(self):
        qid = self.ask()
        since = self.st["sessions"]["pe"]["asks"][0]["since"]
        self.ev("owner.away", None, **OWNER)
        self.ev("owner.back", None, **OWNER)
        self.assertEqual([(a["id"], a["since"]) for a in self.st["sessions"]["pe"]["asks"]], [(qid, since)])


if __name__ == "__main__":
    unittest.main()
