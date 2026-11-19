"""The idle rule (2026-10-30): a session that has a queue, a backlog or executors walks the queue before it ends a turn, names what
holds each task without an executor and launches the rest. The rule is a part of the system prompt (hosts/streamjson.IDLE), so
compaction does not lose it; an executor has neither a board nor a queue, so it does not get it. The gates of the owner's yes
(prod, money, clients, the irreversible) stay in the text."""
import tests  # noqa: F401 — hide the machine's settings
import unittest

from hosts import streamjson
from tests.test_modapi_prompt import Base, Stub, parts_of


class TheRuleInTheSystemPrompt(Base):
    def test_a_hub_a_lead_and_a_mini_hub_get_the_part(self):
        h = self.host()
        agent = self.agent(h)
        for kw in ({}, {"minihub": True}, {"agents": [("maps", "map work")]}):
            self.assertIn(streamjson.IDLE, self.prompt(h.command("w", "s-1", agent_file=agent, **kw)), kw)
        self.assertIn(streamjson.IDLE, self.prompt(h.command("w", "s-1")))   # a session without an agent file too

    def test_an_executor_has_no_part_it_has_no_queue(self):
        h = self.host()
        for kw in ({"bus": "maps"}, {"executor_of": "maps"}, {"executor_of": "maps", "agents": [("maps", "map work")]},
                   {"bus": "maps", "executor_of": "maps"}):
            self.assertNotIn(streamjson.IDLE, self.prompt(h.command("w", "s-1", agent_file=self.agent(h), **kw)), kw)

    def test_the_liaison_has_no_part_it_only_carries_the_bus(self):
        h = self.host()
        self.assertNotIn(streamjson.IDLE, self.prompt(h.command("liaison", "s-1", liaison=True)))

    def test_it_stands_right_after_autonomy(self):
        h = self.host()
        text = self.prompt(h.command("w", "s-1", agent_file=self.agent(h)))
        # then the helpers part (whom the work goes to), with the same sessions
        self.assertIn(streamjson.AUTONOMY + "\n\n" + streamjson.IDLE + "\n\n" + streamjson.HELPERS + "\n\n"
                      + streamjson.FINDINGS, text)

    def test_the_text_is_one_paragraph_and_keeps_the_gates(self):
        self.assertNotIn("\n", streamjson.IDLE)
        self.assertLess(len(streamjson.IDLE), 2200)   # was 1500; the queue-sync part (2026-11-19) added ~640, eval-checked as a whole (agent-dev evals/idle-sync)
        for gate in ("prod", "money", "clients", "the irreversible", "only on the owner's \"yes\""):
            self.assertIn(gate, streamjson.IDLE)

    def test_the_part_is_named_idle_for_modules(self):
        seen = []
        h = self.host(Stub(m=lambda p, d: seen.append([x["name"] for x in parts_of(d)])))
        h.command("w", "s-1", agent_file=self.agent(h))
        self.assertEqual(seen[0].index("idle"), seen[0].index("autonomy") + 1)


if __name__ == "__main__":
    unittest.main()
