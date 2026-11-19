"""A "one for all" tool (owner request 2026-10-01: such tools should be handed over easily and automatically on
request; a personal Telegram is held by only one process — a second session failed on a timeout until the first one
released it by hand). In the catalog `exclusive: true`: granted to another session — it is taken from the previous
holder (which restarts without it when idle), and the new one, if it came up earlier, restarts once more — now with
a free tool."""
import tests  # noqa: F401 — do not see the machine's settings
import unittest

from core import catalog, reducer

OWNER = {"channel": "cli", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}


class Exclusive(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.acts = reducer.empty(), 0, []
        self.st["catalog"] = {"tg": {"kind": "mcp", "server": {}, "risk": [], "exclusive": True},
                              "jira": {"kind": "mcp", "server": {}, "risk": []}}
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        for n in ("hub", "a", "b"):
            if n != "hub":
                self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", **OWNER)
            self.ev("session.started", n)
            self.ev("turn.started", n, marker=None, prompt_head="x")
            self.ev("turn.result", n, text="ok")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.acts += acts
        return why

    def spawns(self, name):
        return [a for a in self.acts if a["do"] == "spawn" and a["session"] == name]

    def test_grant_to_another_takes_it_from_the_holder(self):
        self.ev("tools.granted", "a", tool="tg", **HUB)
        self.assertEqual(self.st["sessions"]["a"]["tools"], ["tg"])
        self.ev("turn.started", "a", marker=None, prompt_head="works with tg")
        self.acts.clear()
        self.assertIsNone(self.ev("tools.granted", "b", tool="tg", **HUB))
        self.assertEqual((self.st["sessions"]["a"]["tools"], self.st["sessions"]["b"]["tools"]), ([], ["tg"]))
        self.assertEqual(len(self.spawns("b")), 1)          # b is idle — came up with tg at once
        self.assertEqual(self.spawns("a"), [])              # a is busy — will release after the turn
        self.ev("session.started", "b")                     # b came up while a still holds it
        self.acts.clear()
        self.ev("turn.result", "a", text="ok")              # a released it
        self.assertEqual(len(self.spawns("a")), 1)
        self.assertEqual(len(self.spawns("b")), 1)          # b — once more, with the now free tool
        self.assertNotIn("tg", self.spawns("a")[0]["tools"])

    def test_ordinary_tool_is_shared(self):
        self.ev("tools.granted", "a", tool="jira", **HUB)
        self.ev("tools.granted", "b", tool="jira", **HUB)
        self.assertEqual((self.st["sessions"]["a"]["tools"], self.st["sessions"]["b"]["tools"]), (["jira"], ["jira"]))

    def test_catalog_keeps_the_flag(self):
        tools = catalog.build({"mcp": {"tg": {"server": {}, "exclusive": True}, "j": {"server": {}}}})
        self.assertEqual((tools["tg"].get("exclusive"), tools["j"].get("exclusive")), (True, None))


if __name__ == "__main__":
    unittest.main()
