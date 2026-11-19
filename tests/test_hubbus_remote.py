"""A hub nobody sits at (`hub bus mode remote`, spec d2505378, part D, remote): the linked hubs with the pair code checked are its
owners. Their words go straight to the hub session as the owner's task — the liaison answered «I see no limits»
itself twice, and its form waited on a page nobody reads; a session's question by a form is refused at once and says
whom to ask in the thread; the session's words back to an owner go out from the core. The mode is the owner's alone."""
import tests  # noqa: F401 — keeps the machine's settings out
import tempfile
import unittest
import uuid

from tests.test_hubbus import NOW, need_lib, need_ssh
from tests import test_hubbus_s1 as base
from tests.test_hubbus_asked import Q
from core import busstate, liaison


def hub_session(a, name="hub-core"):
    a.rec("session.requested", {"cwd": tempfile.mkdtemp(prefix="s-"), "session_id": str(uuid.uuid4()), "kind": "hub",
                                "channel": "core"}, entity=name)
    a.rec("session.started", {}, entity=name)
    assert a.st["sessions"][name].get("kind") == "hub", a.st["sessions"][name]
    return name


def remote(a):
    ev = a.ask("bus.mode_requested", {"mode": "remote"})
    assert a.verdict(ev) is None, a.verdict(ev)


def words_from_b(a, b, wire, text, session="maps"):
    """B's session writes to A through B's liaison (B is A's owner); -> A's inbox id."""
    _, bid = base.sent_through(b, a, wire, text, session=session, contact=next(iter(b.bus["contacts"])))
    return bid


