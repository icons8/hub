"""The reminder "<session> is waiting for your answer" is readable: each question of the card is its own paragraph, the
question sentence is bold, long file and design links are short (four questions glued into one wall of text with " / ",
with links of 90 characters inside). The one-line `text` of the row stays as it was; the Markdown is the new `md` field."""
import tests  # noqa: F401 — hide the machine's settings
import unittest

from core import i18n, owner, views

FIGMA = "https://www.figma.com/design/abc123/Portal?node-id=5125-6744"
QUESTIONS = [
    {"question": "Accept the screen mockups? These are the screens for the recolor. Shots: "
                 "file:///home/me/shots/recolor/. If you accept, I move the task to Done."},
    {"question": "Accept the API key dates mockup (DOC-83, Portal)? Under the key, a line \"Created\". Mockup: " + FIGMA
                 + ", shots: file:///home/me/shots/DOC-83/. If not, it has to be redone."},
    {"question": "Accept the statistics page mockup (DOC-868)? It has a period picker and a CSV export."},
    {"question": "Do it now or later?"},
]


def events(questions):
    base = [("session.requested", "maps", {"cwd": "/m", "session_id": "s-m", "channel": "web", "owner": True}),
            ("session.started", "maps", {}),
            ("turn.started", "maps", {"marker": None, "prompt_head": "x"}),
            ("permission.requested", "maps", {"request_id": "r1", "tool": "AskUserQuestion", "interactive": True,
                                              "input": {"questions": questions}}),
            ("timer.fired", "q:maps:r1", {"n": 1})]
    return [{"seq": i + 1, "ts": 1000 + i, "type": t, "entity": e, "data": d} for i, (t, e, d) in enumerate(base)]


def remind_of(questions):
    r = views.Reader()
    r.feed_all(events(questions))
    (it,) = [it for it in r.timelines["maps"] if it["kind"] == "remind"]
    return it


class Readable(unittest.TestCase):
    def test_every_question_is_its_own_paragraph_with_a_bold_question_sentence(self):
        it = remind_of(QUESTIONS)
        paras = it["md"].split("\n\n")
        self.assertEqual(len(paras), 4, it["md"])
        self.assertNotIn(" / ", it["md"])
        self.assertTrue(paras[0].startswith("**Accept the screen mockups?** These are"), paras[0])
        self.assertTrue(paras[1].startswith("**Accept the API key dates mockup (DOC-83, Portal)?** Under the key"), paras[1])
        self.assertEqual(paras[3], "Do it now or later?")   # nothing but the question: not all bold

    def test_a_long_design_link_is_short_and_still_a_link(self):
        it = remind_of(QUESTIONS)
        label = i18n.t("core.owner.link_design")
        self.assertNotIn("Mockup: https://", it["md"])
        self.assertIn(f"[{label}]({FIGMA})", it["md"])
        self.assertIn("[DOC-83](file:///home/me/shots/DOC-83/)", it["md"])   # a file: its name, the address behind it
        self.assertNotIn(f"[{label}]([", it["md"])
        self.assertEqual(it["md"].count("file:///"), 2)   # both file links of the card are kept, as targets

    def test_the_text_field_is_the_one_line_as_before(self):
        it = remind_of([{"question": "Release today? The tests are green."}])
        self.assertEqual(it["md"], "**Release today?** The tests are green.")
        self.assertEqual(it["text"], "Release today? The tests are green.")
        two = remind_of(QUESTIONS[:2])
        self.assertEqual(two["text"].count(" / "), 1)   # the one-line form (search, comparison, plugins) is unchanged

    def test_a_permission_has_no_markdown_of_its_own(self):
        self.assertIsNone(owner.question_md({"tool": "Bash"}))
        self.assertIsNone(owner.reminder_md({"sessions": {}}, "q:maps:r1"))   # a closed question: nothing


if __name__ == "__main__":
    unittest.main()
