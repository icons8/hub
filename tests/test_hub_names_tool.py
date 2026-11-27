"""The hub's `hub tools grant <session> <risky tool> --for mN|qN` counts only when the owner's words name the tool: his
message to the hub, or the question of the card he answered (with his answer). Before, the hub alone was let through on
any fresh word of his: "show me the status" became a yes to a server with his data, and a session talked into it by a
page or a mail got a risky tool without the owner ever seeing its name. A tool without a risk needs no word at all."""
import unittest

from core import reducer, tools

OWNER = {"channel": "web", "owner": True}
CATALOG = {"atlas": {"kind": "mcp", "risk": ["owner-data"], "description": "maps with the owner's sign-in"},
           "tiles@geo": {"kind": "plugin", "risk": ["code"], "plugin_name": "tiles", "description": "tile hooks"},
           "notes": {"kind": "mcp", "risk": [], "description": "plain notes"},
           "pic-maker": {"kind": "plugin", "risk": ["code"], "description": "pictures"}}


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

    def test_a_name_said_without_its_separator_counts(self):
        # a voice message writes "pic maker" for pic-maker: the core refused the hub's grant, the owner got a card
        self.assertIsNone(self.grant("pic-maker", self.said("give maps the pic maker")))
        self.assertIn("pic-maker", self.tools_of())

    def test_separators_inside_a_name_are_one(self):
        def named(text, names):
            return tools.names_any({"messages": {}}, {"text": text}, names)
        for text in ("use gpt image", "use gpt_image", "use gptimage", "use GPT-Image", "use gpt.image",
                     "gpt image, please", "(gpt image)"):
            self.assertTrue(named(text, {"gpt-image"}), text)
        self.assertTrue(named("the gpt image 2 one", {"gpt-image-2"}))
        self.assertTrue(named("the pic-maker", {"pic_maker"}))
        for text in ("gpt imagery", "my-gpt-image-2", "gpt-image-2", "agpt image", "gpt image_x", "gpt imaging"):
            self.assertFalse(named(text, {"gpt-image"}), text)
        # a source or an entry names itself as written
        self.assertFalse(named("alice notes", {"alice/notes"}))
        self.assertFalse(named("tiles geo", {"tiles@geo"}))
        self.assertTrue(named("alice/notes", {"alice/notes"}))

    @staticmethod
    def named(text, names):
        return tools.names_any({"messages": {}}, {"text": text}, names)

    def test_a_folder_or_a_key_is_said_as_written(self):
        # Windows: hub module add names a folder by its full path, with no /: another folder passed the gate
        path = tools.name_forms(r"C:\x\tools\my_mod", bare=False)
        self.assertFalse(self.named(r"add C:\x\tools\my-mod", path))
        self.assertFalse(self.named(r"add C:\x\tools\mymod", path))
        self.assertTrue(self.named(r"add C:\x\tools\my_mod", path))
        self.assertFalse(self.named("alice notes", {"alice:notes"}))
        self.assertTrue(self.named("alice:notes", {"alice:notes"}))

    def test_a_name_written_as_it_is_names_itself(self):
        for name in ("foo__bar", "_private", "foo_", "a--b-c"):
            self.assertTrue(self.named(f"use {name} please", {name}), name)
        # a separator at the start or the end is part of the name
        self.assertFalse(self.named("use private", {"_private"}))
        self.assertFalse(self.named("use foo", {"foo_"}))

    def test_short_parts_and_numbers_are_not_glued(self):
        for text, name in (("ai", "a-i"), ("an api call", "a-p-i"), ("gpt-41", "gpt-4.1"), ("gpt 41", "gpt-4.1"),
                           ("gpt 4-1", "gpt-4.1"), ("gptimage2", "gpt-image-2"), ("v12", "v-1-2"),
                           # two digits are not glued at the edge of a longer part either: "370" is no 3-70
                           ("llama370b", "llama3-70b")):
            self.assertFalse(self.named(text, {name}), (text, name))
        for text in ("llama3-70b", "llama3 70b", "llama3.70b"):
            self.assertTrue(self.named(text, {"llama3-70b"}), text)
        for text, name in (("a i", "a-i"), ("a.p.i", "a-p-i"), ("gpt-4.1", "gpt-4.1"), ("gpt 4.1", "gpt-4.1"),
                           ("gpt image 2", "gpt-image-2"), ("gpt image 2", "gpt-image"),
                           ("gptimage", "gpt-image")):
            self.assertTrue(self.named(text, {name}), (text, name))


if __name__ == "__main__":
    unittest.main()
