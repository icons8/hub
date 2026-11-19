"""The catalog marks tools from `base` (every session has them): the core does not ask the owner's "yes" for them (07.10)."""
import tests  # noqa: F401 — keep this machine's settings out
import unittest

from core import catalog


class BaseFlag(unittest.TestCase):
    def test_base_tools_are_marked(self):
        cfg = {**catalog.DEFAULTS, "base": ["core:hub-base", "crm"],
               "mcp": {"crm": {"description": "CRM", "risk": ["external"], "server": {}},
                       "mail": {"description": "mail", "risk": ["owner-data"], "server": {}}}}
        tools = catalog.build(cfg)
        self.assertTrue(tools["crm"].get("base"))
        self.assertTrue(tools["core:hub-base"].get("base"))
        self.assertFalse(tools["mail"].get("base"))



class BaseDropped(unittest.TestCase):
    """14.10: revoking a base tool (`base` in hub.json, present on every session) is refused with a reason: before,
    `hub tools revoke` silently removed the grant while base kept the tool on the session."""
    TOOL = {"kind": "mcp", "server": {}, "risk": ["external"]}

    def run_events(self, evs):
        from core import reducer
        st, out, self.acts = reducer.empty(), [], []
        for seq, (t, ent, data) in enumerate(evs, 1):
            st, self.acts, why = reducer.reduce(st, {"seq": seq, "ts": 100 + seq, "src": "web", "type": t, "entity": ent,
                                             "data": data})
            out.append(why)
        return st, out

    def start(self, name, tools=()):
        return [("session.requested", name, {"cwd": "/w", "session_id": name, "tools": list(tools)}),
                ("session.started", name, {})]

    def test_revoking_a_base_tool_is_refused_with_the_reason(self):
        evs = [("catalog.loaded", None, {"tools": {"crm": {**self.TOOL, "base": True}}})] + self.start("a", ["crm"]) \
            + [("tools.revoked", "a", {"tool": "crm"})]
        st, out = self.run_events(evs)
        self.assertIn("base", out[-1] or "")
        self.assertEqual(st["sessions"]["a"]["tools"], ["crm"])


if __name__ == "__main__":
    unittest.main()
