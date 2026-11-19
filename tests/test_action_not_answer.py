"""A page button writes to a session on behalf of the owner («Install» in «About the hub», «Ask the hub to update Claude
Code», «Clean up after executors», «Fix the plugin») — this is an action, not an answer to an open card (2026-10-09:
«Install the hub update: hub update …» reached the hub as an answer to the card «Send the PR …?» — the card closed, and
the hub had to ask again). The action is marked `action`: it clears neither the hub's card, nor a session form, nor a
card to which the hub will forward its words. The owner's words — as text, by voice, as a reply in a file comment
thread — are still an answer (PR #81 applied this to buttons only, 2026-10-10)."""
import tests  # noqa: F401 — do not see the machine's settings
import unittest

from core import i18n, owner, reducer

OWNER = {"channel": "web", "owner": True}
Q = {"questions": [{"question": "Send the PR to the shared hub?", "options": [{"label": "Yes"}, {"label": "No"}]}]}
UPDATE = i18n.t("web.updates.msg_install", behind=1)   # the page's «Install» message


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


class ActionNotAnswer(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "dev", cwd="/d", session_id="s-dev", **OWNER)
        self.ev("session.started", "hub")
        self.ev("session.started", "dev")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def card(self, name):
        self.ev("turn.started", name, marker=None, prompt_head="x")
        self.ev("permission.requested", name, request_id="r1", tool="AskUserQuestion", interactive=True, input=Q)
        self.ev("turn.result", name, text="asked")
        self.ev("turn.stopped", name)
        return self.st["sessions"][name]["asks"][-1]["id"]

    def send(self, to, text, **extra):
        self.assertIsNone(self.ev("message.received", None, to=to, text=text, **OWNER, **extra))
        return self.st["messages"][f"m{self.seq}"]

    def asks(self, name):
        return [a["id"] for a in self.st["sessions"][name].get("asks") or []]

    def test_the_update_button_does_not_answer_the_hubs_card(self):
        q = self.card("hub")
        msg = self.send("hub", UPDATE, action=True)
        self.assertFalse(msg.get("answers_question"))
        self.assertEqual(msg["text"], UPDATE)                                   # no «Answer to question …»
        self.assertEqual(self.asks("hub"), [q])                                 # the card stays open
        self.assertIn("q:hub:r1", [x[0] for x in owner.questions(self.st)])     # and so do its reminders
        # and the owner's words afterwards are an answer, as before
        msg = self.send("hub", "yes, send it")
        self.assertEqual((msg.get("answers_question"), self.asks("hub")), (q, []))

    def test_a_button_quoting_the_card_does_not_answer_it(self):
        # a button text with the card's question verbatim is still not an answer (owner.quoted_card)
        q = self.card("dev")
        msg = self.send("hub", "Send the PR to the shared hub? Yes", action=True)
        self.assertEqual((msg["session"], msg.get("answers_question")), ("hub", None))
        self.assertEqual(self.asks("dev"), [q])

    def test_a_button_does_not_answer_a_sessions_open_form(self):
        # a form that holds the turn (not a card): the owner answers in it, a button does not
        self.ev("turn.started", "dev", marker=None, prompt_head="x")
        self.ev("permission.requested", "dev", request_id="r1", tool="AskUserQuestion", interactive=False, input=Q)
        self.assertEqual(self.st["sessions"]["dev"]["state"], "dialog")
        self.actions = []
        msg = self.send("dev", "Fix plugin x and send a PR to its author.", action=True)
        self.assertFalse(msg.get("answered_form"))
        self.assertFalse([a for a in self.actions if a.get("do") == "respond"])  # the form waits for an answer in it
        self.assertEqual(self.st["sessions"]["dev"]["state"], "dialog")

    def test_the_hub_relaying_a_button_does_not_answer_the_sessions_card(self):
        q = self.card("dev")
        word = self.send("hub", UPDATE, action=True)
        self.assertIsNone(self.ev("message.received", None, to="dev", text="The owner pressed Install: hub update",
                                  **{"for": word["id"]}, **by("hub")))
        msg = self.st["messages"][f"m{self.seq}"]
        self.assertFalse(msg.get("answers_question"))
        self.assertEqual(self.asks("dev"), [q])

    def test_the_hub_delegating_a_button_does_not_answer_the_sessions_card(self):
        q = self.card("dev")
        word = self.send("hub", UPDATE, action=True)
        self.ev("delegation.created", None, session="dev", **{"for": word["id"]}, context="install the update",
                **by("hub"))
        self.assertEqual(self.asks("dev"), [q])

    def test_a_voice_message_and_a_file_comment_still_answer_the_card(self):
        # PR #81 (2026-10-10): only buttons were taken from it — a voice message and a reply in a file comment thread
        # are still an answer to the open card (otherwise the card stays open and reminds, and the person answers twice)
        q = self.card("hub")
        msg = self.send("hub", "🎤 voice: yes, send it")
        self.assertEqual((msg.get("answers_question"), self.asks("hub")), (q, []))
        q = self.card("hub")
        msg = self.send("hub", "The owner's reply in the comment thread on the file `/w/spec.md`, on the piece "
                               "\"x\":\n\nyes", attachments=[], comment="c1")
        self.assertEqual((msg.get("answers_question"), self.asks("hub")), (q, []))

    def test_only_the_owners_channels_mark_an_action(self):
        # a session cannot clear someone else's card with the flag or hide an answer behind it: the flag comes only from the owner's channels
        q = self.card("hub")
        self.ev("message.received", None, to="hub", text="done", action=True, **by("dev"))
        self.assertFalse(self.st["messages"][f"m{self.seq}"].get("action"))
        self.assertEqual(self.asks("hub"), [q])


if __name__ == "__main__":
    unittest.main()
