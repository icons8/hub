"""The owner corrects the liaison's card (spec d2423478; analysis d2762134): what goes to the other hub is what he saw
and wrote, in the thread of the session's request.

The liaison asks the owner by a form about a session's request («Send to bee: …?»), the form is a card and the turn
goes on; the owner answers it — by a button, by his words (typed to the liaison or in the card's own field, passed on
by the hub with `--for --card`), or closes it (×) and writes. Then:
- `--text-from <his words>` sends only his words: no head of the core («Answer to question qN …», «This is the user's
  answer … passed on by the hub»), no tail of a Telegram word — the page shows him exactly those words;
- `--text-from <the session's words>` after he answered in words is refused (`owner_changed`): the old text must not
  go; the liaison sends the text as he changed it (`--text`);
- the liaison's own words, or his by reference, are the answer to that session's request: its thread, its outcome, and
  the other hub's answer comes back to the session — not a new thread and «has not handled your request» later.
A press of «Send» keeps `--text-from <the session's words>` as before."""
import tests  # noqa: F401 — keeps the machine's settings out
import json
import os
import tempfile
import unittest
import uuid

from core import busd, busstate, coretext, liaison, views
from tests.test_hubbus import NOW, need_lib, need_ssh
from tests.test_hubbus_s1 import acquainted
from tests.test_hubbus_threads import answer, ask, delivered

QUESTION = "Send to bee: «Deploy is at 10:00»?"
FORM = {"questions": [{"question": QUESTION, "header": "Bus", "multiSelect": False,
                       "options": [{"label": "Send", "description": ""}, {"label": "Don't send", "description": ""}]}]}


