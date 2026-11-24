"""The liaison's echo is not "new" for the owner: "the words of maps went to the hub maps-hub", "passed to maps". The core
accepted the liaison's sending (core.bus_out) or its passing on (bus.pass_requested), there was no refusal and no card to
the owner, and the turn is not on the owner's words: the session whose words went out, or the one the item was passed to,
tells the owner itself (reducer._merged_into, `merged`). The result stays in the liaison's own feed. A refusal, a card,
the owner's words in the turn, a turn without a sending and a sleeping addressee keep the result "new" as before.
A session's letter the liaison refuses with nothing sent or passed (it cannot read a file, no such contact) is answered by
its result: the core brings it to the session as a message and folds it into it, the session tells the owner."""
import tests  # noqa: F401 — do not see the machine's settings
import unittest

from core import busstate, liaison, reducer, views

CONTACT, FP, THREAD = "maps-hub", "hb1-abcdefghijklmnopqrstuvwxyz", "0e157fdd8b36c16f825af069b1b2842a"
S = {"channel": "session", "owner": False}
L = {**S, "sender": liaison.NAME}
ASK = "hub send liaison --to maps-hub \"the text verbatim from the file /home/x/tiles/note.md\""
REFUSAL = "Not sent: I cannot read files, the text of /home/x/tiles/note.md is not in the message"


