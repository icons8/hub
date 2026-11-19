"""A question to the owner goes as a form, not text (owner decision 2026-10-08: a session asked in text at the end of
its reply, which is very inconvenient). The note "business decisions — to the owner as options (AskUserQuestion)" used
to be only on tasks; the owner's direct words reached sessions without it, and the session asked in text at the end of
a reply (an eval on a conversation copy: 0 → 1 with the line). Now the line is on every owner message except Claude
Code commands."""
import tests  # noqa: F401 — do not see the machine's settings
import hashlib
import unittest
from unittest import mock

from core import reducer
from hosts import streamjson
from tests.test_modapi_prompt import Base

OWNER = {"channel": "web", "owner": True}
LINE = reducer.ASK_BY_FORM


class OwnerAsksByForm(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        for n, kind in (("hub", "hub"), ("maps", "work"), ("docs", "work")):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", kind=kind, **OWNER)
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def typed(self, name):
        out = [a["text"] for a in self.actions if a.get("do") == "type" and a["session"] == name]
        self.actions = []
        return out

    def test_the_owners_words_to_a_session_carry_the_line(self):
        self.ev("message.received", None, to="maps", text="And what is this package?", **OWNER)
        (t,) = self.typed("maps")
        self.assertIn("And what is this package?", t)
        self.assertIn(LINE, t)
        self.assertIn("AskUserQuestion", LINE)

    def test_and_to_the_hub(self):
        self.ev("message.received", None, to="hub", text="what next?", **OWNER)
        (t,) = self.typed("hub")
        self.assertIn(LINE, t)

    def test_a_session_writing_to_another_gets_no_line(self):
        self.ev("message.received", None, to="docs", text="look at the diagram", channel="session", sender="maps")
        (t,) = self.typed("docs")
        self.assertNotIn(LINE, t)

    def test_a_claude_code_command_stays_as_typed(self):
        self.ev("message.received", None, to="maps", text="/compact", **OWNER)
        (t,) = self.typed("maps")
        self.assertTrue(t.startswith("/compact"), t)
        self.assertNotIn(LINE, t)

    def test_a_file_comment_asks_back_in_its_thread_not_by_form(self):
        # owner decision 2026-10-10: when negotiating through file comments this specifics must be considered — the
        # turn result goes to a branch, and the "form only" line sent the question as a card to the general feed
        self.ev("message.received", None, to="maps", text="The owner's comment on the file: why so?", comment="k1.ab",
                **OWNER)
        (t,) = self.typed("maps")
        self.assertNotIn(LINE, t)
        self.assertIn(reducer.ASK_IN_THREAD, t)
        self.ev("message.received", None, to="docs", text="and more", **OWNER)
        (t,) = self.typed("docs")
        self.assertIn(LINE, t)                         # the owner's ordinary words — still as a form

    def test_the_line_is_the_same_for_every_hub(self):
        # a general core rule: no services or agents that not everyone has (Teams is a personal setup, 2026-10-08)
        self.assertNotIn("Teams", LINE)
        # 2026-10-16: the line grew by the card rule (418 characters); 2026-10-28 that rule moved to the system prompt
        # (CARDS), the tail is repeated in every owner word — the limit keeps it from growing back
        self.assertLess(len(LINE), 200)
        self.assertNotIn("\n", LINE)

    def test_the_line_goes_only_to_the_session_not_to_what_the_owner_sees(self):
        # the note lives only in the session's input: the owner's words in state (feed, results, Telegram) — without it
        self.ev("message.received", None, to="maps", text="And what is this package?", **OWNER)
        (t,) = self.typed("maps")
        self.assertTrue(t.endswith(LINE), t[-80:])
        (msg,) = [m for m in self.st["messages"].values() if m["session"] == "maps"]
        self.assertEqual(msg["text"], "And what is this package?")


class CardRuleInTheSystemPrompt(Base):
    """2026-10-28: how a card reads (a decision of the owner, the first phrase, no jargon, no batch) is a part of the
    system prompt — it is not lost by distance or compaction; an executor of a mini-hub asks the owner nothing, so it has
    no such part."""

    def test_a_session_gets_the_cards_part(self):
        h = self.host()
        text = self.prompt(h.command("w", "s-1", agent_file=self.agent(h)))
        self.assertIn(streamjson.CARDS, text)   # the Secrets feature is on (an empty vault too): the first password goes there
        self.assertNotIn(streamjson.CARDS_NO_SECRETS, text)
        with mock.patch.object(streamjson.StreamHost, "secrets_on", return_value=False):
            text = self.prompt(h.command("w", "s-1", agent_file=self.agent(h)))
        self.assertIn(streamjson.CARDS_NO_SECRETS, text)
        self.assertNotIn(streamjson.CARDS, text)
        for cards in (streamjson.CARDS, streamjson.CARDS_NO_SECRETS):
            self.assertIn("\"Need more context\"", cards)   # the card's button (core/owner.MORE_CONTEXT)
            self.assertNotIn("\n", cards)

    def test_the_form_only_line_is_not_in_it_a_comment_thread_asks_in_text(self):
        self.assertNotIn("only through the AskUserQuestion form", streamjson.CARDS)   # core/reducer.ASK_BY_FORM

    def test_an_executor_of_a_mini_hub_has_no_cards_part(self):
        h = self.host()
        text = self.prompt(h.command("w", "s-1", agent_file=self.agent(h), bus="maps"))
        self.assertNotIn(streamjson.CARDS, text)
        self.assertNotIn(streamjson.CARDS_NO_SECRETS, text)

    def test_a_step_only_the_owner_can_do_is_one_card_not_a_line_in_the_result(self):
        # 2026-11-18 (owner, q2582426 "A card for such steps"): "sign in, then write 'done'" went as a tail of a result and
        # a backlog line, no card; agent-dev evals (k=3, Opus): a card 0/3 -> 9/9, Secrets first for a password 9/9
        cards = streamjson.CARDS
        self.assertTrue(cards.startswith("A card to the owner (AskUserQuestion) is only about their decision"))
        self.assertIn("and about a step only they can do (a password sign-in with no entry in `hub secret list`", cards)
        self.assertIn("never a line in a result or a message", cards)
        self.assertIn("the first option is \"I'll add it to Secrets\"", cards)
        self.assertIn("the second is \"I'll sign in myself\"", cards)
        self.assertIn("results do not mention the step at all", cards)
        self.assertIn("send the card now and drop that line", cards)
        # the rule that the card is about the owner's decision goes on right after it (the text before it is unchanged)
        self.assertIn("drop that line. How to do it, in what order and when", cards)

    def test_with_the_secrets_kept_the_text_is_as_it_was_byte_for_byte(self):
        # the text of 2026-11-18 (agent-dev evals: Secrets first for a password 9/9) is not touched by the template
        self.assertEqual(hashlib.sha256(streamjson.CARDS.encode("utf-8")).hexdigest(),
                         "eeb12a727d242945b374063243cee803dfb103c1af9eeada54e09dde552499e0")

    def test_with_the_secrets_module_off_the_card_has_no_secrets_option(self):
        # 2026-11-18: a hub whose SECRETS part is not in the prompt must not send a session to Settings → Secrets or to
        # `hub secret list`: for a password the first option is "I'll sign in myself"
        cards = streamjson.CARDS_NO_SECRETS
        for word in ("Secrets", "hub secret", "secret fill"):
            self.assertNotIn(word, cards)
        self.assertIn("(a password sign-in, Touch ID, a code from their phone, a CAPTCHA)", cards)
        self.assertIn("For a password the first option is \"I'll sign in myself\". While the card hangs", cards)
        # the rest is the same text, word for word: only the two password places differ
        self.assertEqual(cards, streamjson.CARDS.replace(" with no entry in `hub secret list`", "").replace(
            "\"I'll add it to Secrets\" (Settings → Secrets; then you carry on yourself with `hub secret fill`), the "
            "second is \"I'll sign in myself\"", "\"I'll sign in myself\""))

    def test_what_they_want_is_theirs_to_decide(self):
        # 2026-11-07: a short message with an unclear WHAT is a card for the owner, not a technical choice to decide alone
        self.assertIn("WHAT they want", streamjson.CARDS)
        self.assertNotIn("\n", streamjson.ASK_WHAT)

    def test_the_ask_what_rule_is_in_a_session_but_not_in_a_mini_hub_executor(self):
        h = self.host()
        text = self.prompt(h.command("w", "s-1", agent_file=self.agent(h)))
        self.assertIn(streamjson.ASK_WHAT, text)
        self.assertLess(text.index(streamjson.FIRST_ACTION), text.index(streamjson.ASK_WHAT))
        text = self.prompt(h.command("w", "s-1", agent_file=self.agent(h), bus="maps"))
        self.assertNotIn(streamjson.ASK_WHAT, text)   # the form is closed to it

    def test_the_part_is_named_cards_for_modules(self):
        from tests.test_modapi_prompt import Stub, parts_of
        seen = []

        def spy(point, payload):
            seen.extend(p["name"] for p in payload["parts"])
            return parts_of(payload)
        h = self.host(Stub(m=spy))
        h.command("w", "s-1", agent_file=self.agent(h))
        self.assertIn("cards", seen)


if __name__ == "__main__":
    unittest.main()
