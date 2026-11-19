"""A session's words to another hub continue the thread it got that hub's words in (spec d2423478): the core keeps the
thread on the session's request to the liaison — the one of another hub's words the session's turn is on, else the
latest thread that hub's words came to it in — while the answer in it would still come back to that session (alive,
under 50 messages, no other session's source). The liaison's own `--thread` wins; at sealing the core checks once more,
so the envelope never goes into a thread another session took meanwhile."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest
from unittest import mock

from core import busd, busstate
from tests.test_hubbus import need_lib, need_ssh
from tests.test_hubbus_s1 import LiveHub, Wire, acquainted


def ask(a, wire, session, text, thread=None, contact="bee"):
    """A session of A writes to another hub through the liaison (`--text-from`); -> the outgoing record. A session
    already up is left as it is: it may be in a turn."""
    if session not in a.st["sessions"]:
        a.up(session)
    mid, why = a.say(session, text, bus_to=contact, bus_to_via="local")
    assert why is None, why
    _, why = a.as_liaison("bus.send_requested", {"contact": contact, "text_from": mid,
                                                  **({"thread": thread} if thread else {})})
    assert why is None, why
    for card in a.cards():
        a.answer(card, "send")
    wire.settle()
    return next(o for o in a.bus["out"].values() if o.get("basis") == mid)


def answer(b, name_at_b, wire, thread, text):
    """B's liaison answers in the thread (its own words): at A it is an answer in the thread of A's session."""
    _, why = b.as_liaison("bus.send_requested", {"contact": name_at_b, "thread": thread, "text": text})
    assert why is None, why
    for card in b.cards():
        b.answer(card, "send")
    wire.settle()


def opened_by_b(b, name_at_b, wire, text):
    """B's liaison opens a new thread with A (its own words); -> the thread id."""
    _, why = b.as_liaison("bus.send_requested", {"contact": name_at_b, "text": text})
    assert why is None, why
    for card in b.cards():
        b.answer(card, "send")
    wire.settle()
    return [o for o in b.bus["out"].values() if o.get("kind") == "message"][-1]["thread"]


def delivered(a, session, text):
    """The message the core made for the session with another hub's words `text`."""
    return next(m for m in reversed(a.messages_to(session)) if m.get("channel") == "hubbus" and text in m["text"])


def item_of(a, text):
    return next(k for k, x in a.bus["inbox"].items() if x["text"] == text)


