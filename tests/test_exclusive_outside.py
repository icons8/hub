"""An "exclusive" tool (a personal Telegram) did not come up in a session, and nobody else in the hub holds it — so a
process outside the hub holds it (2026-10-08: Claude in the owner's Terminal kept a Telegram session for a day, the
hub sessions' MCP failed with CONNECTION_CLOSED, and the hub did not know why). The hub gets one line: what is busy,
whom to look for, what to do."""
import tests  # noqa: F401 — keep the machine's settings out
import unittest

from core import reducer

OWNER = {"channel": "cli", "owner": True}
TG = {"kind": "mcp", "exclusive": True, "risk": ["owner-data"], "description": "",
      "server": {"type": "stdio", "command": "/x/uv", "args": ["run", "chat-mcp"]}}


class Outside(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("catalog.loaded", None, tools={"chat": TG, "jira": {"kind": "mcp", "risk": [], "server": {}}})
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.started", "hub")
        for n in ("a", "b"):
            self.ev("session.requested", n, cwd="/w", session_id="s-" + n, **OWNER)
            self.ev("session.started", n)
        self.st["sessions"]["a"]["tools"] = ["chat"]

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def to_hub(self):
        return [m["text"] for m in self.st["messages"].values() if m["session"] == "hub"]

    def test_an_exclusive_tool_that_failed_with_no_holder_in_the_hub_is_named_to_the_hub(self):
        self.ev("session.info", "a", mcp=["chat"], mcp_failed=["chat"])
        said = [t for t in self.to_hub() if "chat" in t and "outside the hub" in t]
        self.assertEqual(len(said), 1, self.to_hub())
        self.assertIn("chat-mcp", said[0])                        # how to find the holder: by the server command
        self.ev("session.info", "a", mcp=["chat"], mcp_failed=["chat"])
        self.assertEqual(len([t for t in self.to_hub() if "outside the hub" in t]), 1)   # not repeated in a loop

    def test_an_ordinary_tool_or_one_held_inside_the_hub_is_not_blamed_on_outside(self):
        self.ev("session.info", "a", mcp=["jira"], mcp_failed=["jira"])
        self.st["sessions"]["b"]["tools"] = ["chat"]              # inside the hub, b holds it
        self.st["sessions"]["b"]["info"] = {"mcp": ["chat"], "mcp_failed": []}
        self.ev("session.info", "a", mcp=["chat"], mcp_failed=["chat"])
        self.assertEqual([t for t in self.to_hub() if "outside the hub" in t], [])

    def test_once_it_connects_a_later_failure_is_told_again(self):
        self.ev("session.info", "a", mcp=["chat"], mcp_failed=["chat"])
        self.ev("session.info", "a", mcp=["chat"], mcp_failed=[])
        self.ev("session.info", "a", mcp=["chat"], mcp_failed=["chat"])
        self.assertEqual(len([t for t in self.to_hub() if "outside the hub" in t]), 2)

    def test_a_server_that_a_session_knows_by_as_is_found_in_the_catalog(self):
        # the session reports the server under its `as`; the exclusive flag is the entry's, found by its id
        self.ev("catalog.loaded", None, tools={"chat": {**TG, "as": "plugin:tg:chat"}})
        self.ev("session.info", "a", mcp=["plugin:tg:chat"], mcp_failed=["plugin:tg:chat"])
        self.assertEqual(len([t for t in self.to_hub() if "outside the hub" in t]), 1, self.to_hub())
        self.st["sessions"]["b"]["tools"] = ["chat"]               # held inside the hub by the entry's id
        self.st["outside_told"] = []
        self.ev("session.info", "a", mcp=["plugin:tg:chat"], mcp_failed=[])
        self.ev("session.info", "a", mcp=["plugin:tg:chat"], mcp_failed=["plugin:tg:chat"])
        self.assertEqual(len([t for t in self.to_hub() if "outside the hub" in t]), 1, self.to_hub())   # not blamed again


if __name__ == "__main__":
    unittest.main()
