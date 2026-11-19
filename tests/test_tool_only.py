"""The "only for" tool: with `only: [session]` in the catalog it can be granted only to the named ones; to the rest the
core replies whom to ask. Revoking works for sleeping and parked sessions too: without it a tool could not be taken
off the fleet."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from core import catalog, reducer

OWNER = {"channel": "cli", "owner": True}
ONLY = {"kind": "mcp", "risk": [], "only": ["secretary"], "only_note": "write through hub send secretary"}


class ToolOnly(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("catalog.loaded", None, tools={"mail": ONLY})
        for name in ("secretary", "a"):
            self.ev("session.requested", name, cwd="/w", session_id="s-" + name, **OWNER)
            self.ev("session.started", name)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.seq, "type": type, "entity": entity,
                                                   "data": data})
        return why

    def test_only_named_session_gets_it(self):
        self.assertIn("hub send secretary", self.ev("tools.granted", "a", tool="mail", **OWNER))
        self.assertNotIn("mail", self.st["sessions"]["a"]["tools"])
        self.assertIsNone(self.ev("tools.granted", "secretary", tool="mail", **OWNER))
        self.assertIn("mail", self.st["sessions"]["secretary"]["tools"])

    def test_request_from_another_session_is_answered_not_queued(self):
        why = self.ev("tools.requested", "a", tool="mail", why="needed", channel="session", owner=False, sender="a")
        self.assertIn("hub send secretary", why)
        self.assertEqual(self.st["sessions"]["a"]["requests"], [])

    def test_revoke_works_for_asleep_session(self):
        self.ev("tools.granted", "secretary", tool="mail", **OWNER)
        self.st["sessions"]["secretary"]["state"] = "asleep"
        self.assertIsNone(self.ev("tools.revoked", "secretary", tool="mail", **OWNER))
        self.assertNotIn("mail", self.st["sessions"]["secretary"]["tools"])

    def test_grant_to_asleep_session_waits_for_its_wake(self):
        self.st["sessions"]["secretary"]["state"] = "asleep"   # 19.10: into the set at once, the process — with the wake-up
        self.assertIsNone(self.ev("tools.granted", "secretary", tool="mail", **OWNER))
        self.assertEqual(self.st["sessions"]["secretary"]["tools"], ["mail"])
        self.st["sessions"]["a"]["state"] = "asleep"   # one not named — the same reason as for a live one: whom to ask
        self.assertIn("hub send secretary", self.ev("tools.granted", "a", tool="mail", **OWNER))

    def test_catalog_carries_only_from_config(self):
        built = catalog.build({"mcp": {"m": {"server": {"command": "true"}, "only": ["secretary"],
                                             "only_note": "x"}}})
        self.assertEqual(built["m"]["only"], ["secretary"])
        self.assertEqual(built["m"]["only_note"], "x")


if __name__ == "__main__":
    unittest.main()
