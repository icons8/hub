"""The skills line (2026-11-18, the kit audit): every session's addendum ends with the rule to open the skill of its set that
fits the task before the first step, and not to follow an older recipe from its own file instead. The text is the owner's
decision (verbatim); it is the last core part, for every kind of session (the Codex thread: tests/test_codex_liaison)."""
import tests  # noqa: F401 — hide the machine's settings
import unittest

from hosts import streamjson
from tests.test_modapi_prompt import Base

LINE = ("Skills: the reminder at the start of the conversation lists the skills of your set. Before the first step of a task, "
        "check that list: if the task falls under a skill's description (data through an MCP server, a deploy, a manifest, the "
        "repo's work procedure), open that skill with the Skill tool first and work by it. A recipe in your own file or memory "
        "does not replace it: the skill is kept up to date, your file may be older.")


class TheLineInTheAddendum(Base):
    def test_the_text_is_the_decided_one_in_one_paragraph(self):
        self.assertEqual(streamjson.SKILLS, LINE)
        self.assertNotIn("\n", streamjson.SKILLS)

    def test_every_kind_of_session_ends_with_it(self):
        h = self.host()
        agent = self.agent(h)
        for kw in ({}, {"minihub": True}, {"agents": [("maps", "map work")]}, {"executor_of": "maps"}, {"bus": "maps"},
                   {"executor_of": "maps", "bus": "maps"}):
            self.assertTrue(self.prompt(h.command("w", "s-1", agent_file=agent, **kw)).endswith(LINE + "\n"), kw)
        self.assertTrue(self.prompt(h.command("w", "s-1")).endswith(LINE + "\n"))
        self.assertTrue(self.prompt(h.command("liaison", "s-1", liaison=True)).endswith(LINE + "\n"))

    def test_codex_differs_only_where_the_skill_tool_is_named(self):
        self.assertEqual(streamjson.SKILLS_CODEX, LINE.replace("with the Skill tool first", "first (read its SKILL.md)"))
        self.assertNotIn("Skill tool", streamjson.SKILLS_CODEX)


if __name__ == "__main__":
    unittest.main()