@need_lib
@need_ssh
class AnAnswerContinuesTheThread(unittest.TestCase):
    def setUp(self):
        self.a, self.b, self.wire = acquainted()
        self.name_at_b = next(iter(self.b.bus["contacts"]))

    def turn_on(self, session, *messages):
        self.a.rec("turn.started", {"marker": messages[0], **({"also": list(messages[1:])} if messages[1:] else {})},
                   entity=session)

    def test_the_answer_to_a_direct_answer_goes_in_the_same_thread_and_comes_back_again(self):
        a, b = self.a, self.b
        first = ask(a, self.wire, "api", "Which schema does /v1/items take?")
        thread = first["thread"]
        answer(b, self.name_at_b, self.wire, thread, "Schema v2")
        msg = delivered(a, "api", "Schema v2")
        self.turn_on("api", msg["id"])
        second = ask(a, self.wire, "api", "And the limit per page?")
        self.assertEqual(second["thread"], thread)
        self.assertNotEqual(second["id"], thread)
        self.assertTrue(second.get("thread_auto"))
        (theirs,) = [x for x in b.bus["inbox"].values() if x["text"] == "And the limit per page?"]
        self.assertEqual(theirs["thread"], thread)                       # B sees it in the same thread
        answer(b, self.name_at_b, self.wire, thread, "50 per page")
        self.assertEqual(a.bus["inbox"][item_of(a, "50 per page")]["passed_to"], ["api"])   # straight to api again
        self.assertEqual(a.bus["threads"][thread]["n"], 4)

    def test_the_thread_of_the_words_the_turn_is_on_beats_the_latest_one(self):
        a, b = self.a, self.b
        t1 = ask(a, self.wire, "api", "first question")["thread"]
        t2 = ask(a, self.wire, "api", "second question")["thread"]
        answer(b, self.name_at_b, self.wire, t1, "answer one")
        answer(b, self.name_at_b, self.wire, t2, "answer two")
        self.turn_on("api", delivered(a, "api", "answer one")["id"])
        self.assertEqual(ask(a, self.wire, "api", "about answer one")["thread"], t1)

    def test_a_finished_turn_does_not_choose_the_thread(self):
        a, b = self.a, self.b
        t1 = ask(a, self.wire, "api", "first question")["thread"]
        t2 = ask(a, self.wire, "api", "second question")["thread"]
        answer(b, self.name_at_b, self.wire, t1, "answer one")
        answer(b, self.name_at_b, self.wire, t2, "answer two")
        self.turn_on("api", delivered(a, "api", "answer one")["id"])
        a.rec("turn.result", {"text": "done", "turns": 1}, entity="api")
        self.assertNotIn(a.st["sessions"]["api"]["state"], ("busy", "dialog"))
        self.assertEqual(ask(a, self.wire, "api", "a script's words, no turn")["thread"], t2)

    def test_words_the_liaison_still_has_to_decide_in_the_thread_stay_open(self):
        a, b = self.a, self.b
        thread = opened_by_b(b, self.name_at_b, self.wire, "Question one")
        a.up("api")
        _, why = a.as_liaison("bus.pass_requested", {"inbox": item_of(a, "Question one"), "session": "api"})
        self.assertIsNone(why)
        answer(b, self.name_at_b, self.wire, thread, "Question two")   # nobody's thread yet: to the liaison
        waiting = item_of(a, "Question two")
        self.assertIsNone(a.bus["inbox"][waiting]["outcome"])
        self.turn_on("api", delivered(a, "api", "Question one")["id"])
        self.assertEqual(ask(a, self.wire, "api", "answer to one")["thread"], thread)
        self.assertIsNone(a.bus["inbox"][waiting]["outcome"])           # the liaison still decides on it

    def test_words_of_another_hub_from_two_threads_in_the_turn_leave_it_to_the_latest_thread(self):
        a, b = self.a, self.b
        t1, t2, t3 = (ask(a, self.wire, "api", f"question {i}")["thread"] for i in (1, 2, 3))
        for t, text in ((t1, "one"), (t2, "two"), (t3, "three")):
            answer(b, self.name_at_b, self.wire, t, text)
        self.turn_on("api", delivered(a, "api", "one")["id"], delivered(a, "api", "two")["id"])
        self.assertEqual(ask(a, self.wire, "api", "to both")["thread"], t3)

    def test_later_words_in_another_turn_go_in_the_latest_thread_that_hub_wrote_to_the_session_in(self):
        a, b = self.a, self.b
        thread = ask(a, self.wire, "api", "please check the access")["thread"]
        answer(b, self.name_at_b, self.wire, thread, "checking")
        mid, _ = a.say("api", "an executor's report", to="api")
        self.turn_on("api", mid)
        self.assertEqual(ask(a, self.wire, "api", "access is fine on our side too")["thread"], thread)

    def test_words_passed_by_the_liaison_from_a_thread_the_other_hub_opened_are_answered_in_it(self):
        a, b = self.a, self.b
        thread = opened_by_b(b, self.name_at_b, self.wire, "Can you share the build log?")
        a.up("api")
        _, why = a.as_liaison("bus.pass_requested", {"inbox": item_of(a, "Can you share the build log?"), "session": "api"})
        self.assertIsNone(why)
        self.turn_on("api", delivered(a, "api", "Can you share the build log?")["id"])
        self.assertEqual(ask(a, self.wire, "api", "here it is")["thread"], thread)
        self.assertEqual(a.bus["threads"][thread]["source"], "api")      # its answers now come to api

    def test_a_session_that_got_nothing_from_that_hub_opens_a_new_thread(self):
        o = ask(self.a, self.wire, "api", "hello")
        self.assertEqual(o["thread"], o["id"])
        self.assertFalse(o.get("thread_auto"))

    def test_the_liaisons_own_thread_wins(self):
        a, b = self.a, self.b
        t1 = ask(a, self.wire, "api", "first question")["thread"]
        t2 = ask(a, self.wire, "api", "second question")["thread"]
        answer(b, self.name_at_b, self.wire, t2, "answer two")
        self.turn_on("api", delivered(a, "api", "answer two")["id"])
        o = ask(a, self.wire, "api", "back to the first", thread=t1)
        self.assertEqual(o["thread"], t1)
        self.assertFalse(o.get("thread_auto"))

    def test_an_old_thread_is_not_continued(self):
        a, b = self.a, self.b
        thread = ask(a, self.wire, "api", "question")["thread"]
        answer(b, self.name_at_b, self.wire, thread, "answer")
        self.turn_on("api", delivered(a, "api", "answer")["id"])
        a.now = b.now = a.now + busstate.THREAD_TTL + 60
        o = ask(a, self.wire, "api", "two weeks later")
        self.assertEqual(o["thread"], o["id"])

    def test_a_thread_that_cannot_take_one_more_answer_is_not_continued(self):
        a, b = self.a, self.b
        thread = ask(a, self.wire, "api", "question")["thread"]
        answer(b, self.name_at_b, self.wire, thread, "answer")
        msg = delivered(a, "api", "answer")
        for n, same in ((busstate.THREAD_MAX - 1, True), (busstate.THREAD_MAX, False), (busstate.THREAD_MAX + 1, False)):
            a.bus["threads"][thread]["n"] = n
            self.turn_on("api", msg["id"])
            o = ask(a, self.wire, "api", f"at {n}")
            self.assertEqual(o["thread"] == thread, same, n)

    def test_an_answer_at_fifty_messages_is_still_delivered_to_the_source(self):
        a, b = self.a, self.b
        thread = ask(a, self.wire, "api", "question")["thread"]
        a.bus["threads"][thread]["n"] = busstate.THREAD_MAX - 1
        answer(b, self.name_at_b, self.wire, thread, "answer at fifty")
        self.assertEqual(a.bus["inbox"][item_of(a, "answer at fifty")]["passed_to"], ["api"])

    def test_a_thread_whose_answers_go_to_another_session_is_not_taken(self):
        a, b = self.a, self.b
        thread = ask(a, self.wire, "docs", "question of docs")["thread"]
        answer(b, self.name_at_b, self.wire, thread, "for docs")
        a.up("api")
        _, why = a.as_liaison("bus.pass_requested", {"inbox": item_of(a, "for docs"), "session": "api"})
        self.assertIsNone(why)
        self.turn_on("api", delivered(a, "api", "for docs")["id"])
        o = ask(a, self.wire, "api", "words of api")
        self.assertEqual(o["thread"], o["id"])
        self.assertEqual(a.bus["threads"][thread]["source"], "docs")

    def test_a_thread_another_session_took_while_the_request_waited_is_left_at_sealing(self):
        a, b = self.a, self.b
        thread = opened_by_b(b, self.name_at_b, self.wire, "Who owns the cache?")
        bid = item_of(a, "Who owns the cache?")
        for s in ("api", "docs"):
            a.up(s)
            _, why = a.as_liaison("bus.pass_requested", {"inbox": bid, "session": s})
            self.assertIsNone(why)
        self.turn_on("api", delivered(a, "api", "Who owns the cache?")["id"])
        mid, _ = a.say("api", "api owns it", bus_to="bee", bus_to_via="local")
        with mock.patch.object(busd, "_seal", lambda *args, **kw: None):   # the request waits (a card, a busy daemon)
            _, why = a.as_liaison("bus.send_requested", {"contact": "bee", "text_from": mid})
        self.assertIsNone(why)
        oid = next(k for k, o in a.bus["out"].items() if o.get("basis") == mid)
        self.assertEqual((a.bus["out"][oid]["thread"], a.bus["out"][oid]["status"]), (thread, "requested"))
        self.turn_on("docs", delivered(a, "docs", "Who owns the cache?")["id"])
        self.assertEqual(ask(a, self.wire, "docs", "docs owns it")["thread"], thread)
        self.assertEqual(a.bus["threads"][thread]["source"], "docs")
        with a.env():
            busd.seal(a.h, {"out": oid})
        o = a.bus["out"][oid]
        self.assertEqual((o["status"], o["thread"]), ("signed", o["id"]))   # a new thread in the record ...
        self.wire.settle()
        (theirs,) = [x for x in b.bus["inbox"].values() if x["text"] == "api owns it"]
        self.assertEqual(theirs["thread"], o["id"])                      # ... and in the envelope B read
        self.assertEqual(a.bus["threads"][o["id"]]["source"], "api")

    def test_a_thread_of_the_owners_own_words_is_never_taken(self):
        a = self.a
        th = a.bus["threads"]["f" * 32] = {"contact": "bee", "source": None, "ts": a.now, "n": 1, "owner": True}
        self.assertFalse(busstate.auto_thread_ok(a.bus, "f" * 32, "bee", "api", a.now))
        del th["owner"]   # the same thread without the mark is fine: the mark alone keeps it out
        self.assertTrue(busstate.auto_thread_ok(a.bus, "f" * 32, "bee", "api", a.now))


@need_lib
@need_ssh
class TwoHubs(unittest.TestCase):
    def test_words_to_one_hub_never_take_the_thread_of_another_hub(self):
        a, b, wire = acquainted()
        c = LiveHub("Hub C")
        a.ask("bus.contact_requested", {"op": "add", "name": "cee", "card": c.card(), "code": c.code()})
        wire = Wire(a, b, c)
        wire.settle()
        (card,) = c.cards()
        c.answer(card, "accept")
        wire.settle()
        name_at_b, name_at_c = next(iter(b.bus["contacts"])), next(iter(c.bus["contacts"]))
        tb = ask(a, wire, "api", "to bee")["thread"]
        tc = ask(a, wire, "api", "to cee", contact="cee")["thread"]
        answer(b, name_at_b, wire, tb, "from bee")
        answer(c, name_at_c, wire, tc, "from cee")
        a.rec("turn.started", {"marker": delivered(a, "api", "from bee")["id"],
                               "also": [delivered(a, "api", "from cee")["id"]]}, entity="api")
        self.assertEqual(ask(a, wire, "api", "back to bee")["thread"], tb)
        self.assertEqual(ask(a, wire, "api", "back to cee", contact="cee")["thread"], tc)


if __name__ == "__main__":
    unittest.main()