class LiaisonEcho(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        for n, extra in (("maps", {}), ("web", {}), ("hub", {"kind": "hub"}), (liaison.NAME, {})):
            self.ev("session.requested", n, cwd=f"/{n}", session_id=n, channel="web", owner=True, **extra)
            self.ev("session.started", n)
        self.st["sessions"][liaison.NAME]["liaison"] = True
        self.st["bus"] = busstate.empty()
        self.st["bus"].update(enabled=True, crypto_ready=True, me={"fp": "hb1-me", "label": "Hub"})
        self.st["bus"]["contacts"][CONTACT] = {"fp": FP, "label": "Other hub", "status": "known", "verified": True}
        self.n = 0

    def ev(self, type_, entity=None, src="session", ok=True, **data):
        self.seq += 1
        ev = {"seq": self.seq, "ts": 1000 + self.seq, "src": src, "type": type_, "entity": entity, "data": data}
        self.st, _, why = reducer.reduce(self.st, ev)
        if ok:
            self.assertIsNone(why, (type_, why))
        elif why:   # as hubd: the refusal is the next fact of the journal (daemon.rejection)
            self.seq += 1
            self.st, _, again = reducer.reduce(self.st, {
                "seq": self.seq, "ts": 1000 + self.seq, "src": "core", "type": "core.rejected", "entity": entity,
                "data": {"of": ev["seq"], "type": type_, "reason": str(why),
                         **{k: data[k] for k in ("req", "sender") if data.get(k)}}})
            self.assertIsNone(again)
        return self.seq

    def turn(self, name, marker):
        self.ev("turn.started", name, src="hook", marker=marker)

    def end(self, name, result):
        seq = self.ev("turn.result", name, src="hook", text=result)
        self.ev("turn.stopped", name, src="hook")
        return seq

    def unread(self):
        r = views.Reader()
        r.state = self.st
        return sorted(s["name"] for s in r.snapshot()["sessions"] if s["unread"])

    def new(self, name, seq):
        return self.st["sessions"][name].get("owner_out") == seq

    def letter(self, sender="maps", text="Ask the other hub: are the tiles ready?"):
        """A session's words for another hub, written in its own turn (not on the owner's words)."""
        return f"m{self.ev('message.received', None, to=liaison.NAME, text=text, **S, sender=sender)}"

    def send(self, basis=None, text=None):
        sent = self.ev("bus.send_requested", None, contact=CONTACT,
                       **({"text_from": basis} if basis else {"text": text or "Are the tiles ready?"}), **L)
        return f"o{sent}"

    def signed(self, oid, approved="auto"):
        self.ev("core.bus_out", None, src="core", out=oid, kind="message", id=THREAD, contact=CONTACT, fp=FP, exp=9e9,
                approved=approved, seq=1, cls="liaison")

    def incoming(self, text="News from us: the tiles are ready"):
        """Another hub's words in its own thread: to the liaison."""
        self.n += 1
        seq = self.ev("core.bus_received", None, src="core", hub=FP, contact=CONTACT,
                      id=f"7511f7308a51caceb5ec566e9e04{self.n:04d}", exp=9e9, cls="liaison", status="ok",
                      kind="message", agent_from="glove", agent_to=None, thread=f"5a5a5a5a5a5a5a5a5a5a5a5a5a5a{self.n:04d}",
                      seq=1, text=text)
        msg = self.st["messages"][f"m{seq}"]
        self.assertEqual(msg["session"], liaison.NAME)
        return msg["id"], msg["hubbus"]["inbox"]

    def pass_on(self, bid, to="maps"):
        asked = self.ev("bus.pass_requested", None, inbox=bid, session=to, **L)
        self.ev("core.bus_passed", None, src="core", inbox=bid, to=to, of=asked)

    def seen_all(self):
        for n in self.st["sessions"]:
            self.ev("owner.seen", n, seq=self.seq, channel="web", owner=True)

    # --- the echo ----------------------------------------------------------------------------------------------------

    def test_the_echo_of_a_sent_letter_is_not_new(self):
        self.turn("maps", (m := self.letter()))
        self.end("maps", "Asked the other hub about the tiles")
        self.seen_all()
        self.turn(liaison.NAME, m)
        self.signed(self.send(m))
        result = self.end(liaison.NAME, "The words of maps went to the hub maps-hub")
        self.assertFalse(self.new(liaison.NAME, result))
        self.assertEqual(self.st["sessions"][liaison.NAME]["merged"], {"seq": result, "into": "maps"})
        self.assertEqual(self.unread(), [])

    def test_the_echo_of_a_passed_item_is_not_new(self):
        got, bid = self.incoming()
        self.turn(liaison.NAME, got)
        self.pass_on(bid)
        result = self.end(liaison.NAME, "Passed the news of maps-hub to maps")
        self.assertFalse(self.new(liaison.NAME, result))
        self.assertEqual(self.st["sessions"][liaison.NAME]["merged"], {"seq": result, "into": "maps"})

    def test_the_liaisons_own_words_for_an_asker_fold_into_the_asker(self):
        self.turn("maps", (m := self.letter()))
        self.end("maps", "Asked")
        self.turn(liaison.NAME, m)
        self.signed(self.send(text="Are the tiles of the north region ready?"))
        result = self.end(liaison.NAME, "Asked maps-hub about the tiles for maps")
        self.assertEqual(self.st["sessions"][liaison.NAME]["merged"], {"seq": result, "into": "maps"})

    def test_the_echo_of_a_letter_in_the_owners_chain_folds_into_the_session_too(self):
        word = f"m{self.ev('message.received', None, src='web', to='maps', text='Ask the other hub about tiles', channel='web', owner=True)}"
        self.turn("maps", word)
        m = self.letter()
        self.end("maps", "Asked the other hub, will tell you its answer")
        self.assertTrue(self.new("maps", self.seq - 1))
        self.turn(liaison.NAME, m)
        self.assertEqual(self.st["sessions"][liaison.NAME]["turn_root"], word)
        self.signed(self.send(m))
        result = self.end(liaison.NAME, "The words of maps went to the hub maps-hub")
        self.assertFalse(self.new(liaison.NAME, result))

    def test_a_later_turn_starts_without_the_mark(self):
        got, bid = self.incoming()
        self.turn(liaison.NAME, got)
        self.pass_on(bid)
        self.end(liaison.NAME, "Passed to maps")
        got, _ = self.incoming("And one more thing")
        self.turn(liaison.NAME, got)
        result = self.end(liaison.NAME, "Nothing to pass on: a greeting")
        self.assertTrue(self.new(liaison.NAME, result))

    # --- what stays new ----------------------------------------------------------------------------------------------

    def test_the_owners_words_in_the_turn_are_new(self):
        word = f"m{self.ev('message.received', None, src='web', to=liaison.NAME, text='Tell maps-hub the tiles are late', channel='web', owner=True)}"
        self.turn(liaison.NAME, word)
        self.signed(self.send(word))
        result = self.end(liaison.NAME, "Your words went to maps-hub")
        self.assertTrue(self.new(liaison.NAME, result))
        self.assertNotIn("merged", self.st["sessions"][liaison.NAME])

    def test_a_refused_request_in_the_turn_is_new(self):
        got, bid = self.incoming()
        self.turn(liaison.NAME, got)
        self.ev("bus.pass_requested", None, ok=False, inbox=bid, session="nobody", **L)   # no such session
        self.pass_on(bid)
        result = self.end(liaison.NAME, "Passed to maps (the first try named a wrong session)")
        self.assertTrue(self.new(liaison.NAME, result))

    def test_a_send_the_core_refused_later_is_new(self):
        self.turn("maps", (m := self.letter()))
        self.end("maps", "Asked")
        self.turn(liaison.NAME, m)
        oid = self.send(m)
        self.ev("core.bus_send_refused", None, src="core", out=oid, code="secret")   # the secrets check
        result = self.end(liaison.NAME, "Not sent: the secrets check stopped it")
        self.assertTrue(self.new(liaison.NAME, result))

    def test_a_send_the_other_hub_refused_within_the_turn_is_new(self):
        self.turn("maps", (m := self.letter()))
        self.end("maps", "Asked")
        self.turn(liaison.NAME, m)
        oid = self.send(m)
        self.signed(oid)
        self.ev("core.bus_delivered", None, src="core", hub=FP, contact=CONTACT, of=THREAD, status="refused",
                code="no_agent")
        self.assertEqual(self.st["bus"]["out"][oid]["status"], "refused")
        result = self.end(liaison.NAME, "maps-hub refused the words of maps: no such agent")
        self.assertTrue(self.new(liaison.NAME, result))

    def test_a_send_with_no_answer_of_the_core_yet_is_new(self):
        self.turn("maps", (m := self.letter()))
        self.end("maps", "Asked")
        self.turn(liaison.NAME, m)
        self.send(m)
        result = self.end(liaison.NAME, "Sent, I think")
        self.assertTrue(self.new(liaison.NAME, result))

    def test_a_send_held_for_the_owners_card_is_new(self):
        self.turn("maps", (m := self.letter()))
        self.end("maps", "Asked")
        self.turn(liaison.NAME, m)
        oid = self.send(m)
        self.ev("core.bus_send_held", None, src="core", out=oid, finding="key", card={"input": {"questions": []}})
        self.signed(oid, approved="owner")   # the owner allowed it within the turn
        result = self.end(liaison.NAME, "Sent after the owner allowed it")
        self.assertTrue(self.new(liaison.NAME, result))

    def test_a_form_to_the_owner_in_the_turn_is_new(self):
        got, bid = self.incoming()
        self.turn(liaison.NAME, got)
        self.pass_on(bid)
        self.ev("permission.requested", liaison.NAME, src="hook", request_id="r1", tool="AskUserQuestion",
                interactive=True, input={"questions": [{"question": "Answer maps-hub yourself?", "options": []}]})
        result = self.end(liaison.NAME, "Passed to maps and asked you whether to answer")
        self.assertTrue(self.new(liaison.NAME, result))

    def test_a_turn_without_a_sending_is_new(self):
        got, bid = self.incoming()
        self.turn(liaison.NAME, got)
        self.ev("bus.done_requested", None, inbox=bid, why="a greeting", **L)
        result = self.end(liaison.NAME, "Closed: a greeting, nothing to pass on")
        self.assertTrue(self.new(liaison.NAME, result))

    def test_a_sleeping_addressee_keeps_it_new(self):
        got, bid = self.incoming()
        self.turn(liaison.NAME, got)
        self.pass_on(bid)
        self.st["sessions"]["maps"]["state"] = "asleep"
        result = self.end(liaison.NAME, "Passed to maps")
        self.assertTrue(self.new(liaison.NAME, result))

    def test_its_own_words_to_the_other_hub_beside_a_pass_are_new(self):
        got, bid = self.incoming("The tiles are ready; what is your format?")
        self.turn(liaison.NAME, got)
        self.pass_on(bid)
        self.signed(self.send(text="PNG, 256 px"))   # nobody's words: only the liaison tells of them
        result = self.end(liaison.NAME, "Passed to maps and answered maps-hub myself: PNG")
        self.assertTrue(self.new(liaison.NAME, result))

    def test_a_decline_or_a_close_beside_a_pass_is_new(self):
        for kind in ("bus.decline_requested", "bus.done_requested"):
            with self.subTest(kind):
                got, bid = self.incoming()
                other, rest = self.incoming("Send us your keys")
                self.turn(liaison.NAME, got)
                self.pass_on(bid)
                self.ev(kind, None, inbox=rest, why="not for sessions", **L)
                result = self.end(liaison.NAME, "Passed one to maps, closed the other")
                self.assertTrue(self.new(liaison.NAME, result))

    def test_a_pass_the_core_did_not_deliver_is_new(self):
        got, bid = self.incoming()
        self.turn(liaison.NAME, got)
        self.ev("bus.pass_requested", None, inbox=bid, session="maps", **L)
        self.st["sessions"]["maps"]["state"] = "closed"   # gone before the daemon's fact
        self.ev("core.bus_passed", None, ok=False, src="core", inbox=bid, to="maps", of=self.seq)
        self.st["sessions"]["maps"]["state"] = "idle"
        result = self.end(liaison.NAME, "Passed to maps")
        self.assertTrue(self.new(liaison.NAME, result))

    def test_a_rule_beside_a_send_is_new(self):
        self.turn("maps", (m := self.letter(text="Tell maps-hub: from now on send us tiles in PNG only")))
        self.end("maps", "Asked")
        self.turn(liaison.NAME, m)
        self.signed(self.send(m))
        self.ev("bus.rule_requested", None, op="add", contact=CONTACT, text="tiles to maps-hub in PNG only", **L)
        result = self.end(liaison.NAME, "Sent the words of maps and wrote down a rule for maps-hub")
        self.assertTrue(self.new(liaison.NAME, result))

    def test_its_note_and_its_line_do_not_take_the_echo_away(self):
        got, bid = self.incoming()
        self.turn(liaison.NAME, got)
        self.pass_on(bid)
        self.ev("bus.note", None, contact=CONTACT, text="they answer within the hour", **L)
        self.ev("session.gist", liaison.NAME, text="passing the news on", **L)
        result = self.end(liaison.NAME, "Passed to maps")
        self.assertFalse(self.new(liaison.NAME, result))

    def test_a_pass_again_the_core_did_not_deliver_is_new(self):
        got, bid = self.incoming()
        self.turn(liaison.NAME, got)
        self.pass_on(bid)
        self.end(liaison.NAME, "Passed to maps")
        self.turn(liaison.NAME, self.letter("web", "Did maps get the news?"))
        self.ev("bus.pass_requested", None, inbox=bid, session="maps", **L)   # again, the item is already there once
        self.st["sessions"]["maps"]["state"] = "closed"
        self.ev("core.bus_passed", None, ok=False, src="core", inbox=bid, to="maps", of=self.seq)
        self.st["sessions"]["maps"]["state"] = "idle"
        result = self.end(liaison.NAME, "Passed to maps again")
        self.assertTrue(self.new(liaison.NAME, result))

    def test_a_request_in_the_owners_chats_beside_a_pass_is_new(self):
        self.ev("chat.connected", None, service="telegram", reply=True, user={"id": "1001", "name": "Owner"},
                channel="telegram", owner=False)
        self.ev("chat.received", None, service="telegram", conv="2002", msg=7, text="when is the call?",
                **{"from": {"id": "2002", "name": "Pat"}}, channel="telegram", owner=False)
        person = f"c{self.seq}"
        got, bid = self.incoming()
        self.turn(liaison.NAME, got)
        self.pass_on(bid)
        self.ev("chat.done_requested", None, inbox=person, **L)
        result = self.end(liaison.NAME, "Passed the news to maps, closed Pat's message")
        self.assertTrue(self.new(liaison.NAME, result))

    def test_items_passed_to_two_sessions_fold_only_with_both_awake(self):
        for asleep, folded in ((None, True), ("web", False)):
            with self.subTest(asleep=asleep):
                first, b1 = self.incoming()
                second, b2 = self.incoming("And the roads are ready too")
                self.turn(liaison.NAME, first)
                self.pass_on(b1)
                self.pass_on(b2, to="web")
                if asleep:
                    self.st["sessions"][asleep]["state"] = "asleep"
                result = self.end(liaison.NAME, "Passed the tiles to maps and the roads to web")
                self.assertEqual(self.new(liaison.NAME, result), not folded)
                if folded:
                    self.assertEqual(self.st["sessions"][liaison.NAME]["merged"], {"seq": result, "into": "maps"})

    def test_a_send_with_nobodys_words_has_nobody_to_fold_into(self):
        got, _ = self.incoming("What is your tile format?")
        self.turn(liaison.NAME, got)
        self.signed(self.send(text="PNG, 256 px"))   # the liaison answers the other hub itself: no session's words
        result = self.end(liaison.NAME, "Answered maps-hub: PNG, 256 px")
        self.assertTrue(self.new(liaison.NAME, result))

    # --- a refusal with nothing sent: the answer to the asking session ---------------------------------------------------
    def told(self, name="maps"):
        """The messages the liaison's results brought to a session."""
        return [m for m in self.st["messages"].values()
                if m["session"] == name and m.get("relay") and (m.get("from") or {}).get("name") == liaison.NAME]

    def test_a_refusal_with_nothing_sent_reaches_the_asking_session(self):
        self.turn("maps", (m := self.letter(text=ASK)))
        self.end("maps", "Asked")
        self.seen_all()
        self.turn(liaison.NAME, m)
        result = self.end(liaison.NAME, REFUSAL)
        got = self.told()
        self.assertEqual([x["text"] for x in got], [REFUSAL])
        self.assertIn(m, got[0]["origin"])
        self.assertFalse(self.new(liaison.NAME, result))
        self.assertEqual(self.st["sessions"][liaison.NAME]["merged"], {"seq": result, "into": "maps"})

    def test_the_same_in_the_owners_chain(self):
        word = f"m{self.ev('message.received', None, src='web', to='maps', text='Ask the other hub', channel='web', owner=True)}"
        self.turn("maps", word)
        m = self.letter(text=ASK)
        self.end("maps", "Asked the other hub, will tell you its answer")
        self.turn(liaison.NAME, m)
        result = self.end(liaison.NAME, REFUSAL)
        self.assertEqual([x["text"] for x in self.told()], [REFUSAL])
        self.assertFalse(self.new(liaison.NAME, result))

    def test_a_short_refusal_is_delivered_too(self):
        self.turn("maps", (m := self.letter(text=ASK)))
        self.end("maps", "Asked")
        self.turn(liaison.NAME, m)
        self.end(liaison.NAME, "No file access")
        self.assertEqual([x["text"] for x in self.told()], ["No file access"])

    def test_a_request_the_core_refused_is_delivered_too(self):
        self.turn("maps", (m := self.letter(text="Tell nobody-hub: hello")))
        self.end("maps", "Asked")
        self.turn(liaison.NAME, m)
        self.ev("bus.send_requested", None, ok=False, contact="nobody-hub", text="hello", **{"channel": "session", "owner": False, "sender": liaison.NAME})
        self.end(liaison.NAME, "Not sent: no such contact nobody-hub")
        self.assertEqual(len(self.told()), 1)

    def test_a_sent_letter_stays_an_echo(self):
        self.turn("maps", (m := self.letter()))
        self.end("maps", "Asked")
        self.turn(liaison.NAME, m)
        self.signed(self.send(m))
        result = self.end(liaison.NAME, "The words of maps went to the hub maps-hub")
        self.assertEqual(self.told(), [])
        self.assertEqual(self.st["sessions"][liaison.NAME]["merged"], {"seq": result, "into": "maps"})

    def test_a_passed_item_stays_an_echo(self):
        got, bid = self.incoming()
        self.turn(liaison.NAME, got)
        self.pass_on(bid)
        self.end(liaison.NAME, "Passed the news of maps-hub to maps")
        self.assertEqual(self.told(), [])

    def test_a_fyi_letter_is_not_asked_an_answer(self):
        self.turn("maps", (m := f"m{self.ev('message.received', None, to=liaison.NAME, text=ASK, fyi=True, channel='session', owner=False, sender='maps')}"))
        self.end("maps", "Told")
        self.turn(liaison.NAME, m)
        result = self.end(liaison.NAME, REFUSAL)
        self.assertEqual(self.told(), [])
        self.assertTrue(self.new(liaison.NAME, result))

    def test_the_owners_words_stay_with_the_owner(self):
        word = f"m{self.ev('message.received', None, src='web', to=liaison.NAME, text='Tell maps-hub the tiles are late', channel='web', owner=True)}"
        self.turn(liaison.NAME, word)
        result = self.end(liaison.NAME, "Not sent: no such contact")
        self.assertEqual(self.told(), [])
        self.assertTrue(self.new(liaison.NAME, result))

    def test_a_session_that_is_gone_gets_nothing(self):
        self.turn("maps", (m := self.letter(text=ASK)))
        self.end("maps", "Asked")
        self.turn(liaison.NAME, m)
        self.st["sessions"]["maps"]["state"] = "closed"
        result = self.end(liaison.NAME, REFUSAL)
        self.assertEqual(self.told(), [])
        self.assertTrue(self.new(liaison.NAME, result))

    def test_a_sleeping_session_is_woken_and_the_owner_still_sees_it(self):
        self.turn("maps", (m := self.letter(text=ASK)))
        self.end("maps", "Asked")
        self.turn(liaison.NAME, m)
        self.st["sessions"]["maps"]["state"] = "asleep"
        result = self.end(liaison.NAME, REFUSAL)
        self.assertEqual(len(self.told()), 1)
        self.assertTrue(self.new(liaison.NAME, result))

    def test_an_error_result_is_not_delivered(self):
        self.turn("maps", (m := self.letter(text=ASK)))
        self.end("maps", "Asked")
        self.turn(liaison.NAME, m)
        self.ev("turn.result", liaison.NAME, src="hook", text="API error", is_error=True)
        self.assertEqual(self.told(), [])

    def test_a_later_turn_does_not_repeat_it(self):
        self.turn("maps", (m := self.letter(text=ASK)))
        self.end("maps", "Asked")
        self.turn(liaison.NAME, m)
        self.end(liaison.NAME, REFUSAL)
        got, bid = self.incoming()
        self.turn(liaison.NAME, got)
        self.pass_on(bid)
        self.end(liaison.NAME, "Passed to maps")
        self.assertEqual(len(self.told()), 1)

    def test_a_decision_on_an_item_beside_the_letter_keeps_the_result_with_the_owner(self):
        self.ev("chat.connected", None, service="telegram", reply=True, user={"id": "1001", "name": "Owner"},
                channel="telegram", owner=False)
        for kind, extra in (("chat.send_requested", {"text": "tomorrow"}), ("chat.done_requested", {}),
                            ("bus.done_requested", {"why": "a greeting"})):
            with self.subTest(kind):
                self.ev("chat.received", None, service="telegram", conv="2002", msg=self.seq, text="when is the call?",
                        **{"from": {"id": "2002", "name": "Pat"}}, channel="telegram", owner=False)
                person = f"c{self.seq}"
                self.turn("maps", (m := self.letter(text=ASK)))
                self.end("maps", "Asked")
                got, bid = self.incoming()
                self.turn(liaison.NAME, m)
                self.ev(kind, None, inbox=bid if kind.startswith("bus") else person, **extra, **L)
                result = self.end(liaison.NAME, "Not sent: no file access; answered the other one")
                self.assertEqual(self.told(), [])
                self.assertTrue(self.new(liaison.NAME, result))

    def test_a_rule_beside_a_refused_letter_still_answers_the_session(self):
        self.turn("maps", (m := self.letter(text=ASK)))
        self.end("maps", "Asked")
        self.turn(liaison.NAME, m)
        self.ev("bus.rule_requested", None, op="add", contact=CONTACT, text="tiles in PNG only", **L)
        self.end(liaison.NAME, "Not sent (no file access); wrote down the rule")
        self.assertEqual(len(self.told()), 1)

    def test_two_letters_of_one_session_get_one_answer(self):
        self.turn("maps", (m := self.letter(text=ASK)))
        self.end("maps", "Asked")
        again = self.letter(text=ASK + " twice")
        self.ev("turn.started", liaison.NAME, src="hook", marker=m, also=[m, again])   # read together
        self.end(liaison.NAME, REFUSAL)
        self.assertEqual(len(self.told()), 1)

    def test_a_refused_letter_beside_a_sent_one_of_another_session(self):
        self.turn("maps", (a := self.letter(text=ASK)))
        self.end("maps", "Asked")
        self.turn("web", (b := self.letter("web", "Ask the other hub: are the roads ready?")))
        self.end("web", "Asked")
        self.ev("turn.started", liaison.NAME, src="hook", marker=a, also=[a, b])   # read together
        self.signed(self.send(b))
        result = self.end(liaison.NAME, "The words of web went out; the file of maps I cannot read")
        self.assertEqual([x["text"] for x in self.told("maps")], ["The words of web went out; the file of maps I cannot read"])
        self.assertEqual(self.told("web"), [])
        self.assertEqual(self.st["sessions"][liaison.NAME]["merged"], {"seq": result, "into": "web"})

    def test_a_refused_letter_beside_a_send_not_yet_decided_stays_new(self):
        self.turn("maps", (a := self.letter(text=ASK)))
        self.end("maps", "Asked")
        self.turn("web", (b := self.letter("web", "Ask the other hub: are the roads ready?")))
        self.end("web", "Asked")
        self.ev("turn.started", liaison.NAME, src="hook", marker=a, also=[a, b])   # read together
        self.send(b)   # no word of the core yet: the owner must hear how it ends
        result = self.end(liaison.NAME, "Sent the roads, the file of maps I cannot read")
        self.assertEqual(len(self.told("maps")), 1)
        self.assertTrue(self.new(liaison.NAME, result))

    def test_a_result_with_no_turn_of_its_own_is_not_relayed_again(self):
        self.turn("maps", (m := self.letter(text=ASK)))
        self.end("maps", "Asked")
        self.turn(liaison.NAME, m)
        self.end(liaison.NAME, REFUSAL)
        self.ev("turn.result", liaison.NAME, src="hook", text="Compacted")   # `/compact`, `/clear`, a background job's end
        self.assertEqual(len(self.told()), 1)

    def test_a_question_to_the_owner_keeps_the_result_with_him(self):
        self.turn("maps", (m := self.letter(text=ASK)))
        self.end("maps", "Asked")
        self.turn(liaison.NAME, m)
        self.ev("permission.requested", liaison.NAME, src="hook", request_id="r1", tool="AskUserQuestion",
                interactive=True, input={"questions": [{"question": "Send it by words?", "options": []}]})
        result = self.end(liaison.NAME, "I cannot read the file; asked the owner whether to send it by words")
        self.assertEqual(len(self.told()), 1)   # the session still learns the letter did not go
        self.assertTrue(self.new(liaison.NAME, result))

    def test_a_waiting_session_does_not_take_the_result_of_a_decided_item(self):
        self.turn("maps", (m := self.letter(text="Did the tiles arrive?")))
        self.end("maps", "Asked")
        self.ev("session.waiting", "maps", text="the liaison's answer", channel="session", owner=False, sender="maps")
        got, bid = self.incoming()
        self.ev("turn.started", liaison.NAME, src="hook", marker=m, also=[m, got])
        self.ev("bus.done_requested", None, inbox=bid, why="a greeting", **L)
        result = self.end(liaison.NAME, "Closed the greeting; the tiles question is answered by the other hub itself")
        self.assertEqual(len(self.told()), 1)   # it waits for the answer: it gets it, as before
        self.assertTrue(self.new(liaison.NAME, result))   # but the closing of the item stays the owner's


    def test_the_once_a_turn_guard_holds_without_a_turn_number(self):
        self.turn("maps", (m := self.letter(text=ASK)))
        self.end("maps", "Asked")
        self.turn(liaison.NAME, m)
        self.st["sessions"][liaison.NAME].pop("turn_seq", None)   # the state of an older core
        self.end(liaison.NAME, REFUSAL)
        self.ev("turn.result", liaison.NAME, src="hook", text="Compacted")
        self.assertEqual(len(self.told()), 1)


if __name__ == "__main__":
    unittest.main()
