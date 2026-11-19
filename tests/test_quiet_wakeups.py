"""Service wake-ups come from the core, without a session turn (owner decision 2026-10-16; a day's review showed ~131
idle turns of empty replies such as "—", "nothing", "already recorded"):
1. An executor handed in a task — the lessons from its result (the "Lessons" block) go to whoever assigned it by
   themselves; no block, or "Lessons: none" — do not wake, a line goes into the task record. The word "lessons" without a
   block ("lessons are recorded in hub task note") — wake as before.
2. To the hub — without "REPLIED (continuation)" (a continuation already goes to whoever waits for it); a message to a
   busy specialist goes into its queue without a hub turn.
3. "Owner's decision — write it down" — not to the hub and not to whoever heard the owner's word themselves (their
   card, their message)."""
import tests  # noqa: F401 — keeps the machine's settings out
import re
import shutil
import tempfile
import unittest

from core import executors, journal, lessons, reducer
from tests.fakes import lang

OWNER = {"channel": "cli", "owner": True}
# a session's Russian "Lessons" block, from the words the rule is built of (language data)
LESSONS = lang.word("words", "lessons_head").capitalize()           # the heading
NO_LESSONS = lang.word("words", "lessons_head", 1).capitalize()     # its genitive: "no lessons"
NONE, NO_NEW, NOTHING, WITHOUT_NEW = (lang.word("words", "lessons_none", i) for i in range(4))   # "none", "nothing" …
EXCEPT, BUT = lang.word("words", "lessons_but"), lang.word("words", "lessons_but", 1)
FOR = lang.data("words", "lessons_for")[0]                          # "for " of "Lessons for CLAUDE.md"
# the optional word of "for CLAUDE.md of the profession: nothing" (the pattern's group, not a plain alternative)
PROFESSION = re.search(r"\( ([^\W\d_]+)\)\?", lang.data("words", "lessons_none_claude")).group(1)


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


class Base(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "frontend", cwd="/fe", session_id="s-fe", role="agent", about="Frontend", **OWNER)
        self.ev("session.requested", "maps", cwd="/m", session_id="s-m", role="agent", about="Maps", **OWNER)
        for n in ("hub", "frontend", "maps"):
            self.ev("session.started", n)

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def to(self, name, part=""):
        return [m for m in self.st["messages"].values() if m["session"] == name and part in m["text"]]

    def executor(self, name="frontend-ci", raiser="frontend"):
        self.assertIsNone(self.ev("task.run_requested", name, task="fix the CI secrets", **by(raiser)))
        run = self.st["runs"][name]
        for type, entity, data in executors.events_for(self.st, name, run, cwd="/fe/.claude/worktrees/x", worktree=None):
            self.assertIsNone(self.ev(type, entity, **data), (type, data))
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == name]
        self.ev("session.started", name)
        return d

    def turn(self, name, marker, text, **extra):
        self.ev("turn.started", name, marker=marker, prompt_head="x")
        self.ev("turn.result", name, text=text, **extra)
        self.ev("turn.stopped", name)

    def retire(self, name="frontend-ci"):
        self.ev("session.close_requested", name, retire=True)
        self.ev("session.exited", name, why="sleep")