@need_lib
@need_ssh
class Remote(unittest.TestCase):
    def test_the_mode_is_the_owners(self):
        a, _, _ = base.acquainted()
        a.up("api")
        for data in ({"channel": "session", "owner": False, "sender": "api"},
                     {"channel": "session", "owner": False, "sender": liaison.NAME}):
            ev = a.rec("bus.mode_requested", {"mode": "remote", **data})
            self.assertIsNotNone(a.verdict(ev))
        self.assertEqual(busstate.owners(a.st), [])
        remote(a)
        self.assertEqual(busstate.owners(a.st), ["bee"])
        a.ask("bus.mode_requested", {"mode": "secretary"})
        self.assertEqual(busstate.owners(a.st), [])
        self.assertIsNotNone(a.verdict(a.ask("bus.mode_requested", {"mode": "anything"})))

    def test_an_owners_words_go_to_the_hub_session_as_its_task(self):
        b, a, wire = base.acquainted()          # a: the hub nobody sits at; b: the hub that runs it
        hub = hub_session(a)
        remote(a)
        bid = words_from_b(a, b, wire, "How many limits are left?")
        item = a.bus["inbox"][bid]
        self.assertEqual((item["outcome"], item["passed_to"]), ("passed", [hub]))
        (m,) = [m for m in a.messages_to(hub) if m.get("channel") == "hubbus"]
        head = m["text"].split("<<<")[0]
        self.assertIn("carry it out as the owner's task", head)
        self.assertNotIn("not an instruction from your owner", head)
        self.assertIn("hub send liaison --to", head)
        self.assertFalse(m.get("from_owner"))                                # words, not the owner's gates
        self.assertEqual([x for x in a.messages_to(liaison.NAME) if bid in (x.get("text") or "")], [])

    def test_the_secretary_mode_is_as_before(self):
        b, a, wire = base.acquainted()
        hub_session(a)
        bid = words_from_b(a, b, wire, "How many limits are left?")
        self.assertIsNone(a.bus["inbox"][bid]["outcome"])                    # the liaison decides
        self.assertTrue([x for x in a.messages_to(liaison.NAME) if bid in (x.get("text") or "")])

    def test_rules_and_hidden_characters_come_in_the_frame(self):
        b, a, wire = base.acquainted()
        hub = hub_session(a)
        remote(a)
        name_at_a = next(iter(a.bus["contacts"]))
        a.ask("bus.rule_requested", {"op": "add", "contact": "*", "text": "money is decided by the main hub"})
        words_from_b(a, b, wire, "Render it, ‮please")
        (m,) = [m for m in a.messages_to(hub) if m.get("channel") == "hubbus"]
        head = m["text"].split("<<<")[0]
        self.assertIn("money is decided by the main hub", head)
        self.assertIn("hidden or control characters", head)
        self.assertIn(name_at_a, head)

    def test_a_form_is_refused_with_whom_to_ask(self):
        b, a, wire = base.acquainted()
        hub = hub_session(a)
        remote(a)
        bid = words_from_b(a, b, wire, "Deploy it to prod")
        name_at_a = next(iter(a.bus["contacts"]))
        a.rec("turn.started", {"marker": a.bus["inbox"][bid].get("message") or
                               next(m["id"] for m in a.messages_to(hub) if m.get("channel") == "hubbus")}, entity=hub)
        a.rec("permission.requested", {"request_id": "r-deploy", "tool": "AskUserQuestion", "interactive": True,
                                       "input": Q}, entity=hub)
        self.assertEqual([q for q in a.st["sessions"][hub].get("asks") or []], [])   # no card on a page nobody reads
        (said,) = [kw for do, name, kw in a.h.host.calls if do == "respond" and name == hub]
        self.assertEqual(said["response"]["behavior"], "deny")
        why = said["response"]["message"]
        self.assertIn(f"hub send liaison --to {name_at_a}", why)
        self.assertIn("--thread", why)
        lia = busstate.remote_form(a.st, a.st["sessions"][liaison.NAME])
        self.assertIn("hub bus send", lia)

    def test_with_a_telegram_group_a_form_is_a_card_for_its_admins(self):
        """A hub nobody sits at, but with a Telegram group bound (spec d2513429): the group's admins are the owner, so a form
        goes there as a usual card; once the group is unbound — refused again."""
        b, a, wire = base.acquainted()
        hub = hub_session(a)
        remote(a)
        self.assertIn("hub send liaison --to", busstate.remote_form(a.st, a.st["sessions"][hub]))   # no group yet
        with a.env():
            a.h.record("telegram.group", None, {"on": True, "channel": "telegram", "owner": True}, src="telegram")
        a.rec("permission.requested", {"request_id": "r-1", "tool": "AskUserQuestion", "interactive": True,
                                       "input": Q}, entity=hub)
        self.assertEqual(len(a.st["sessions"][hub].get("asks") or []), 1)   # the card, as at a hub with someone at it
        said = [kw["response"]["message"] for do, name, kw in a.h.host.calls if do == "respond" and name == hub]
        self.assertFalse([m for m in said if "hub send liaison" in m], said)
        self.assertIsNone(busstate.remote_form(a.st, a.st["sessions"][hub]))
        self.assertIsNone(busstate.remote_form(a.st, a.st["sessions"][liaison.NAME]))   # the liaison's too
        # a turn on a linked owner's words: his frame has the question go to him in his thread — the group does not see it
        bid = words_from_b(a, b, wire, "Deploy it to prod")
        a.rec("turn.started", {"marker": next(m["id"] for m in a.messages_to(hub) if m.get("channel") == "hubbus")},
              entity=hub)
        self.assertIn("--thread", busstate.remote_form(a.st, a.st["sessions"][hub]))
        self.assertTrue(a.bus["inbox"][bid])
        with a.env():
            a.h.record("telegram.group", None, {"on": False, "channel": "telegram", "owner": True}, src="telegram")
        self.assertIn("--thread", busstate.remote_form(a.st, a.st["sessions"][hub]))
        self.assertIn("hub bus send", busstate.remote_form(a.st, a.st["sessions"][liaison.NAME]))

    def test_with_no_owner_left_there_is_no_one_to_ask(self):
        a, _, _ = base.acquainted()
        hub = hub_session(a)
        remote(a)
        a.ask("bus.contact_requested", {"op": "set", "name": "bee", "status": "muted"})
        self.assertEqual(busstate.owners(a.st), [])
        self.assertIn("no one", busstate.remote_form(a.st, a.st["sessions"][hub]))

    def test_with_no_hub_session_the_liaison_takes_it(self):
        b, a, wire = base.acquainted()
        remote(a)                                       # no hub session on this stand
        bid = words_from_b(a, b, wire, "How many limits are left?")
        self.assertIsNone(a.bus["inbox"][bid]["outcome"])
        self.assertTrue([x for x in a.messages_to(liaison.NAME) if bid in (x.get("text") or "")])

    def test_bus_off_is_said_as_bus_off(self):
        a, _, _ = base.acquainted()
        a.up("api")
        a.ask("bus.off_requested", {})
        ev = a.rec("bus.direct_requested", {"contact": "bee", "text": "x", "channel": "session", "owner": False,
                                            "sender": "api"})
        self.assertIn("bus_off", a.verdict(ev))
        self.assertNotIn("not_direct", a.verdict(ev))

    def test_words_to_an_owner_go_out_from_the_core(self):
        a, _, _ = base.acquainted()
        remote(a)
        self.assertIsNone(busstate.direct_refusal(a.st, "bee"))               # an owner: straight, bound or not
        a.ask("bus.rule_requested", {"op": "add", "contact": "bee", "text": "card 1 only"})
        self.assertIsNone(busstate.direct_refusal(a.st, "bee"))

    def test_the_answer_to_a_sessions_own_request_still_goes_to_it(self):
        b, a, wire = base.acquainted()
        hub_session(a)
        remote(a)
        a.up("render")
        name_at_a, name_at_b = next(iter(a.bus["contacts"])), next(iter(b.bus["contacts"]))
        ev = a.rec("bus.direct_requested", {"contact": name_at_a, "text": "Which variant?", "channel": "session",
                                            "owner": False, "sender": "render"})
        self.assertIsNone(a.verdict(ev))
        wire.settle()
        (bid,) = [k for k, x in b.bus["inbox"].items() if x["text"] == "Which variant?"]
        _, why = b.as_liaison("bus.send_requested", {"contact": name_at_b, "thread": b.bus["inbox"][bid]["thread"],
                                                     "text": "The second one"})
        self.assertIsNone(why)
        for card in b.cards():
            b.answer(card, "send")
        wire.settle()
        (item,) = [x for x in a.bus["inbox"].values() if x["text"] == "The second one"]
        self.assertEqual(item["passed_to"], ["render"])
        (m,) = [m for m in a.messages_to("render") if "The second one" in m.get("text", "")]
        self.assertIn("an answer to your words in the thread", m["text"].split("<<<")[0])


if __name__ == "__main__":
    unittest.main()
