"""The hub's `hub tools grant <session> <risky tool> --for mN|qN` counts only when the owner's words name the tool: his
message to the hub, or the question of the card he answered (with his answer). Before, the hub alone was let through on
any fresh word of his: "show me the status" became a yes to a server with his data, and a session talked into it by a
page or a mail got a risky tool without the owner ever seeing its name. A tool without a risk needs no word at all."""
import unittest

from core import reducer, tools

OWNER = {"channel": "web", "owner": True}
CATALOG = {"atlas": {"kind": "mcp", "risk": ["owner-data"], "description": "maps with the owner's sign-in"},
           "tiles@geo": {"kind": "plugin", "risk": ["code"], "plugin_name": "tiles", "description": "tile hooks"},
           "notes": {"kind": "mcp", "risk": [], "description": "plain notes"}}


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


class HubNamesTheTool(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.ts = reducer.empty(), 0, 1000
        self.ev("catalog.loaded", None, tools=CATALOG)
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "maps", cwd="/m", session_id="s-maps", **OWNER)
        self.ev("session.started", "hub")
        self.ev("session.started", "maps")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.ts += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.ts, "type": type, "entity": entity,
                                                   "data": data})
        return why

    def said(self, text):
        self.assertIsNone(self.ev("message.received", None, text=text, **OWNER))
        return f"m{self.seq}"

    def card(self, question, label, rid):
        self.ev("turn.started", "maps", marker=None, prompt_head="x")
        self.ev("permission.requested", "maps", request_id=rid, tool="AskUserQuestion", interactive=True,
                input={"questions": [{"question": question, "header": "Tool",
                                      "options": [{"label": label}, {"label": "No"}]}]})
        self.ev("turn.result", "maps", text="asked")
        self.ev("turn.stopped", "maps")
        q = self.st["sessions"]["maps"]["asks"][-1]["id"]
        self.assertIsNone(self.ev("permission.answered", "maps", request_id=rid, behavior="allow",
                                  answers={question: label}, **OWNER))
        return q

    def grant(self, tool, ref):
        return self.ev("tools.granted", "maps", tool=tool, **{"for": ref}, **by("hub"))

    def tools_of(self):
        return self.st["sessions"]["maps"]["tools"]

    def test_an_unrelated_word_of_the_owner_is_no_yes_to_a_risky_tool(self):
        ref = self.said("show me the status")
        why = self.grant("atlas", ref)
        self.assertNotIn("atlas", self.tools_of())
        self.assertIn("do not name atlas", why or "")
        why = self.grant("tiles@geo", ref)
        self.assertNotIn("tiles@geo", self.tools_of())
        self.assertIn("do not name tiles@geo", why or "")

    def test_his_word_that_names_it_counts(self):
        self.assertIsNone(self.grant("atlas", self.said("connect atlas to maps")))
        self.assertIsNone(self.grant("tiles@geo", self.said("maps may have the tiles plugin")))   # the plugin's name
        self.assertEqual(self.tools_of(), ["atlas", "tiles@geo"])

    def test_his_answer_counts_when_the_cards_question_names_the_tool(self):
        q = self.card("Connect atlas to maps for the report?", "Yes", "r1")
        self.assertIsNone(self.grant("atlas", q))
        self.assertIn("atlas", self.tools_of())

    def test_his_answer_to_a_card_that_does_not_name_it_is_not_a_yes(self):
        q = self.card("Shall the agent do it by itself?", "The agent itself", "r1")
        why = self.grant("atlas", q)
        self.assertNotIn("atlas", self.tools_of())
        self.assertIn("do not name atlas", why or "")

    def test_a_sleeping_sessions_request_takes_the_same_rule(self):
        # the grant to a session that is not up goes through its own path (tools.apply, "asleep")
        self.ev("tools.requested", "maps", tool="atlas", why="the report", **by("maps"))
        self.ev("tools.granted", "maps", tool="atlas", **by("hub"))           # no --for: waits for the owner
        self.assertEqual(self.st["sessions"]["maps"]["requests"][-1]["state"], "needs-owner")
        self.st["sessions"]["maps"]["state"] = "asleep"
        night = self.said("good night")
        why = self.grant("atlas", night)
        self.assertNotIn("atlas", self.tools_of())
        self.assertIn("do not name atlas", why or "")
        req = self.st["sessions"]["maps"]["requests"][-1]
        self.assertNotIn("owner_words", req)
        self.seq += 1                                                         # a replay folds a refusal in place
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": self.ts, "type": "tools.granted",
                                                   "entity": "maps", "data": {"tool": "atlas", "for": night,
                                                                              **by("hub")}}, "accepted")
        self.assertIn("do not name atlas", why or "")
        self.assertNotIn("owner_words", self.st["sessions"]["maps"]["requests"][-1])
        self.assertIsNone(self.grant("atlas", self.said("yes, atlas for maps")))
        self.assertIn("atlas", self.tools_of())

    def test_a_tool_without_a_risk_needs_no_word(self):
        self.assertIsNone(self.ev("tools.granted", "maps", tool="notes", **by("hub")))
        self.assertIn("notes", self.tools_of())

    def test_names_tool_reads_the_whole_answer_text(self):
        q = self.card("Which way?", "Connect atlas", "r1")                   # named in his chosen option
        words = tools.owner_word(self.st, {"for": q, **by("hub")}, self.ts, cards=True)
        self.assertTrue(tools.names_tool(self.st, words, "atlas"))
        self.assertFalse(tools.names_tool(self.st, words, "tiles@geo"))


if __name__ == "__main__":
    unittest.main()
