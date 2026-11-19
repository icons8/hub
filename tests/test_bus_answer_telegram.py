"""An answer from another hub to the owner's own request reaches him where he asked. The owner writes to the hub in
Telegram "ask the other hub X"; the hub passes it to the liaison (`hub send liaison --for mN`); the liaison sends its
own words over the bus; the answer in that thread goes straight to the hub (the thread's source). The hub's result on
that answer is the reply to his Telegram message, as a reply to his own message would be (14.11: it stayed on the
page). The chain is the core's link — the thread remembers the chain of its source's latest request — never the text.
Words from the bus that answer no request of his stay on the page: the bus is not a chat with the owner."""
import tests  # noqa: F401 — do not see the machine's settings
import copy
import unittest

from core import busstate, chain, journal, liaison
from tests import test_telegram as base

S = {"channel": "session", "owner": False}
CONTACT, FP, THREAD = "maps-1", "hb1-abcdefghijklmnopqrstuvwxyz", "0e157fdd8b36c16f825af069b1b2842a"


class BusAnswerToTelegram(unittest.TestCase):
    # a fake Bot API and journal — as in tests/test_telegram.py (without its tests: they run there)
    setUp, tearDown = base.TelegramTest.setUp, base.TelegramTest.tearDown
    spooled, msg, ingest = base.TelegramTest.spooled, base.TelegramTest.msg, base.TelegramTest.ingest

    def seed(self, *sessions):
        """The liaison and the sessions, a known and checked contact: a snapshot of the state, as hubd writes it."""
        for n in (liaison.NAME, *sessions):
            self.j.append("session.requested", n, {"cwd": f"/{n}", "session_id": n})
            self.j.append("session.started", n, {"session_id": n})
        self.bot.tick()
        st = copy.deepcopy(self.bot.reader.state)
        st["sessions"][liaison.NAME]["liaison"] = True
        st["bus"] = bus = busstate.empty()
        bus.update(enabled=True, crypto_ready=True, me={"fp": "hb1-me", "label": "Hub"})
        bus["contacts"][CONTACT] = {"fp": FP, "label": "Other hub", "status": "known", "verified": True}
        self.j.append("core.snapshot", None, {"state": st, "why": "periodic"})
        self.bot.tick()

    @property
    def st(self):
        return self.bot.reader.state

    def from_telegram(self, text, tg_id):
        self.fake.updates = [self.msg(text, tg_id)]
        self.bot.poll(timeout=0)
        self.ingest()
        return f"m{[e for e in journal.read(self.j.path) if e['type'] == 'message.received'][-1]['seq']}"

    def turn(self, name, marker, result=None, **extra):
        self.j.append("turn.started", name, {"marker": marker, **extra})
        if result is not None:
            self.end(name, result)

    def end(self, name, result):
        self.j.append("turn.result", name, {"text": result})
        self.j.append("turn.stopped", name, {})

    def to_liaison(self, sender, text="Ask the other hub: thin wires?", **extra):
        ev = self.j.append("message.received", None, {"to": liaison.NAME, "text": text, **extra, **S, "sender": sender})
        return f"m{ev['seq']}"

    def liaison_asks(self, marker, text="Will thin wires do for a prototype?", also=None, **send):
        """The liaison's turn on a message and its own words (or the session's, text_from) out in the thread; the
        daemon signs the envelope."""
        self.turn(liaison.NAME, marker, **({"also": also} if also else {}))
        ask = {"text": text} if "text_from" not in send else {}
        ev = self.j.append("bus.send_requested", None, {"contact": CONTACT, **ask, **send, **S, "sender": liaison.NAME})
        self.j.append("core.bus_out", None, {"out": f"o{ev['seq']}", "kind": "message", "id": send.get("thread") or THREAD,
                                             "contact": CONTACT, "fp": FP, "exp": 9e9, "approved": "auto", "seq": 1,
                                             "cls": "liaison"}, src="core")
        self.end(liaison.NAME, "Sent, will pass the answer on")
        self.bot.tick()
        self.assertEqual(self.st["bus"]["out"][f"o{ev['seq']}"]["status"], "signed")   # the core took and signed it

    def answer(self, text="Yes, for a first try thin wires do", thread=THREAD):
        ev = self.j.append("core.bus_received", None, {
            "hub": FP, "contact": CONTACT, "id": f"7511f7308a51caceb5ec566e9e04{len(self.st['bus']['inbox']):04d}",
            "exp": 9e9, "cls": "liaison", "status": "ok", "kind": "message", "agent_from": "glove", "agent_to": None,
            "label": "Other hub", "thread": thread, "in_reply_to": thread, "seq": 2, "text": text}, src="core")
        self.bot.tick()
        return f"m{ev['seq']}"

    def run_(self):
        self.fake.calls.clear()
        self.bot.tick()
        self.bot.tick()
        return self.fake.sent()

    def told(self, word):
        return [(s["text"], (s.get("reply_parameters") or {}).get("message_id")) for s in self.run_() if word in s["text"]]

    def hub_asks_for_him(self, tg_id=11186):
        """His Telegram message; the hub's turn on it passes his request to the liaison with --for."""
        word = self.from_telegram("Ask the other hub whether thin wires will do", tg_id)
        self.turn("hub", word)
        ask = self.to_liaison("hub", **{"for": word})
        self.end("hub", "Passed it to the liaison")
        return word, ask

    def test_the_hubs_result_on_the_answer_to_his_telegram_request_goes_to_telegram(self):
        self.seed()
        word, ask = self.hub_asks_for_him()
        self.liaison_asks(ask)
        self.run_()
        got = self.answer()
        self.assertEqual(self.st["messages"][got]["session"], "hub")   # the thread's source, no liaison turn
        self.assertFalse(self.st["messages"][got].get("from_owner"))   # words of another hub, not his
        self.turn("hub", got, "The other hub says: yes, thin wires do for a first try")
        self.assertEqual(self.told("thin wires do"), [("The other hub says: yes, thin wires do for a first try", 11186)])

    def test_the_session_words_sent_by_reference_carry_the_chain_too(self):
        self.seed()
        word = self.from_telegram("Ask the other hub whether thin wires will do", 11186)
        self.turn("hub", word)
        ask = self.to_liaison("hub", text="Will thin wires do for a prototype?")   # no --for: the turn's chain
        self.end("hub", "Passed it to the liaison")
        self.liaison_asks(ask, text_from=ask)
        self.run_()
        got = self.answer()
        self.turn("hub", got, "The other hub says: yes, thin wires do for a first try")
        self.assertEqual(self.told("thin wires do"), [("The other hub says: yes, thin wires do for a first try", 11186)])

    def test_an_answer_in_a_thread_a_session_opened_on_its_own_stays_on_the_page(self):
        self.seed("api")
        self.turn("api", None)   # a session's own turn, no owner word in its chain
        ask = self.to_liaison("api", text="Ask the other hub about tiles")
        self.liaison_asks(ask)
        self.run_()
        got = self.answer("Tiles are ready")
        self.assertEqual(self.st["messages"][got]["session"], "api")
        self.assertNotIn("root", self.st["messages"][got])
        self.turn("api", got, "The other hub says the tiles are ready")
        self.assertEqual(self.told("tiles"), [])

    def test_an_answer_to_the_hubs_own_request_without_his_word_stays_on_the_page(self):
        self.seed()
        self.turn("hub", None)   # the hub on its own (a schedule, a check): no owner message started it
        ask = self.to_liaison("hub", text="Ask the other hub about tiles")
        self.end("hub", "Asked")
        self.liaison_asks(ask)
        self.run_()
        got = self.answer("Tiles are ready")
        self.turn("hub", got, "The other hub says the tiles are ready")
        self.assertEqual(self.told("tiles"), [])

    def test_a_later_request_in_the_thread_not_on_his_word_takes_the_chain_away(self):
        self.seed("api")
        word, ask = self.hub_asks_for_him()
        self.liaison_asks(ask)
        got = self.answer()
        self.turn("hub", got, "The other hub says: yes, thin wires do")
        self.run_()
        self.turn("api", None)   # later a session asks the hub something, not on his word
        note = self.j.append("message.received", None, {"to": "hub", "text": "Do they have tiles?", **S, "sender": "api"})
        self.end("api", "Asked the hub")
        self.turn("hub", f"m{note['seq']}")   # one more question in the same thread, on the session's words
        again = self.to_liaison("hub", text="Ask them about tiles too")
        self.end("hub", "Asked")
        self.liaison_asks(again, text="And the tiles?", thread=THREAD)
        self.assertNotIn("root", self.st["bus"]["threads"][THREAD])
        got = self.answer("Tiles are ready")
        self.assertNotIn("root", self.st["messages"][got])
        self.turn("hub", got, "The other hub says the tiles are ready")
        self.assertEqual(self.told("tiles"), [])

    def test_another_sessions_request_in_the_thread_does_not_lend_its_chain_to_the_source(self):
        self.seed("api")
        self.turn("hub", None)   # the hub opened the thread on its own
        ask = self.to_liaison("hub", text="Ask the other hub about tiles")
        self.end("hub", "Asked")
        self.liaison_asks(ask)
        word = self.from_telegram("Ask the other hub about the export", 11190)
        self.turn("api", word)   # api, on his word, asks in the hub's thread: the answer goes to the hub
        mine = self.to_liaison("api", text="Ask them about the export")
        self.liaison_asks(mine, text="And the export?", thread=THREAD)
        self.assertEqual(self.st["bus"]["threads"][THREAD]["source"], "hub")
        self.assertNotIn("root", self.st["bus"]["threads"][THREAD])
        got = self.answer("Tiles are ready")
        self.assertEqual(self.st["messages"][got]["session"], "hub")
        self.turn("hub", got, "The other hub says the tiles are ready")
        self.assertEqual(self.told("tiles"), [])

    def test_a_liaison_turn_with_another_hubs_words_in_it_is_not_his_request(self):
        self.seed()
        theirs = "5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a5a"
        letter = self.answer("A question from us: is the export ready?", thread=theirs)   # their thread, to the liaison
        (bus_msg,) = [m for m in self.st["messages"].values() if m["session"] == liaison.NAME and m.get("channel") == "hubbus"]
        self.assertEqual((bus_msg["id"], self.st["bus"]["threads"][theirs].get("source")), (letter, None))
        word = self.from_telegram("Tell the other hub the export is ready", 11192)
        self.turn("hub", word)
        ask = self.to_liaison("hub", text="Tell them the export is ready", **{"for": word})
        self.end("hub", "Passed it to the liaison")
        self.liaison_asks(ask, text="The export is ready", also=[bus_msg["id"]], thread=theirs)
        self.assertNotIn("root", self.st["bus"]["threads"][theirs])
        got = self.answer("Thanks, and the tiles?", thread=theirs)
        self.assertEqual(self.st["messages"][got]["session"], liaison.NAME)
        self.assertNotIn("root", self.st["messages"][got])

    def test_the_liaisons_own_words_in_the_thread_keep_his_chain(self):
        self.seed()
        word, ask = self.hub_asks_for_him()
        self.liaison_asks(ask)
        letter = self.answer("Which wires?", thread="6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b6b")   # another thread, to the liaison
        self.liaison_asks(letter, text="Thin copper ones", thread=THREAD)   # its own clarification, no asker
        self.assertEqual(self.st["bus"]["threads"][THREAD].get("root"), word)
        got = self.answer()
        self.turn("hub", got, "The other hub says: yes, thin wires do for a first try")
        self.assertEqual(self.told("thin wires do"), [("The other hub says: yes, thin wires do for a first try", 11186)])

    def test_a_request_from_a_schedule_firings_turn_keeps_the_mark(self):
        self.seed()
        word = self.from_telegram("Ask the other hub about tiles every morning", 11194)
        self.turn("hub", word)
        ask = self.to_liaison("hub", text="Ask them about tiles")   # sent by --text: the liaison's turn on it
        self.end("hub", "Asked")
        self.bot.tick()
        st = copy.deepcopy(self.st)   # as the core marks a hub turn of a recurring job's firing (core/budget.py)
        st["messages"][ask]["via_schedule"] = True
        self.j.append("core.snapshot", None, {"state": st, "why": "periodic"})
        self.bot.tick()
        self.assertEqual(chain.root(self.st, ask), word)
        self.liaison_asks(ask)
        got = self.answer("Tiles are ready")
        self.assertEqual(chain.root(self.st, got), word)
        self.assertTrue(chain.scheduled(self.st, got))


