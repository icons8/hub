"""The text the core puts into every task (core/delegations.CONTRACT) is what always reaches the session: it does not
always open the hub skill. agent-dev eval: the "milestones — show them" rule in the skill, on an ordinary
result of a visible change, scored 0/2 (no screenshot, no link); with one phrase in the task, 2/2."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from core import delegations


class TaskContract(unittest.TestCase):
    def test_every_task_asks_to_show_what_is_visible(self):
        for text in (delegations.task_text("make a button", "m1", None, did="d1"),
                     delegations.task_text(None, None, "a button", for_session="maps", by="maps", did="d2"),
                     delegations.task_text(None, None, "a button", did="d3")):
            self.assertIn("Made something visible", text)
            self.assertIn("hub send-file", text)

    def test_every_task_asks_to_show_a_proposal_before_the_question(self):
        """A PR review with a page change went to the owner as a question card without a picture — the session
        read the "made something visible" rule as being about what is done, not what is proposed; a question card has
        no picture."""
        text = delegations.task_text("make a button", "m1", None, did="d1")
        self.assertIn("Proposing something visible", text)
        self.assertIn("hub shot", text)
        self.assertIn("before the question form", text)

    def test_every_task_asks_to_report_a_long_step(self):
        """The session went quiet: it silently ran a full test run or a build mid-turn.
        agent-dev eval: 0/2 without the phrase, 2/2 with it."""
        text = delegations.task_text("make a button", "m1", None, did="d1")
        self.assertIn("Something long", text)
        self.assertIn("run_in_background", text)
        self.assertIn("will write the result", text)

    def test_a_missing_tool_is_requested_not_asked_about(self):
        """A session twice asked the owner what is on a public page that returns 403, instead of taking a browser;
        the owner's view: the task should have been done by itself, using a browser when possible."""
        text = delegations.task_text("compare prices", "m1", None, did="d1")
        self.assertIn("hub tools request browser:", text)
        self.assertIn("do not get around it with search", text)   # agent-dev eval: 0/2 browser requests without it, 2/2 with it
        self.assertIn("403", text)
        self.assertIn("Do not ask the owner what you can find out yourself", text)


    def test_the_language_line_is_in_both_branches_and_only_with_a_known_language(self):
        """The language line is in a task from the owner and in a task from a session (with the mini-hub bus rule)."""
        for text in (delegations.task_text("make a button", "m1", None, did="d1", lang="Russian"),
                     delegations.task_text(None, None, "a button", for_session="maps", by="maps", did="d2", bus="maps",
                                           lang="Russian")):
            self.assertTrue(text.endswith("The language of everything you write is Russian: the turn's result, "
                                          "\"waiting…\", every line between steps — also when command output, "
                                          "notifications and skills are in another language."))
        self.assertNotIn("The language of everything", delegations.task_text(None, None, "a button", for_session="maps",
                                                                              did="d3"))


if __name__ == "__main__":
    unittest.main()
