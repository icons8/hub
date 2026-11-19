"""The Hubnet network is removed from the hub (owner decision 2026-10-02: remove the network from the hub code, it is
no longer needed). Its events stay in old journals: the fold rejects them without crashing or touching state."""
import tests  # noqa: F401 — do not see the machine's settings
import unittest

from core import reducer


class NoNet(unittest.TestCase):
    def test_old_network_events_are_rejected_quietly(self):
        st = reducer.empty()
        for i, (kind, data) in enumerate((("net.request", {"from": "alice", "text": "hi", "channel": "net"}),
                                          ("net.sent", {"channel": "net"}), ("net.grant", {"agent": "a"}))):
            st, actions, why = reducer.reduce(st, {"seq": i + 1, "ts": 1, "type": kind, "entity": "t1", "data": data})
            self.assertEqual((actions, why), ([], reducer.NO_HUBNET))
        self.assertNotIn("net", st)


    def test_former_network_operator_can_be_put_to_sleep_by_the_hub(self):
        # an agent is assigned and dismissed by the owner, while the network operator was created by the core — there
        # is no network, the hub removes it itself
        st, seq = reducer.empty(), 0

        def ev(type, entity=None, **data):
            nonlocal st, seq
            seq += 1
            st, _, why = reducer.reduce(st, {"seq": seq, "ts": 100 + seq, "type": type, "entity": entity, "data": data})
            return why
        own = {"channel": "web", "owner": True}
        ev("session.requested", "hub", cwd="/w", session_id="s-h", kind="hub", **own)
        ev("session.started", "hub")
        ev("session.requested", "net", cwd="/n", session_id="s-n", kind="operator", role="agent", about="operator", **own)
        ev("session.started", "net")
        self.assertIsNone(ev("session.close_requested", "net", channel="session", owner=False, sender="hub"))
        self.assertNotEqual(st["sessions"]["net"]["closing"], "none")


if __name__ == "__main__":
    unittest.main()