class Lessons(Base):
    def notes(self):
        return [m for m in self.to("frontend", "handed in its task")]

    def trail(self):
        return " | ".join(a["text"] for a in self.actions if a.get("do") == "task_note")

    def test_the_lessons_themselves_go_to_whoever_raised_it(self):
        d = self.executor()
        self.turn("frontend-ci", d["message"], "Done, MR !12.\n\n**Lessons (for the CLAUDE.md of the profession):**\n"
                                               "- CI secrets — only through vault\n- do not put an MR without no-qa")
        self.retire()
        (note,) = self.notes()
        self.assertIn("CI secrets — only through vault", note["text"])
        self.assertIn("do not put an MR without no-qa", note["text"])
        self.assertIn("CLAUDE.md", note["text"])

    def test_no_lessons_block_or_an_empty_one_wakes_nobody(self):
        for i, text in enumerate(("Done, MR !12.", "Done.\n\n**Lessons:** none.", f"Done.\n\n{NO_LESSONS} {FOR}CLAUDE.md {NONE}.",
                                  "Done.\n\n## Lessons\nno new ones, all recorded in the previous report.")):
            with self.subTest(text=text):
                name = f"frontend-ci{i}"
                d = self.executor(name)
                self.turn(name, d["message"], text)
                self.retire(name)
                self.assertEqual(self.to("frontend", f"Executor {name} "), [])
                self.assertIn(f"{name} handed in and fell asleep", self.trail())
                self.assertIn("no lessons", self.trail())

    def test_lessons_mentioned_without_a_block_wake_the_old_way(self):
        d = self.executor()
        self.turn("frontend-ci", d["message"], "Done. The result and the \"Lessons\" are recorded in `hub task note`.")
        self.retire()
        (note,) = self.notes()
        self.assertIn("hub show frontend-ci", note["text"])

    def test_lessons_of_an_earlier_turn_are_not_lost(self):
        d = self.executor()
        self.turn("frontend-ci", d["message"], "Done.\n\n### Lessons\n- test first")
        self.turn("frontend-ci", None, "Checked again, all green.")
        self.retire()
        (note,) = self.notes()
        self.assertIn("test first", note["text"])

    def test_lessons_cut_off_by_the_journal_come_from_the_host(self):
        from hosts.streamjson import StreamHost, StreamProcess
        host = StreamHost(tempfile.mkdtemp(prefix="lessons-"))
        popen = type("P", (), {"stdin": None, "poll": lambda self: None, "pid": 1})()
        host.procs["a"] = StreamProcess(popen)
        long = "words " * 20000 + "\n\n**Lessons**\n- check the branch with git grep for markers before merging"   # beyond journal.TEXT_MAX
        host.inbox.put(("a", {"type": "result", "result": long, "num_turns": 3}))
        (res,) = [d for k, _, d in host.poll() if k == "turn.result"]
        self.assertEqual((len(res["text"]), res["cut"]), (journal.TEXT_MAX, len(long)))
        self.assertNotIn("markers", res["text"])                       # the result goes into the journal truncated
        d = self.executor()
        self.turn("frontend-ci", d["message"], res["text"], **{k: v for k, v in res.items() if k != "text"})
        self.retire()
        (note,) = self.notes()
        self.assertIn("git grep for markers", note["text"])

    def test_a_later_no_lessons_clears_an_earlier_mention(self):
        d = self.executor()
        self.turn("frontend-ci", d["message"], "Waiting for CI, will add the lessons at the end.")
        self.turn("frontend-ci", None, "CI green, MR merged.\n\n**Lessons:** none.")
        self.retire()
        self.assertEqual(self.notes(), [])

    def test_an_executor_raised_by_the_previous_core_wakes_the_old_way(self):
        d = self.executor()
        self.st["sessions"]["frontend-ci"].pop("lessons")                 # started before this version
        self.turn("frontend-ci", d["message"], "Done, MR !12.")
        self.retire()
        (note,) = self.notes()
        self.assertIn("look yourself", note["text"])

    def test_a_cut_result_without_the_hosts_lessons_wakes_the_old_way(self):
        d = self.executor()
        self.turn("frontend-ci", d["message"], "words " * 666 + "etc.")      # exactly 4000 characters: the previous host cut it
        self.retire()
        self.assertEqual(len(self.notes()), 1)

    def test_a_runaway_result_with_no_word_of_lessons_does_not_wake(self):
        # cut by the host past journal.TEXT_MAX, scanned whole by it: no lessons in it at all — nothing to look for
        from hosts.streamjson import StreamHost, StreamProcess
        tmp = tempfile.mkdtemp(prefix="lessons-")
        self.addCleanup(shutil.rmtree, tmp, True)
        host = StreamHost(tmp)
        host.procs["a"] = StreamProcess(type("P", (), {"stdin": None, "poll": lambda self: None, "pid": 1})())
        host.inbox.put(("a", {"type": "result", "result": "words " * 20000, "num_turns": 3}))
        (res,) = [d for k, _, d in host.poll() if k == "turn.result"]
        d = self.executor()
        self.turn("frontend-ci", d["message"], res["text"], **{k: v for k, v in res.items() if k != "text"})
        self.retire()
        self.assertEqual(self.notes(), [])

    def test_a_whole_long_result_without_a_block_is_not_a_cut_one(self):
        d = self.executor()
        self.turn("frontend-ci", d["message"], "words " * 700)            # 4200 whole characters, no "Lessons" block: nothing is lost
        self.retire()
        self.assertEqual(self.notes(), [])

    def test_lessons_are_sent_once(self):
        d = self.executor()
        self.turn("frontend-ci", d["message"], "Done.\n\n**Lessons:**\n- test first")
        self.retire()
        self.ev("session.wake_requested", "frontend-ci", **by("hub"))
        self.ev("session.started", "frontend-ci")
        self.turn("frontend-ci", None, "Finished the tail.\n\n**Lessons:** none.")
        self.retire()
        self.assertEqual(len(self.notes()), 1)

    def asleep_then_removed(self, name="frontend-ci"):
        # put to sleep without handing in again (idle, a hub restart): the handover before sleep, then sleep
        self.ev("session.close_requested", name, **OWNER)
        self.turn(name, None, "HANDOFF-DONE\nDone: all handed over.")
        self.ev("session.exited", name, why="sleep")
        self.assertEqual(self.st["sessions"][name]["state"], "asleep")
        self.ev("session.remove_requested", name, **by("hub"))
        self.assertEqual(self.st["sessions"][name]["state"], "removed")

    def test_removing_one_woken_after_handing_in_without_new_lessons_wakes_nobody(self):
        # a hub restart resumes it, it answers the handover ("HANDOFF-DONE", no lessons), falls asleep; then it is
        # removed: its lessons were asked for when it handed in, no "look yourself" a second time
        d = self.executor()
        self.turn("frontend-ci", d["message"], "Done.\n\n**Lessons:**\n- test first")
        self.retire()
        self.ev("session.wake_requested", "frontend-ci", **by("hub"))
        self.ev("session.started", "frontend-ci")
        self.turn("frontend-ci", None, "Ok, all handed over earlier.")
        self.asleep_then_removed()
        self.assertEqual(len(self.notes()), 1)

    def test_removing_one_woken_after_handing_in_tells_its_new_lessons(self):
        d = self.executor()
        self.turn("frontend-ci", d["message"], "Done.\n\n**Lessons:**\n- test first")
        self.retire()
        self.ev("session.wake_requested", "frontend-ci", **by("hub"))
        self.ev("session.started", "frontend-ci")
        self.turn("frontend-ci", None, "Finished the tail.\n\n**Lessons:**\n- measure by the number of calls")
        self.asleep_then_removed()
        self.assertEqual(len(self.notes()), 2)
        self.assertIn("measure by the number of calls", self.notes()[-1]["text"])

    def test_handing_in_again_after_an_answer_without_lessons_wakes_nobody(self):
        # woken after handing in, it answers a question with no word of lessons and hands in again: not "lessons were
        # mentioned without a block — look yourself" (that default is for one raised by a previous core)
        d = self.executor()
        self.turn("frontend-ci", d["message"], "Done.\n\n**Lessons:**\n- test first")
        self.retire()
        self.ev("session.wake_requested", "frontend-ci", **by("hub"))
        self.ev("session.started", "frontend-ci")
        self.turn("frontend-ci", None, "Answered a question: the branch is merged.")
        self.retire()
        self.assertEqual(len(self.notes()), 1)

    def test_a_raiser_that_was_down_at_the_hand_in_still_hears_look_yourself_later(self):
        # its lessons reached nobody at the hand-in: a later hand-in without a block falls back to "look yourself"
        d = self.executor()
        self.turn("frontend-ci", d["message"], "Done.\n\n**Lessons:**\n- test first")
        self.st["sessions"]["frontend"]["state"] = "failed"
        self.retire()
        self.assertEqual(self.notes(), [])
        self.st["sessions"]["frontend"]["state"] = "idle"
        self.ev("session.wake_requested", "frontend-ci", **by("hub"))
        self.ev("session.started", "frontend-ci")
        self.turn("frontend-ci", None, "Answered a question: the branch is merged.")
        self.retire()
        (note,) = self.notes()
        self.assertIn(str(reducer.LESSONS_UNCLEAR.format(where="", form="", name="frontend-ci"))[:40], note["text"])

    def test_a_direction_gets_its_lessons_into_the_backlog(self):
        self.ev("session.requested", "atlas", cwd="/a", session_id="s-at", **OWNER)
        self.ev("session.started", "atlas")
        self.ev("task.backlog", None, slug="atlas", **by("atlas"))
        d = self.executor("atlas-tiles", raiser="atlas")
        self.turn("atlas-tiles", d["message"], "Done.\n\n**Lessons**\n- cache tiles by version")
        self.retire("atlas-tiles")
        (note,) = self.to("atlas", "handed in its task")
        self.assertIn("hub direction atlas", note["text"])
        self.assertIn("cache tiles by version", note["text"])


