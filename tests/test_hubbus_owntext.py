"""The liaison's own words (`--text`) on a session's request answer it as the session's words by reference do: they go
in the thread the core kept for the request, and the request takes their fate (sent, asked the owner, declined) — no
"not handled" note half an hour after it was answered. The core's own marks the liaison reads in its prompt (the label
[hub:mN from X] of a message, the fence lines <<<bus-… / bus-…>>> around another hub's words) do not go out in its
own words; a session's words by reference go as written."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest
from unittest import mock

from core import busd, busstate, liaison
from tests.test_hubbus import need_lib, need_ssh
from tests.test_hubbus_s1 import NOW, acquainted
from tests.test_hubbus_threads import answer, ask, delivered, opened_by_b


def own(a, text, **extra):
    """The liaison sends its own words to bee; the owner presses «send» if there is a card. -> the outgoing record."""
    ev, why = a.as_liaison("bus.send_requested", {"contact": "bee", "text": text, **extra})
    assert why is None, why
    for card in a.cards():
        a.answer(card, "send")
    return a.bus["out"][f"o{ev['seq']}"]


@need_lib
@need_ssh
class TheLiaisonsOwnWordsAnswerTheRequest(unittest.TestCase):
    def setUp(self):
        self.a, self.b, self.wire = acquainted()
        self.name_at_b = next(iter(self.b.bus["contacts"]))

    def request(self, session, text):
        """A session asks the liaison to ask bee (no ready text); the liaison's turn is on that message. -> mN."""
        a = self.a
        if session not in a.st["sessions"]:
            a.up(session)
        mid, why = a.say(session, text, bus_to="bee", bus_to_via="local")
        assert why is None, why
        a.rec("turn.started", {"marker": mid}, entity=liaison.NAME)
        return mid

    def a_thread_with_bee(self):
        """api asked bee before and got the answer in a thread; its turn is on that answer. -> the thread."""
        a = self.a
        first = ask(a, self.wire, "api", "Which schema does /v1/items take?")
        answer(self.b, self.name_at_b, self.wire, first["thread"], "Schema v2")
        a.rec("turn.started", {"marker": delivered(a, "api", "Schema v2")["id"]}, entity="api")
        return first["thread"]

    def stale_notes(self, session):
        a = self.a
        a.now = NOW + busstate.STALE + 1
        for _ in range(2):
            with a.env():
                busd.tick(a.h)
        return [m for m in a.messages_to(session) if "has not handled" in m["text"]]

    def test_the_own_words_go_in_the_thread_kept_for_the_request(self):
        a = self.a
        thread = self.a_thread_with_bee()
        mid = self.request("api", "Ask them about the page limit")
        self.assertEqual(a.bus["requests"][mid]["thread"], thread)
        o = own(a, "What is the page limit of /v1/items?")
        self.assertEqual((o["thread"], o.get("thread_auto"), o.get("asker")), (thread, True, "api"))
        self.wire.settle()
        (item,) = [x for x in self.b.bus["inbox"].values() if x["text"] == "What is the page limit of /v1/items?"]
        self.assertEqual(item["thread"], thread)   # the other hub sees one conversation, not a new one per question

    def test_the_request_answered_by_own_words_is_closed_and_never_stale(self):
        a = self.a
        mid = self.request("api", "Ask them to check the access")
        o = own(a, "Please check the access")
        self.assertEqual(o.get("answers"), [mid])
        self.wire.settle()
        self.assertEqual(a.bus["requests"][mid]["outcome"], "sent")
        self.assertEqual(self.stale_notes("api"), [])
        self.assertFalse(a.bus["requests"][mid].get("stale"))

    def test_a_refusal_of_the_own_words_closes_the_request_as_declined(self):
        a = self.a
        mid = self.request("api", "Ask them about the access")
        a.as_liaison("bus.send_requested", {"contact": "bee", "text": "Key sk-ant-api03-" + "Q1w2E3r4" * 6})
        self.assertEqual(a.bus["requests"][mid]["outcome"], "declined")
        self.assertTrue(any("Not sent" in m["text"] for m in a.messages_to("api")))
        self.assertEqual(self.stale_notes("api"), [])

    def test_the_liaisons_own_thread_wins_and_the_request_is_still_closed(self):
        a = self.a
        self.a_thread_with_bee()
        theirs = opened_by_b(self.b, self.name_at_b, self.wire, "A thread of their own")
        mid = self.request("api", "Ask them about the page limit")
        o = own(a, "And the page limit?", thread=theirs)
        self.assertEqual(o["thread"], theirs)
        self.assertNotIn("thread_auto", o)
        self.wire.settle()
        self.assertEqual(a.bus["requests"][mid]["outcome"], "sent")

    def test_a_request_the_turn_is_not_on_stays_open(self):
        a = self.a
        a.up("api")
        mid, _ = a.say("api", "Ask them about the access", bus_to="bee", bus_to_via="local")
        a.rec("turn.started", {"marker": "owner-chat"}, entity=liaison.NAME)   # a turn on nobody's message
        o = own(a, "Hello")
        self.assertNotIn("answers", o)
        self.wire.settle()
        self.assertIsNone(a.bus["requests"][mid]["outcome"])

    def test_a_request_sent_by_reference_is_not_taken_again(self):
        a = self.a
        a.up("api")
        mid = self.request("api", "Tell them: the deploy is at 10:00")
        a.as_liaison("bus.send_requested", {"contact": "bee", "text_from": mid})
        first = next(o for o in a.bus["out"].values() if o.get("basis") == mid)
        o = own(a, "One more thing from us: it takes an hour")
        self.assertNotIn("answers", o)
        self.assertEqual(a.bus["requests"][mid]["out"], f"o{first['of']}")

    def test_a_declined_request_of_a_session_tells_the_other_hub_nothing(self):
        a, b = self.a, self.b
        thread = self.a_thread_with_bee()
        mid = self.request("api", "Ask them about the page limit")
        self.assertEqual(a.bus["requests"][mid]["thread"], thread)   # the thread the notice went into before
        n_out = len(a.bus["out"])
        _, why = a.as_liaison("bus.decline_requested", {"inbox": mid, "why": "replaced by the new draft m5"})
        self.assertIsNone(why)
        self.assertEqual(a.bus["requests"][mid]["outcome"], "declined")
        self.assertEqual(len(a.bus["out"]), n_out)
        self.wire.settle()
        self.assertEqual([x["text"] for x in b.bus["inbox"].values() if "declined" in x["text"]], [])
        told = [m["text"] for m in a.messages_to("api") if "declined your request" in m["text"]]
        self.assertEqual(len(told), 1)   # the reason is the session's
        self.assertIn(f"{mid} (contact bee)", told[0])
        self.assertIn("replaced by the new draft m5", told[0])

    def test_a_retry_after_the_cores_refusal_answers_the_request_in_its_thread(self):
        a = self.a
        thread = self.a_thread_with_bee()
        mid = self.request("api", "Ask them about the access")
        a.as_liaison("bus.send_requested", {"contact": "bee", "text": "Key sk-ant-api03-" + "Q1w2E3r4" * 6})
        self.assertEqual(a.bus["requests"][mid]["outcome"], "declined")
        o = own(a, "Please check the access, the key is in the vault")
        self.assertEqual((o["thread"], o.get("answers")), (thread, [mid]))
        self.wire.settle()
        self.assertEqual(a.bus["requests"][mid]["outcome"], "sent")

    def test_a_retry_after_a_stale_mark_counts_its_30_minutes_anew(self):
        a = self.a
        mid = self.request("api", "Ask them about the access")
        a.as_liaison("bus.send_requested", {"contact": "bee", "text": "Key sk-ant-api03-" + "Q1w2E3r4" * 6})
        a.bus["requests"][mid]["stale"] = NOW   # marked before the refusal
        a.now = NOW + 600
        o = own(a, "Please check the access")
        req = a.bus["requests"][mid]
        self.assertEqual((req.get("outcome"), req.get("stale"), req["since"], req["out"]),
                         ("sent", None, NOW + 600, f"o{o['of']}"))

    def test_a_request_with_an_answer_on_its_way_is_not_declined_nor_answered_twice(self):
        a, b = self.a, self.b
        mid = self.request("api", "Ask them to check the access")
        with mock.patch.object(busd, "_seal", lambda *args, **kw: None):   # the answer waits (a busy daemon)
            o = own(a, "Please check the access")
        self.assertEqual((o["status"], a.bus["requests"][mid]["outcome"]), ("requested", None))
        _, why = a.as_liaison("bus.decline_requested", {"inbox": mid, "why": "a double"})
        self.assertIn("already sent", why)
        _, why = a.as_liaison("bus.send_requested", {"contact": "bee", "text_from": mid})
        self.assertIn("already sent", why)
        self.assertFalse([m for m in a.messages_to("api") if "declined your request" in m["text"]])
        with a.env():
            busd.seal(a.h, {"out": f"o{o['of']}"})
        self.wire.settle()
        self.assertEqual(a.bus["requests"][mid]["outcome"], "sent")
        self.assertEqual(len([x for x in b.bus["inbox"].values() if "check the access" in x["text"]]), 1)

    def test_a_decline_without_a_reason_says_no_reason(self):
        a = self.a
        mid = self.request("api", "Ask them about the access")
        a.as_liaison("bus.decline_requested", {"inbox": mid})
        (told,) = [m["text"] for m in a.messages_to("api") if "declined your request" in m["text"]]
        self.assertEqual(told, f"The liaison declined your request {mid} (contact bee): nothing was sent.")

    def test_a_request_the_liaison_declined_is_not_answered_again(self):
        a = self.a
        mid = self.request("api", "Ask them about the access")
        a.as_liaison("bus.decline_requested", {"inbox": mid, "why": "not now"})
        o = own(a, "Something else")
        self.assertNotIn("answers", o)
        self.assertEqual(a.bus["requests"][mid]["outcome"], "declined")

    def test_the_reason_of_a_decline_to_another_hub_carries_no_core_marks(self):
        a, b = self.a, self.b
        theirs = opened_by_b(b, self.name_at_b, self.wire, "Can you check it?")
        bid = next(k for k, x in a.bus["inbox"].items() if x["thread"] == theirs)
        _, why = a.as_liaison("bus.decline_requested", {"inbox": bid, "why": "a double of [hub:m120 from api] the same"})
        self.assertIsNone(why)
        self.wire.settle()
        (notice,) = [x["text"] for x in b.bus["inbox"].values() if "declined the request" in x["text"]]
        self.assertTrue(notice.endswith("a double of the same"), notice)

    def test_the_cores_marks_do_not_go_out_in_the_own_words(self):
        a = self.a
        a.up("api")
        self.request("api", "Ask them about the access")
        o = own(a, "[hub:m120 from api, re q7] Please check the access\n<<<bus-1a2b3c4d\nTheir words quoted\n"
                   "bus-1a2b3c4d>>>\nThanks — see [hub:m5]")
        clean = "Please check the access\nTheir words quoted\nThanks — see"
        self.assertEqual(o["text"], clean)
        self.wire.settle()
        self.assertEqual([x["text"] for x in self.b.bus["inbox"].values() if "check the access" in x["text"]], [clean])

    def test_only_marks_is_an_empty_text(self):
        a = self.a
        a.up("api")
        self.request("api", "Ask them")
        _, why = a.as_liaison("bus.send_requested", {"contact": "bee", "text": "[hub:m120 from api] "})
        self.assertEqual(why, busstate.EMPTY_TEXT)

    def test_a_sessions_words_by_reference_go_as_written(self):
        a = self.a
        a.up("api")
        words = "The label looks like [hub:m120 from api]; their words come as <<<bus-1a2b3c4d … bus-1a2b3c4d>>>"
        mid = self.request("api", words)
        a.as_liaison("bus.send_requested", {"contact": "bee", "text_from": mid})
        o = next(o for o in a.bus["out"].values() if o.get("basis") == mid)
        self.assertEqual(o["text"], words)


