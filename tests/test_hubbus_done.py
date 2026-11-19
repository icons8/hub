"""The liaison closes an incoming item that needs nothing (`hub bus done bN`): a "thank you", "got it" — read, passed to
nobody, answered with nothing. Without such an outcome the item stayed "deciding" and after 30 minutes the core told
the owner «the liaison has not decided» about a message that had been decided. Nothing goes to the other hub (a decline
would send «Hub X declined the request»)."""
import tests  # noqa: F401 — keeps the machine's settings out
import json
import os
import unittest
from unittest import mock

from tests import test_hubbus_s1 as base
from tests.test_hubbus import NOW, need_lib, need_ssh
from core import busd, busstate, buscli, liaison


def thanked():
    """A and B know each other; B's session `maps` writes to A a plain thank-you; it waits with A's liaison."""
    a, b, wire = base.acquainted()
    _, bid = base.sent_through(b, a, wire, "thank you, all went fine on our side too", session="maps",
                               contact=next(iter(b.bus["contacts"])))
    assert bid and a.bus["inbox"][bid]["outcome"] is None, a.bus["inbox"]
    return a, b, wire, bid


def liaison_put(hub, rejected):
    """bin/hub put for core/buscli as the liaison's process would do it: the fact signed by the session's name."""
    def put(type_, entity, data, say=True, **_):
        ev, why = hub.as_liaison(type_, data)
        if why:
            rejected[ev["seq"]] = why
        return ev["seq"]
    return put


def notices(h):
    with open(os.path.join(h.h.state_dir, "journal.jsonl"), encoding="utf-8") as f:
        return [e for e in map(json.loads, f) if e["type"] == "owner.notify" and (e["data"] or {}).get("source") == "bus"]


@need_lib
@need_ssh
class Done(unittest.TestCase):
    def test_done_closes_the_item_and_sends_nothing(self):
        a, b, wire, bid = thanked()
        out, inbox_b = dict(a.bus["out"]), dict(b.bus["inbox"])
        _, verdict = a.as_liaison("bus.done_requested", {"inbox": bid, "why": "a thank-you,\n nothing to do"})
        self.assertIsNone(verdict)
        item = a.bus["inbox"][bid]
        self.assertEqual((item["outcome"], item["why"]), ("read", "a thank-you, nothing to do"))
        wire.settle()
        self.assertEqual(a.bus["out"], out)            # no envelope, no decline notice
        self.assertEqual(b.bus["inbox"], inbox_b)
        self.assertEqual([m for m in a.st["messages"].values() if m.get("session") != liaison.NAME
                          and bid in (m.get("hubbus") or {}).get("inbox", "")], [])

    def test_a_closed_item_is_not_stale_and_the_owner_is_not_told(self):
        a, _, _, bid = thanked()
        a.as_liaison("bus.done_requested", {"inbox": bid})
        a.now = NOW + busstate.STALE + 1
        for _ in range(2):
            with a.env():
                busd.tick(a.h)
        self.assertFalse(a.bus["inbox"][bid].get("stale"))
        self.assertEqual(notices(a), [])

    def test_an_item_left_open_is_still_told_once(self):
        a, _, _, bid = thanked()
        a.now = NOW + busstate.STALE + 1
        with a.env():
            busd.tick(a.h)
        self.assertTrue(a.bus["inbox"][bid].get("stale"))
        self.assertEqual(len(notices(a)), 1)

    def test_what_done_refuses(self):
        a, _, _, bid = thanked()
        a.up("api")
        mid, _ = a.say("api", "ask them", bus_to="bee", bus_to_via="local")
        _, verdict = a.as_liaison("bus.done_requested", {"inbox": mid})   # a session's request is not read and closed
        self.assertIn("decline", verdict or "")
        _, verdict = a.as_liaison("bus.done_requested", {"inbox": "b1"})
        self.assertIn("b1", verdict or "")
        a.as_liaison("bus.done_requested", {"inbox": bid})
        _, verdict = a.as_liaison("bus.done_requested", {"inbox": bid})
        self.assertTrue((verdict or "").endswith(": read"), verdict)
        a.bus["inbox"]["b900"] = {**a.bus["inbox"][bid], "outcome": None, "status": "held"}
        _, verdict = a.as_liaison("bus.done_requested", {"inbox": "b900"})
        self.assertIn("(held)", verdict or "")
        ev = a.rec("bus.done_requested", {"inbox": bid, "channel": "session", "owner": False, "sender": "api"})
        self.assertIn("not_liaison", a.verdict(ev) or "")

    def test_a_closed_item_may_still_be_passed(self):
        # the liaison changed its mind (or the owner told it to): passing is not refused by the outcome
        a, _, _, bid = thanked()
        a.up("api")
        a.as_liaison("bus.done_requested", {"inbox": bid})
        self.assertIsNone(busstate.pass_refusal(a.st, bid, "api", NOW))

    def test_the_command(self):
        a, _, _, bid = thanked()
        rejected = {}
        with a.env(), mock.patch("builtins.print") as said:
            rc = buscli.main(["done", bid, "--why", "thanks only"], liaison_put(a, rejected), lambda: a.st,
                             a.h.state_dir, me=liaison.NAME)
        self.assertEqual((rc, rejected), (0, {}), said.call_args_list)
        self.assertEqual(a.bus["inbox"][bid]["outcome"], "read")
        with a.env(), self.assertRaises(SystemExit):
            buscli.main(["done"], liaison_put(a, rejected), lambda: a.st, a.h.state_dir, me=liaison.NAME)
        with a.env(), self.assertRaises(SystemExit):
            buscli.main(["done", bid], liaison_put(a, rejected), lambda: a.st, a.h.state_dir, me="api")

    def test_the_liaison_is_told_how_to_close_it(self):
        a, _, _, bid = thanked()
        (msg,) = [m for m in a.messages_to(liaison.NAME) if bid in m["text"]]
        self.assertIn(f"hub bus done {bid}", msg["text"])
        self.assertIn("hub bus done", liaison.PROFILE)


if __name__ == "__main__":
    unittest.main()
