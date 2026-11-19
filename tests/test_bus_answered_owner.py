"""Another hub's answer to a request a session sent in a turn of the owner's is an answer to him: the session's result
on it is in "Answered you" (owner_reply), as an executor's answer to his task (owner decision 18.11). The owner asks
a session "find out from the other hub X"; the session asks over the bus; the answer comes back to it in the thread;
its result ("the other hub says…") went to the folded "Sent on their own" and was gone in a day. The mark is the
core's: the session's turn when it wrote the request — never the text. A request from a turn without the owner, news
from another hub and the liaison stay as they were; the answer is never the owner's word (no from_owner, re,
answers_question; turn_owner and turn_his stay unset)."""
import tests  # noqa: F401 — do not see the machine's settings
import unittest

from core import busstate, liaison, reducer

CONTACT, FP, THREAD = "maps-1", "hb1-abcdefghijklmnopqrstuvwxyz", "0e157fdd8b36c16f825af069b1b2842a"
S = {"channel": "session", "owner": False}
PERSON = {"id": "c0000a0b1", "name": "Pat Lee"}


class BusAnswerIsAnsweredYou(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        for n, extra in (("api", {}), ("web", {}), ("hub", {"kind": "hub"}), (liaison.NAME, {})):
            self.ev("session.requested", n, cwd=f"/{n}", session_id=n, channel="web", owner=True, **extra)
            self.ev("session.started", n)
        self.st["sessions"][liaison.NAME]["liaison"] = True
        self.st["bus"] = busstate.empty()
        self.st["bus"].update(enabled=True, crypto_ready=True, me={"fp": "hb1-me", "label": "Hub"})
        self.st["bus"]["contacts"][CONTACT] = {"fp": FP, "label": "Other hub", "status": "known", "verified": True}
        self.n = 0

    def ev(self, type_, entity=None, src="session", **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "src": src, "type": type_,
                                                   "entity": entity, "data": data})
        self.assertIsNone(why, (type_, why))
        return self.seq

    def owner_says(self, to, text="Find out from the other hub whether thin wires will do"):
        return f"m{self.ev('message.received', None, src='web', to=to, text=text, channel='web', owner=True)}"

    def turn(self, name, marker, result=None):
        self.ev("turn.started", name, src="hook", marker=marker)
        if result is not None:
            return self.end(name, result)

    def end(self, name, result):
        seq = self.ev("turn.result", name, src="hook", text=result)
        self.ev("turn.stopped", name, src="hook")
        return seq

    def to_liaison(self, sender, text="Ask the other hub: will thin wires do?"):
        return f"m{self.ev('message.received', None, to=liaison.NAME, text=text, **S, sender=sender)}"

    def liaison_sends(self, marker, by_reference=True, thread=None):
        """The liaison's turn on the session's request: its words by reference (or its own), signed in the thread."""
        self.turn(liaison.NAME, marker)
        what = {"text_from": marker} if by_reference else {"text": "Will thin wires do for a prototype?"}
        sent = self.ev("bus.send_requested", None, contact=CONTACT, **what, **({"thread": thread} if thread else {}),
                       **S, sender=liaison.NAME)
        self.ev("core.bus_out", None, src="core", out=f"o{sent}", kind="message", id=thread or THREAD, contact=CONTACT,
                fp=FP, exp=9e9, approved="auto", seq=1, cls="liaison")
        self.end(liaison.NAME, "Sent, the answer goes to the session")
        self.assertEqual(self.st["bus"]["out"][f"o{sent}"]["status"], "signed")

    def answer(self, text="Yes, for a first try thin wires do", thread=THREAD):
        self.n += 1
        seq = self.ev("core.bus_received", None, src="core", hub=FP, contact=CONTACT,
                      id=f"7511f7308a51caceb5ec566e9e04{self.n:04d}", exp=9e9, cls="liaison", status="ok",
                      kind="message", agent_from="glove", agent_to=None, thread=thread, in_reply_to=thread, seq=2,
                      text=text)
        return f"m{seq}"

    def asked_in_his_turn(self, by_reference=True):
        word = self.owner_says("api")
        self.turn("api", word)
        ask = self.to_liaison("api")
        self.end("api", "Asked the other hub, will tell you its answer")
        self.liaison_sends(ask, by_reference)
        return word

    def answered(self, name, seq):
        return self.st["sessions"][name].get("owner_reply") == seq

    def test_the_result_on_the_answer_to_a_request_from_his_turn_answers_him(self):
        self.asked_in_his_turn()
        got = self.answer()
        msg = self.st["messages"][got]
        self.assertEqual(msg["session"], "api")   # the thread's source, no liaison turn
        self.ev("turn.started", "api", src="hook", marker=got)
        sess = self.st["sessions"]["api"]
        # words of another hub, not his: nothing of the owner's word rides with them
        self.assertFalse(msg.get("from_owner") or msg.get("re") or msg.get("answers_question"))
        self.assertFalse(sess.get("turn_owner") or sess.get("turn_his"))
        result = self.end("api", "The other hub says: yes, thin wires do for a first try")
        self.assertTrue(self.answered("api", result))
        self.assertEqual(self.st["sessions"]["api"]["reply_head"], "The other hub says: yes, thin wires do for a first try")

    def test_the_liaisons_own_words_for_the_session_carry_the_mark_too(self):
        self.asked_in_his_turn(by_reference=False)
        result = self.turn("api", self.answer(), "The other hub says: yes")
        self.assertTrue(self.answered("api", result))

    def test_words_sent_straight_to_an_own_hub_carry_the_mark(self):
        self.st["bus"]["contacts"][CONTACT]["person"] = PERSON
        self.assertIsNone(busstate.direct_refusal(self.st, CONTACT))
        word = self.owner_says("api")
        self.turn("api", word)
        sent = self.ev("bus.direct_requested", None, contact=CONTACT, text="Will thin wires do?", via="local", **S,
                       sender="api")
        self.end("api", "Asked the other hub")
        self.ev("core.bus_out", None, src="core", out=f"o{sent}", kind="message", id=THREAD, contact=CONTACT, fp=FP,
                exp=9e9, approved="auto", seq=1, cls="liaison")
        result = self.turn("api", self.answer(), "The other hub says: yes")
        self.assertTrue(self.answered("api", result))

    def test_a_request_from_a_turn_without_the_owner_stays_sent_on_its_own(self):
        note = self.ev("message.received", None, to="api", text="Ask the other hub about tiles", **S, sender="web")
        self.turn("api", f"m{note}")   # another session's words, not his
        ask = self.to_liaison("api", "Ask the other hub about tiles")
        self.end("api", "Asked")
        self.liaison_sends(ask)
        result = self.turn("api", self.answer("Tiles are ready"), "The other hub says the tiles are ready")
        self.assertEqual(self.st["sessions"]["api"].get("owner_out"), result)   # new, as before
        self.assertFalse(self.answered("api", result))

    def test_news_from_another_hub_is_not_an_answer_to_him(self):
        self.asked_in_his_turn()   # a request of his turn is open in THREAD
        theirs = "5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a"
        letter = self.answer("News from us: the export is ready", thread=theirs)   # their own thread: to the liaison
        self.assertEqual(self.st["messages"][letter]["session"], liaison.NAME)
        bid = self.st["messages"][letter]["hubbus"]["inbox"]
        self.turn(liaison.NAME, letter)
        pass_ = self.ev("bus.pass_requested", None, inbox=bid, session="api", **S, sender=liaison.NAME)
        self.ev("core.bus_passed", None, src="core", inbox=bid, to="api", of=pass_)   # the daemon's fact
        self.end(liaison.NAME, "Passed it to api")
        (passed,) = [m for m in self.st["messages"].values() if m["session"] == "api" and m.get("channel") == "hubbus"]
        result = self.turn("api", passed["id"], "Noted: the other hub's export is ready")
        self.assertFalse(self.answered("api", result))

    def test_the_answer_passed_to_another_session_is_not_its_answer_to_him(self):
        self.asked_in_his_turn()
        self.st["sessions"]["api"]["state"] = "closed"   # the source is gone: the liaison decides whom to give it
        got = self.answer()
        self.assertEqual(self.st["messages"][got]["session"], liaison.NAME)
        bid = self.st["messages"][got]["hubbus"]["inbox"]
        self.turn(liaison.NAME, got)
        pass_ = self.ev("bus.pass_requested", None, inbox=bid, session="web", **S, sender=liaison.NAME)
        self.ev("core.bus_passed", None, src="core", inbox=bid, to="web", of=pass_)   # the daemon's fact
        self.end(liaison.NAME, "Passed it to web")
        (passed,) = [m for m in self.st["messages"].values() if m["session"] == "web" and m.get("channel") == "hubbus"]
        result = self.turn("web", passed["id"], "The other hub says: yes")
        self.assertFalse(self.answered("web", result))

    def test_a_later_request_in_the_thread_not_from_his_turn_takes_the_mark_away(self):
        self.asked_in_his_turn()
        self.turn("api", self.answer(), "The other hub says: yes")
        note = self.ev("message.received", None, to="api", text="Ask them about tiles too", **S, sender="web")
        self.turn("api", f"m{note}")
        again = self.to_liaison("api", "Ask them about tiles too")
        self.end("api", "Asked")
        self.liaison_sends(again, thread=THREAD)
        self.assertNotIn("in_owner_turn", self.st["bus"]["threads"][THREAD])
        result = self.turn("api", self.answer("Tiles are ready"), "The other hub says the tiles are ready")
        self.assertFalse(self.answered("api", result))

    def test_a_follow_up_from_the_answer_turn_keeps_the_mark(self):
        self.asked_in_his_turn()
        self.turn("api", self.answer("Which wires do you mean?"))   # its turn on the answer asks again in the thread
        again = self.to_liaison("api", "Thin copper ones")
        self.end("api", "Clarified to the other hub")
        self.liaison_sends(again, thread=THREAD)
        result = self.turn("api", self.answer("Then yes, they do"), "The other hub says: yes, thin copper ones do")
        self.assertTrue(self.answered("api", result))

    def test_a_request_from_a_turn_on_his_task_answers_him(self):
        word = self.owner_says("hub", "Have api find out from the other hub whether thin wires will do")
        self.turn("hub", word)
        task = self.ev("delegation.created", None, session="api", **{"for": word}, context="find out", **S, sender="hub")
        self.end("hub", "Gave it to api")
        self.turn("api", f"m{task}")
        ask = self.to_liaison("api")
        self.end("api", "Asked the other hub, will report its answer")   # the hand-in
        self.liaison_sends(ask)
        result = self.turn("api", self.answer(), "The other hub says: yes, thin wires do")
        self.assertTrue(self.answered("api", result))

    def test_a_request_from_a_turn_on_a_sessions_task_is_not_for_him(self):
        # an executor reports to whoever set the task (a mini-hub, another session): not to the owner over its head
        task = self.ev("delegation.created", None, session="api", for_session="web", context="find out", **S,
                       sender="web")
        self.turn("api", f"m{task}")
        ask = self.to_liaison("api")
        self.end("api", "Asked the other hub")
        self.liaison_sends(ask)
        result = self.turn("api", self.answer(), "The other hub says: yes, thin wires do")
        self.assertFalse(self.answered("api", result))

    def test_a_later_turn_of_the_answer_turn_without_a_marker_is_his_too(self):
        self.asked_in_his_turn()
        self.turn("api", self.answer(), "Checking what they said, in the background")
        self.ev("turn.started", "api", src="hook")   # Claude Code woke it: the background job ended
        result = self.end("api", "Checked: thin wires do")
        self.assertTrue(self.answered("api", result))

    def test_a_schedule_jobs_words_straight_to_an_own_hub_carry_no_mark(self):
        self.st["bus"]["contacts"][CONTACT]["person"] = PERSON
        self.turn("api", self.owner_says("api"))   # a job of the schedule writes on its behalf meanwhile
        sent = self.ev("bus.direct_requested", None, contact=CONTACT, text="Daily ping", via="local", **S, sender="api",
                       schedule_job="j1")
        self.assertNotIn("in_owner_turn", self.st["bus"]["out"][f"o{sent}"])

    def test_a_request_from_a_recurring_firings_turn_carries_no_mark(self):
        self.turn("api", self.owner_says("api", "Ask the other hub about tiles every morning"))
        self.st["sessions"]["api"]["turn_sched"] = True   # as the core marks a turn of a recurring job's firing (budget)
        ask = self.to_liaison("api", "Ask them about tiles")
        self.assertNotIn("in_owner_turn", self.st["messages"][ask])

    def test_the_liaisons_words_on_two_requests_one_not_from_his_turn_carry_no_mark(self):
        word = self.owner_says("api")
        self.turn("api", word)
        first = self.to_liaison("api")
        self.end("api", "Asked")
        note = self.ev("message.received", None, to="api", text="Ask them about tiles too", **S, sender="web")
        self.turn("api", f"m{note}")
        second = self.to_liaison("api", "And ask about tiles")
        self.end("api", "Asked")
        self.ev("turn.started", liaison.NAME, src="hook", marker=first, also=[second])
        sent = self.ev("bus.send_requested", None, contact=CONTACT, text="Thin wires, and tiles?", **S,
                       sender=liaison.NAME)
        self.assertEqual(self.st["bus"]["out"][f"o{sent}"].get("asker"), "api")
        self.assertNotIn("in_owner_turn", self.st["bus"]["out"][f"o{sent}"])

    def test_the_answer_given_to_its_source_marks_the_hub_answered_and_the_owner_opens_it(self):
        # "Now" keeps a hub that answered him until he opens it, on any device: the core marks the answer when it gives
        # it to the asking session, and keeps what he opened (owner.seen of ":hub:<contact>")
        from core import busflow
        self.asked_in_his_turn()
        got = self.answer()
        bid = self.st["messages"][got]["hubbus"]["inbox"]
        self.assertTrue(self.st["bus"]["inbox"][bid].get("answered_owner"))
        row = busflow.list_view(self.st)["contacts"][0]
        self.assertEqual((row["answered"], row["seen"]), (self.st["bus"]["inbox"][bid]["ts"], 0))
        hub = ":hub:" + CONTACT
        st, _, why = reducer.reduce(self.st, {"seq": 9001, "ts": 9001, "src": "session", "type": "owner.seen",
                                              "entity": hub, "data": {"ts": 5, **S, "sender": "api"}})
        self.assertIsNotNone(why)   # a session's "read" is not his
        self.ev("owner.seen", hub, src="web", ts=row["answered"], channel="web", owner=True)
        self.ev("owner.seen", hub, src="web", ts=1, channel="web", owner=True)   # an older mark does not move it back
        self.assertEqual(busflow.list_view(self.st)["contacts"][0]["seen"], row["answered"])
        for bad in ({"entity": ":hub:nobody", "ts": 5}, {"entity": hub, "ts": "x"}, {"entity": hub, "ts": True},
                    {"entity": hub, "ts": float("inf")}, {"entity": hub, "ts": float("nan")}, {"entity": hub, "ts": -1},
                    {"entity": hub, "ts": 9002 + 2 * 86400}):   # a mark from the future would hide every later answer
            st, _, why = reducer.reduce(self.st, {"seq": 9002, "ts": 9002, "src": "web", "type": "owner.seen",
                                                  "entity": bad["entity"], "data": {"ts": bad["ts"], "channel": "web",
                                                                                    "owner": True}})
            self.assertIsNotNone(why, bad)
        self.assertNotIn(hub, self.st["sessions"])

    def test_the_liaison_itself_gets_no_mark(self):
        word = self.owner_says(liaison.NAME, "Tell the other hub hello")
        self.turn(liaison.NAME, word)
        sent = self.ev("bus.send_requested", None, contact=CONTACT, text="Hello", **S, sender=liaison.NAME)
        self.ev("core.bus_out", None, src="core", out=f"o{sent}", kind="message", id=THREAD, contact=CONTACT, fp=FP,
                exp=9e9, approved="auto", seq=1, cls="liaison")
        self.end(liaison.NAME, "Said hello")
        self.assertNotIn("in_owner_turn", self.st["bus"]["out"][f"o{sent}"])
        got = self.answer("Hello back")
        self.assertNotIn("in_owner_turn", self.st["messages"][got]["hubbus"])


if __name__ == "__main__":
    unittest.main()