class Parse(unittest.TestCase):
    """Heading variants come from the executors' results in the journal (426 hand-ins as of 16.10)."""

    def test_headings(self):
        for text, want in (
                (f"**{LESSONS}**\n- bump version in plugin.json", "- bump version in plugin.json"),
                (f"## {LESSONS}\n- first\n- second\n\n## Open\nnothing", "- first\n- second"),
                (f"## {LESSONS}\n- a\n### Tests\n- b", "- a\n### Tests\n- b"),                  # a subsection is part of the lessons
                (f"**{LESSONS}:** {NO_NEW}, {BUT} confirmed: test first", f"{NO_NEW}, {BUT} confirmed: test first"),
                (f"**{LESSONS} ({FOR}CLAUDE.md backend):**\n- one", "- one"),
                (f"**{LESSONS}:** `pnpm` is not in PATH — npx", "`pnpm` is not in PATH — npx"),
                (f"- **{LESSONS}** the same as in the previous report: check the token",
                 "the same as in the previous report: check the token"),
                (f"**{LESSONS}**\n**Test first.** A new test fails on the old code", "**Test first.** A new test fails on the old code"),
                (f"{LESSONS}:\n- Vitest from the root picks up the nested copy", "- Vitest from the root picks up the nested copy"),
                ("### Lessons\n- Stripe payment id is not the invoice id", "- Stripe payment id is not the invoice id")):
            with self.subTest(text=text):
                self.assertEqual(lessons.scan(text), ["yes", want])

    def test_empty(self):
        for text in (f"**{LESSONS}:** {NONE}.", f"{LESSONS}: {NONE}.", f"{NO_LESSONS} {NONE}.",
                     f"{NO_LESSONS} {FOR}CLAUDE.md of the profession {NONE}.",
                     f"**{LESSONS}:** {NOTHING} to write.", f"**{LESSONS}:** {NONE}.\n\nStarted a run, will write the result.",
                     f"**{LESSONS}** — {WITHOUT_NEW}, all recorded in the previous turn's result.",
                     f"{NO_LESSONS} beyond those of the previous report, {NONE}.",
                     f"**{LESSONS}:** {FOR}CLAUDE.md {PROFESSION} {NOTHING}.",   # the optional word of the pattern: no plain word in the data
                     f"A line added before «## {LESSONS}».\n\n**{LESSONS}:** {NONE}."):
            with self.subTest(text=text):
                self.assertEqual(lessons.scan(text), ["none", ""])

    def test_edges_from_review(self):
        self.assertEqual(lessons.scan(f"**{LESSONS}**\n**Test first.** a\n**Small commits.** b"),
                         ["yes", "**Test first.** a\n**Small commits.** b"])           # consecutive bold items are lessons
        item = f"- {LESSONS.lower()} of the previous task were confirmed, b"   # an item, not a second heading
        self.assertEqual(lessons.scan(f"## {LESSONS}\n- a\n{item}\n- c"), ["yes", f"- a\n{item}\n- c"])
        self.assertEqual(lessons.scan(f"## {LESSONS}\n\n**Test first.** a\n\n**More.** b"),
                         ["yes", "**Test first.** a\n\n**More.** b"])                    # bold paragraphs too
        self.assertEqual(lessons.scan(f"{NO_LESSONS} {NONE}, {EXCEPT} one:\n- X"), ["unclear", ""])
        self.assertEqual(lessons.scan(f"{NO_LESSONS} {NONE}.\nStill, I will write one:\n- X"), ["unclear", ""])
        self.assertEqual(lessons.scan(f"Done.\n\n**{LESSONS}**"), ["unclear", ""])           # an empty block is not "none"

    def test_unclear_and_absent(self):
        self.assertEqual(lessons.scan(f"The result and the «{LESSONS}» block are in `hub task note d1`."), ["unclear", ""])
        self.assertEqual(lessons.scan(f"{LESSONS} are also recorded in `hub task note`."), ["unclear", ""])
        self.assertEqual(lessons.scan("Done, MR !12."), ["", ""])


