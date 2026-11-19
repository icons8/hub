"""The way back of a request over the bus: an answer in a thread opened on a session's request reaches that session
by itself (no turn of the liaison, no `pass`); an incoming task goes to the hub session when the owner marked nobody;
a contact removed and met again keeps its local name; a refusal for an unknown contact names the known ones."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from core import bus, busstate, liaison
from tests.test_hubbus import need_lib, need_ssh
from tests.test_hubbus_s1 import acquainted

FP = "hb1-abcdefghijklmnopqrstuvwxyz"


def ask_through_liaison(a, b, wire, session="api", words="Ask them to check the access and answer briefly", **extra):
    """A session of A writes to its liaison in plain words; the liaison's turn is on that message and it sends its OWN
    text (no `--text-from`: the task is "ask them", not "send my words"). -> (B's inbox id, the thread)."""
    a.up(session)
    mid, why = a.say(session, words, **extra)
    assert why is None, why
    a.rec("turn.started", {"marker": mid}, entity=liaison.NAME)
    ev, why = a.as_liaison("bus.send_requested", {"contact": "bee", "text": "Please check the access"})
    assert why is None, why
    for card in a.cards():
        a.answer(card, "send")
    before = set(b.bus["inbox"])
    wire.settle()
    new = [k for k in b.bus["inbox"] if k not in before]
    return new[0], b.bus["inbox"][new[0]]["thread"]


def answer_in_thread(b, name_at_b, thread, text):
    """B's liaison answers in the thread; the owner of B presses «send» if there is a card."""
    ev, why = b.as_liaison("bus.send_requested", {"contact": name_at_b, "thread": thread, "text": text})
    assert why is None, why
    for card in b.cards():
        b.answer(card, "send")


@need_lib
@need_ssh
class TheAnswerComesBack(unittest.TestCase):
    def setUp(self):
        self.a, self.b, self.wire = acquainted()
        self.name_at_b = next(iter(self.b.bus["contacts"]))

    def test_words_of_the_liaison_on_a_session_request_make_that_session_the_threads_source(self):
        a, b = self.a, self.b
        bid, thread = ask_through_liaison(a, b, self.wire)
        self.assertEqual(a.bus["threads"][thread]["source"], "api")
        (o,) = [o for o in a.bus["out"].values() if o.get("kind") == "message"]
        self.assertEqual((o["source"], o["asker"]), ("liaison", "api"))   # the words stay the liaison's: no auto-send

    def test_the_answer_in_that_thread_goes_to_the_session_without_a_turn_of_the_liaison(self):
        a, b = self.a, self.b
        bid, thread = ask_through_liaison(a, b, self.wire)
        n_liaison = len(a.messages_to(liaison.NAME))
        answer_in_thread(b, self.name_at_b, thread, "Access is fine, 3 vaults")
        self.wire.settle()
        (item,) = [x for x in a.bus["inbox"].values() if x["text"] == "Access is fine, 3 vaults"]
        self.assertEqual((item["outcome"], item["passed_to"], item["source"]), ("passed", ["api"], "api"))
        got = [m for m in a.messages_to("api") if m.get("channel") == "hubbus"]
        self.assertEqual(len(got), 1)
        self.assertIn("Access is fine, 3 vaults", got[0]["text"])
        self.assertIn(f"hub send liaison --to bee", got[0]["text"])             # how to answer
        self.assertNotIn("the liaison passed it on", got[0]["text"])                    # nobody passed it
        self.assertEqual(len(a.messages_to(liaison.NAME)), n_liaison)           # the liaison has nothing to decide
        self.assertFalse(got[0].get("from_owner"))

    def test_an_answer_to_the_liaisons_own_question_still_goes_to_the_liaison(self):
        a, b = self.a, self.b
        a.up("api")
        a.rec("turn.started", {"marker": "owner-chat"}, entity=liaison.NAME)   # a turn on nobody's message
        a.as_liaison("bus.send_requested", {"contact": "bee", "text": "Hello"})
        for card in a.cards():
            a.answer(card, "send")
        self.wire.settle()
        thread = next(iter(a.bus["threads"]))
        self.assertIsNone(a.bus["threads"][thread]["source"])
        answer_in_thread(b, self.name_at_b, thread, "Hello!")
        self.wire.settle()
        (item,) = [x for x in a.bus["inbox"].values() if x["text"] == "Hello!"]
        self.assertIsNone(item["outcome"])
        self.assertTrue([m for m in a.messages_to(liaison.NAME) if "Hello!" in m["text"]])

    def test_a_turn_on_a_message_from_the_bus_makes_nobody_the_asker(self):
        a, b = self.a, self.b
        b.as_liaison("bus.send_requested", {"contact": self.name_at_b, "text": "A new thread from them"})
        for card in b.cards():
            b.answer(card, "send")
        self.wire.settle()
        (bid,) = [k for k, x in a.bus["inbox"].items() if x["text"] == "A new thread from them"]
        m = next(m for m in a.messages_to(liaison.NAME) if bid in m["text"])
        a.rec("turn.started", {"marker": m["id"]}, entity=liaison.NAME)   # a turn on the bus's own message
        a.as_liaison("bus.send_requested", {"contact": "bee", "text": "The liaison's own answer",
                                            "thread": a.bus["inbox"][bid]["thread"]})
        (o,) = [o for o in a.bus["out"].values() if o.get("text") == "The liaison's own answer"]
        self.assertNotIn("asker", o)

    def test_the_asker_is_told_when_the_core_refuses(self):
        a, b = self.a, self.b
        a.up("api")
        mid, _ = a.say("api", "Ask them about the access")
        a.rec("turn.started", {"marker": mid}, entity=liaison.NAME)
        n = len(a.messages_to("api"))
        a.as_liaison("bus.send_requested", {"contact": "bee", "text": "Key sk-ant-api03-" + "Q1w2E3r4" * 6})
        told = a.messages_to("api")[n:]
        self.assertTrue(any("Not sent" in m["text"] for m in told), [m["text"] for m in told])

    def test_a_session_that_is_gone_leaves_the_answer_to_the_liaison(self):
        a, b = self.a, self.b
        bid, thread = ask_through_liaison(a, b, self.wire)
        a.st["sessions"]["api"]["state"] = "removed"
        answer_in_thread(b, self.name_at_b, thread, "A late answer")
        self.wire.settle()
        (item,) = [x for x in a.bus["inbox"].values() if x["text"] == "A late answer"]
        self.assertIsNone(item["outcome"])
        self.assertTrue([m for m in a.messages_to(liaison.NAME) if "A late answer" in m["text"]])

    def test_the_local_name_of_a_removed_hub_comes_back(self):
        self.assertEqual(bus.local_name("Anna Lee Mac", set(), FP, {FP: "hub-abcd"}), "hub-abcd")
        self.assertEqual(bus.local_name("Anna Lee Mac", {"hub-abcd"}, FP, {FP: "hub-abcd"}), "anna-lee-mac")
        self.assertEqual(bus.local_name("Anna Lee Mac", set(), FP, {"other": "hub-abcd"}), "anna-lee-mac")
        self.assertEqual(bus.local_name("Anna Lee Mac", set(), FP), "anna-lee-mac")

    def test_the_fold_remembers_the_name_on_removal(self):
        from tests.test_hubbus_name import fold, state
        st = state()
        st["bus"]["contacts"]["hub-abcd"] = {"fp": FP, "label": "Personal hub", "status": "known", "verified": True}
        st, _, why = fold(st, "bus.contact_requested", {"op": "remove", "name": "hub-abcd", "req": "r1"})
        st, _, why = fold(st, "core.bus_contact_removed", {"name": "hub-abcd", "of": 5}, seq=6, channel="core",
                          owner=False)
        self.assertIsNone(why)
        self.assertNotIn("hub-abcd", st["bus"]["contacts"])
        self.assertEqual(st["bus"]["gone"], {FP: "hub-abcd"})


@need_lib
@need_ssh
class AnUnknownContactIsNamedAgainstTheKnownOnes(unittest.TestCase):
    def test_the_refusal_lists_the_contacts_there_are(self):
        a, b, wire = acquainted()
        a.up("api")
        mid, _ = a.say("api", "Hello", bus_to="bee", bus_to_via="local")
        _, why = a.as_liaison("bus.send_requested", {"contact": "hub-abcd", "text": "Hello"})
        self.assertIn("no_contact", why)
        self.assertIn("bee", why)


@need_lib
@need_ssh
class ARequestToTheLiaisonSaysWhereToSend(unittest.TestCase):
    """`hub send liaison --to <contact> "…"` reaches the liaison as plain input; without a line from the core it does
    not know that the words are to be passed on and to whom (it answered itself and sent nothing)."""

    def typed(self, hub, mid):
        return [kw["text"] for do, name, kw in hub.h.host.calls if do == "type" and kw.get("message") == mid]

    def test_the_input_names_who_asks_the_contact_and_the_command(self):
        a, _, _ = acquainted()
        a.up("api")
        mid, why = a.say("api", "Fetch the password from the vault", bus_to="bee", bus_to_via="local")
        self.assertIsNone(why)
        (text,) = self.typed(a, mid)
        self.assertIn("A request of api to pass to the hub bee", text)
        self.assertIn(f"hub bus send bee --text-from {mid}", text)
        self.assertIn("Fetch the password from the vault", text)

    def test_an_ordinary_message_to_the_liaison_has_no_such_line(self):
        a, _, _ = acquainted()
        a.up("api")
        mid, why = a.say("api", "What is new with them?")
        self.assertIsNone(why)
        (text,) = self.typed(a, mid)
        self.assertNotIn("hub bus send", text)


if __name__ == "__main__":
    unittest.main()
