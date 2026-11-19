"""`--for qN` on commands gated by "the owner's word for this action" (tools grant, task run/go; module add is in
tests/test_modapi_sources.py): the owner's answer to any session's card, no older than a day, counts for the hub.
20.10: the owner answered "Connect" on a specialist's card, the hub got a refusal on --for qN and asked him again with
a second card. Forwarding his words (`hub send --for`) is still only a message to the hub."""
import unittest

from core import reducer, tools

OWNER = {"channel": "web", "owner": True}
Q = {"questions": [{"question": "Connect the notes module?", "header": "Module",
                    "options": [{"label": "Connect"}, {"label": "No need"}]}]}


def by(name):
    return {"channel": "session", "sender": name}


class CardWord(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.ts = reducer.empty(), 0, 1000
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "dev", cwd="/d", session_id="s-dev", **OWNER)
        self.ev("session.started", "hub")
        self.ev("session.started", "dev")

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.ts = ts or self.ts + 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.ts, "type": type, "entity": entity,
                                                   "data": data})
        return why

    def card(self, name, rid="r1", q=Q):
        self.ev("turn.started", name, marker=None, prompt_head="x")
        self.ev("permission.requested", name, request_id=rid, tool="AskUserQuestion", interactive=True, input=q)
        self.ev("turn.result", name, text="asked")
        self.ev("turn.stopped", name)
        return self.st["sessions"][name]["asks"][-1]["id"]

    def answer(self, name, rid="r1", q=Q):
        self.assertIsNone(self.ev("permission.answered", name, request_id=rid, behavior="allow",
                                  answers={q["questions"][0]["question"]: "Connect"}, **OWNER))

    def word(self, ref, sender="hub", at=None, **kw):
        data = {"for": ref, **by(sender)}
        return tools.owner_word(self.st, data, at or self.ts, **kw)

    def test_the_hub_cites_the_owners_answer_to_another_sessions_card(self):
        q = self.card("dev")
        self.answer("dev")
        words = self.word(q, cards=True)
        self.assertIn("Connect", words["text"])
        self.assertEqual(self.st["messages"][words["message"]]["answers_question"], q)

    def test_refused_without_his_answer_too_old_from_a_session_or_for_relaying(self):
        q = self.card("dev")
        with self.assertRaises(tools.Rejected):                                 # a card with no answer
            self.word(q, cards=True)
        self.answer("dev")
        with self.assertRaises(tools.Rejected):                                 # older than a day
            self.word(q, at=self.ts + tools.OWNER_WORD_TTL + 1, cards=True)
        with self.assertRaises(tools.Rejected):                                 # not the hub — an answer to another's card
            self.ev("session.requested", "seo", cwd="/s", session_id="s-seo", **OWNER)
            self.word(q, sender="seo", own=True, cards=True)
        with self.assertRaises(tools.Rejected):                                 # forwarding words (hub send --for)
            self.word(q)

    def test_a_session_refused_on_a_gate_is_told_to_ask_the_hub_not_to_repeat_the_command(self):
        # a direction's session cites the owner's answer to its own card in `hub tools grant`: only the hub takes the
        # word there, so the refusal must say who to ask (not leave "mN or qN" to be retried by the same session)
        q = self.card("dev")
        self.answer("dev")
        with self.assertRaises(tools.Rejected) as got:
            self.word(q, sender="dev", cards=True)
        self.assertIn("hub send hub", str(got.exception))
        with self.assertRaises(tools.Rejected) as got:                          # the hub itself: no such hint
            self.word("m999", cards=True)
        self.assertNotIn("hub send hub", str(got.exception))

    def test_a_card_closed_without_an_answer_is_not_his_word(self):
        q = self.card("dev")
        self.assertIsNone(self.ev("question.dismissed", "dev", request_id="r1", **OWNER))
        with self.assertRaises(tools.Rejected):
            self.word(q, cards=True)

    def test_a_card_he_denied_is_not_his_word(self):
        q = self.card("dev")
        self.assertIsNone(self.ev("permission.answered", "dev", request_id="r1", behavior="deny", **OWNER))
        with self.assertRaises(tools.Rejected):
            self.word(q, cards=True)

    def test_his_no_is_not_a_yes_by_its_message_id_but_still_relays(self):
        # "Deny" on the hub's card is a note to the hub from_owner: by mN it is not a "yes" for the gate, but passing his
        # answer on (hub send --for mN) is allowed
        q = self.card("hub")
        self.assertIsNone(self.ev("permission.answered", "hub", request_id="r1", behavior="deny", **OWNER))
        m = next(m["id"] for m in self.st["messages"].values() if m.get("answers_question") == q)
        with self.assertRaises(tools.Rejected):
            self.word(m, cards=True)
        self.assertEqual(self.word(m)["message"], m)

    def test_a_card_closed_before_the_mark_is_not_his_word_either(self):
        # a × close from state.json from before the card_dismissed mark is recognized by the core's text
        q = self.card("dev")
        self.ev("question.dismissed", "dev", request_id="r1", **OWNER)
        for m in self.st["messages"].values():
            m.pop("card_dismissed", None)
        with self.assertRaises(tools.Rejected):
            self.word(q, cards=True)

    def test_an_answer_not_from_the_owner_is_not_his_word(self):
        q = self.card("dev")
        self.assertIsNotNone(self.ev("permission.answered", "dev", request_id="r1", behavior="allow",
                                     answers={"x": "Connect"}, **by("hub")))
        with self.assertRaises(tools.Rejected):
            self.word(q, cards=True)

    def test_a_risky_tool_is_granted_on_his_answer_to_the_sessions_card(self):
        # the card names the tool: a card about something else is no yes to it (tests/test_hub_names_tool.py)
        self.ev("catalog.loaded", None, tools={"mail": {"kind": "mcp", "risk": ["external"]}})
        other = self.card("dev")
        self.answer("dev")
        self.assertIsNotNone(self.ev("tools.granted", "dev", tool="mail", **{"for": other}, **by("hub")))
        named = {"questions": [{**Q["questions"][0], "question": "Connect mail for dev?"}]}
        q = self.card("dev", "r2", named)
        self.answer("dev", "r2", named)
        self.assertIsNone(self.ev("tools.granted", "dev", tool="mail", **{"for": q}, **by("hub")))
        self.assertIn("mail", self.st["sessions"]["dev"]["tools"])
        self.assertEqual(self.st["sessions"]["dev"]["requests"][-1]["owner_words"]["message"],
                         next(m for m in self.st["messages"].values() if m.get("answers_question") == q)["id"])

    def test_the_hub_raises_an_executor_above_the_threshold_on_his_answer(self):
        self.ev("account.limits", None, windows={"seven_day": {"used": 0.99}})
        q = self.card("dev")
        self.assertIsNotNone(self.ev("task.run_requested", "dev-x", task="t", work="normal", cwd="/d",
                                     **{"for": q}, **by("hub")))                # no answer yet
        self.answer("dev")
        self.assertIsNone(self.ev("task.run_requested", "dev-x", task="t", work="normal", cwd="/d",
                                  **{"for": q}, **by("hub")))
        self.assertTrue(self.st["runs"]["dev-x"]["owner_ok"])


if __name__ == "__main__":
    unittest.main()
