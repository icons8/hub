"""The address "hub" (a session "sent the hub a request" and the hub did not
answer. Sessions write `hub send hub …`, as the skill teaches, while the hub session is named hub-core: the core
treated the address as unknown and silently marked the message "not delivered" — 8 of those in a day). "hub" and
its Russian equivalent both mean the hub session; an unknown address from a session is rejected with a reason, not silently lost."""
import tests  # noqa: F401 — keep this machine's settings out
import unittest

from core import reducer
from tests.fakes import lang

HUB_RU = lang.word("words", "hub")   # the hub's name in Russian
OWNER = {"channel": "cli", "owner": True}
FROM_A = {"channel": "session", "owner": False, "sender": "a"}


class Address(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub-core", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.requested", "a", cwd="/w", session_id="s-a", **OWNER)
        self.ev("session.started", "hub-core")
        self.ev("session.started", "a")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.seq, "type": type, "entity": entity,
                                                   "data": data})
        return why

    def last(self):
        return self.st["messages"][f"m{self.seq}"]

    def test_hub_is_the_hub_session(self):
        for word in ("hub", HUB_RU, "Hub"):
            self.assertIsNone(self.ev("message.received", None, to=word, text="move me to a direction", **FROM_A))
            self.assertEqual(self.last()["session"], "hub-core")

    def test_unknown_address_from_a_session_is_rejected_with_a_reason(self):
        why = self.ev("message.received", None, to="nope", text="x", **FROM_A)
        self.assertIn("no session nope", why)
        self.assertIn("hub status", why)
        self.assertNotIn(f"m{self.seq}", self.st["messages"])
        # from the owner it is not lost: it goes to the hub with the addressee named
        self.assertIsNone(self.ev("message.received", None, to="nope", text="x", **OWNER))
        self.assertEqual(self.last()["session"], "hub-core")
        self.assertIn("addressed to nope", self.last()["origin"])

    def test_tools_with_hub_address_go_to_the_hub_session(self):
        # the fleet skill teaches "hub tools grant <session> <id>, for yourself too: <session> = hub", yet the hub
        # session has a different name
        self.ev("catalog.loaded", None, tools={"t": {"kind": "skill"}})
        for word in ("hub", HUB_RU):
            self.assertIsNone(self.ev("tools.granted", word, tool="t", **OWNER))
            self.assertIn("t", self.st["sessions"]["hub-core"]["tools"])
            self.assertIsNone(self.ev("tools.revoked", word, tool="t", **OWNER))
            self.assertNotIn("t", self.st["sessions"]["hub-core"]["tools"])
        self.assertEqual(self.ev("tools.granted", "nope", tool="t", **OWNER), "no live session")

    def test_cli_writes_the_real_hub_name(self):
        # otherwise the page feed would attach the entry to a nonexistent "hub"
        import os
        import subprocess
        import sys
        import tempfile
        from core import journal, spool
        state = tempfile.mkdtemp(prefix="addr-")
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        j.append("session.requested", "hub-core", {"cwd": "/h", "session_id": "s", "kind": "hub", **OWNER})
        j.append("catalog.loaded", None, {"tools": {"t": {"kind": "skill"}}})
        hub = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0"}
        env.pop("HUB_SESSION", None)
        subprocess.run([sys.executable, hub, "tools", "grant", HUB_RU, "t"], capture_output=True, text=True, env=env)
        (item,) = [e for _, e in spool.take(os.path.join(state, "spool"))]
        self.assertEqual((item["type"], item["entity"]), ("tools.granted", "hub-core"))


if __name__ == "__main__":
    unittest.main()
