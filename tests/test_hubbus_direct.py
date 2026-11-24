"""A session's words to the owner's own hub go out from the core itself, the liaison is not woken (spec d2505378, part A, outgoing).
Of the liaison's turns over four days, 690 did nothing but `hub bus send --text-from mN` for a session's request — the
core already wrote to it "this is not a question to you, send its words by reference". Straight only to a known hub
with the pair code checked, bound to a person of the owner's contacts book and with no rule about it; any other hub —
the liaison, as before. Its own fact (`bus.direct_requested`), not a message to the liaison: a core rolled back
refuses the fact and does not hand the liaison requests the core has already sent."""
import tests  # noqa: F401 — keeps the machine's settings out
import io
import unittest
from contextlib import redirect_stdout

from tests.test_hubbus import need_lib, need_ssh
from tests import test_hubbus_s1 as base
from core import busd, buscli, busstate, liaison

PERSON = {"id": "c0000a0b1", "name": "Pat Lee"}


def own(a):
    """A's hub `bee` bound to a person of the book: the owner's own people."""
    a.ask("bus.contact_requested", {"op": "link", "name": "bee", "person": PERSON})
    assert busstate.direct_refusal(a.st, "bee") is None, busstate.direct_refusal(a.st, "bee")


def session_put(hub, session, rejected):
    """bin/hub put for core/buscli as a session's process does it."""
    def put(type_, entity, data, say=True, **_):
        ev = hub.rec(type_, {**data, "channel": "session", "owner": False, "sender": session}, entity=entity)
        why = hub.verdict(ev)
        if why:
            rejected[ev["seq"]] = why
        return ev["seq"]
    return put


def send(a, text, session="api", thread=None):
    """`hub send liaison --to bee "<text>"` from a session of A: (exit code, what it printed, refusals). A session
    already up is left as it is: it may be in a turn."""
    if session not in a.st["sessions"]:
        a.up(session)
    with a.env():
        busd.write_view(a.h)
    rejected, out = {}, io.StringIO()
    argv = ["request", "--to", "bee", *(["--thread", thread] if thread else []), liaison.NAME, text]
    with a.env(), redirect_stdout(out):
        code = buscli.main(argv, session_put(a, session, rejected), lambda: a.st, a.h.state_dir, me=session,
                           outsider=None, rejected=rejected)
    return code, out.getvalue(), rejected


def liaison_messages(a):
    return [m for m in a.st["messages"].values() if m.get("session") == liaison.NAME and m.get("bus_to")]


