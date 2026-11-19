"""An external agent's letter (review of PR #300): the page and plugins read who wrote it from the `agent` field of an `in`
timeline row, not from an "[agent X]" label in the text. The field comes from the `message.received` data, and only on a
core module's fact (the core writes it with channel plugin, sender mod:<name>, owner false — core/spool.outsider): the
owner's words (page, Telegram, hubmod.owner), sessions and plugin processes (through `hub` commands) cannot pass as an
agent."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from core import reducer, views

MOD = {"channel": "plugin", "owner": False, "sender": "mod:agent-relay"}


class AgentField(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.events = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", channel="web", owner=True)
        self.ev("session.started", "hub")
        self.ev("session.requested", "a", cwd="/w", session_id="s-a", channel="web", owner=True)
        self.ev("session.started", "a")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        ev = {"seq": self.seq, "ts": 1000 + self.seq, "src": "test", "type": type, "entity": entity, "data": data}
        self.events.append(ev)
        self.st, _, why = reducer.reduce(self.st, ev)
        return why

    def row(self, **data):
        self.assertIsNone(self.ev("message.received", None, to="a", **data))
        r = views.Reader()
        for ev in self.events:
            r.feed(ev)
        (item,) = [i for i in r.timelines["a"] if i.get("kind") == "in" and i["seq"] == self.seq]
        return item

    def test_a_module_fact_carries_the_agent_name(self):
        item = self.row(text="[agent bot] the build is ready", agent="bot", **MOD)
        self.assertEqual(item["agent"], "bot")
        self.assertEqual(item["sender"], "mod:agent-relay")              # the byline "agent X · module Y" comes from both fields
        self.assertEqual(item["from"], {"kind": "module", "name": "agent-relay"})
        self.assertEqual(item["text"], "[agent bot] the build is ready")      # the text as the session got it

    def test_the_name_is_one_short_line(self):
        self.assertEqual(self.row(text="x", agent="  codex\n  audit ", **MOD)["agent"], "codex audit")
        self.assertEqual(self.row(text="x", agent="a" * views.AGENT_MAX, **MOD)["agent"], "a" * views.AGENT_MAX)
        # longer is not a name rather than cut: two agents with a common start do not merge into one
        for bad in ("", "   ", 5, ["bot"], {"name": "bot"}, None, "a" * (views.AGENT_MAX + 1)):
            with self.subTest(agent=bad):
                self.assertNotIn("agent", self.row(text="x", agent=bad, **MOD))

    def test_a_name_with_invisible_or_reordering_characters_is_not_a_name(self):
        # the byline "Agent X · module Y" is one line: a direction change mark in X would reorder "module Y" (the module
        # would hide itself), a zero-width space would make two different names indistinguishable, ESC would reach the
        # terminal and apps
        for name, bad in {"RLO": "bot " + chr(0x202E) + "ab", "isolate": chr(0x2067) + "bot", "ZWSP": "bot" + chr(0x200B) + "x",
                          "mark": "bot" + chr(0x200F), "BOM": chr(0xFEFF) + "bot", "ESC": "bot" + chr(0x1B) + "[2J",
                          # str.split() takes these for spaces: they are rejected, not turned into a space
                          "US": "bot" + chr(0x1F) + "x", "LS": "bot" + chr(0x2028) + "x"}.items():
            with self.subTest(name):
                self.assertNotIn("agent", self.row(text="x", agent=bad, **MOD))
        # ordinary characters are a name, including emoji and a zero-width joiner inside a compound emoji
        for ok in ("\U0001F47E dot", "\U0001F469" + chr(0x200D) + "\U0001F4BB dev"):
            with self.subTest(ok):
                self.assertEqual(self.row(text="x", agent=ok, **MOD)["agent"], ok)

    def test_nobody_but_a_module_names_an_agent(self):
        cases = {
            "page": {"channel": "web", "owner": True},
            "Telegram": {"channel": "telegram", "owner": True},
            "owner words from a module handle": {"channel": "mod:agent-relay", "owner": True},
            "session": {"channel": "session", "owner": False, "sender": "hub"},
            "plugin process": {"channel": "plugin", "owner": False, "sender": "ext:relay"},
            "plugin of an older version": {"channel": "plugin", "owner": False, "sender": "relay"},
            "module, but the owner's word": {**MOD, "owner": True},
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.assertNotIn("agent", self.row(text="[agent bot] hello", agent="bot", **data))

    def test_a_plain_module_message_has_no_agent(self):
        self.assertNotIn("agent", self.row(text="the module writes by itself", **MOD))


if __name__ == "__main__":
    unittest.main()