@need_lib
@need_ssh
class TheOwnersCorrectionGoesOut(unittest.TestCase):
    def setUp(self):
        a, b, wire = acquainted()
        self.a, self.b, self.wire = a, b, wire
        self.nb = next(iter(b.bus["contacts"]))
        first = ask(a, wire, "api", "Which schema does /v1/items take?")       # a live thread api <-> bee
        self.thread = first["thread"]
        answer(b, self.nb, wire, self.thread, "Schema v2")
        a.rec("turn.started", {"marker": delivered(a, "api", "Schema v2")["id"]}, entity="api")
        self.m1, why = a.say("api", "Tell them: deploy is at 10:00", bus_to="bee", bus_to_via="local")
        self.assertIsNone(why)
        self.assertEqual(a.bus["requests"][self.m1]["thread"], self.thread)
        # the liaison's turn on the request: it asks the owner by a form; the form is a card, the turn ends
        a.rec("turn.started", {"marker": self.m1}, entity=liaison.NAME)
        a.rec("permission.requested", {"request_id": "r-form", "tool": "AskUserQuestion", "interactive": True,
                                       "input": FORM}, entity=liaison.NAME)
        self.qid = next(x["id"] for x in a.st["sessions"][liaison.NAME]["asks"] if x.get("request_id") == "r-form")
        a.rec("turn.ended", {}, entity=liaison.NAME)

    # --- the steps ------------------------------------------------------------------------------------------------

    def owner_says(self, text, channel="web", to=liaison.NAME):
        ev = self.a.rec("message.received", {"to": to, "text": text, "channel": channel, "owner": True})
        return f"m{ev['seq']}"

    def liaison_turn(self, *marks):
        self.a.rec("turn.started", {"marker": marks[0], **({"also": list(marks[1:])} if marks[1:] else {})},
                   entity=liaison.NAME)

    def press(self, label):
        self.a.rec("permission.answered", {"request_id": "r-form", "behavior": "allow", "answers": {QUESTION: label},
                                           "channel": "web", "owner": True}, entity=liaison.NAME)
        return next(m["id"] for m in reversed(self.a.messages_to(liaison.NAME)) if m.get("answers_question") == self.qid)

    def send(self, **data):
        """The liaison's `hub bus send bee …`: (the outgoing record, the refusal — by its code when it has one)."""
        ev, why = self.a.as_liaison("bus.send_requested", {"contact": "bee", **data})
        code = next((e["data"].get("code") for e in map(json.loads, open(
            os.path.join(self.a.h.state_dir, "journal.jsonl"), encoding="utf-8"))
            if e["type"] == "core.rejected" and e["data"].get("of") == ev["seq"]), None)
        return self.a.bus["out"].get(f"o{ev['seq']}"), (f"{code}: {why}" if code else why)

    def at_b(self):
        self.wire.settle()
        return sorted(self.b.bus["inbox"].values(), key=lambda x: x["ts"])[-1]

    def back_to(self, item):
        """bee answers in the thread its item came in: to whom of A it goes, and the 30-minute notes to api."""
        answer(self.b, self.nb, self.wire, item["thread"], "OK, 11:00 works")
        got = {s: len([m for m in self.a.messages_to(s) if m.get("channel") == "hubbus" and "11:00 works" in m["text"]])
               for s in ("api", liaison.NAME)}
        self.a.now = NOW + busstate.STALE + 1
        for _ in range(2):
            with self.a.env():
                busd.tick(self.a.h)
        stale = [m for m in self.a.messages_to("api") if "has not handled" in m["text"]]
        return got, stale

    def assert_answers_the_request(self, o, item):
        self.assertEqual(o["thread"], self.thread)                  # the thread of api's request, not a new one
        self.assertEqual(o.get("answers"), [self.m1])
        self.assertEqual(busstate.whose(o), "api")
        self.assertEqual(self.a.bus["requests"][self.m1]["outcome"], "sent")
        got, stale = self.back_to(item)
        self.assertEqual(got, {"api": 1, liaison.NAME: 0})        # bee's answer goes to api itself
        self.assertEqual(stale, [])

    # --- S2: his words by reference -------------------------------------------------------------------------------

    def test_his_typed_answer_by_reference_goes_without_the_cores_head(self):
        mo = self.owner_says("Η ανάπτυξη είναι στις 11:00")
        self.assertTrue(self.a.st["messages"][mo]["text"].startswith("Answer to question"))   # what the liaison reads
        self.liaison_turn(mo)
        o, why = self.send(text_from=mo)
        self.assertIsNone(why)
        item = self.at_b()
        self.assertEqual(item["text"], "Η ανάπτυξη είναι στις 11:00")
        # the same words the page shows him as his answer
        self.assertEqual(views._feed_label(self.a.st["messages"][mo]),
                         views._feed_label({"answers_question": self.qid, "answer_body": item["text"]}))
        self.assert_answers_the_request(o, item)

    # --- S5: the hub passes his words on -------------------------------------------------------------------------

    def hub_passes(self, channel):
        a = self.a
        a.rec("session.requested", {"cwd": tempfile.mkdtemp(), "session_id": str(uuid.uuid4()), "channel": "core",
                                    "kind": "hub"}, entity="hub")
        a.up("hub")
        word = self.owner_says("Tell bee: deploy is at 11:00", channel=channel, to="hub")
        a.rec("turn.started", {"marker": word}, entity="hub")
        mh, why = a.say("hub", "Deploy is at 11:00", **{"for": word, "card": self.qid})
        self.assertIsNone(why)
        return mh

    def test_his_words_the_hub_passed_on_go_without_the_cores_head(self):
        mh = self.hub_passes("web")
        self.assertIn("passed on by the hub", self.a.st["messages"][mh]["text"])
        self.liaison_turn(mh)
        o, why = self.send(text_from=mh)
        self.assertIsNone(why)
        item = self.at_b()
        self.assertEqual(item["text"], "Deploy is at 11:00")
        self.assert_answers_the_request(o, item)

    def test_a_telegram_word_the_hub_passed_on_goes_without_the_tail(self):
        mh = self.hub_passes("telegram")
        self.assertIn("reply to them", self.a.st["messages"][mh]["text"])   # the tail the liaison reads
        self.liaison_turn(mh)
        _, why = self.send(text_from=mh)
        self.assertIsNone(why)
        self.assertEqual(self.at_b()["text"], "Deploy is at 11:00")

    # --- S1: after his words, not the session's old words --------------------------------------------------------

    def test_the_sessions_words_after_his_typed_correction_are_refused(self):
        mo = self.owner_says("No — change it: deploy is at 11:00")
        self.liaison_turn(mo)
        o, why = self.send(text_from=self.m1)
        self.assertIsNone(o)
        self.assertTrue(str(why).startswith("owner_changed: owner_changed:"), why)
        self.assertIn(mo, str(why))
        self.assertEqual(len(self.b.bus["inbox"]), 1)               # nothing new reached bee
        o, why = self.send(text="Deploy is at 11:00")              # the corrected text, its own words
        self.assertIsNone(why)
        item = self.at_b()
        self.assertEqual(item["text"], "Deploy is at 11:00")
        self.assert_answers_the_request(o, item)

    def test_the_sessions_words_after_his_own_answer_in_the_card_are_refused(self):
        mo = self.press("Send it, but say 11:00")                  # typed into the card's own field
        self.liaison_turn(mo)
        o, why = self.send(text_from=self.m1)
        self.assertTrue(str(why).startswith("owner_changed: owner_changed:"), why)
        o, why = self.send(text="Deploy is at 11:00")
        self.assertIsNone(why)
        item = self.at_b()
        self.assertEqual(item["text"], "Deploy is at 11:00")
        self.assert_answers_the_request(o, item)

    def test_the_sessions_words_he_did_not_read_yet_are_refused_too(self):
        self.liaison_turn(self.m1)                                  # still the old turn: his words wait in its queue
        self.owner_says("No — change it: deploy is at 11:00")
        _, why = self.send(text_from=self.m1)
        self.assertTrue(str(why).startswith("owner_changed: owner_changed:"), why)

    def test_a_press_of_send_keeps_the_sessions_words_by_reference(self):   # control: green on the old code too
        mo = self.press("Send")
        self.liaison_turn(mo)
        o, why = self.send(text_from=self.m1)
        self.assertIsNone(why)
        item = self.at_b()
        self.assertEqual(item["text"], "Tell them: deploy is at 10:00")
        self.assertEqual(o["thread"], self.thread)
        self.assertEqual(self.a.bus["requests"][self.m1]["outcome"], "sent")

    # --- S3/S4: its own words on his correction answer the request -----------------------------------------------

    def test_its_own_words_on_his_correction_answer_the_request(self):
        mo = self.owner_says("No — change it: deploy is at 11:00")
        self.liaison_turn(mo)
        o, why = self.send(text="Deploy is at 11:00")
        self.assertIsNone(why)
        self.assert_answers_the_request(o, self.at_b())

    def test_its_own_words_on_his_words_after_he_closed_the_card_answer_the_request(self):
        self.a.rec("question.dismissed", {"request_id": "r-form", "channel": "web", "owner": True}, entity=liaison.NAME)
        mo = self.owner_says("Change it: deploy is at 11:00, then send")
        self.liaison_turn(mo)
        o, why = self.send(text="Deploy is at 11:00")
        self.assertIsNone(why)
        self.assert_answers_the_request(o, self.at_b())
        _, why = self.send(text_from=self.m1)                     # answered already
        self.assertIsNotNone(why)

    def test_after_he_closed_the_card_and_wrote_the_sessions_words_are_refused(self):
        self.a.rec("question.dismissed", {"request_id": "r-form", "channel": "web", "owner": True}, entity=liaison.NAME)
        mo = self.owner_says("Change it: deploy is at 11:00, then send")
        self.liaison_turn(mo)
        _, why = self.send(text_from=self.m1)
        self.assertTrue(str(why).startswith("owner_changed: owner_changed:"), why)
        self.assertIn(f"question {self.qid} about {self.m1} in words ({mo})", why)   # the question his × closed

    # --- review: the edges ---------------------------------------------------------------------------------------

    def test_his_words_long_after_he_closed_the_card_are_not_about_it(self):
        self.a.rec("question.dismissed", {"request_id": "r-form", "channel": "web", "owner": True}, entity=liaison.NAME)
        self.a.now = NOW + busstate.STALE + 60
        mo = self.owner_says("Tell bee: we are late today")
        self.liaison_turn(mo)
        o, why = self.send(text="We are late today")
        self.assertIsNone(why)
        self.assertIsNone(o.get("answers"))
        self.assertNotEqual(o["thread"], self.thread)
        _, why = self.send(text_from=self.m1)                     # not his correction of it either
        self.assertIsNone(why)

    def test_after_his_words_no_later_answer_lets_the_sessions_old_words_go(self):
        """His next card may show his corrected text: a press on it, a refusal or a × is no yes to the old words."""
        mo = self.owner_says("No — change it: deploy is at 11:00")
        self.liaison_turn(mo)
        press = {"behavior": "allow", "answers": {QUESTION: "Send"}}
        for n, answer in enumerate((press, {"behavior": "deny"}, None, press, press)):   # past the window of 4
            rid = f"r-form{n + 2}"
            self.a.rec("permission.requested", {"request_id": rid, "tool": "AskUserQuestion", "interactive": True,
                                                "input": FORM}, entity=liaison.NAME)
            if answer is None:
                self.a.rec("question.dismissed", {"request_id": rid, "channel": "web", "owner": True},
                           entity=liaison.NAME)
            else:
                self.a.rec("permission.answered", {"request_id": rid, **answer, "channel": "web", "owner": True},
                           entity=liaison.NAME)
            _, why = self.send(text_from=self.m1)
            self.assertTrue(str(why).startswith("owner_changed: owner_changed:"), (answer, why))
            self.assertIn(f"({mo})", why)
        self.assertEqual(len(self.a.bus["requests"][self.m1]["questions"]), 4)

    def test_a_press_of_a_label_with_spaces_is_not_words(self):
        self.a.st["sessions"][liaison.NAME]["asks"][-1]["input"]["questions"][0]["options"][0]["label"] = "Send "
        self.a.bus["requests"][self.m1]["questions"][-1]["labels"] = ["Send", "Don't send"]   # as liaison_asked keeps them
        mo = self.press("Send")
        self.liaison_turn(mo)
        _, why = self.send(text_from=self.m1)
        self.assertIsNone(why)

    def test_a_press_with_files_attached_on_the_page_is_not_words(self):
        # the page puts the paths of files attached to an answer under it (cards.js withFiles): still a press, so the
        # session's words go as they were, and the paths of his disk do not go to the other hub as his words
        mo = self.press("Send\n\nAttached files:\n- /home/x/uploads/shot.png ([Image #1])")
        self.liaison_turn(mo)
        _, why = self.send(text_from=self.m1)
        self.assertIsNone(why)

    def test_his_own_words_with_files_are_still_his_words(self):
        mo = self.press("Change it: deploy is at 11:00\n\nAttached files:\n- /home/x/uploads/plan.pdf")
        self.liaison_turn(mo)
        _, why = self.send(text_from=self.m1)
        self.assertTrue(str(why).startswith("owner_changed:"), why)

    def test_the_cores_note_of_his_press_is_not_words_to_send(self):
        mo = self.press("Send")
        self.liaison_turn(mo)
        o, why = self.send(text_from=mo)
        self.assertIsNone(o)
        self.assertIsNotNone(why)
        self.assertIsNone(self.a.bus["requests"][self.m1]["outcome"])

    def test_a_turn_on_his_answer_and_the_request_answers_it_once(self):
        mo = self.owner_says("No — change it: deploy is at 11:00")
        self.liaison_turn(mo, self.m1)
        o, why = self.send(text="Deploy is at 11:00")
        self.assertIsNone(why)
        self.assertEqual(o["answers"], [self.m1])

    def test_his_failed_words_are_not_a_correction(self):
        self.a.rec("question.dismissed", {"request_id": "r-form", "channel": "web", "owner": True}, entity=liaison.NAME)
        mo = self.owner_says("Change it: deploy is at 11:00")
        self.a.st["messages"][mo]["state"] = "failed"
        _, why = self.send(text_from=self.m1)
        self.assertIsNone(why)

    def test_his_words_on_nothing_it_asked_stay_a_new_thread(self):   # control: green on the old code too
        self.press("Don't send")
        mo = self.owner_says("Also tell bee hello from me")
        self.liaison_turn(mo)
        o, why = self.send(text="Hello from the owner")
        self.assertIsNone(why)
        self.assertNotEqual(o["thread"], self.thread)
        self.assertIsNone(o.get("answers"))
        self.assertIsNone(o.get("asker"))