@need_lib
@need_ssh
class Direct(unittest.TestCase):
    def test_words_to_an_own_hub_go_out_without_the_liaison(self):
        a, b, wire = base.acquainted()
        own(a)
        code, said, rejected = send(a, "The report is ready: 12 pages, all checked")
        self.assertEqual((code, rejected), (0, {}), said)
        self.assertEqual(liaison_messages(a), [])                       # nothing woke the liaison
        (o,) = [o for o in a.bus["out"].values() if o.get("kind") == "message"]
        self.assertEqual((o["source"], o["direct"], o["basis"]), ("api", True, None))
        before = set(b.bus["inbox"])
        wire.settle()
        (new,) = [k for k in b.bus["inbox"] if k not in before]
        item = b.bus["inbox"][new]
        self.assertEqual((item["text"], item["agent_from"]), ("The report is ready: 12 pages, all checked", "api"))

    def test_the_answer_in_the_thread_comes_back_to_the_session(self):
        a, b, wire = base.acquainted()
        own(a)
        send(a, "Can you check the access?")
        wire.settle()
        (bid,) = list(b.bus["inbox"])
        name_at_b = next(iter(b.bus["contacts"]))
        _, why = b.as_liaison("bus.send_requested", {"contact": name_at_b, "thread": b.bus["inbox"][bid]["thread"],
                                                     "text": "Checked, the access works"})
        self.assertIsNone(why)
        for card in b.cards():
            b.answer(card, "send")
        wire.settle()
        got = [m for m in a.st["messages"].values() if m.get("session") == "api" and "access works" in m.get("text", "")]
        self.assertEqual(len(got), 1)
        thread = b.bus["inbox"][bid]["thread"]
        self.assertIn(f"hub send liaison --to bee --thread {thread}", got[0]["text"].split("<<<")[0])   # it can answer there

    def test_the_session_the_question_was_passed_to_answers_in_the_thread_and_gets_the_reply(self):
        for t in ("named", None):                                         # with --thread; by itself — never a led thread
            with self.subTest(thread=t):
                a, b, wire = base.acquainted()
                own(a)
                send(a, "Which port does the gateway take?")
                wire.settle()
                (bid,) = list(b.bus["inbox"])
                thread = b.bus["inbox"][bid]["thread"]
                name_at_b = next(iter(b.bus["contacts"]))

                def bee_says(text):
                    _, why = b.as_liaison("bus.send_requested", {"contact": name_at_b, "thread": thread, "text": text})
                    self.assertIsNone(why)
                    for card in b.cards():
                        b.answer(card, "send")
                    wire.settle()
                bee_says("staging or prod?")
                (got,) = [m for m in a.st["messages"].values() if m.get("session") == "api" and "staging" in m["text"]]
                a.rec("turn.started", {"marker": got["id"]}, entity="api")
                a.up("docs")
                mid, why = a.say("api", "please answer bee: prod", to="docs")   # api passes the question on
                self.assertIsNone(why)
                a.rec("turn.started", {"marker": mid}, entity="docs")
                self.assertEqual(a.bus["threads"][thread]["source"], "api")
                code, said, rejected = send(a, "prod: 8443", session="docs", thread=thread if t else None)
                self.assertEqual((code, rejected), (0, {}), said)
                wire.settle()
                (theirs,) = [x for x in b.bus["inbox"].values() if x["text"] == "prod: 8443"]
                self.assertEqual(theirs["thread"] == thread, bool(t))
                self.assertEqual(a.bus["threads"][thread]["source"], "docs" if t else "api")
                bee_says("thanks, works")
                (item,) = [x for x in a.bus["inbox"].values() if x["text"] == "thanks, works"]
                self.assertEqual(item["passed_to"], ["docs" if t else "api"])   # the reply comes back to who leads

    def test_a_hub_not_bound_to_a_person_or_with_a_rule_goes_through_the_liaison(self):
        a, _, _ = base.acquainted()
        self.assertEqual(busstate.direct_refusal(a.st, "bee"), "no_person")
        code, _, rejected = send(a, "Hello")
        self.assertEqual(code, 0)
        self.assertEqual(len(liaison_messages(a)), 1)
        own(a)
        a.ask("bus.rule_requested", {"op": "add", "contact": "bee", "text": "with them — English only"})
        self.assertEqual(busstate.direct_refusal(a.st, "bee"), "rules")
        send(a, "Hello again")
        self.assertEqual(len(liaison_messages(a)), 2)
        self.assertNotIn("direct", busstate.view(a.st)["contacts"]["bee"])

    def test_a_stale_view_falls_back_to_the_liaison(self):
        a, _, _ = base.acquainted()
        own(a)
        a.up("api")
        with a.env():
            busd.write_view(a.h)                                       # the view says «straight»
        a.ask("bus.rule_requested", {"op": "add", "contact": "bee", "text": "ask the owner first"})
        rejected, out = {}, io.StringIO()
        with a.env(), redirect_stdout(out):
            code = buscli.main(["request", "--to", "bee", liaison.NAME, "Hello"], session_put(a, "api", rejected),
                               lambda: a.st, a.h.state_dir, me="api", outsider=None, rejected=rejected)
        self.assertEqual(code, 0)
        self.assertEqual(len(liaison_messages(a)), 1)                    # the core refused straight, the liaison got it

    def test_a_refusal_goes_to_the_session_not_the_liaison(self):
        a, _, _ = base.acquainted()
        own(a)
        code, said, rejected = send(a, "x" * (busstate.SEND_MAX + 1))
        self.assertEqual(code, 1)
        self.assertIn("60 KB", said)
        self.assertEqual(liaison_messages(a), [])
        a2, _, _ = base.acquainted()
        own(a2)
        send(a2, "the key is sk-ant-api03-" + "A" * 40)                # a secret: refused at sealing
        told = [m["session"] for m in a2.st["messages"].values() if "Not sent to the hub bee" in (m.get("text") or "")]
        self.assertEqual(told, ["api"])

    def test_an_explicit_thread_must_take_the_words(self):
        a, _, _ = base.acquainted()
        own(a)
        code, said, rejected = send(a, "Hello", thread="t-nobody-knows")
        self.assertEqual(code, 1)
        self.assertIn("thread_closed", " ".join(rejected.values()))

    def test_only_a_session_sends_straight(self):
        a, _, _ = base.acquainted()
        own(a)
        _, why = a.as_liaison("bus.direct_requested", {"contact": "bee", "text": "x"})
        self.assertIsNotNone(why)
        ev = a.ask("bus.direct_requested", {"contact": "bee", "text": "x"})
        self.assertIsNotNone(a.verdict(ev))


if __name__ == "__main__":
    unittest.main()
