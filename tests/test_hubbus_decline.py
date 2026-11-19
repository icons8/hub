"""The liaison declines an incoming request (`hub bus decline`): the core tells the sender in the same thread — a short
refusal that a hub of the previous release takes as an ordinary answer — and the receiving side keeps a row of its own
in the liaison's feed, so the refusal does not depend on the model writing a result of its turn."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from tests import test_hubbus_s1 as base
from tests.test_hubbus import need_lib, need_ssh
from core import busstate, liaison, views


def asked(why=None):
    """A and B know each other; A's session `api` asked B, B's liaison declines (with `why`, if given)."""
    a, b, wire = base.acquainted()
    name_at_b = next(iter(b.bus["contacts"]))
    mid, bid = base.sent_through(a, b, wire, "Send the access token", session="api")
    assert bid, a.bus.get("last")
    data = {"inbox": bid, **({"why": why} if why is not None else {})}
    ev, verdict = b.as_liaison("bus.decline_requested", data)
    assert verdict is None, verdict
    wire.settle()
    return a, b, wire, name_at_b, bid, ev


@need_lib
@need_ssh
class DeclineGoesBack(unittest.TestCase):
    def test_the_sender_gets_the_refusal_in_the_same_thread(self):
        a, b, wire, name_at_b, bid, _ = asked("we do not send tokens and secrets over the bus")
        sent = b.bus["inbox"][bid]
        (out,) = [o for o in b.bus["out"].values() if o.get("kind") == "message"]   # a delivered one keeps no text
        text = "Hub Hub B declined the request: we do not send tokens and secrets over the bus"
        self.assertEqual((out["thread"], out["contact"]), (sent["thread"], name_at_b))
        answers = {k: x for k, x in a.bus["inbox"].items() if x["text"] == text}
        self.assertEqual(len(answers), 1)
        (aid, answer), = answers.items()
        self.assertEqual((answer["thread"], answer["source"]), (sent["thread"], "api"))
        # the asking session gets it at once as the answer to its words — the page then ends «waiting for a reply»
        self.assertTrue(answer.get("direct"))
        got = [m for m in a.messages_to("api") if m.get("channel") == "hubbus" and text in m["text"]]
        self.assertEqual(len(got), 1)
        self.assertEqual((sent["outcome"], out["status"]), ("declined", "delivered"))

    def test_without_a_reason_it_says_so(self):
        a, b, _, _, _, _ = asked()
        self.assertTrue(any(x["text"] == "Hub Hub B declined the request: no explanation" for x in a.bus["inbox"].values()))

    def test_the_reason_is_cut_to_the_one_the_fold_keeps(self):
        a, b, _, _, bid, _ = asked("a\n\n  b " + "x" * 400)
        why = b.bus["inbox"][bid]["why"]
        self.assertEqual((len(why), "\n" in why), (300, False))
        self.assertTrue(any(x["text"] == "Hub Hub B declined the request: " + why for x in a.bus["inbox"].values()))

    def test_a_second_decline_or_a_refused_one_sends_nothing(self):
        a, b, wire, _, bid, _ = asked("no")
        n = len(b.bus["out"])
        _, verdict = b.as_liaison("bus.decline_requested", {"inbox": bid})
        self.assertIn("already has an outcome", verdict)
        self.assertEqual(len(b.bus["out"]), n)

    def test_a_decline_notice_is_not_declined_back(self):
        # two liaisons must not answer each other's refusals for ever
        a, b, wire, name_at_b, bid, _ = asked("no")
        (item,) = [x for x in a.bus["inbox"].values() if x["text"].startswith(busstate.DECLINE_PREFIX)]
        a.bus["inbox"]["b800"] = {**item, "outcome": None, "source": None}   # as if it came to the liaison
        before = len(a.bus["out"])
        _, verdict = a.as_liaison("bus.decline_requested", {"inbox": "b800", "why": "ok"})
        self.assertIsNone(verdict)
        self.assertEqual(len(a.bus["out"]), before)

    def test_a_reason_that_looks_like_a_key_does_not_cost_the_sender_the_notice(self):
        a, b, _, _, bid, _ = asked("here is the key sk-ant-api03-" + "A1b2C3d4" * 6)
        texts = [x["text"] for x in a.bus["inbox"].values()]
        self.assertIn("Hub Hub B declined the request: no explanation", texts)
        self.assertFalse(any("sk-ant" in x for x in texts))

    def test_the_notice_answers_only_the_declined_request_not_others_of_the_thread(self):
        a, b, wire = base.acquainted()
        mid, bid = base.sent_through(a, b, wire, "Send the token", session="api")
        b.bus["inbox"]["b700"] = {**b.bus["inbox"][bid], "id": "other", "outcome": None, "ts": b.bus["inbox"][bid]["ts"] + 1}
        _, verdict = b.as_liaison("bus.decline_requested", {"inbox": bid, "why": "no"})
        self.assertIsNone(verdict)
        self.assertEqual(b.bus["inbox"]["b700"]["outcome"], None)

    def test_an_item_without_a_thread_or_from_an_unknown_hub_sends_nothing(self):
        a, b, wire, _, bid, _ = asked("no")
        bus = b.bus
        n = len(bus["out"])
        bus["inbox"]["b900"] = {"contact": "bee", "thread": None, "text": "x", "status": "ok", "outcome": None, "ts": 1}
        bus["inbox"]["b901"] = {"contact": "ghost", "thread": "t", "text": "x", "status": "ok", "outcome": None, "ts": 1}
        bus["threads"]["t-other"] = {"contact": "someone-else", "source": None, "ts": 1, "n": 1}
        known = bus["inbox"][bid]["contact"]   # a known, checked contact naming the thread id of another contact
        bus["inbox"]["b902"] = {"contact": known, "thread": "t-other", "text": "x", "status": "ok", "outcome": None, "ts": 1}
        for item in ("b900", "b901", "b902"):
            _, verdict = b.as_liaison("bus.decline_requested", {"inbox": item})
            self.assertIsNone(verdict)
        self.assertEqual(len(bus["out"]), n)


@need_lib
@need_ssh
class ForeignThread(unittest.TestCase):
    def test_another_contacts_thread_id_is_not_kept_on_the_item(self):
        # the item is not in that thread (_received does not take it over): the frame offers no --thread the core refuses
        a, b, wire, name_at_b, bid, _ = asked("no")
        b.bus["threads"]["t-other"] = {"contact": "someone-else", "source": None, "ts": 1, "n": 1}
        sent = b.bus["inbox"][bid]
        data = {k: sent.get(k) for k in ("contact", "fp", "agent_from", "agent_to", "text", "ts", "status", "marker")}
        data.update(hub=data.pop("fp"), id="foreign-1", exp=sent["ts"] + 3600, kind="message", size=10, label="Hub A",
                    thread="t-other", in_reply_to=None, seq=99)
        ev = b.rec("core.bus_received", data)
        self.assertIsNone(b.verdict(ev))
        item = b.bus["inbox"][f"b{ev['seq']}"]
        self.assertIsNone(item["thread"])
        self.assertEqual(b.bus["threads"]["t-other"]["contact"], "someone-else")


@need_lib
@need_ssh
class DeclineIsVisible(unittest.TestCase):
    def test_a_row_of_the_receiving_liaison_says_it_by_a_fact_of_the_core(self):
        a, b, _, name_at_b, bid, ev = asked("we do not send tokens")
        rows = dict(views._items(b.st, ev))
        row = rows[liaison.NAME]
        self.assertEqual(row["kind"], "sys")
        self.assertIn("declined by the liaison", row["text"])
        self.assertIn("we do not send tokens", row["text"])
        self.assertIn("Hub A", row["text"])   # the hub's own label, as the owner sees it

    def test_no_reason_no_invention(self):
        a, b, _, _, bid, ev = asked()
        self.assertIn("no reason given", dict(views._items(b.st, ev))[liaison.NAME]["text"])


class EarlierNotice(unittest.TestCase):
    """A decline notice is English now; one of a hub of an earlier release (and the items journals hold) carries the
    earlier form from the language data — both are notices, so neither is declined back."""
    def test_both_forms_are_notices(self):
        old = busstate.DECLINE_OLD
        for text in (busstate.DECLINE_PREFIX + "Hub B" + busstate.DECLINE_MIDDLE + "no",
                     old["prefix"] + "Hub B" + old["middle"] + busstate.DECLINE_NO_WHY):
            self.assertTrue(busstate.is_decline_notice(text), text)
        self.assertNotEqual(old["middle"], busstate.DECLINE_MIDDLE)
        self.assertFalse(busstate.is_decline_notice("Hub B says hello\n" + busstate.DECLINE_MIDDLE))
        self.assertFalse(busstate.is_decline_notice(None))


if __name__ == "__main__":
    unittest.main()