class Hub(Base):
    def test_no_replied_continuation_for_the_hub_when_the_reply_reaches_its_recipient(self):
        self.ev("delegation.created", None, session="frontend", context="T", for_session="maps", **by("hub"))
        did, mid = f"d{self.seq}", f"m{self.seq}"
        self.turn("frontend", mid, "waiting for CI, will write the result")
        self.assertEqual(len(self.to("hub", f"REPLIED {did}")), 1)       # the first reply goes to the hub, as before
        self.turn("frontend", None, "CI green, MR merged")              # a continuation (a turn without a label)
        self.assertEqual(len(self.to("maps", "CI green")), 1)        # the recipient got the continuation
        self.assertEqual(self.to("hub", "(continuation)"), [])
        self.assertEqual(self.st["delegations"][did]["reply"], "CI green, MR merged")
        self.st["sessions"]["maps"]["state"] = "removed"                 # the recipient is gone
        self.turn("frontend", None, "and the build on the stand")              # no recipient — to the hub, as before
        self.assertEqual(len(self.to("hub", "(continuation)")), 1)

    def test_a_sessions_message_to_a_busy_specialist_waits_in_its_queue_without_the_hub(self):
        self.ev("turn.started", "frontend", marker=None, prompt_head="long work")
        self.assertIsNone(self.ev("message.received", None, to="frontend", text="is the build green?", **by("maps")))
        mid = f"m{self.seq}"
        m = self.st["messages"][mid]
        self.assertEqual((m["state"], m.get("held")), ("queued", None))
        self.assertEqual(self.to("hub", "busy"), [])
        self.ev("delegation.created", None, session="frontend", context="check the build", for_session="maps", **by("maps"))
        d = self.st["delegations"][f"d{self.seq}"]
        self.assertEqual(self.to("hub", "busy"), [])
        self.assertIsNone(self.ev("message.routed", d["id"], route="executor", work="normal", **by("hub")))  # the hub may
        self.assertTrue(self.st["messages"][d["message"]].get("handover"))
        self.ev("message.received", None, to="frontend", text="and one more question", **by("maps"))
        m2 = f"m{self.seq}"
        self.assertIsNone(self.ev("message.routed", m2, route="drop", why="already answered itself", **by("hub")))
        self.assertEqual(self.st["messages"][m2]["state"], "failed")
        self.ev("message.received", None, to="frontend", text="the owner's words", **OWNER)
        self.assertIn("is not from a session", self.ev("message.routed", f"m{self.seq}", route="drop", why="x", **by("hub")))
        self.ev("message.received", None, text="fix the map", **OWNER)               # the owner to the hub
        self.ev("delegation.created", None, session="frontend", for_=None, **{"for": f"m{self.seq}"}, context="fix",
                **by("hub"))
        self.assertIn("is not from a session", self.ev("message.routed", f"d{self.seq}", route="drop", why="x", **by("hub")))
        self.ev("turn.stopped", "frontend")
        self.assertEqual(self.st["messages"][mid]["state"], "typing")  # it became free — it got it


