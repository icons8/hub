"""The words a session sends straight to an own hub (`hub send liaison --to <contact> "…"`, the core sends them itself,
`bus.direct_requested`) are a `bus` row in the sender's feed, as the words handed to the liaison are: the page draws them
as the first words of a thread with that hub and joins the other hub's answer to them. Without the row the thread had
only the answers."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from core import busstate, reducer, views

CONTACT, FP = "maps-hub", "hb1-abcdefghijklmnopqrstuvwxyz"
PERSON = {"id": "c0000a0b1", "name": "Pat Lee"}
S = {"channel": "session", "owner": False}


def state():
    st, seq = reducer.empty(), 0
    for n in ("maps", "liaison"):
        for typ, data in (("session.requested", {"cwd": "/w", "session_id": "s-" + n, "channel": "web", "owner": True}),
                          ("session.started", {})):
            seq += 1
            st, _, why = reducer.reduce(st, {"seq": seq, "ts": 1000 + seq, "src": "test", "type": typ, "entity": n,
                                             "data": data})
            assert why is None, why
    st["sessions"]["liaison"]["liaison"] = True
    st["bus"] = busstate.empty()
    st["bus"].update(enabled=True, crypto_ready=True, me={"fp": "hb1-me", "label": "Hub"})
    st["bus"]["contacts"][CONTACT] = {"fp": FP, "label": "Maps hub", "status": "known", "verified": True,
                                      "person": PERSON}
    return st


def feed(st, *events):
    r = views.Reader()
    r.state = st
    r.feed_all([{"ts": 1100 + e["seq"], "src": "session", "entity": None, **e} for e in events])
    return r


class DirectFeed(unittest.TestCase):
    def test_the_senders_feed_has_the_words_as_a_bus_row(self):
        st = state()
        self.assertIsNone(busstate.direct_refusal(st, CONTACT))
        r = feed(st, {"seq": 20, "type": "bus.direct_requested",
                      "data": {"contact": CONTACT, "text": "Will the tiles be ready?", "via": "local", **S, "sender": "maps"}})
        rows = [it for it in r.timelines.get("maps", []) if it.get("kind") == "bus"]
        self.assertEqual([{k: it[k] for k in ("kind", "dir", "contact", "text", "id")} for it in rows],
                         [{"kind": "bus", "dir": "out", "contact": CONTACT, "text": "Will the tiles be ready?", "id": "m20"}])
        self.assertEqual(r.timelines.get("liaison", []), [])   # the liaison was not woken: nothing in its feed

    def test_words_the_core_refused_are_not_a_row(self):
        st = state()
        st["bus"]["contacts"][CONTACT]["person"] = None   # not an own hub: the liaison sends them, the fact is refused
        r = feed(st, {"seq": 20, "type": "bus.direct_requested",
                      "data": {"contact": CONTACT, "text": "Will the tiles be ready?", "via": "local", **S, "sender": "maps"}})
        self.assertEqual([it for it in r.timelines.get("maps", []) if it.get("kind") == "bus"], [])

    def test_words_through_the_liaison_stay_one_row(self):
        st = state()
        r = feed(st, {"seq": 20, "type": "message.received",
                      "data": {"to": "liaison", "text": "Will the tiles be ready?", "bus_to": CONTACT, "bus_to_via": "local",
                               **S, "sender": "maps"}})
        self.assertEqual(len([it for it in r.timelines["maps"] if it.get("kind") == "bus"]), 1)


if __name__ == "__main__":
    unittest.main()
