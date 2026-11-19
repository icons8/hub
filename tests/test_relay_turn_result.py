"""A session answers another's plain `hub send` with the turn's text, not with `hub send` back (the asker saw
nothing and re-asked later). The core sends the turn result to the asker, once; not for `--fyi`, not for a
short acknowledgement, only to an asker that said it waits (`hub waiting`), not when the answer was sent by `hub send`, not for the hub, not for a relay itself (no ping-pong)."""
import tests  # noqa: F401 — ignore the machine's settings
import unittest

from core import reducer

OWNER = {"channel": "web", "owner": True}


class RelayTurnResult(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.started", "hub")
        for n in ("maps", "tiles"):
            self.ev("session.requested", n, cwd=f"/w/{n}", session_id=f"s-{n}", **OWNER)
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, self.acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                           "entity": entity, "data": data})
        return why

    def send(self, to, text, sender, waits=True, **extra):
        self.assertIsNone(self.ev("message.received", None, to=to, text=text, channel="session", owner=False,
                                  sender=sender, **extra))
        mid = f"m{self.seq}"
        if waits and sender != "hub":   # the asker signals that it waits for the answer
            self.assertIsNone(self.ev("session.waiting", sender, text=f"answer {to}", channel="session", sender=sender))
        return mid

    def turn(self, name, mid, result, send_to=None):
        self.ev("turn.started", name, marker=mid, prompt_head="x")
        if send_to:
            self.send(send_to, "here is the answer", name)
        self.ev("turn.result", name, text=result)
        self.ev("turn.stopped", name)

    def got(self, name):
        return [m for m in self.st["messages"].values() if m["session"] == name and m.get("relay")]

    def test_a_text_answer_goes_to_the_asker(self):
        mid = self.send("tiles", "how many tiles are left in region 3", "maps")
        self.turn("tiles", mid, "Region 3 has 40 tiles left, will finish by evening")
        (m,) = self.got("maps")
        self.assertIn("40 tiles left", m["text"])
        self.assertIn(mid, m["origin"])

    def test_a_plain_report_without_waiting_is_not_forwarded(self):
        mid = self.send("tiles", "done, here is the task report", "maps", waits=False)
        self.turn("tiles", mid, "Accepted, I will answer the owner separately, thanks")
        self.assertEqual(self.got("maps"), [])

    def test_a_stale_waiting_is_not_a_signal(self):
        mid = self.send("tiles", "how many tiles are left in region 3", "maps")
        self.send("maps", "not needed for now", "tiles", waits=False)   # written to since: its word about waiting went stale
        self.turn("tiles", mid, "Region 3 has 40 tiles left, will finish by evening")
        self.assertEqual(self.got("maps"), [])

    def test_an_answer_by_hub_send_is_not_doubled(self):
        mid = self.send("tiles", "how many tiles are left in region 3", "maps")
        self.turn("tiles", mid, "Region 3 has 40 tiles left, will finish by evening", send_to="maps")
        self.assertEqual(self.got("maps"), [])

    def test_fyi_and_short_results_and_the_hub_get_nothing(self):
        mid = self.send("tiles", "for your information", "maps", fyi=True)
        self.turn("tiles", mid, "Accepted, will take this into account in the next checks")
        mid = self.send("tiles", "do it", "maps")
        self.turn("tiles", mid, "—")
        mid = self.send("tiles", "do it", "hub")
        self.turn("tiles", mid, "Done, here is the result of the work and details")
        self.assertEqual((self.got("maps"), self.got("hub")), ([], []))

    def test_the_relay_is_not_relayed_back(self):
        mid = self.send("tiles", "how many tiles are left in region 3", "maps")
        self.turn("tiles", mid, "Region 3 has 40 tiles left, will finish by evening")
        (m,) = self.got("maps")
        self.turn("maps", m["id"], "Thanks, rolling out the style after the redraw")
        self.assertEqual(self.got("tiles"), [])

    def test_a_turn_cut_without_a_result_does_not_send_the_next_one_to_the_old_asker(self):
        mid = self.send("tiles", "how many tiles are left in region 3", "maps")
        self.ev("turn.started", "tiles", marker=mid, prompt_head="x")
        self.ev("turn.stopped", "tiles")   # no result: the process was cut
        self.assertIsNone(self.ev("message.received", None, to="tiles", text="report to me", **OWNER))
        self.turn("tiles", f"m{self.seq}", "To the owner: region 3 is redrawn, 40 tiles left")
        self.assertEqual(self.got("maps"), [])