class Decisions(Base):
    def setUp(self):
        super().setUp()
        self.executor()
        self.ev("session.requested", "bb", cwd="/b", session_id="s-b", role="agent", about="Servers", **by("hub"))
        self.ev("session.started", "bb")

    def decided(self, text, sender="frontend-ci"):
        self.ev("owner.decided", None, text=text, **by(sender))

    def test_not_to_the_hub(self):
        self.st["sessions"]["bb"]["raised_by"] = "hub"                  # an agent started by the hub
        self.decided("calls only with the owner", sender="bb")
        self.assertEqual(self.to("hub", "An owner's decision"), [])

    def test_not_to_whoever_heard_the_owners_word_itself(self):
        self.ev("message.received", None, to="frontend", text="deploy on Wednesdays", **OWNER)
        heard = f"m{self.seq}"
        self.decided(f"deploy on Wednesdays · owner {heard}")
        self.assertEqual(self.to("frontend", "An owner's decision"), [])
        self.ev("turn.started", "frontend", marker=None, prompt_head="x")
        self.ev("permission.requested", "frontend", request_id="r1", tool="AskUserQuestion", interactive=True,
                input={"questions": [{"question": "Where to?", "options": [{"label": "site"}, {"label": "store"}]}]})
        q = self.st["sessions"]["frontend"]["asks"][0]["id"]
        self.ev("message.received", None, to="frontend", text="site", **OWNER)        # an answer to a card
        self.assertTrue(any(m.get("answers_question") == q for m in self.st["messages"].values()))
        self.decided(f"release only on the site · {q}")
        self.assertEqual(self.to("frontend", "An owner's decision"), [])

    def test_to_whoever_raised_it_when_the_word_was_said_to_the_executor(self):
        self.ev("message.received", None, to="frontend-ci", text="we do not track GTM", **OWNER)
        self.decided(f"we do not track GTM until the new analytics · m{self.seq}")
        self.assertEqual(len(self.to("frontend", "An owner's decision")), 1)
        self.decided("no link to the owner's word")
        self.assertEqual(len(self.to("frontend", "An owner's decision")), 2)
        self.ev("message.received", None, to="frontend", text="analysis from the hub", **by("hub"))
        self.decided(f"do not merge without review · see m{self.seq}")                    # not the owner's word to whoever started it
        self.assertEqual(len(self.to("frontend", "An owner's decision")), 3)


if __name__ == "__main__":
    unittest.main()
