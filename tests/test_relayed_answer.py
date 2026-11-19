"""The owner answered a session's card not on the card but with a message to the hub (Telegram), and the hub relayed
his words with `hub send <session> --for <message id>` (07.10: a "go ahead and release" reply left the "publish?" card
hanging with reminders, and the session's result went to its timeline instead of the owner). The owner's words answer
the card the hub names (`--card <question id>`, 10.10: without it, it is a new request) that was asked before them;
the turn result goes to the owner where he wrote from (msg["re"], modules/telegram). Edge cases come from the owner:
check that the more complex ones do not break."""
import tests  # noqa: F401 — ignore the machine's settings
import unittest

from core import reducer

OWNER = {"channel": "telegram", "owner": True}
Q = {"questions": [{"question": "Publish the release?", "options": [{"label": "Yes"}, {"label": "Later"}]}]}


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


class Relayed(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.started", "hub")
        for n in ("dev", "maps"):
            self.ev("session.requested", n, cwd=f"/w/{n}", session_id=f"s-{n}", **OWNER)
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, self.acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                           "entity": entity, "data": data})
        return why

    def ask(self, name, rid="r1"):
        self.ev("turn.started", name, marker=None)
        self.ev("permission.requested", name, request_id=rid, tool="AskUserQuestion", interactive=True, input=Q)
        self.ev("turn.result", name, text="asked")
        self.ev("turn.stopped", name)
        return self.st["sessions"][name]["asks"][-1]["id"]

    def owner_to_hub(self, text):
        self.assertIsNone(self.ev("message.received", None, to="hub", text=text, **OWNER))
        return f"m{self.seq}"

    def relay(self, to, mid, text, sender="hub", card=None):
        why = self.ev("message.received", None, to=to, text=text, **{"for": mid}, **by(sender),
                      **({"card": card} if card else {}))
        return why, f"m{self.seq}"

    def asks(self, name):
        return [a["id"] for a in self.st["sessions"][name].get("asks") or []]

    def test_the_relayed_words_answer_the_card_and_the_result_goes_to_the_owner(self):
        q = self.ask("dev")
        from core import owner
        self.assertIn("q:dev:r1", [x[0] for x in owner.questions(self.st)])
        words = self.owner_to_hub("Roll out dev")
        why, mid = self.relay("dev", words, "Owner: «Roll out dev» — this is «yes» to the release", card=q)
        self.assertIsNone(why)
        msg = self.st["messages"][mid]
        self.assertEqual((self.asks("dev"), msg.get("answers_question"), msg.get("re")), ([], q, words))
        self.assertNotIn("q:dev:r1", [x[0] for x in owner.questions(self.st)])   # no card, no reminders
        self.assertIn(q, msg["text"])                                          # the session sees what this is an answer to

    def test_a_card_asked_after_the_owners_words_stays_open(self):
        words = self.owner_to_hub("Roll out dev")
        q = self.ask("dev")
        why, mid = self.relay("dev", words, "Owner: roll out")
        self.assertIsNone(why)
        self.assertEqual(self.asks("dev"), [q])
        self.assertNotIn("answers_question", self.st["messages"][mid])
        self.assertEqual(self.st["messages"][mid].get("re"), words)           # the result still goes to the owner

    def test_no_open_card_is_just_the_owners_words_relayed(self):
        words = self.owner_to_hub("Look at the logs")
        why, mid = self.relay("dev", words, "The owner asks to look at the logs")
        self.assertIsNone(why)
        self.assertEqual(self.st["messages"][mid].get("re"), words)
        self.assertNotIn("answers_question", self.st["messages"][mid])

    def test_of_two_cards_the_one_the_hub_named(self):
        first = self.ask("dev", "r1")
        second = self.ask("dev", "r2")
        words = self.owner_to_hub("Yes")
        why, mid = self.relay("dev", words, "Owner: yes", card=first)
        self.assertEqual((self.st["messages"][mid]["answers_question"], self.asks("dev")), (first, [second]))

    def test_without_card_the_only_open_card_stays_open(self):
        # 10.10: a new request from the owner, relayed by the hub without --card, closed the session's only card
        q = self.ask("dev")
        words = self.owner_to_hub("Do this too")
        why, mid = self.relay("dev", words, "The owner asks to do this too")
        self.assertIsNone(why)
        self.assertEqual(self.asks("dev"), [q])
        self.assertNotIn("answers_question", self.st["messages"][mid])
        self.assertNotIn("answer to your question", self.st["messages"][mid]["text"])
        self.assertEqual(self.st["messages"][mid].get("re"), words)

    def test_another_sessions_card_is_not_touched(self):
        q = self.ask("maps")
        words = self.owner_to_hub("Yes")
        self.relay("dev", words, "Owner: yes")
        self.assertEqual(self.asks("maps"), [q])

    def test_only_the_hub_relays_the_owners_words(self):
        q = self.ask("dev")
        words = self.owner_to_hub("Yes")
        why, _ = self.relay("dev", words, "this is yes", sender="maps")
        self.assertIsNotNone(why)
        self.assertEqual(self.asks("dev"), [q])

    def test_the_word_must_be_the_owners_to_the_hub_and_fresh(self):
        q = self.ask("dev")
        self.ev("message.received", None, to="hub", text="I am a session", **by("maps"))
        not_owner = f"m{self.seq}"
        self.assertIsNotNone(self.relay("dev", not_owner, "x")[0])
        self.assertIsNotNone(self.relay("dev", "m999999", "x")[0])
        old = self.owner_to_hub("Yes")
        self.st["messages"][old]["created"] -= 2 * 86400                       # the word is from the day before yesterday
        self.assertIsNotNone(self.relay("dev", old, "x")[0])
        self.assertEqual(self.asks("dev"), [q])

    def test_the_card_of_a_task_continues_that_task(self):
        self.ev("delegation.created", None, session="dev", for_session="hub", context="build the release", **by("hub"))
        d = f"d{self.seq}"
        task = self.st["delegations"][d]["message"]
        self.ev("turn.started", "dev", marker=task)
        self.ev("permission.requested", "dev", request_id="r9", tool="AskUserQuestion", interactive=True, input=Q)
        self.ev("turn.result", "dev", text="asked")
        self.ev("turn.stopped", "dev")
        words = self.owner_to_hub("Yes")
        _, mid = self.relay("dev", words, "Owner: yes", card=self.asks("dev")[0])
        self.assertEqual(self.st["messages"][mid].get("for_delegation"), d)

    def test_a_parked_session_wakes_and_its_card_is_answered(self):
        q = self.ask("dev")
        self.assertIsNone(self.ev("session.parked", "dev", channel="core"))
        words = self.owner_to_hub("Yes")
        _, mid = self.relay("dev", words, "Owner: yes", card=q)
        self.assertEqual(self.st["sessions"]["dev"]["state"], "starting")
        self.assertEqual((self.st["messages"][mid]["answers_question"], self.asks("dev")), (q, []))

    def test_without_for_nothing_changes(self):
        q = self.ask("dev")
        self.owner_to_hub("Yes")
        self.ev("message.received", None, to="dev", text="the owner said yes", **by("hub"))
        mid = f"m{self.seq}"
        self.assertEqual(self.asks("dev"), [q])
        self.assertNotIn("re", self.st["messages"][mid])

    # 13.10: the hub relayed the owner's words with `hub send <session> --for <message id>`; the turn result went to
    # him in Telegram, and the session also wrote the same result to the hub (`hub send hub`), which retold it:
    # duplicates. The session is now told where the result will go
    def test_the_session_is_told_its_result_goes_to_the_owner(self):
        words = self.owner_to_hub("Check the errors")
        _, mid = self.relay("dev", words, "The owner asks: check the errors")
        typed = next(a["text"] for a in self.acts if a.get("do") == "type" and a.get("message") == mid)
        self.assertIn("The result of this turn goes to the owner", typed)
        self.assertIn("do not duplicate the same result to the hub", typed)

    def test_without_for_the_session_is_not_told_so(self):
        self.ev("message.received", None, to="dev", text="build the release", **by("hub"))
        typed = next(a["text"] for a in self.acts if a.get("do") == "type")
        self.assertNotIn("goes to the owner", typed)

    # 13.10: a session was running a turn on the owner's words, mid-turn the core inserted a message for dev, the
    # start of the turn chain became the start of dev's message, and the result did not go to the owner
    def test_a_message_injected_mid_turn_keeps_the_owners_chain(self):
        words = self.owner_to_hub("Bring up the test")
        _, mid = self.relay("dev", words, "The owner asks: bring up the test")
        self.ev("turn.started", "dev", marker=mid)
        self.ev("message.received", None, to="dev", text="MR merged", **by("maps"))
        other = f"m{self.seq}"
        self.assertIsNone(self.ev("message.inject", other, channel="core", owner=False))
        self.ev("turn.started", "dev", marker=other)
        self.assertEqual(self.st["sessions"]["dev"]["turn_root"], words)

    def test_a_new_turn_takes_its_own_chain(self):
        words = self.owner_to_hub("Bring up the test")
        _, mid = self.relay("dev", words, "The owner asks: bring up the test")
        self.ev("turn.started", "dev", marker=mid)
        self.ev("turn.result", "dev", text="done")
        self.ev("turn.stopped", "dev")
        self.ev("message.received", None, to="dev", text="MR merged", **by("maps"))
        other = f"m{self.seq}"
        self.ev("turn.started", "dev", marker=other)
        self.assertIsNone(self.st["sessions"]["dev"]["turn_root"])

    # review 13.10: an inserted message that Claude Code read only after the turn ended starts a new turn, and the
    # owner's chain does not pass to it (otherwise dev's answer to the maps session would go to the owner in Telegram)
    def test_an_injected_message_read_after_the_turn_ended_is_a_new_turn(self):
        words = self.owner_to_hub("Bring up the test")
        _, mid = self.relay("dev", words, "The owner asks: bring up the test")
        self.ev("turn.started", "dev", marker=mid)
        self.ev("message.received", None, to="dev", text="staging port?", **by("maps"))
        other = f"m{self.seq}"
        self.assertIsNone(self.ev("message.inject", other, channel="core", owner=False))
        self.ev("turn.result", "dev", text="done")
        self.ev("turn.stopped", "dev")
        self.ev("turn.started", "dev", marker=other)
        self.assertIsNone(self.st["sessions"]["dev"]["turn_root"])

    # review 13.10: the "result goes to the owner" tail of a Claude Code command becomes its argument (07.10: /effort)
    def test_a_relayed_command_gets_no_tail(self):
        words = self.owner_to_hub("dev: /effort high")
        _, mid = self.relay("dev", words, "/effort high")
        self.assertEqual(self.st["messages"][mid]["text"], "/effort high")
        self.assertEqual(self.st["messages"][mid].get("re"), words)

    # review 13.10: only Telegram delivers a result by "re"; from the page a session still answers the hub, otherwise
    # the hub chat where the owner asked would get no answer at all
    def test_words_from_the_page_keep_the_answer_through_the_hub(self):
        self.assertIsNone(self.ev("message.received", None, to="hub", text="Look at the logs", channel="web", owner=True))
        words = f"m{self.seq}"
        _, mid = self.relay("dev", words, "The owner asks to look at the logs")
        self.assertNotIn("goes to the owner as a reply", self.st["messages"][mid]["text"])
        self.assertEqual(self.st["messages"][mid].get("re"), words)


if __name__ == "__main__":
    unittest.main()