@need_lib
@need_ssh
class TwoSessionsOneQuestion(unittest.TestCase):
    """The liaison asked about requests of two sessions at once: whose answer his correction is the core cannot tell —
    its own words answer nobody's request (as before)."""

    def test_no_asker(self):
        a, b, wire = acquainted()
        a.up("api")
        a.up("docs")
        m1, _ = a.say("api", "Deploy is at 10:00", bus_to="bee", bus_to_via="local")
        m2, _ = a.say("docs", "Docs are at /v2", bus_to="bee", bus_to_via="local")
        a.rec("turn.started", {"marker": m1, "also": [m2]}, entity=liaison.NAME)
        a.rec("permission.requested", {"request_id": "r-form", "tool": "AskUserQuestion", "interactive": True,
                                       "input": FORM}, entity=liaison.NAME)
        a.rec("turn.ended", {}, entity=liaison.NAME)
        ev = a.rec("message.received", {"to": liaison.NAME, "text": "Send both at 11:00", "channel": "web",
                                        "owner": True})
        a.rec("turn.started", {"marker": f"m{ev['seq']}"}, entity=liaison.NAME)
        ev, why = a.as_liaison("bus.send_requested", {"contact": "bee", "text": "Deploy at 11:00; docs at /v2"})
        self.assertIsNone(why)
        o = a.bus["out"][f"o{ev['seq']}"]
        self.assertIsNone(o.get("asker"))
        self.assertIsNone(o.get("answers"))


