"""The "only the hub or the owner" refusal does not talk about a different action (`hub session remove` from a
session got "new sessions are opened by the hub" — about opening, though removal was asked)."""
import tests  # noqa: F401 — ignore this machine's settings
import unittest

from core import reducer

OWNER = {"channel": "cli", "owner": True}


class GuardReason(unittest.TestCase):
    def test_remove_by_a_session_is_refused_with_a_reason_about_removing(self):
        st, seq = reducer.empty(), 0

        def ev(kind, entity=None, **data):
            nonlocal st, seq
            seq += 1
            st, _, why = reducer.reduce(st, {"seq": seq, "ts": 1000 + seq, "type": kind, "entity": entity, "data": data})
            return why
        for n in ("maps", "old"):
            ev("session.requested", n, cwd="/w", session_id=f"s-{n}", **OWNER)
            ev("session.started", n, session_id=f"s-{n}")
        st["sessions"]["old"]["state"] = "asleep"   # asleep — only the hub or the owner can remove it
        why = ev("session.remove_requested", "old", channel="session", owner=False, sender="maps")
        self.assertNotIn("new sessions", why)            # not about opening
        self.assertIn("the hub or the owner", why)


if __name__ == "__main__":
    unittest.main()
