"""A session's question is shown by the hub to the owner in its own chat as the same card (`hub question show <id>`),
not asked again (2026-10-12: the hub asked the session's question with a second form: the owner had two cards for one
question and closed the extra one himself). There is one card: visible in the session's chat and in the hub's chat;
counters, Telegram, pushes and clients see one question; an answer from any place goes to the session, and the card
goes away everywhere."""
import tests  # noqa: F401 — hide the machine's settings
import unittest

from core import owner, reducer, views

OWNER = {"channel": "web", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}
Q = {"questions": [{"question": "Which tiles to use?", "options": [{"label": "Raster"}, {"label": "Vector"}]}]}


class Shown(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "maps", cwd="/m", session_id="s-m", **OWNER)
        self.ev("session.started", "hub")
        self.ev("session.started", "maps")
        self.ev("turn.started", "maps", marker=None, prompt_head="x")
        self.ev("permission.requested", "maps", request_id="r1", tool="AskUserQuestion", interactive=True, input=Q)
        self.qid = self.st["sessions"]["maps"]["asks"][0]["id"]

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def snap(self, name):
        r = views.Reader()
        r.state = self.st
        return next(s for s in r.snapshot(now=1000 + self.seq)["sessions"] if s["name"] == name)

    def test_the_card_is_shown_in_the_hub_chat_and_stays_one_question(self):
        self.assertIsNone(self.ev("question.shown", "maps", id=self.qid, **HUB))
        hub = self.snap("hub")
        (card,) = hub["shown"]
        self.assertEqual((card["request_id"], card["session"]), ("r1", "maps"))   # an answer from the card goes to maps
        self.assertEqual(hub["pending"], [])        # counters, pushes, clients go by pending: not doubled
        self.assertEqual([p["request_id"] for p in self.snap("maps")["pending"]], ["r1"])
        self.assertEqual(len(owner.questions(self.st)), 1)                         # Telegram, reminders: one

    def test_an_answer_from_either_chat_closes_it_everywhere_and_goes_to_the_session(self):
        self.ev("question.shown", "maps", id=self.qid, **HUB)
        self.ev("permission.answered", "maps", request_id="r1", behavior="allow",
                answers={"Which tiles to use?": "Vector"}, **OWNER)
        self.assertEqual(self.snap("hub")["shown"], [])
        self.assertEqual(self.snap("maps")["pending"], [])
        (ans,) = [m for m in self.st["messages"].values() if m.get("answers_question") == self.qid]
        self.assertEqual(ans["session"], "maps")

    def test_the_hub_relays_the_owners_words_and_the_card_closes_in_both_chats(self):
        self.ev("question.shown", "maps", id=self.qid, **HUB)
        self.ev("message.received", None, to="hub", text="vector, of course", **OWNER)
        mid = f"m{self.seq}"
        self.assertIn(self.qid, self.st["messages"][mid].get("cards") or "")   # the hub sees what the words may refer to
        self.ev("message.received", None, to="maps", text="vector", card=self.qid, **{"for": mid}, **HUB)
        self.assertEqual(self.snap("hub")["shown"], [])
        self.assertEqual(self.snap("maps")["pending"], [])

    def test_the_snapshot_reads_each_sessions_cards_once(self):
        # a snapshot per event: the cards shown by the hub in one pass over sessions, not by going through all questions
        # of all the others for every session (N^2)
        from unittest import mock
        for i in range(20):
            self.ev("session.requested", f"w{i}", cwd="/w", session_id=f"s-w{i}", **OWNER)
        self.ev("question.shown", "maps", id=self.qid, **HUB)
        r = views.Reader()
        r.state = self.st
        with mock.patch.object(owner, "open_asks", wraps=owner.open_asks) as reads:
            snap = r.snapshot(now=1000 + self.seq)
        self.assertLessEqual(reads.call_count, 2 * len(self.st["sessions"]))
        shown = {s["name"]: [(c["request_id"], c["session"]) for c in s["shown"]] for s in snap["sessions"]}
        self.assertEqual(shown, {**{n: [] for n in self.st["sessions"]}, "hub": [("r1", "maps")]})

    def test_only_a_hub_shows_and_only_an_open_card(self):
        self.assertIsNotNone(self.ev("question.shown", "maps", id=self.qid, channel="session", owner=False, sender="maps"))
        self.assertIsNotNone(self.ev("question.shown", "maps", id="q999", **HUB))
        self.assertEqual(self.snap("hub")["shown"], [])

    def test_with_two_hubs_the_card_goes_to_the_one_that_showed_it(self):
        self.ev("session.requested", "hub2", cwd="/h2", session_id="s-h2", kind="hub", **OWNER)
        self.ev("session.started", "hub2")
        self.assertIsNone(self.ev("question.shown", "maps", id=self.qid, channel="session", owner=False, sender="hub2"))
        self.assertEqual([c["request_id"] for c in self.snap("hub2")["shown"]], ["r1"])
        self.assertEqual(self.snap("hub")["shown"], [])

    def test_the_hubs_card_list_offers_show_instead_of_asking_again(self):
        note = owner.cards_note(self.st, "m1", 1000 + self.seq, hub="hub")
        self.assertIn("hub question show <qN>", note)
        self.ev("question.shown", "maps", id=self.qid, **HUB)
        self.assertIn(f"{self.qid} at maps (already in your chat)", owner.cards_note(self.st, "m1", 1000 + self.seq, hub="hub"))

    def test_an_options_preview_reaches_the_card_untouched_through_state_snapshot_and_the_hubs_chat(self):
        # the card shows a variant's sketch or screenshot (`preview` of an AskUserQuestion option): nothing on
        # the way (state, the snapshot, the hub's copy of the question, the scrub of old journals) may cut or mask it
        from core import scrub
        sketch = "┌──┐\n│OK│  token=abc123456789012345678901234567890\n└──┘"
        q = {"questions": [{"question": "Which look?", "options": [
            {"label": "A", "preview": sketch}, {"label": "B", "preview": "/tmp/shot.png"}]}]}
        self.ev("permission.requested", "maps", request_id="r2", tool="AskUserQuestion", interactive=True, input=q)
        qid = [a["id"] for a in self.st["sessions"]["maps"]["asks"] if a["request_id"] == "r2"][0]
        self.ev("question.shown", "maps", id=qid, **HUB)
        want = [o["preview"] for o in q["questions"][0]["options"]]
        got = lambda req: [o.get("preview") for o in req["input"]["questions"][0]["options"]]  # noqa: E731
        self.assertEqual(got(next(p for p in self.snap("maps")["pending"] if p["request_id"] == "r2")), want)
        self.assertEqual(got(next(c for c in self.snap("hub")["shown"] if c["request_id"] == "r2")), want)
        self.assertEqual(got(scrub._request(next(a for a in self.st["sessions"]["maps"]["asks"] if a["id"] == qid))), want)


if __name__ == "__main__":
    unittest.main()