class TheAuthorsWords(unittest.TestCase):
    """coretext.said: the author's words of a message without what the core put around them."""

    def test_fields(self):
        self.assertEqual(coretext.said({"text": "Answer to question q1 («x»): yes", "answer_body": "yes"}), "yes")
        self.assertEqual(coretext.said({"text": "head: words\n\ntail", "said": "words"}), "words")
        self.assertEqual(coretext.said({"text": "plain"}), "plain")              # an earlier message: its text
        task = {"text": "", "brief": [0, 0]}
        task["text"] = "Task from the hub: Deploy is at 11:00\n(the rules of an executor …)"
        task["brief"] = [len("Task from the hub: "), len("Task from the hub: Deploy is at 11:00")]
        coretext.prepend(task, "This is the user's answer to your question q1 («x»), passed on by the hub.\n")
        self.assertEqual(coretext.said(task), "Deploy is at 11:00")           # a task's frame and head are the core's
        self.assertEqual(coretext.said({"text": "short", "brief": [0, 99]}), "short")   # a span past the text: its text


class APressWithFilesOnTheBusCard(unittest.TestCase):
    def test_send_with_a_file_attached_on_the_page_is_still_send(self):
        # the page puts the paths of files attached to a card's answer under the label (cards.js withFiles): a card of
        # the bus (held words, an offer, an introduction) takes that as the press it is — not as a refusal that hands
        # the paths back as his words
        from tests.test_liaison_echo import LiaisonEcho
        h = LiaisonEcho("setUp")
        h.setUp()
        h.turn(liaison.NAME, (m := h.letter()))
        oid = h.send(m)
        q = "Send to maps-hub: «Are the tiles ready?»"
        h.ev("core.bus_send_held", None, src="core", out=oid, finding="key", card={
            "input": {"questions": [{"question": q, "options": [{"label": "Send"}, {"label": "Don't send"}]}]},
            "labels": {"send": "Send", "no": "Don't send"}})
        (card,) = [x for x in h.st["sessions"][liaison.NAME]["asks"] if x.get("hubbus")]
        h.ev("permission.answered", liaison.NAME, request_id=card["request_id"], behavior="allow", channel="web",
             owner=True, answers={q: "Send\n\nAttached files:\n- /home/x/uploads/shot.png ([Image #1])"})
        out = h.st["bus"]["out"][oid]
        self.assertEqual((out.get("approved"), out.get("status")), ("owner", "held"), out)


if __name__ == "__main__":
    unittest.main()
