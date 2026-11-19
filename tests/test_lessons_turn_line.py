"""A turn only on the core's lessons note (an executor handed in, reducer._lessons_note) is the session's own
housekeeping: its result does not light "new" and does not replace the start of the last result or the gist — the
"Now" line keeps "MR opened", not "recorded the lessons". A turn that also took the owner's words is an ordinary one."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from core import views
from tests.test_quiet_wakeups import Base, OWNER, LESSONS


class LessonsTurnLine(Base):
    def handed_in(self):
        """frontend answered the owner, then its executor handed in with lessons: the core's note to frontend."""
        self.ev("message.received", None, to="frontend", text="open the MR", **OWNER)
        self.turn("frontend", f"m{self.seq}", "MR !12 opened, the reviewer is set.")
        d = self.executor()
        self.turn("frontend-ci", d["message"], f"Done, MR !12.\n\n**{LESSONS}:**\n- CI secrets — only through vault")
        self.retire()
        (note,) = [k for k, m in self.st["messages"].items() if m["session"] == "frontend" and m.get("lessons_of")]
        return note

    def row(self):
        r = views.Reader()
        r.state = self.st
        return {s["name"]: s for s in r.snapshot(now=10 ** 6)["sessions"]}["frontend"]

    def test_the_lessons_turn_keeps_the_last_real_result(self):
        note = self.handed_in()
        s = self.st["sessions"]["frontend"]
        out, head, gist = s["owner_out"], s["out_head"], s.get("gist")
        self.turn("frontend", note, "Recorded the executor's lessons in my CLAUDE.md, in the line about CI.")
        s = self.st["sessions"]["frontend"]
        self.assertEqual((s["owner_out"], s["out_head"], s.get("gist")), (out, head, gist))
        self.assertEqual(self.row()["out_head"], "MR !12 opened, the reviewer is set.")

    def test_the_snapshot_gist_and_the_away_summary_keep_the_real_result(self):
        note = self.handed_in()
        before = self.row()["gist"]
        self.ev("owner.away", None, **OWNER)
        self.ev("message.received", None, to="frontend", text="and the tests?", **OWNER)
        self.turn("frontend", f"m{self.seq}", "The tests are green.")
        self.turn("frontend", note, "Recorded the executor's lessons in my CLAUDE.md.")
        self.assertEqual(self.row()["gist"]["text"], "The tests are green.")
        self.assertIsNotNone(before)
        done = [d["text"] for d in self.st["owner"]["done"] if d["session"] == "frontend"]
        self.assertEqual(done, ["The tests are green."])

    def test_a_background_wake_after_a_lessons_turn_is_ordinary(self):
        note = self.handed_in()
        self.turn("frontend", note, "Recorded the executor's lessons in my CLAUDE.md.")
        self.turn("frontend", None, "All 40 tests are green, MR !12 is ready.")    # Claude Code woke it: a background job ended
        self.assertEqual(self.st["sessions"]["frontend"]["out_head"], "All 40 tests are green, MR !12 is ready.")

    def test_the_owners_words_inserted_mid_turn_make_it_ordinary(self):
        note = self.handed_in()
        self.ev("turn.started", "frontend", marker=note, prompt_head="x")
        self.ev("message.received", None, to="frontend", text="and what about the tests?", **OWNER)
        m = f"m{self.seq}"
        self.st["messages"][m]["injected"] = True
        self.ev("turn.started", "frontend", marker=m, prompt_head="x")
        r = self.ev("turn.result", "frontend", text="Lessons recorded; the tests are green.")
        self.assertIsNone(r)
        self.assertEqual(self.st["sessions"]["frontend"]["out_head"], "Lessons recorded; the tests are green.")

    def test_a_note_and_the_owners_message_in_one_turn_is_ordinary(self):
        note = self.handed_in()
        self.ev("message.received", None, to="frontend", text="and what about the tests?", **OWNER)
        self.ev("turn.started", "frontend", marker=note, also=[f"m{self.seq}"], prompt_head="x")
        self.ev("turn.result", "frontend", text="Lessons recorded; the tests are green.")
        self.assertEqual(self.st["sessions"]["frontend"]["out_head"], "Lessons recorded; the tests are green.")


if __name__ == "__main__":
    unittest.main()