class TheChainKeepsItsScheduleMark(unittest.TestCase):
    """An answer to a request made in a turn of a schedule firing is not the owner's live word (chain.scheduled), even
    though its root is his message: the limit economy and the quiet-card hold read the mark."""

    def test_the_answer_carries_via_schedule(self):
        from core import reducer
        st, seq = reducer.empty(), [0]

        def ev(type_, entity=None, src="session", **data):
            seq[0] += 1
            nonlocal st
            st, _, why = reducer.reduce(st, {"seq": seq[0], "ts": 1000 + seq[0], "src": src, "type": type_,
                                             "entity": entity, "data": data})
            self.assertIsNone(why, (type_, why))
            return f"m{seq[0]}"

        for n, extra in (("hub", {"kind": "hub"}), (liaison.NAME, {})):
            ev("session.requested", n, cwd=f"/{n}", session_id=n, channel="web", owner=True, **extra)
            ev("session.started", n)
        st["sessions"][liaison.NAME]["liaison"] = True
        st["bus"] = busstate.empty()
        st["bus"].update(enabled=True, crypto_ready=True, me={"fp": "hb1-me", "label": "Hub"})
        st["bus"]["contacts"][CONTACT] = {"fp": FP, "label": "Other hub", "status": "known", "verified": True}
        word = ev("message.received", None, src="telegram", text="Check the other hub every morning", channel="telegram",
                  owner=True)
        ask = ev("message.received", None, to=liaison.NAME, text="Ask them about tiles", channel="session", owner=False,
                 sender="hub")
        st["messages"][ask].update(root=word, via_schedule=True)   # written in a turn of a schedule firing
        self.assertTrue(chain.scheduled(st, ask))
        ev("turn.started", liaison.NAME, src="hook", marker=ask)
        sent = ev("bus.send_requested", None, contact=CONTACT, text_from=ask, channel="session", owner=False,
                  sender=liaison.NAME)
        ev("core.bus_out", None, src="core", out="o" + sent[1:], kind="message", id=THREAD, contact=CONTACT, fp=FP,
           exp=9e9, approved="auto", seq=1, cls="liaison")
        got = ev("core.bus_received", None, src="core", hub=FP, contact=CONTACT, id="7511f7308a51caceb5ec566e9e040000",
                 exp=9e9, cls="liaison", status="ok", kind="message", agent_from="glove", agent_to=None,
                 thread=THREAD, in_reply_to=THREAD, seq=2, text="Tiles are ready")
        self.assertEqual(st["messages"][got]["session"], "hub")
        self.assertEqual(chain.root(st, got), word)
        self.assertTrue(chain.scheduled(st, got))


if __name__ == "__main__":
    unittest.main()