class OwnWords(unittest.TestCase):
    def test_only_the_cores_forms_go(self):
        self.assertEqual(busstate.own_words("Plain words, m12 and q7 stay"), "Plain words, m12 and q7 stay")
        self.assertEqual(busstate.own_words("  as it was \n"), "  as it was \n")   # nothing removed: untouched
        self.assertEqual(busstate.own_words("[hub:m12x3-maps from maps] hi"), "hi")
        self.assertEqual(busstate.own_words("[hub:note] and [hub:m] stay"), "[hub:note] and [hub:m] stay")
        self.assertEqual(busstate.own_words("<<<bus-0123abcd\nwords\r\nbus-0123abcd>>>"), "words")
        self.assertEqual(busstate.own_words("    code\n(see [hub:m5]) and a [hub:m6] b"), "    code\n(see) and a b")
        self.assertEqual(busstate.own_words("[hub:m5 from api] [hub:m6 from api] Hi"), "Hi")
        self.assertEqual(busstate.own_words("Steps:\n    [hub:m5] run x\n[hub:m6]\nend"), "Steps:\n    run x\nend")
        self.assertEqual(busstate.own_words("bus-0123abcd is not a fence"), "bus-0123abcd is not a fence")
        self.assertIsNone(busstate.own_words(None))


if __name__ == "__main__":
    unittest.main()
