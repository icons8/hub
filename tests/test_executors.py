"""Executors instead of forks (spec "tasks, specialists, executors", release 2). A specialist (or an executor, or the
hub) starts an executor for a task: `hub task run <name> "<task>" [--repo]`; an executor is a new session with the
profile of whoever started it (CLAUDE.md, tools), in its own working copy on the branch task/<name>, with a report rule;
the result goes to whoever started it; after handing in it leaves, and the starter is told "write up the lessons". Limits: 3 live per specialist, 6 per
executor or hub; with the account's weekly limit at 85% or above a new executor starts only on the owner's word."""
import tests  # noqa: F401 — ignore this machine's settings
import os
import subprocess
import tempfile
import unittest

from core import executors, forms, reducer
from tests.fakes import lang

OWNER = {"channel": "cli", "owner": True}


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


def git(cwd, *a):
    return subprocess.run(["git", "-C", cwd, *a], check=True, capture_output=True, text=True).stdout


class Counted(dict):
    """A state collection that counts the records handed out by values(): how many times a caller walks it."""
    walked = 0

    def values(self):
        for v in super().values():
            Counted.walked += 1
            yield v


class Core(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "frontend", cwd="/fe", session_id="s-fe", role="agent", about="Frontend",
                icon="🎨", tools=["design@kit"], **OWNER)
        self.ev("session.started", "frontend")

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def run_task(self, name, text="fix the CI secrets", who="frontend", **extra):
        return self.ev("task.run_requested", name, task=text, **by(who), **extra)

    def start(self, name, raiser="frontend", cwd="/fe/.claude/worktrees/x"):
        """Like the daemon: the executor is started and got the task (core/executors.events_for)."""
        run = self.st["runs"][name]
        for type, entity, data in executors.events_for(self.st, name, run, cwd=cwd, worktree=None):
            self.assertIsNone(self.ev(type, entity, **data), (type, data))

    def test_executors_report_continues_the_task_even_after_the_raisers_turn_ended(self):
        # a specialist started an executor for a task and ended its turn before the daemon made the working copy (or the request waited
        # in the queue): the executor's task is still a subtask of that task, its result continues that task's turn
        self.ev("session.requested", "seo", cwd="/seo", session_id="s-seo", **OWNER)
        self.ev("session.started", "seo")
        self.ev("delegation.created", None, session="frontend", for_session="seo", context="T", **by("hub"))
        did, mid = f"d{self.seq}", f"m{self.seq}"
        self.ev("turn.started", "frontend", marker=mid)
        self.assertIsNone(self.run_task("frontend-ci"))
        self.ev("turn.result", "frontend", text="raised an executor, I will write the result")
        self.ev("turn.stopped", "frontend")
        self.start("frontend-ci")                                             # the specialist is already free
        (sub,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ci"]
        self.assertEqual(sub.get("parent"), did)
        self.ev("session.started", "frontend-ci")
        self.ev("turn.started", "frontend-ci", marker=sub["message"])
        self.ev("turn.result", "frontend-ci", text="ready")
        report = f"m{self.seq}x1"
        self.ev("turn.stopped", "frontend-ci")
        to_seo = lambda: [m for m in self.st["messages"].values() if m["session"] == "seo"]   # noqa: E731
        base = len(to_seo())
        self.ev("turn.started", "frontend", marker=report)
        self.ev("turn.result", "frontend", text="on T: the executor did it, checked")
        self.assertEqual(len(to_seo()), base + 1)                              # the result goes to whoever set T

    def test_executor_reloaded_at_its_own_request_reports_the_continuation_to_who_raised_it(self):
        # the executor asked for a reload mid-task and ended its turn with "I will continue after the restart" (that went to the starter);
        # the result of the continuation goes to the starter too, not into the void
        self.run_task("frontend-ci")
        self.start("frontend-ci")
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ci"]
        self.ev("session.started", "frontend-ci")
        self.ev("turn.started", "frontend-ci", marker=d["message"], prompt_head="task", ts=2000)
        self.ev("session.reload_requested", "frontend-ci", **by("frontend-ci"), ts=2000)
        self.ev("turn.result", "frontend-ci", text="I will continue after the restart", ts=2001)
        self.ev("turn.stopped", "frontend-ci", ts=2001)
        self.ev("session.started", "frontend-ci", ts=2005)
        (go,) = [m for m in self.st["messages"].values() if m["session"] == "frontend-ci" and "at your request" in m["text"]]
        self.ev("turn.started", "frontend-ci", marker=go["id"], ts=2010)
        self.ev("turn.result", "frontend-ci", text="Done after the restart, MR !12", ts=2011)
        self.ev("turn.stopped", "frontend-ci", ts=2011)
        self.assertTrue([m for m in self.st["messages"].values() if m["session"] == "frontend" and "MR !12" in m["text"]])

    def test_executors_error_reaches_who_raised_it(self):
        # the executor's turn broke off with an error: STOPPED goes to the hub and to whoever waits for the result (otherwise it waits silently)
        self.ev("session.started", "hub")
        for raiser, name in (("frontend", "frontend-ci"), ("hub", "hub-ci")):
            self.assertIsNone(self.run_task(name, who=raiser))
            self.start(name)
            (d,) = [d for d in self.st["delegations"].values() if d["session"] == name]
            self.ev("session.started", name)
            self.ev("turn.started", name, marker=d["message"])
            self.ev("turn.result", name, text="", is_error=True, subtype="error_during_execution")
            self.assertEqual(self.st["delegations"][d["id"]]["state"], "stopped")
            got = lambda who: [m for m in self.st["messages"].values() if m["session"] == who   # noqa: E731
                               and m["text"].startswith(f"STOPPED {d['id']}")]
            self.assertEqual((len(got(raiser)), len(got("hub"))), (1, 1))                    # the hub gets it once

    def test_the_raisers_turn_on_stopped_continues_its_task(self):
        # STOPPED to whoever waits for the result is about its task: the turn on it continues the task for which it started the
        # executor, and the result of that turn goes where its answer goes (as a reply to the task)
        self.ev("session.requested", "seo", cwd="/seo", session_id="s-seo", **OWNER)
        self.ev("session.started", "seo")
        self.ev("delegation.created", None, session="frontend", for_session="seo", context="T", **by("hub"))
        did, mid = f"d{self.seq}", f"m{self.seq}"
        self.ev("turn.started", "frontend", marker=mid)
        self.assertIsNone(self.run_task("frontend-ci"))
        self.start("frontend-ci")
        self.ev("turn.result", "frontend", text="raised an executor, I will write the result")
        self.ev("turn.stopped", "frontend")
        (sub,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ci"]
        self.ev("session.started", "frontend-ci")
        self.ev("turn.started", "frontend-ci", marker=sub["message"])
        self.ev("turn.result", "frontend-ci", text="", is_error=True, subtype="error_during_execution")
        (stop,) = [m for m in self.st["messages"].values() if m["session"] == "frontend"
                   and m["text"].startswith(f"STOPPED {sub['id']}")]
        self.assertEqual(stop.get("task"), sub["id"])
        self.ev("turn.started", "frontend", marker=stop["id"])
        self.assertEqual(self.st["sessions"]["frontend"].get("delegation"), did)
        to_seo = lambda: [m for m in self.st["messages"].values() if m["session"] == "seo"]   # noqa: E731
        base = len(to_seo())
        self.ev("turn.result", "frontend", text="on T: the executor failed, did it myself")
        self.assertEqual(len(to_seo()), base + 1)

    def test_stopped_of_a_fallen_executor_does_not_suggest_writing_to_it(self):
        # the executor crashed for good: a message cannot be delivered to it — the starter is told not "hub send" but how to bring it back or
        # start it again; if the turn broke off with an error but the session is alive — "hub send"
        self.assertIsNone(self.run_task("frontend-ci"))
        self.start("frontend-ci")
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ci"]
        self.ev("session.started", "frontend-ci")
        self.ev("turn.started", "frontend-ci", marker=d["message"])
        for _ in range(reducer.MAX_RESTARTS + 1):
            self.ev("session.exited", "frontend-ci")
        self.assertEqual(self.st["sessions"]["frontend-ci"]["state"], "failed")
        (stop,) = [m for m in self.st["messages"].values() if m["session"] == "frontend"
                   and m["text"].startswith(f"STOPPED {d['id']}")]
        self.assertNotIn("hub send frontend-ci", stop["text"])
        self.assertIn("hub session revive frontend-ci", stop["text"])
        self.assertIn("hub task run", stop["text"])
        self.assertIsNone(self.run_task("frontend-ui"))
        self.start("frontend-ui")
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ui"]
        self.ev("session.started", "frontend-ui")
        self.ev("turn.started", "frontend-ui", marker=d["message"])
        self.ev("turn.result", "frontend-ui", text="", is_error=True, subtype="error_during_execution")
        (stop,) = [m for m in self.st["messages"].values() if m["session"] == "frontend"
                   and m["text"].startswith(f"STOPPED {d['id']}")]
        self.assertIn("hub send frontend-ui", stop["text"])

    def test_stopped_of_a_retired_executor_says_how_to_wake_it(self):
        # one stopped mid-task sleeps as handed in (retired) — the starter is told how to wake it, not "crashed"
        self.assertIsNone(self.run_task("frontend-ci"))
        self.start("frontend-ci")
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ci"]
        self.ev("session.started", "frontend-ci")
        self.ev("turn.started", "frontend-ci", marker=d["message"])
        self.assertIsNone(self.ev("session.stop_requested", "frontend-ci", **OWNER))
        self.assertEqual(self.st["sessions"]["frontend-ci"]["state"], "retired")
        (stop,) = [m for m in self.st["messages"].values() if m["session"] == "frontend"
                   and m["text"].startswith(f"STOPPED {d['id']}")]
        self.assertIn("hub session wake frontend-ci", stop["text"])
        self.assertNotIn("it crashed", stop["text"])

    def test_the_asked_tasks_chain_does_not_stick_to_a_later_task(self):
        # the executor was started for a task but had no task of its own (a refusal, a break between events): the mark "which task
        # it was asked for" is cleared by its first turn — a task given to it later does not become a subtask of that one
        self.ev("delegation.created", None, session="frontend", context="T", **by("hub"))
        did = f"d{self.seq}"
        self.ev("turn.started", "frontend", marker=f"m{self.seq}")
        self.assertIsNone(self.run_task("frontend-ci"))
        run = self.st["runs"]["frontend-ci"]
        ((type, entity, data), _) = executors.events_for(self.st, "frontend-ci", run, cwd="/x", worktree=None)
        self.assertIsNone(self.ev(type, entity, **data))                   # the start exists, the task does not
        self.assertEqual(self.st["sessions"]["frontend-ci"].get("task_parent"), did)
        self.ev("session.started", "frontend-ci")
        self.ev("message.received", None, to="frontend-ci", text="take a look", **by("hub"))
        self.ev("turn.started", "frontend-ci", marker=f"m{self.seq}")
        self.ev("turn.result", "frontend-ci", text="looked")
        self.ev("turn.stopped", "frontend-ci")
        self.ev("session.requested", "seo", cwd="/seo", session_id="s-seo", **OWNER)
        self.ev("session.started", "seo")
        self.assertIsNone(self.ev("delegation.created", None, session="frontend-ci", context="another", **by("seo")))
        self.assertIsNone(self.st["delegations"][f"d{self.seq}"].get("parent"))
        self.assertNotIn("task_root", self.st["sessions"]["frontend-ci"])

    def test_a_specialist_raises_an_executor_with_its_profile(self):
        self.assertIsNone(self.run_task("frontend-ci"))
        self.start("frontend-ci")
        x = self.st["sessions"]["frontend-ci"]
        self.assertEqual((x["role"], x["raised_by"], x["profile"], x["tools"], x["icon"]),
                         ("executor", "frontend", "s-fe", ["design@kit"], "🎨"))
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ci"]
        self.assertEqual(d["for_session"], "frontend")
        task = self.st["messages"][d["message"]]["text"]
        self.assertIn("fix the CI secrets", task)
        self.assertIn('"Lessons" block', task)                             # the report rule is in the start text
        self.assertNotIn("frontend-ci", self.st.get("runs") or {})

    def test_the_task_tells_where_the_setters_words_end_and_the_cores_frame_begins(self):
        """The page folds the core's frame (the rules, the notes) and shows the setter's words: the core marks them
        (`lead` of the task) and the feed row carries the span, the page does not parse the text."""
        from core import delegations
        for kw in ({}, {"branch": "task/x"}):
            with self.subTest(kw=kw):
                self.setUp()
                self.assertIsNone(self.run_task("frontend-ci", "fix the 😀 CI secrets\nand the cache", **kw))
                self.start("frontend-ci")
                (d,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ci"]
                self.assertEqual(d["context"][:d["lead"]], "fix the 😀 CI secrets\nand the cache")
                self.assertGreater(len(d["context"]), d["lead"])   # the rule follows the words
                msg = self.st["messages"][d["message"]]
                text = msg["text"]
                a, b = delegations.brief_of(msg)
                raw = text.encode("utf-16-le")
                self.assertEqual(raw[a * 2:b * 2].decode("utf-16-le"), "fix the 😀 CI secrets\nand the cache")
                self.assertIn("You are the executor of this task", raw[b * 2:].decode("utf-16-le"))

    def test_the_span_survives_a_trimmed_context_and_a_head_put_before_the_text(self):
        from core import coretext, delegations
        self.assertIsNone(self.run_task("frontend-ci", "result"))   # a brief the frame repeats ("The result of this turn…")
        self.start("frontend-ci")
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ci"]
        msg = self.st["messages"][d["message"]]
        a, b = delegations.brief_of(msg)
        self.assertEqual((msg["text"][a:b], msg["text"][:a].count("result")), ("result", 0))   # the real place, not the tail's
        d["context"] = d["context"][:20] + "…"   # what the trim action leaves
        self.assertEqual(delegations.brief_of(msg), [a, b])
        coretext.prepend(msg, "FYI: ")
        a2, b2 = delegations.brief_of(msg)
        self.assertEqual((msg["text"][a2:b2], a2 - a), ("result", 5))

    def test_the_task_names_the_owners_language_for_every_line_of_the_turn(self):
        """An executor started a turn with an English line between steps and ended with "I'm waiting for…" for a Russian
        owner; turn replay — with the language-name line in the task 0/12 slips, without the name the English knocked it off Russian."""
        for words, line in ((" ".join(lang.words("watch", "doubt")), "is Russian:"),   # a Russian owner's words
                            ("Please check why the build keeps failing again", "is English:"), (None, None)):
            with self.subTest(words=words):
                self.setUp()
                if words:
                    self.ev("message.received", "hub", text=words, **OWNER)
                self.assertIsNone(self.run_task("frontend-ci"))
                self.start("frontend-ci")
                (d,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ci"]
                task = self.st["messages"][d["message"]]["text"]
                if line is None:   # the language is unknown — there is no line without the name: it was what threw it off (replay 4/12)
                    self.assertNotIn("The language of everything you write", task)
                    continue
                self.assertIn("The language of everything you write " + line, task)
                self.assertIn("\"waiting…\", every line between steps", task)

    def test_an_executor_is_not_put_to_sleep_the_moment_its_background_run_ends(self):
        # journal: an executor was waiting for its background test run; the run ended at 01:43:18 — and the same second the core
        # put it to sleep (free longer than the limit, no background left) before it could read the result: twice in one night
        self.run_task("frontend-ci")
        self.start("frontend-ci")
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ci"]
        self.ev("session.started", "frontend-ci")
        self.ev("turn.started", "frontend-ci", marker=d["message"], prompt_head="task", ts=2000)
        self.ev("session.background", "frontend-ci", tasks=[{"id": "b1", "kind": "bash", "title": "full run"}], ts=2001)
        self.ev("turn.result", "frontend-ci", text="Started the full run, ~20 min, I will write the result", ts=2002)
        self.ev("turn.stopped", "frontend-ci", ts=2002)
        end = 2002 + 20 * 60
        self.assertEqual(executors.retiring(self.st, end), [])                       # the background is running — do not put it to sleep
        self.ev("session.background", "frontend-ci", tasks=[], ts=end)
        self.assertEqual(executors.retiring(self.st, end), [])                       # it has just ended — give time to read
        self.assertEqual(executors.retiring(self.st, end + executors.RETIRE_AFTER - 1), [])
        self.assertEqual(executors.retiring(self.st, end + executors.RETIRE_AFTER), [("sleep", "frontend-ci")])

    def test_an_executor_waiting_for_the_owner_is_not_put_to_sleep(self):
        # an executor answered the owner's comments on a spec and said `hub waiting`, and the core put it to sleep twice,
        # 2 minutes after the result — the next comment woke it again. Its "waiting" holds until it clears it or
        # someone writes to it (the word is stale); the owner or the hub can still put it to sleep
        self.run_task("frontend-ci")
        self.start("frontend-ci")
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ci"]
        self.ev("session.started", "frontend-ci")
        self.ev("turn.started", "frontend-ci", marker=d["message"], prompt_head="task", ts=2000)
        self.assertIsNone(self.ev("session.waiting", "frontend-ci", text="the owner is editing the spec — I answer at once",
                                  ts=2001, **by("frontend-ci")))
        self.ev("turn.result", "frontend-ci", text="Made the edits, waiting for comments", ts=2002)
        self.ev("turn.stopped", "frontend-ci", ts=2002)
        self.assertEqual(executors.retiring(self.st, 2002 + 10 * executors.RETIRE_AFTER), [])     # it is waiting — do not put it to sleep
        # WAITING_HOLD after the result (not from a "waiting" mid long turn): a forgotten mark does not hold the starter's slot forever
        self.assertEqual(executors.retiring(self.st, 2002 + executors.WAITING_HOLD - 1), [])
        self.assertEqual(executors.retiring(self.st, 2002 + executors.WAITING_HOLD), [("sleep", "frontend-ci")])
        self.assertIsNone(self.ev("session.close_requested", "frontend-ci", ts=2003, **OWNER))    # the owner can
        self.assertNotEqual(self.st["sessions"]["frontend-ci"]["closing"], "none")

    def test_the_waiting_of_an_executor_stops_holding_it_when_cleared_or_stale(self):
        for how in ("clear", "stale"):
            with self.subTest(how):
                self.setUp()
                self.run_task("frontend-ci")
                self.start("frontend-ci")
                (d,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ci"]
                self.ev("session.started", "frontend-ci")
                self.ev("turn.started", "frontend-ci", marker=d["message"], prompt_head="task", ts=2000)
                self.ev("session.waiting", "frontend-ci", text="waiting for the owner's reply", ts=2001, **by("frontend-ci"))
                self.ev("turn.result", "frontend-ci", text="Done, waiting", ts=2002)
                self.ev("turn.stopped", "frontend-ci", ts=2002)
                if how == "clear":
                    self.ev("session.waiting", "frontend-ci", clear=True, ts=2003, **by("frontend-ci"))
                    end = 2002
                else:   # someone wrote to it — a turn on the message; its "waiting" after that is stale, it does not hold
                    self.ev("message.received", None, to="frontend-ci", text="and also this", ts=2003, **by("frontend"))
                    self.ev("turn.started", "frontend-ci", marker=f"m{self.seq}", ts=2004)
                    self.ev("turn.result", "frontend-ci", text="did it", ts=2005)
                    self.ev("turn.stopped", "frontend-ci", ts=2005)
                    self.assertIn("stale", self.st["sessions"]["frontend-ci"]["waiting"])
                    end = 2005
                self.assertEqual(executors.retiring(self.st, end + executors.RETIRE_AFTER), [("sleep", "frontend-ci")])

    def test_the_result_goes_to_whoever_raised_and_the_executor_leaves_with_a_lessons_note(self):
        self.run_task("frontend-ci")
        self.start("frontend-ci")
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ci"]
        self.ev("session.started", "frontend-ci")
        self.ev("turn.started", "frontend-ci", marker=d["message"], prompt_head="task", ts=2000)
        self.ev("turn.result", "frontend-ci", text="Done, MR !12. Lessons: CI secrets — only through vault", ts=2001)
        self.ev("turn.stopped", "frontend-ci", ts=2001)
        replied = [m for m in self.st["messages"].values() if m["session"] == "frontend" and "MR !12" in m["text"]]
        self.assertEqual(len(replied), 1)                                    # the result goes to the starter
        self.assertEqual(executors.retiring(self.st, 2001 + executors.RETIRE_AFTER), [("sleep", "frontend-ci")])
        self.ev("session.close_requested", "frontend-ci", retire=True, ts=2200)
        notes = [m for m in self.st["messages"].values() if m["session"] == "frontend" and "lessons" in m["text"].lower()
                 and "CLAUDE.md" in m["text"]]
        self.assertEqual(len(notes), 1)
        # a correction of the owner is recorded as "do it this way", not as a new "ask first" (2026-10-28, stops audit)
        self.assertIn('"do it like this"', notes[0]["text"])
        self.assertIn('"do it like this"', d["context"])

    def cut_off_executor(self, text="", state=None,
                         close=("session.close_requested", "frontend-ci", 2200, {"retire": True, "channel": "core"})):
        """An executor whose turn broke off with an error; the core puts it to sleep after the follow-up did not take."""
        self.assertIsNone(self.run_task("frontend-ci"))
        self.start("frontend-ci")
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ci"]
        self.ev("session.started", "frontend-ci")
        self.ev("turn.started", "frontend-ci", marker=d["message"], prompt_head="task", ts=2000)
        if text:
            self.ev("turn.result", "frontend-ci", text=text, ts=2001)
        else:
            self.ev("turn.result", "frontend-ci", text="", is_error=True, subtype="error_during_execution", ts=2001)
        self.ev("turn.stopped", "frontend-ci", ts=2001)
        if state:   # the task is open again (a follow-up took it back) when the core puts it to sleep
            self.st["delegations"][d["id"]]["state"] = state
        self.assertIsNone(self.ev(close[0], close[1], ts=close[2], **close[3]))
        return self.st["delegations"][d["id"]]

    def notes_to_raiser(self):
        return [m for m in self.st["messages"].values() if m["session"] == "frontend" and m["origin"] == forms.ORIGIN_CORE
                and not m["text"].startswith("STOPPED")]

    def test_the_raiser_is_told_an_executor_fell_asleep_without_a_result_not_that_it_handed_in(self):
        d = self.cut_off_executor()
        self.assertEqual(d["state"], "stopped")
        (note,) = self.notes_to_raiser()
        self.assertNotIn("handed in", note["text"])
        self.assertIn("fell asleep without a result", note["text"])
        self.assertIn(d["id"], note["text"])
        self.assertIn("stopped", note["text"])

    def test_a_cut_off_executor_is_not_marked_handed_in_and_its_lessons_wait_for_the_real_result(self):
        since = len(self.actions)
        d = self.cut_off_executor(text=self.LESSON, state="stopped")   # the result had lessons, then the task was cut off
        (note,) = self.notes_to_raiser()
        self.assertNotIn("handed in", note["text"])
        self.assertIn("fell asleep without a result", note["text"])
        self.assertIn(f"{d['id']} is stopped", note["text"])
        sess = self.st["sessions"]["frontend-ci"]
        self.assertFalse(sess.get("handed_in"))                                      # not told as handed in
        self.assertIn("CI secrets — only through vault", sess["lessons"]["text"])   # and its lessons are not lost
        # the line in the task record says the same
        trails = [a["text"] for a in self.actions[since:] if a.get("do") == "task_note"]
        self.assertTrue([t for t in trails if "fell asleep without a result" in t])
        self.assertFalse([t for t in trails if "handed in" in t])

    def test_a_cut_off_executor_removed_later_leaves_its_lessons_under_a_head_that_is_true(self):
        self.cut_off_executor(text=self.LESSON, state="stopped")
        self.st["sessions"]["frontend-ci"]["state"] = "asleep"   # as after its raiser's plain sleep (retired ones are not noted)
        self.assertIsNone(self.ev("session.remove_requested", "frontend-ci", ts=9000, **OWNER))
        self.assertEqual(self.st["sessions"]["frontend-ci"]["state"], "removed")
        (note,) = [m for m in self.notes_to_raiser() if "not done" in m["text"]]    # not the earlier "fell asleep" line
        self.assertNotIn("handed in", note["text"])
        self.assertIn("was removed with its task not done", note["text"])
        self.assertIn("CI secrets — only through vault", note["text"])             # the lessons are not lost

    def test_a_handed_in_executor_still_gets_the_hand_in_note_and_trail(self):
        since = len(self.actions)
        self.cut_off_executor(text=self.LESSON)   # a result with lessons: the task is handed in
        self.assertEqual(self.lessons_note()["session"], "frontend")
        self.assertTrue([a for a in self.actions[since:] if a.get("do") == "task_note" and "handed in and fell asleep" in a["text"]])

    def test_a_born_minihub_task_queued_before_the_lesson_hint_still_gets_its_rule(self):
        old = (executors.REPORT_RULE.replace(executors.LESSON_BAR, "", 1).replace(executors.LESSON_HINT, "", 1)
               .format(raiser="hub"))
        self.assertNotEqual(old, executors.REPORT_RULE.format(raiser="hub"))
        st = {"delegations": {"d1": {"session": "maps", "for_session": "hub", "message": "m1", "context": "x\n\n" + old}},
              "messages": {"m1": {"state": "queued", "text": "x\n\n" + old}}}
        executors.born_brief(st, "maps")
        self.assertEqual(st["messages"]["m1"]["text"], "x\n\n" + executors.BORN_RULE.format(raiser="hub"))
        self.assertEqual(st["delegations"]["d1"]["context"], "x\n\n" + executors.BORN_RULE.format(raiser="hub"))

    def test_a_born_minihub_task_queued_with_the_russian_rule_still_gets_its_rule(self):
        """A task queued by a version before the English texts keeps the Russian REPORT_RULE in the journal (with and
        without the lesson hint; raised by the hub or the owner it named the raiser with the old hub word)."""
        hub_word = "".join(map(chr, (1093, 1072, 1073)))   # the old Russian word for the hub
        no_hint = executors.REPORT_RULE_RU.replace(executors.LESSON_HINT_RU, "", 1)
        for raiser, old in (("maps-lead", executors.REPORT_RULE_RU.format(raiser="maps-lead")),
                            ("maps-lead", no_hint.format(raiser="maps-lead")),
                            (None, executors.REPORT_RULE_RU.format(raiser=hub_word))):
            with self.subTest(raiser=raiser, hint=executors.LESSON_HINT_RU in old):
                st = {"delegations": {"d1": {"session": "maps", "for_session": raiser, "message": "m1",
                                             "context": "x\n\n" + old}},
                      "messages": {"m1": {"state": "queued", "text": "x\n\n" + old}}}
                executors.born_brief(st, "maps")
                new = "x\n\n" + executors.BORN_RULE.format(raiser=raiser or "hub")
                self.assertEqual(st["messages"]["m1"]["text"], new)
                self.assertEqual(st["delegations"]["d1"]["context"], new)

    def test_a_message_to_a_gone_executor_goes_to_whoever_raised_it_with_its_last_result(self):
        # spec, case 8: "I write to maps-2, maps receives it" — now with the departed one's context
        self.run_task("frontend-ci")
        self.start("frontend-ci")
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ci"]
        self.ev("session.started", "frontend-ci")
        self.ev("turn.started", "frontend-ci", marker=d["message"], prompt_head="task", ts=2000)
        self.ev("turn.result", "frontend-ci", text="Done, MR !12", ts=2001)
        self.ev("turn.stopped", "frontend-ci", ts=2001)
        self.ev("session.close_requested", "frontend-ci", retire=True, ts=2200)
        self.ev("session.remove_requested", "frontend-ci", retired=True, ts=2201)
        self.assertIsNone(self.ev("message.received", None, to="frontend-ci", text="and also fix the README", **OWNER))
        m = self.st["messages"][f"m{self.seq}"]
        self.assertEqual(m["session"], "frontend")
        self.assertEqual(m["text"], "and also fix the README")   # the words as they are, the note separately
        self.assertIn("MR !12", m["routing_note"])
        self.assertEqual(m["addressed"], "frontend-ci")   # the feed row says whom he wrote to

    def done_and_asleep(self, name="frontend-ci", raiser="frontend", text="Done, MR !12"):
        self.run_task(name, who=raiser)
        self.start(name, raiser=raiser)
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == name]
        self.ev("session.started", name)
        self.ev("turn.started", name, marker=d["message"], prompt_head="task", ts=2000)
        self.ev("turn.result", name, text=text, ts=2001)
        self.ev("turn.stopped", name, ts=2001)
        self.assertEqual(executors.retiring(self.st, 2001 + executors._after(self.st, self.st["sessions"][name])), [("sleep", name)])
        self.ev("session.close_requested", name, retire=True, ts=2200)
        self.ev("session.exited", name, why="sleep", ts=2201)
        self.assertEqual(self.st["sessions"][name]["state"], "retired")

    def test_a_done_executor_sleeps_and_is_never_removed_unless_a_term_is_set(self):
        # decision: let them pile up, nothing gets lost; a term in hub.json (PR #87) — they leave after it
        self.done_and_asleep()
        s = self.st["sessions"]["frontend-ci"]
        self.assertEqual(s["state"], "retired")
        self.assertEqual(executors.retiring(self.st, 10 ** 9), [])                  # by default it is never removed
        self.assertEqual(executors.retiring(self.st, s["since"] + 3600, keep=7200), [])
        self.assertEqual(executors.retiring(self.st, s["since"] + 7200, keep=7200), [("remove", "frontend-ci")])

    def put_to_sleep_by_raiser(self, name="frontend-ci", text="Done, MR !12"):
        """The raiser puts its executor to sleep with a plain `hub session close` (not as handed in): HANDOFF-DONE, asleep."""
        self.run_task(name)
        self.start(name)
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == name]
        self.ev("session.started", name)
        self.ev("turn.started", name, marker=d["message"], prompt_head="task", ts=2000)
        self.ev("turn.result", name, text=text, ts=2001)
        self.ev("turn.stopped", name, ts=2001)
        self.assertIsNone(self.ev("session.close_requested", name, ts=2002, **by("frontend")))
        handoff = f"m{self.seq}"
        self.ev("turn.started", name, marker=handoff, prompt_head="handoff", ts=2003)
        self.ev("turn.result", name, text="HANDOFF-DONE\nDone: MR\nOpen: nothing\nHanded over: nobody\nBackground: none", ts=2004)
        self.ev("turn.stopped", name, ts=2004)
        self.ev("session.exited", name, why="sleep", ts=2005)
        s = self.st["sessions"][name]
        self.assertEqual((s["state"], s.get("closing", "none")), ("asleep", "none"))
        return s

    def test_an_executor_its_raiser_put_to_sleep_is_removed_by_the_term_like_a_handed_in_one(self):
        # a raiser's plain sleep leaves the executor "asleep", not "retired": the term never removed it, and such
        # executors piled up with their working copies under the raiser
        s = self.put_to_sleep_by_raiser()
        self.assertEqual(executors.retiring(self.st, 10 ** 9), [])                  # no term — never removed, as before
        self.assertEqual(executors.retiring(self.st, s["since"] + 3599, keep=3600), [])
        self.assertEqual(executors.retiring(self.st, s["since"] + 3600, keep=3600), [("remove", "frontend-ci")])

    LESSON = "Done, MR !12.\n\n**Lessons**\n- CI secrets — only through vault"

    def lessons_note(self):
        """The raiser's one "handed in — record the lessons" note."""
        (note,) = [m for m in self.st["messages"].values() if m["session"] == "frontend" and "handed in its task" in m["text"]]
        return note

    def test_an_executor_removed_long_after_it_fell_asleep_leaves_its_lessons_aside(self):
        # the term removes asleep and parked executors too: the lessons note then comes hours after the work, and
        # waking the raiser for it cost a turn per executor; it rides with the raiser's next message instead
        for how in ("term", "by hand", "parked"):
            with self.subTest(how):
                self.setUp()
                self.put_to_sleep_by_raiser(text=self.LESSON)
                (result,) = [m for m in self.st["messages"].values() if m["session"] == "frontend"]
                self.ev("turn.started", "frontend", marker=result["id"], ts=2100)   # the raiser took the result
                self.ev("turn.stopped", "frontend", ts=2101)
                if how == "parked":   # woken, then parked by a hub restart
                    self.ev("session.wake_requested", "frontend-ci", **OWNER, ts=2110)
                    self.ev("session.started", "frontend-ci", ts=2111)
                    self.ev("session.parked", "frontend-ci", channel="core", why="restart", ts=2116)
                    self.assertEqual(self.st["sessions"]["frontend-ci"]["state"], "parked")
                extra = OWNER if how == "by hand" else {"retired": True}
                since = len(self.actions)
                self.assertIsNone(self.ev("session.remove_requested", "frontend-ci", ts=9000, **extra))
                self.assertEqual(self.st["sessions"]["frontend-ci"]["state"], "removed")
                note = self.lessons_note()
                self.assertIn("CI secrets — only through vault", note["text"])          # the same text
                self.assertIn("This is not just FYI — record in this turn", note["text"])   # aside, yet a job
                self.assertEqual((note.get("fyi"), note["state"]), (True, "aside"))
                self.assertFalse([a for a in self.actions[since:] if a.get("session") == "frontend"])   # no turn
                self.ev("message.received", None, to="frontend", text="a new task", ts=9100, **OWNER)
                self.assertIn("CI secrets — only through vault", self.st["messages"][f"m{self.seq}"]["fyi_note"])

    def test_a_cleanup_of_many_sleepers_caps_the_late_notes_of_one_raiser(self):
        # the first pass of the term on a hub with hundreds of old executors: every late note rode in one FYI block
        # with no cap (~80 KB for one raiser). A block holds LATE_NOTES notes, the rest are named in one more line
        n = reducer.LATE_NOTES + reducer.LATE_NAMES + 3
        names = [f"frontend-ci-{i}" for i in range(n)]
        for name in names:
            self.put_to_sleep_by_raiser(name, text=self.LESSON + " " + name)
        for m in [m for m in self.st["messages"].values() if m["session"] == "frontend" and m["state"] != "aside"]:
            self.ev("turn.started", "frontend", marker=m["id"], ts=2100)   # the raiser took the results
            self.ev("turn.stopped", "frontend", ts=2101)
        since = len(self.actions)
        for i, name in enumerate(names):
            self.assertIsNone(self.ev("session.remove_requested", name, retired=True, ts=9000 + i))
        self.assertFalse([a for a in self.actions[since:] if a.get("session") == "frontend"])   # still no turn
        aside = [m for m in self.st["messages"].values() if m["session"] == "frontend" and m["state"] == "aside"]
        full = [m for m in aside if "CI secrets" in m["text"]]
        (more,) = [m for m in aside if m.get("late_more")]
        self.assertEqual(len(aside), reducer.LATE_NOTES + 1)
        self.assertEqual([m["text"].rsplit(" ", 1)[1] for m in full], names[:reducer.LATE_NOTES])
        self.assertIn(f"({n - reducer.LATE_NOTES})", more["text"])
        self.assertIn(", ".join(names[reducer.LATE_NOTES:reducer.LATE_NOTES + reducer.LATE_NAMES]) + " and 3 more", more["text"])
        self.assertNotIn(names[-1], more["text"])
        self.ev("message.received", None, to="frontend", text="a new task", ts=9500, **OWNER)
        carrier = f"m{self.seq}"
        note = self.st["messages"][carrier]["fyi_note"]
        self.assertEqual(note.count("CI secrets"), reducer.LATE_NOTES)
        self.assertIn("and 3 more", note)
        self.assertLess(len(note), (reducer.LATE_NOTES + 1) * 1000)
        self.ev("turn.started", "frontend", marker=carrier, prompt_head="a new task", ts=9510)
        self.assertFalse([m for m in self.st["messages"].values() if m["session"] == "frontend" and m["state"] == "aside"])
        # the block arrived with that message: the next removal starts a new block with its own full note
        self.put_to_sleep_by_raiser("frontend-ci-last", text=self.LESSON + " frontend-ci-last")
        self.assertIsNone(self.ev("session.remove_requested", "frontend-ci-last", retired=True, ts=9600))
        (last,) = [m for m in self.st["messages"].values() if m["session"] == "frontend" and m["state"] == "aside"
                   and not m.get("with")]
        self.assertTrue(last["text"].endswith("frontend-ci-last"))

    def test_a_redelivered_block_of_late_notes_still_holds_no_more_than_the_cap(self):
        # the carrier of a block can go back to the queue (the raiser's process left mid-input): its redelivery gathers
        # every aside note again (_ride), so the notes of removals made meanwhile count against the same cap
        names = [f"frontend-ci-{i}" for i in range(2 * reducer.LATE_NOTES + 2)]
        for name in names:
            self.put_to_sleep_by_raiser(name, text=self.LESSON + " " + name)
        for m in [m for m in self.st["messages"].values() if m["session"] == "frontend" and m["state"] != "aside"]:
            self.ev("turn.started", "frontend", marker=m["id"], ts=2100)
            self.ev("turn.stopped", "frontend", ts=2101)
        first, second = names[:reducer.LATE_NOTES + 1], names[reducer.LATE_NOTES + 1:]
        for i, name in enumerate(first):
            self.ev("session.remove_requested", name, retired=True, ts=9000 + i)
        carrier = reducer.new_message(self.st, "mtest", "frontend", "a new task", 9100)
        reducer._ride(self.st, carrier)
        self.assertEqual(carrier["fyi_note"].count("CI secrets"), reducer.LATE_NOTES)
        for i, name in enumerate(second):   # removed while that carrier is still on its way
            self.ev("session.remove_requested", name, retired=True, ts=9200 + i)
        reducer._ride(self.st, carrier)      # delivered again: one block of every aside note
        self.assertEqual(carrier["fyi_note"].count("CI secrets"), reducer.LATE_NOTES)
        for name in names[reducer.LATE_NOTES:]:
            self.assertIn(name, carrier["fyi_note"])

    def test_a_late_removal_with_lessons_mentioned_without_a_block_does_not_say_there_were_none(self):
        self.put_to_sleep_by_raiser(text="Done, MR !12. Lessons — in the task record.")
        since = len(self.actions)
        self.ev("session.remove_requested", "frontend-ci", retired=True, ts=9000)
        (note,) = [a["text"] for a in self.actions[since:] if a.get("do") == "task_note"]
        self.assertIn("lessons mentioned without a block", note)
        self.assertFalse([m for m in self.st["messages"].values() if m["session"] == "frontend"
                          and "handed in" in m["text"]])

    def test_an_executor_that_hands_in_still_wakes_its_raiser_with_the_lessons(self):
        self.run_task("frontend-ci")
        self.start("frontend-ci")
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ci"]
        self.ev("session.started", "frontend-ci")
        self.ev("turn.started", "frontend-ci", marker=d["message"], prompt_head="task", ts=2000)
        self.ev("turn.result", "frontend-ci", text=self.LESSON, ts=2001)
        self.ev("turn.stopped", "frontend-ci", ts=2001)
        self.ev("session.close_requested", "frontend-ci", retire=True, ts=2200)
        note = self.lessons_note()
        self.assertNotIn("fyi", note)
        self.assertNotIn("not just FYI", note["text"])                        # a wake-up says it as before
        self.assertEqual(note["state"], "queued")                                        # in the queue for a turn, as before
        self.ev("session.exited", "frontend-ci", why="sleep", ts=2201)
        self.ev("session.remove_requested", "frontend-ci", retired=True, ts=9000)
        self.assertEqual(self.lessons_note()["id"], note["id"])                          # and no second note on removal

    def test_an_executor_woken_after_it_handed_in_gets_no_second_note_when_the_term_removes_it(self):
        # its lessons went out with the hand-in; woken for one more turn and put to sleep plainly, it is removed by the
        # term from asleep — a late "lessons mentioned without a block, look yourself" would be false and wake nothing
        self.run_task("frontend-ci")
        self.start("frontend-ci")
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ci"]
        self.ev("session.started", "frontend-ci")
        self.ev("turn.started", "frontend-ci", marker=d["message"], prompt_head="task", ts=2000)
        self.ev("turn.result", "frontend-ci", text=self.LESSON, ts=2001)
        self.ev("turn.stopped", "frontend-ci", ts=2001)
        self.ev("session.close_requested", "frontend-ci", retire=True, ts=2200)
        self.ev("session.exited", "frontend-ci", why="sleep", ts=2201)
        note = self.lessons_note()
        self.assertIsNone(self.ev("session.wake_requested", "frontend-ci", ts=3000, **by("frontend")))
        self.ev("session.started", "frontend-ci", ts=3001)
        self.assertIsNone(self.ev("session.close_requested", "frontend-ci", ts=3004, **by("frontend")))
        handoff = f"m{self.seq}"
        self.ev("turn.started", "frontend-ci", marker=handoff, prompt_head="handoff", ts=3005)
        self.ev("turn.result", "frontend-ci", text="HANDOFF-DONE\nDone: nothing\nOpen: nothing\nHanded over: nobody\nBackground: none",
                ts=3006)
        self.ev("turn.stopped", "frontend-ci", ts=3006)
        self.ev("session.exited", "frontend-ci", why="sleep", ts=3010)
        s = self.st["sessions"]["frontend-ci"]
        self.assertEqual(s["state"], "asleep")
        self.assertEqual(executors.retiring(self.st, s["since"] + 3600, keep=3600), [("remove", "frontend-ci")])
        self.ev("session.remove_requested", "frontend-ci", retired=True, ts=9000)
        self.assertEqual(self.st["sessions"]["frontend-ci"]["state"], "removed")
        self.assertEqual(self.lessons_note()["id"], note["id"])                          # the hand-in note only

    def test_the_term_keeps_an_asleep_executor_with_its_task_still_open(self):
        # no result yet: the raiser cannot put it to sleep (the task is open), but its process may end (closed in a
        # terminal) — asleep with the task open, it is not removed
        self.run_task("frontend-ci")
        self.start("frontend-ci")
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ci"]
        self.ev("session.started", "frontend-ci")
        self.ev("turn.started", "frontend-ci", marker=d["message"], prompt_head="task", ts=2000)
        self.ev("turn.stopped", "frontend-ci", ts=2001)
        self.assertIsNotNone(self.ev("session.close_requested", "frontend-ci", ts=2002, **by("frontend")))
        self.ev("session.ended", "frontend-ci", reason="prompt_input_exit", ts=2003)
        s = self.st["sessions"]["frontend-ci"]
        self.assertEqual((s["state"], self.st["delegations"][d["id"]]["state"]), ("asleep", "working"))
        self.assertEqual(executors.retiring(self.st, s["since"] + 10 ** 6, keep=3600), [])

    def waits_then_removed(self, raiser_gone=False):
        """A handed-in executor waits for an answer (`hub wait`: two checks) and one assignment of its own; it was removed by the term
        (remove_after_hours) — the checks fired at the hub as "no such session — decide whom to give it to", and retarget answered
        "already fired"."""
        if raiser_gone:
            self.ev("session.requested", "seo", cwd="/seo", session_id="s-seo", **OWNER)
            self.ev("session.started", "seo")
        self.done_and_asleep(raiser="seo" if raiser_gone else "frontend")
        for k, at in ((1, 2600), (2, 9000)):
            self.ev("schedule.set", f"frontend-ci-wait-65a4-{k}", session="frontend-ci", at=at,
                    prompt="[waiting for reply] the owner's comments on the spec. Check now; …", wait="the owner's comments on the spec",
                    **by("frontend-ci"), ts=2300)
        self.ev("schedule.set", "frontend-ci-daily", session="frontend-ci", every="1d", prompt="summary", **by("frontend-ci"),
                ts=2300)
        if raiser_gone:
            self.ev("session.ended", "seo", reason="prompt_input_exit", ts=2401)
            self.assertIsNone(self.ev("session.remove_requested", "seo", **OWNER, ts=2402))
        self.ev("session.remove_requested", "frontend-ci", retired=True, ts=2500)
        return self.st["schedule"]

    def test_hub_wait_checks_of_a_removed_executor_go_to_whoever_raised_it(self):
        jobs = self.waits_then_removed()
        for k in (1, 2):
            self.assertEqual((jobs[f"frontend-ci-wait-65a4-{k}"]["state"], jobs[f"frontend-ci-wait-65a4-{k}"]["session"]),
                             ("active", "frontend"))
        self.assertIsNone(self.ev("schedule.due", "frontend-ci-wait-65a4-1", slot=2600, channel="core", ts=2601))
        got = [m for m in self.st["messages"].values() if m.get("schedule") == "frontend-ci-wait-65a4-1"]
        self.assertEqual([m["session"] for m in got], ["frontend"])            # not to the hub as "decide whom to give it to"
        self.assertIn("frontend-ci", got[0]["text"])                             # whose check it was
        self.assertIn("the owner's comments on the spec", got[0]["text"])
        # the removed one's own assignment — as before: the hub decides whom to give it to
        self.assertEqual(jobs["frontend-ci-daily"]["session"], "frontend-ci")

    def test_hub_wait_checks_of_a_removed_executor_are_dropped_when_the_raiser_is_gone_too(self):
        jobs = self.waits_then_removed(raiser_gone=True)
        self.assertEqual([jobs[f"frontend-ci-wait-65a4-{k}"]["state"] for k in (1, 2)], ["removed", "removed"])
        self.assertIsNotNone(self.ev("schedule.due", "frontend-ci-wait-65a4-1", slot=2600, channel="core", ts=2601))
        self.assertFalse([m for m in self.st["messages"].values() if m.get("schedule")])

    def woken_turn(self, ts, name="frontend-ci"):
        """A job woke the handed-in executor (reducer._job_wakes): it takes the job's message, answers, and the core puts it
        to sleep again."""
        (m,) = [m for m in self.st["messages"].values() if m["session"] == name and m["state"] == "queued"]
        self.ev("session.started", name, ts=ts)
        self.ev("turn.started", name, marker=m["id"], prompt_head="x", ts=ts + 1)
        self.ev("turn.result", name, text="checked", ts=ts + 2)
        self.ev("turn.stopped", name, ts=ts + 2)
        self.ev("session.close_requested", name, retire=True, ts=ts + 3)
        self.ev("session.exited", name, why="sleep", ts=ts + 4)
        self.assertEqual(self.st["sessions"][name]["state"], "retired")

    def test_a_term_does_not_remove_an_executor_still_waiting_for_a_reply(self):
        # an executor was waiting for the owner's comments on a spec (`hub wait --for 4h` and `hub waiting`), the term
        # remove_after_hours=1 removed it after an hour of sleep — there was nobody left to wait for the answer
        self.done_and_asleep()
        since = self.st["sessions"]["frontend-ci"]["since"]
        self.ev("schedule.set", "frontend-ci-wait-65a4-1", session="frontend-ci", at=since + 4 * 3600,
                prompt="[waiting for reply] x. Check now; …", wait="x", **by("frontend-ci"), ts=since + 1)
        self.assertEqual(executors.retiring(self.st, since + 3600, keep=3600), [])     # the check is still ahead
        self.ev("schedule.due", "frontend-ci-wait-65a4-1", slot=since + 4 * 3600, channel="core", ts=since + 4 * 3600)
        self.woken_turn(since + 4 * 3600 + 10)   # the check woke it (_job_wakes): it looked, answered and sleeps again
        self.assertEqual(executors.retiring(self.st, since + 6 * 3600, keep=3600), [("remove", "frontend-ci")])

    def test_a_term_keeps_a_done_executor_its_live_recurring_job_still_wakes(self):
        # a handed-in executor ran a recurring job until the end of the week, the remove_after_hours term removed it —
        # the job was paused and there was nobody left to wake. A recurring job that wakes it holds it while its finite term (until) is ahead
        self.done_and_asleep()
        since = self.st["sessions"]["frontend-ci"]["since"]
        self.ev("schedule.set", "frontend-ci-lock", session="frontend-ci", every="1h", until=since + 3 * 86400,
                prompt="check the lock", **by("frontend-ci"), ts=since + 1)
        self.ev("schedule.set", "frontend-ci-once", session="frontend-ci", at=since + 1800, prompt="once",
                **by("frontend-ci"), ts=since + 1)
        self.ev("schedule.set", "frontend-ci-short", session="frontend-ci", every="1h", until=since + 7200,
                prompt="briefly", **by("frontend-ci"), ts=since + 1)
        self.ev("schedule.due", "frontend-ci-once", slot=since + 1800, channel="core", ts=since + 1800)
        self.assertEqual(self.st["schedule"]["frontend-ci-once"]["state"], "done")
        self.woken_turn(since + 1810)
        self.assertEqual(executors.retiring(self.st, since + 3 * 3600, keep=3600), [])  # the recurring one is ahead — it holds
        self.ev("schedule.remove_requested", "frontend-ci-lock", **by("frontend-ci"), ts=since + 3 * 3600)
        # a fired one-off and a recurring one past its term (the clock has not removed it yet) do not hold
        self.assertEqual(self.st["schedule"]["frontend-ci-short"]["state"], "active")
        self.assertEqual(executors.retiring(self.st, since + 3 * 3600 + 1, keep=3600), [("remove", "frontend-ci")])

    def test_a_term_waits_out_a_fresh_hub_waiting_but_not_a_forgotten_one(self):
        self.done_and_asleep()
        since = self.st["sessions"]["frontend-ci"]["since"]
        self.assertIsNone(self.ev("session.waiting", "frontend-ci", text="the owner's comments", ts=since + 3000,
                                  **by("frontend-ci")))
        self.assertEqual(executors.retiring(self.st, since + 3600, keep=3600), [])     # the mark is fresh — it holds
        hold = since + 3000 + executors.WAITING_HOLD
        self.assertEqual(executors.retiring(self.st, hold, keep=3600), [("remove", "frontend-ci")])   # a forgotten one does not

    def test_a_term_does_not_remove_an_executor_a_one_shot_job_will_wake(self):
        # the raiser scheduled a recheck that wakes its handed-in executor (a session job or `hub session wake` as a
        # command); the term removed the executor first, and the job then woke a session that was no longer there
        self.done_and_asleep()
        since = self.st["sessions"]["frontend-ci"]["since"]
        self.assertIsNone(self.ev("schedule.set", "recheck", session="frontend-ci", at=since + 4 * 3600,
                                  prompt="recheck the pipeline", **by("frontend"), ts=since + 1))
        self.assertEqual(executors.retiring(self.st, since + 3600, keep=3600), [])     # the recheck is still ahead
        self.assertIsNone(self.ev("schedule.remove_requested", "recheck", **by("frontend"), ts=since + 2 * 3600))
        self.assertEqual(executors.retiring(self.st, since + 2 * 3600, keep=3600), [("remove", "frontend-ci")])
        self.assertIsNone(self.ev("schedule.set", "rewake", command="hub session wake frontend-ci --about d1 'recheck'",
                                  at=since + 4 * 3600, **by("frontend"), ts=since + 2 * 3600))
        self.assertEqual(executors.retiring(self.st, since + 3 * 3600, keep=3600), [])
        self.assertIsNone(self.ev("schedule.due", "rewake", slot=since + 4 * 3600, channel="core", ts=since + 4 * 3600))
        self.assertEqual(self.st["schedule"]["rewake"]["state"], "done")
        # done on due, but the clock runs the command a little later: it still holds until the run is recorded
        self.assertEqual(executors.retiring(self.st, since + 4 * 3600 + 1, keep=3600), [])
        self.assertIsNone(self.ev("schedule.ran", "rewake", slot=since + 4 * 3600, exit=0, ts=since + 4 * 3600 + 5))
        self.assertEqual(executors.retiring(self.st, since + 4 * 3600 + 5, keep=3600), [("remove", "frontend-ci")])
        # a recurring job does not hold: it has no near end, and on removal it goes to the hub to reassign (as before)
        self.assertIsNone(self.ev("schedule.set", "nightly", command="hub session wake frontend-ci 'summary'", every="1d",
                                  **by("frontend"), ts=since + 4 * 3600))
        self.assertEqual(executors.retiring(self.st, since + 5 * 3600, keep=3600), [("remove", "frontend-ci")])

    def test_a_recurring_job_without_a_term_does_not_hold_but_one_with_a_term_does(self):
        self.done_and_asleep()
        since = self.st["sessions"]["frontend-ci"]["since"]
        self.ev("schedule.set", "daily", command="hub session wake frontend-ci 'x'", every="1d", **by("frontend"), ts=since + 1)
        self.assertEqual(executors.retiring(self.st, since + 3600, keep=3600), [("remove", "frontend-ci")])
        self.ev("schedule.set", "lock", session="frontend-ci", every="1h", until=since + 3 * 86400, prompt="lock",
                **by("frontend-ci"), ts=since + 1)
        self.assertEqual(executors.retiring(self.st, since + 3600, keep=3600), [])
        self.assertEqual(executors.retiring(self.st, since + 3 * 86400 + 1, keep=3600), [("remove", "frontend-ci")])

    def idle_after_report(self, name="frontend-ci", raiser="frontend"):
        self.run_task(name, who=raiser)
        self.start(name, raiser=raiser)
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == name]
        self.ev("session.started", name)
        self.ev("turn.started", name, marker=d["message"], prompt_head="task", ts=2000)
        self.ev("turn.result", name, text="Done, MR !12", ts=2001)
        self.ev("turn.stopped", name, ts=2001)

    def test_an_idle_executor_with_its_own_one_shot_job_ahead_is_not_put_to_sleep(self):
        # the executor set itself `hub schedule add --at` and reported: put to sleep (retired) after RETIRE_AFTER, the job
        # then fired into a dormant session — its message went to whoever raised it with "wake it", the window never started
        self.idle_after_report()
        due = 2001 + 3600
        self.assertIsNone(self.ev("schedule.set", "frontend-ci-at", session="frontend-ci", at=due, prompt="start the window",
                                  **by("frontend-ci"), ts=2002))
        end = 2001 + executors._after(self.st, self.st["sessions"]["frontend-ci"])
        self.assertEqual(executors.retiring(self.st, end), [])                       # the job is ahead — it holds
        self.assertEqual(executors.retiring(self.st, due - 1), [])
        self.assertIsNone(self.ev("schedule.due", "frontend-ci-at", slot=due, channel="core", ts=due))
        # fired: the message went to the executor itself, not to whoever raised it
        (m,) = [m for m in self.st["messages"].values() if m.get("schedule") == "frontend-ci-at"]
        self.assertEqual(m["session"], "frontend-ci")
        self.assertEqual(executors.retiring(self.st, due + executors.RETIRE_AFTER), [])   # it works on the message
        self.assertEqual(self.st["schedule"]["frontend-ci-at"]["state"], "done")

    def test_an_idle_executor_is_put_to_sleep_when_its_one_shot_job_is_far_ahead_or_not_its_own(self):
        self.idle_after_report()
        end = 2001 + executors._after(self.st, self.st["sessions"]["frontend-ci"])
        self.ev("schedule.set", "far", session="frontend-ci", at=end + executors.SCHEDULED_HOLD + 1, prompt="x",
                **by("frontend-ci"), ts=2002)
        self.ev("schedule.set", "other", session="frontend", at=2001 + 3600, prompt="x", **by("frontend-ci"), ts=2002)
        self.assertEqual(executors.retiring(self.st, end), [("sleep", "frontend-ci")])
        self.ev("schedule.set", "near", session="frontend-ci", at=end + executors.SCHEDULED_HOLD, prompt="x",
                **by("frontend-ci"), ts=2003)
        self.assertEqual(executors.retiring(self.st, end), [])
        # a command that wakes it holds nothing: `hub session wake` raises a sleeper itself
        self.ev("schedule.remove_requested", "near", **by("frontend-ci"), ts=2004)
        self.ev("schedule.set", "rewake", command="hub session wake frontend-ci 'x'", at=end + 3600, **by("frontend"), ts=2004)
        self.assertEqual(executors.retiring(self.st, end), [("sleep", "frontend-ci")])

    def retired_with_job(self, setter="frontend-ci", **job):
        """The executor reported with a session job addressed to it ahead (set by itself or by whoever raised it) and the
        core put it to sleep (retired) — a recurring job or a far one-shot does not hold it awake."""
        self.idle_after_report()
        self.assertIsNone(self.ev("schedule.set", "frontend-ci-tick", session="frontend-ci", prompt="check the pipeline",
                                  **(job or {"every": "1h"}), **by(setter), ts=2002))
        self.assertEqual(executors.retiring(self.st, 2001 + executors.RETIRE_AFTER), [("sleep", "frontend-ci")])
        self.ev("session.close_requested", "frontend-ci", retire=True, ts=2200)
        self.ev("session.exited", "frontend-ci", why="sleep", ts=2201)
        self.assertEqual(self.st["sessions"]["frontend-ci"]["state"], "retired")
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ci"]
        self.assertEqual(d["state"], "replied")
        return d

    def fire(self, at=5400):
        self.assertIsNone(self.ev("schedule.due", "frontend-ci-tick", slot=at, channel="core", ts=at))
        return [m for m in self.st["messages"].values() if m.get("schedule") == "frontend-ci-tick"]

    def test_a_job_the_executor_set_itself_wakes_it_after_it_handed_in(self):
        # a colleague's hub, four times a day: an executor set itself a job (`hub schedule add … --session <itself>`), reported
        # and was put to sleep (retired); the firing went to whoever raised it with "wake it" — the executor never woke, and
        # the raiser got someone else's alarm. The job wakes it as its raiser would (`hub session wake --about dN`): its
        # handed-in task is in progress again, and the turn's result goes to the raiser as a continuation
        d = self.retired_with_job()
        (m,) = self.fire()
        self.assertEqual((m["session"], m["to"]), ("frontend-ci", "frontend-ci"))
        self.assertNotIn("routing_note", m)
        self.assertIn("check the pipeline", m["text"])
        self.assertNotEqual(self.st["sessions"]["frontend-ci"]["state"], "retired")
        d = self.st["delegations"][d["id"]]
        self.assertEqual((d["state"], d["message"]), ("working", m["id"]))
        self.assertFalse([x for x in self.st["messages"].values() if x["session"] == "frontend" and x.get("schedule")])

    def test_a_far_one_shot_job_of_the_raiser_wakes_its_handed_in_executor(self):
        # the raiser scheduled a recheck for its executor two days ahead (a session job): it came back to the raiser itself
        d = self.retired_with_job(setter="frontend", at=2002 + 2 * 86400)
        (m,) = self.fire(at=2002 + 2 * 86400)
        self.assertEqual(m["session"], "frontend-ci")
        self.assertEqual(self.st["delegations"][d["id"]]["state"], "working")

    def test_a_job_another_session_set_for_a_handed_in_executor_goes_to_its_raiser_as_before(self):
        self.ev("session.requested", "seo", cwd="/seo", session_id="s-seo", **OWNER)
        self.ev("session.started", "seo")
        d = self.retired_with_job(setter="seo")
        (m,) = self.fire()
        self.assertEqual(m["session"], "frontend")
        self.assertIn("hub session wake frontend-ci", m["routing_note"])
        self.assertEqual(self.st["sessions"]["frontend-ci"]["state"], "retired")
        self.assertEqual(self.st["delegations"][d["id"]]["state"], "replied")

    def test_an_own_job_of_a_handed_in_executor_waits_for_a_place_like_its_raisers_wake(self):
        # no place under the raiser (here: the queue is paused): the wake waits in the core's queue, as the raiser's own
        # `hub session wake` would, and nothing goes to the raiser; once there is room the executor gets the job
        d = self.retired_with_job()
        self.st["queue_paused"] = {"by": "owner", "why": "x"}
        self.assertEqual(self.fire(), [])
        run = self.st["runs"]["frontend-ci"]
        self.assertEqual((run["by"], run["wake"]["about"]), ("frontend", d["id"]))
        self.assertIn("check the pipeline", run["wake"]["text"])
        self.assertEqual(self.st["sessions"]["frontend-ci"]["state"], "retired")
        self.assertFalse([x for x in self.st["messages"].values() if x["session"] == "frontend" and "check the pipeline" in x["text"]])
        # the next firing while it waits is one copy (as a queued job message): not a second wake, nothing to the raiser
        self.assertEqual(self.fire(at=9000), [])
        job = self.st["schedule"]["frontend-ci-tick"]
        self.assertEqual((job["runs"], job["skipped"]), (1, 1))
        self.st["queue_paused"] = None
        self.assertIsNone(self.ev("task.run_go", "frontend-ci", ts=9100, **OWNER))   # its place came
        (go,) = [x for x in self.st["messages"].values() if x["session"] == "frontend-ci" and x["state"] == "queued"]
        self.assertIn("check the pipeline", go["text"])
        self.assertEqual((go["schedule"], go["from"]), ("frontend-ci-tick", {"kind": "schedule"}))   # not the raiser's words
        self.assertNotIn("sender", go)
        self.assertEqual((self.st["delegations"][d["id"]]["state"], self.st["delegations"][d["id"]]["message"]),
                         ("working", go["id"]))
        self.assertNotIn("frontend-ci", self.st["runs"])

    def test_a_waiting_job_wake_carries_the_news_of_its_later_checks(self):
        # a job with a precheck: news found while the wake waits for a place goes onto it, as onto a queued job message
        d = self.retired_with_job(every="1h", check="./new.sh")
        self.st["queue_paused"] = {"by": "owner", "why": "x"}
        self.assertIsNone(self.ev("schedule.due", "frontend-ci-tick", slot=5400, exit=3, out="build 1 failed", channel="core",
                                  ts=5400))
        self.assertIsNone(self.ev("schedule.due", "frontend-ci-tick", slot=9000, exit=0, channel="core", ts=9000))
        self.assertIsNone(self.ev("schedule.due", "frontend-ci-tick", slot=12600, exit=3, out="build 2 failed",
                                  channel="core", ts=12600))
        w = self.st["runs"]["frontend-ci"]["wake"]
        self.assertIn("build 1 failed", w["text"])
        self.assertIn("More news", w["text"])
        self.assertIn("build 2 failed", w["text"])
        self.st["queue_paused"] = None
        self.assertIsNone(self.ev("task.run_go", "frontend-ci", ts=13000, **OWNER))
        (go,) = [x for x in self.st["messages"].values() if x["session"] == "frontend-ci" and x["state"] == "queued"]
        self.assertIn("build 2 failed", go["text"])
        self.assertEqual(self.st["delegations"][d["id"]]["message"], go["id"])

    def test_a_waiting_job_wake_is_dropped_when_the_job_or_the_executor_is_gone(self):
        # the job was removed while its wake waited for a place: nobody is woken for it; the executor removed — the raiser
        # gets no "your wake failed" about a wake it never asked for
        for gone in ("job", "executor"):
            with self.subTest(gone=gone):
                self.setUp()
                d = self.retired_with_job()
                self.st["queue_paused"] = {"by": "owner", "why": "x"}
                self.assertEqual(self.fire(), [])
                if gone == "job":
                    self.assertIsNone(self.ev("schedule.remove_requested", "frontend-ci-tick", ts=6000, **by("frontend-ci")))
                else:
                    self.assertIsNone(self.ev("session.remove_requested", "frontend-ci", retired=True, ts=6000))
                before = set(self.st["messages"])
                self.st["queue_paused"] = None
                self.assertIsNone(self.ev("task.run_go", "frontend-ci", ts=9100, **OWNER))
                self.assertEqual([self.st["messages"][m]["session"] for m in set(self.st["messages"]) - before], [])
                self.assertNotEqual(self.st["delegations"][d["id"]]["state"], "working")

    def test_an_own_job_of_a_handed_in_executor_whose_raiser_has_no_executors_goes_to_the_raiser(self):
        # its raiser went "solo" meanwhile: nobody wakes its executors (_wake_refusal), its alarm comes to the raiser as before
        d = self.retired_with_job()
        self.st["owner"]["parallel"], self.st["sessions"]["frontend"]["solo"] = 3, True
        self.assertIsNotNone(executors.refused(self.st, "frontend"))
        (m,) = self.fire()
        self.assertEqual(m["session"], "frontend")
        self.assertEqual(self.st["delegations"][d["id"]]["state"], "replied")

    def test_a_one_shot_job_holds_only_the_executor_it_names(self):
        self.done_and_asleep()
        since = self.st["sessions"]["frontend-ci"]["since"]
        for i, cmd in enumerate(("hub session wake frontend-ci-2", "hub session wake xfrontend-ci", "echo frontend-ci")):
            self.assertIsNone(self.ev("schedule.set", f"other-{i}", command=cmd, at=since + 4 * 3600, **by("frontend"),
                                      ts=since + 1))
        self.assertIsNone(self.ev("schedule.set", "other-s", session="frontend", at=since + 4 * 3600, prompt="frontend-ci",
                                  **by("frontend"), ts=since + 1))
        self.assertEqual(executors.retiring(self.st, since + 3600, keep=3600), [("remove", "frontend-ci")])

    def test_hub_wait_checks_of_a_failed_executor_go_to_whoever_raised_it(self):
        self.done_and_asleep()
        self.ev("schedule.set", "frontend-ci-wait-65a4-1", session="frontend-ci", at=9000,
                prompt="[waiting for reply] x. Check now; …", wait="x", **by("frontend-ci"), ts=2300)
        self.ev("session.wake_requested", "frontend-ci", **OWNER, ts=2400)
        for k in range(5):                                                   # it did not come up — failed
            self.ev("session.exited", "frontend-ci", ts=2401 + k)
        self.assertEqual(self.st["sessions"]["frontend-ci"]["state"], "failed")
        self.assertEqual(self.st["schedule"]["frontend-ci-wait-65a4-1"]["session"], "frontend")
        self.assertIn("executor frontend-ci —", self.st["schedule"]["frontend-ci-wait-65a4-1"]["prompt"])

    def test_a_check_handed_on_twice_keeps_one_note_naming_who_set_it(self):
        from core import schedule
        jobs = self.waits_then_removed()
        schedule.hand_waits(self.st, "frontend", "hub", 2600)      # and the starter was removed too — on to the starter's starter
        prompt = jobs["frontend-ci-wait-65a4-1"]["prompt"]
        self.assertEqual((jobs["frontend-ci-wait-65a4-1"]["session"], prompt.count(schedule.HANDED.strip())),
                         ("hub", 1))
        self.assertIn("executor frontend-ci —", prompt)

    def owner_asks_more(self, name, text, ts):
        """The owner writes to a handed-in hub executor in its field (the page wakes it), it answers and falls asleep again."""
        self.assertIsNone(self.ev("message.received", None, to=name, text=text, wake=True, ts=ts,
                                  channel="web", owner=True))
        mid = f"m{self.seq}"
        self.assertEqual(self.st["messages"][mid]["session"], name)
        self.ev("session.started", name, ts=ts + 1)
        self.ev("turn.started", name, marker=mid, prompt_head="x", ts=ts + 2)
        self.ev("turn.result", name, text="Done: the table is in seeds.md", ts=ts + 3)
        self.ev("turn.stopped", name, ts=ts + 3)
        end = ts + 3 + executors.RETIRE_AFTER_HUB
        self.assertEqual(executors.retiring(self.st, end), [("sleep", name)])   # put to sleep as handed in — as before
        self.ev("session.close_requested", name, retire=True, ts=end)
        self.ev("session.exited", name, why="sleep", ts=end + 1)
        self.assertEqual(self.st["sessions"][name]["state"], "retired")

    def test_an_executor_the_owner_talks_to_is_not_removed_by_the_term(self):
        # a hub executor for an owner task — the owner then wrote to it directly for two days, as to its own session;
        # after 30 min idle the core put it to sleep as handed in, an hour later the remove_after_hours term removed it (owner
        # decision: remove nothing). The owner's conversation partner is not a handed-in task: it does not leave by the term
        self.ev("session.started", "hub")
        self.done_and_asleep("seeds", raiser="hub", text="8 shops, the table is in seeds.md")
        # words that did not wake it and went to the hub are not a conversation with it: a handed-in one leaves by the term, as before
        self.assertIsNone(self.ev("message.received", None, to="seeds", text="and what about the ninth?", ts=4000, **OWNER))
        self.assertEqual(self.st["messages"][f"m{self.seq}"]["session"], "hub")
        s = self.st["sessions"]["seeds"]
        self.assertEqual(executors.retiring(self.st, s["since"] + 3600, keep=3600), [("remove", "seeds")])
        self.owner_asks_more("seeds", "and now by city", ts=5000)
        self.assertEqual(s["state"], "retired")
        self.assertEqual(executors.retiring(self.st, s["since"] + 10 ** 6, keep=3600), [])
        # brought back (hub session revive) after a manual removal — it is still a conversation partner
        self.assertIsNone(self.ev("session.remove_requested", "seeds", ts=9000, **OWNER))
        self.assertIsNone(self.ev("session.requested", "seeds", cwd="/fe/.claude/worktrees/x", revive=True, ts=9001,
                                  session_id=s["session_id"], **OWNER))
        self.ev("session.started", "seeds", ts=9002)
        self.ev("turn.started", "seeds", marker=None, prompt_head="x", ts=9003)
        self.ev("turn.stopped", "seeds", ts=9004)
        self.ev("session.close_requested", "seeds", retire=True, ts=9004 + executors.RETIRE_AFTER_HUB)
        self.ev("session.exited", "seeds", why="sleep", ts=9005 + executors.RETIRE_AFTER_HUB)
        self.assertEqual(executors.retiring(self.st, 10 ** 7, keep=3600), [])

    def test_a_page_button_does_not_make_an_executor_a_partner_an_answer_to_its_question_does(self):
        # the rule is simple: the owner's words that reached the executor itself — and an answer to its question too; a page button
        # (action) is not words
        self.ev("session.started", "hub")
        self.run_task("seeds", who="hub")
        self.start("seeds", raiser="hub")
        self.ev("session.started", "seeds")
        self.ev("turn.started", "seeds", marker=None, prompt_head="x", ts=1900)
        self.ev("turn.stopped", "seeds", ts=1901)
        self.assertIsNone(self.ev("message.received", None, to="seeds", text="Fix plugin x", action=True, ts=1902,
                                  channel="web", owner=True))
        self.assertEqual(self.st["messages"][f"m{self.seq}"]["session"], "seeds")
        self.assertIsNone(self.st["sessions"]["seeds"].get("owner_talks"))
        self.ev("turn.started", "seeds", marker=None, prompt_head="x", ts=1903)
        self.ev("permission.requested", "seeds", request_id="r1", tool="AskUserQuestion", interactive=True, ts=1904,
                input={"questions": [{"question": "Which region?", "options": [{"label": "EU"}, {"label": "US"}]}]})
        (q,) = [a["id"] for a in self.st["sessions"]["seeds"]["asks"]]
        self.assertIsNone(self.ev("message.received", None, to="seeds", text="EU", ts=1905, channel="web", owner=True))
        self.assertEqual(self.st["messages"][f"m{self.seq}"].get("answers_question"), q)   # an answer to its question
        self.assertEqual(self.st["sessions"]["seeds"].get("owner_talks"), 1905)

    def test_messages_do_not_wake_a_sleeping_executor_they_go_to_whoever_raised_it(self):
        # owner decision: messages need not be routed to sleeping sessions… they are woken only by hand or by asking the hub
        self.done_and_asleep()
        self.assertIsNone(self.ev("message.received", None, to="frontend-ci", text="and also fix the README", **OWNER))
        m = self.st["messages"][f"m{self.seq}"]
        self.assertEqual(m["session"], "frontend")
        self.assertEqual(m["text"], "and also fix the README")   # the words as they are, the note separately
        self.assertIn("MR !12", m["routing_note"])                                      # its last result
        self.assertIn("hub session wake frontend-ci", m["routing_note"])               # how to wake it
        self.ev("message.received", None, to="frontend-ci", text="look at the log", **by("devops-x"))
        self.assertEqual(self.st["sessions"]["frontend-ci"]["state"], "retired")  # not woken

    def test_only_the_owner_the_hub_or_whoever_raised_it_wakes_it_explicitly(self):
        self.done_and_asleep()
        self.ev("session.requested", "devops-x", cwd="/d", session_id="s-dx", **OWNER)
        self.ev("session.started", "devops-x")
        self.assertIsNotNone(self.ev("message.received", None, to="frontend-ci", text="wake up", wake=True,
                                     **by("devops-x")))                       # someone else's session cannot
        self.assertEqual(self.st["sessions"]["frontend-ci"]["state"], "retired")  # not woken
        self.ev("session.started", "hub")
        self.assertIsNone(self.ev("message.received", None, to="frontend-ci", text="wake up: README too", wake=True,
                                  **by("hub")))
        m = self.st["messages"][f"m{self.seq}"]
        self.assertEqual(m["session"], "frontend-ci")
        self.assertNotEqual(self.st["sessions"]["frontend-ci"]["state"], "asleep")  # it comes up
        self.assertFalse(self.st["sessions"]["frontend-ci"].get("retired"))

    def test_the_agent_that_raised_it_wakes_it_for_a_next_round(self):
        # a review of a merge request takes days: the agent wakes its own executor with the same conversation, no hub
        self.done_and_asleep()
        self.assertIsNone(self.ev("message.received", None, to="frontend-ci", text="the reviewer answered: fix the thread",
                                  wake=True, **by("frontend")))
        self.assertEqual(self.st["messages"][f"m{self.seq}"]["session"], "frontend-ci")
        self.assertNotEqual(self.st["sessions"]["frontend-ci"]["state"], "retired")
        self.assertFalse(self.st["sessions"]["frontend-ci"].get("retired"))

    def test_hub_session_wake_is_the_owners_the_hubs_or_the_raisers(self):
        self.done_and_asleep()
        self.ev("session.requested", "devops-x", cwd="/d", session_id="s-dx", **OWNER)
        self.ev("session.started", "devops-x")
        self.assertIsNotNone(self.ev("session.wake_requested", "frontend-ci", **by("devops-x")))
        self.assertIsNone(self.ev("session.wake_requested", "frontend-ci", **OWNER))
        self.assertEqual(self.st["sessions"]["frontend-ci"]["state"], "starting")
        self.assertFalse(self.st["sessions"]["frontend-ci"].get("retired"))

    def test_the_raiser_wakes_with_session_wake_without_a_job_too(self):
        self.done_and_asleep()
        self.assertIsNone(self.ev("session.wake_requested", "frontend-ci", **by("frontend")))
        self.assertEqual(self.st["sessions"]["frontend-ci"]["state"], "starting")
        self.assertFalse(self.st["sessions"]["frontend-ci"].get("retired"))

    def three_live_and_a_sleeper(self):
        """frontend has three live executors (its ceiling) and a fourth, asleep after handing in."""
        self.done_and_asleep("frontend-d")
        for n in ("a", "b", "c"):
            self.assertIsNone(self.run_task(f"frontend-{n}"))
            self.start(f"frontend-{n}")

    def test_the_raisers_wake_goes_through_the_ceiling_into_the_queue_not_around_it(self):
        # review: a wake by the raiser skipped executors.waits (the specialist's 3, the hub's ceiling, the saving, the pause)
        self.three_live_and_a_sleeper()
        self.assertIsNone(self.ev("message.received", None, to="frontend-d", text="the reviewer replied: fix it", wake=True,
                                  **by("frontend")))
        self.assertEqual(self.st["sessions"]["frontend-d"]["state"], "retired")      # not woken: no place
        self.assertIn("3", self.queued("frontend-d"))
        self.assertEqual(self.st["runs"]["frontend-d"]["wake"]["text"], "the reviewer replied: fix it")
        self.assertFalse([m for m in self.st["messages"].values() if m["session"] == "frontend-d"
                          and m["text"] == "the reviewer replied: fix it"])               # nothing is delivered yet
        self.assertEqual(executors.promotable(self.st), [])
        self.one_of_three_falls_asleep()   # one hands in and falls asleep
        self.assertEqual(executors.promotable(self.st), ["frontend-d"])               # the place is free: it rises by itself
        self.assertIsNone(self.ev("task.run_ready", "frontend-d", channel="core", owner=False))
        self.assertNotEqual(self.st["sessions"]["frontend-d"]["state"], "retired")
        self.assertEqual([m["text"] for m in self.st["messages"].values() if m["session"] == "frontend-d"][-1],
                         "the reviewer replied: fix it")
        self.assertNotIn("frontend-d", self.st["runs"])

    def one_of_three_falls_asleep(self):
        """frontend-a, the first of three live executors, hands in and goes to sleep: a place comes free."""
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-a"]
        self.ev("session.started", "frontend-a")
        self.ev("turn.started", "frontend-a", marker=d["message"], prompt_head="task")
        self.ev("turn.result", "frontend-a", text="Done")
        self.ev("turn.stopped", "frontend-a")
        self.ev("session.close_requested", "frontend-a", retire=True)
        self.ev("session.exited", "frontend-a", why="sleep")

    def queued_wake_whose_copy_is_taken(self, text):
        """The raiser's wake of frontend-d waits in the queue; the place comes free, but while it waited the raiser put
        another executor into the sleeper's copy (`hub task run y --here`): two in one copy. The wake leaves the queue
        (the gate runs again); the copy is taken through events, nothing is mocked."""
        self.three_live_and_a_sleeper()
        self.st["sessions"]["frontend-d"]["worktree"] = {"path": "/w/d"}
        self.assertIsNone(self.ev("message.received", None, to="frontend-d", text=text, wake=True, **by("frontend")))
        self.queued("frontend-d")
        self.ev("session.requested", "frontend-y", cwd="/w/d", session_id="s-y", **OWNER)
        self.ev("session.started", "frontend-y")
        self.one_of_three_falls_asleep()
        self.assertEqual(executors.promotable(self.st), ["frontend-d"])
        self.assertIsNone(self.ev("task.run_ready", "frontend-d", channel="core", owner=False))

    def test_the_note_about_a_taken_copy_is_one_phrase_before_the_task(self):
        # review: the reason was cut out of the gate's text and kept its own ": wait until…", so the note had two colons
        self.queued_wake_whose_copy_is_taken("fix it")
        (note,) = [m for m in self.st["messages"].values() if m["session"] == "frontend" and "cannot be handed over" in m["text"]]
        self.assertEqual(note["text"], "The wake-up of frontend-d cannot be handed over — its copy /w/d is taken by frontend-y; "
                                       "wait until frontend-y hands in, or a new job — to a new executor: fix it")
        self.assertNotIn("continues", note["text"])

    def test_the_gate_tells_the_owner_and_the_hub_who_continues_but_not_the_raiser(self):
        from unittest import mock
        self.three_live_and_a_sleeper()
        with mock.patch.object(executors, "refused", return_value="concurrency 0"):
            for who in (OWNER, by("hub")):
                self.assertIn('frontend continues: hub send frontend "…"',
                              self.ev("session.wake_requested", "frontend-d", **who) or "")
            for why in (self.ev("session.wake_requested", "frontend-d", **by("frontend")),
                        self.ev("message.received", None, to="frontend-d", text="more", wake=True, **by("frontend"))):
                self.assertIn("concurrency 0", why or "")
                self.assertNotIn("continues", why or "")

    def test_the_note_on_a_refusal_by_the_real_reason_has_one_colon_before_the_task(self):
        # review: executors.refused (minihub.alone) gave a phrase with its own colon, and the task stuck to "do it yourself"; no mock:
        # the raiser's folder turns "solo" by hand in the state (solo_auto: the gate says no, the ceiling stays)
        self.three_live_and_a_sleeper()
        self.assertIsNone(self.ev("message.received", None, to="frontend-d", text="once more", wake=True, **by("frontend")))
        self.one_of_three_falls_asleep()
        self.st["owner"]["parallel"] = 3
        self.st["sessions"]["frontend"]["solo_auto"] = "folder is not git"                  # the daemon's fact: the place is free, the gate says no
        self.assertIsNone(self.ev("task.run_ready", "frontend-d", channel="core", owner=False))
        (note,) = [m for m in self.st["messages"].values() if m["session"] == "frontend" and "cannot be handed over" in m["text"]]
        head, task = note["text"].rsplit(": ", 1)
        self.assertEqual(task, "once more")
        self.assertNotIn(":", head)
        self.assertIn("do it yourself", head)

    def test_a_wake_from_the_queue_meets_the_gate_again_its_copy_taken_meanwhile_is_not_shared(self):
        # review: the place came free, but while the wake waited the raiser put another executor into the sleeper's copy
        # (`hub task run y --here`): two in one copy. The gate runs at the wake, and again when it leaves the queue
        self.queued_wake_whose_copy_is_taken("the reviewer replied: fix it")
        self.assertEqual(self.st["sessions"]["frontend-d"]["state"], "retired")      # not woken into a busy copy
        self.assertNotIn("frontend-d", self.st["runs"])                               # the run is taken off
        notes = [m for m in self.st["messages"].values() if m["session"] == "frontend" and "frontend-d" in m["text"]
                 and "frontend-y" in m["text"]]
        self.assertEqual(len(notes), 1)                                               # the raiser learns why, in one line
        self.assertIn("The wake-up", notes[0]["text"])                                # no --about: not a "rework"
        self.assertFalse([m for m in self.st["messages"].values() if m["session"] == "frontend-d"
                          and m["text"] == "the reviewer replied: fix it"])               # and the text was not delivered

    def test_the_gates_refusal_to_the_raiser_does_not_tell_it_to_continue_by_itself(self):
        # "<raiser> continues: hub send <raiser>" is for the owner and the hub; the raiser is the one who "continues"
        from unittest import mock
        self.three_live_and_a_sleeper()
        self.assertIsNone(self.ev("message.received", None, to="frontend-d", text="more", wake=True, **by("frontend")))
        self.one_of_three_falls_asleep()
        with mock.patch.object(executors, "refused", return_value="concurrency 0 — do it yourself"):
            self.assertIsNone(self.ev("task.run_ready", "frontend-d", channel="core", owner=False))
        (note,) = [m for m in self.st["messages"].values() if m["session"] == "frontend" and "cannot be handed over" in m["text"]]
        self.assertIn("concurrency 0 — do it yourself", note["text"])
        self.assertNotIn("continues", note["text"])
        self.assertNotIn("hub send frontend", note["text"])

    def test_a_session_wake_without_a_job_is_not_doubled_in_the_queue_either(self):
        self.three_live_and_a_sleeper()
        self.assertIsNone(self.ev("message.received", None, to="frontend-d", text="more", wake=True, **by("frontend")))
        self.one_of_three_falls_asleep()
        self.assertIn("in the queue", self.ev("session.wake_requested", "frontend-d", **by("frontend")) or "")
        self.assertEqual(self.st["sessions"]["frontend-d"]["state"], "retired")        # not woken past its own place in the queue

    def test_the_raisers_session_wake_without_a_job_is_refused_at_the_ceiling_the_owner_and_the_hub_are_not(self):
        self.three_live_and_a_sleeper()
        why = self.ev("session.wake_requested", "frontend-d", **by("frontend"))
        self.assertIn("3", why or "")
        self.assertIn('hub session wake frontend-d "', why)                           # with a job it takes the queue
        self.assertEqual(self.st["sessions"]["frontend-d"]["state"], "retired")
        self.assertIsNone(self.ev("session.wake_requested", "frontend-d", **OWNER))   # they decide for themselves
        self.assertEqual(self.st["sessions"]["frontend-d"]["state"], "starting")

    def test_the_raisers_wake_with_a_task_name_continues_that_task(self):
        self.done_and_asleep()
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "frontend-ci"]
        self.assertIn("--about", self.ev("message.received", None, to="frontend-ci", text="once more", wake=True,
                                         about="d999", **by("frontend")) or "")       # not its task
        self.assertIsNone(self.ev("message.received", None, to="frontend-ci", text="once more", wake=True, about=d["id"],
                                  **by("frontend")))
        self.assertEqual(self.st["delegations"][d["id"]]["state"], "working")

    def test_a_wake_in_the_queue_is_not_doubled_and_a_direction_is_woken_only_by_the_hub_or_the_owner(self):
        self.three_live_and_a_sleeper()
        wake = lambda: self.ev("message.received", None, to="frontend-d", text="more", wake=True,  # noqa: E731
                               **by("frontend"))
        self.assertIsNone(wake())
        self.assertIn("in the queue", wake() or "")
        self.st["runs"].clear()
        # a direction (a session with a backlog) is not an executor, so a retired one with a backlog is not reached through
        # the core today: the state is set by hand, to check the gate itself (the same one as for stop and remove)
        self.st["sessions"]["frontend-d"]["backlog"] = "atlas"
        self.assertIn("a direction", wake() or "")
        self.assertEqual(self.st["sessions"]["frontend-d"]["state"], "retired")

    def test_a_directions_executor_knows_the_backlog_and_the_direction_gets_lessons(self):
        # a direction's executor started without a profile, and the direction never got "write up the lessons"
        self.ev("session.requested", "atlas", cwd="/a", session_id="s-at", **OWNER)
        self.ev("session.started", "atlas")
        self.ev("task.backlog", None, slug="atlas", **by("atlas"))
        self.done_and_asleep("atlas-tiles", raiser="atlas", text="Done, MR !12\n\n**Lessons**\n- tiles — by version")
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "atlas-tiles"]
        task = self.st["messages"][d["message"]]["text"]
        self.assertIn("hub direction atlas", task)                               # the direction's backlog
        notes = [m for m in self.st["messages"].values() if m["session"] == "atlas" and "handed in its task" in m["text"]]
        self.assertEqual(len(notes), 1)
        self.assertIn("hub direction atlas", notes[0]["text"])                   # it has no CLAUDE.md of its own — into the backlog

    def test_a_task_for_a_sleeping_executor_is_refused_and_the_hubs_open_question_stays(self):
        # the hub delegated the owner's words to the departed maps-export — the delegation went to the hub itself (the departed
        # executor's address led to whoever started it) and became "the owner's answer to a question" — to its foreign form
        self.ev("session.started", "hub")
        self.ev("message.received", None, to="hub", text="publish the sounds plugin publicly", ts=1500, **OWNER)
        words = f"m{self.seq}"                                               # the owner's words come before the question (as in the original incident)
        self.done_and_asleep("maps-export", raiser="hub")
        self.ev("turn.started", "hub", marker=None, prompt_head="x", ts=2300)
        self.ev("permission.requested", "hub", request_id="r1", tool="AskUserQuestion", interactive=True, ts=2301,
                input={"questions": [{"question": "Round 9: do you accept?", "options": [{"label": "Yes"}, {"label": "No"}]}]})
        asks = list(self.st["sessions"]["hub"].get("asks") or [])
        self.assertTrue(asks)                                                # the hub has an open question card
        why = self.ev("delegation.created", None, session="maps-export", **{"for": words}, context="publish", ts=2401,
                      **by("hub"))
        self.assertIn("hub session wake maps-export", why or "")             # refusal: it sleeps — wake it explicitly
        self.assertEqual(self.st["sessions"]["hub"].get("asks") or [], asks)  # the hub's question is untouched
        self.assertFalse([m for m in self.st["messages"].values() if "user's answer to your question" in m["text"]])
        self.assertIn("for yourself", self.ev("delegation.created", None, session="hub", context="x", ts=2402, **by("hub")) or "")
        self.ev("session.remove_requested", "maps-export", ts=2500)            # a refusal for the removed one too, not for the starter
        self.assertIn("is gone", self.ev("delegation.created", None, session="maps-export", context="x", ts=2501,
                                     **by("hub")) or "")

    def test_sleeping_executors_do_not_take_places(self):
        for n in ("a", "b", "c"):
            self.done_and_asleep(f"frontend-{n}")
        self.assertIsNone(self.run_task("frontend-d"))                         # three sleeping are not three live

    def queued(self, name):
        run = self.st["runs"][name]
        self.assertEqual(run["state"], "queued")
        return run["why"]

    def test_three_live_executors_per_specialist_the_fourth_waits_in_the_queue(self):
        # decision: the queue lives in the core and the hub sees it — a request is not lost to a refusal, it waits for a slot
        for n in ("a", "b", "c"):
            self.assertIsNone(self.run_task(f"frontend-{n}"))
            self.start(f"frontend-{n}")
        self.assertIsNone(self.run_task("frontend-d"))
        self.assertIn("3", self.queued("frontend-d"))
        hub_note = [m for m in self.st["messages"].values() if m["session"] == "hub" and "frontend-d" in m["text"]]
        self.assertEqual(len(hub_note), 1)                                         # the hub sees what is waiting
        self.assertIn("hub task cancel frontend-d", hub_note[0]["text"])
        self.assertEqual(executors.promotable(self.st), [])
        self.one_of_three_falls_asleep()   # one handed in and fell asleep
        self.assertEqual(self.st["sessions"]["frontend-a"]["state"], "retired")
        self.assertEqual(executors.promotable(self.st), ["frontend-d"])
        self.assertIsNone(self.ev("task.run_ready", "frontend-d", channel="core", owner=False))
        self.assertEqual(self.st["runs"]["frontend-d"]["state"], "ready")

    def test_ready_runs_count_and_one_place_promotes_one(self):
        # review: requests in ready were not counted — three in a row with two live gave five against a ceiling of three; one
        # freed slot brought up the whole queue
        for n in ("a", "b"):
            self.run_task(f"frontend-{n}")
            self.start(f"frontend-{n}")
        for n in ("c", "d", "e"):
            self.run_task(f"frontend-{n}")
        states = {n: self.st["runs"][f"frontend-{n}"]["state"] for n in ("c", "d", "e")}
        self.assertEqual(states, {"c": "ready", "d": "queued", "e": "queued"})
        self.start("frontend-c")
        self.one_of_three_falls_asleep()
        self.assertEqual(executors.promotable(self.st), ["frontend-d"])               # one slot — one

    def test_a_queue_pass_walks_the_sessions_once_not_once_per_request(self):
        # a big hub: thousands of sessions and a queue — every daemon tick walked all sessions for every queued request
        # (live_of, the max_live total); a pass counts the places once, with the same answers
        self.ev("owner.settings", None, timezone="UTC", executors={"max_live": 40}, channel="core")
        for i in range(30):
            self.ev("session.requested", f"idle-{i}", cwd=f"/i{i}", session_id=f"s-i{i}", **OWNER)
        for n in ("a", "b", "c"):
            self.run_task(f"frontend-{n}")
            self.start(f"frontend-{n}")
        for i in range(10):
            self.run_task(f"frontend-q{i}")
        queued = [n for n, r in self.st["runs"].items() if r["state"] == "queued"]
        self.assertEqual(len(queued), 10)
        self.st["sessions"], Counted.walked = Counted(self.st["sessions"]), 0
        self.assertEqual(executors.promotable(self.st), [])
        executors.recount(self.st, 2000)
        # two passes, each walks the sessions twice (places and budget.mode), not once or twice per request
        self.assertLessEqual(Counted.walked, 4 * len(self.st["sessions"]))
        for n in queued:   # the same reason as a walk per request gives: the raiser's three places are taken
            run = self.st["runs"][n]
            self.assertEqual(run["why"], executors.CAP_RAISER.format(raiser=run["by"], cap=3))

    def test_an_executor_of_the_hub_may_raise_up_to_six(self):
        # a specialist's executor starts nobody (a decision); a hub executor — up to 6
        self.ev("session.started", "hub")
        self.assertIsNone(self.run_task("maps-lead", text="a big task", who="hub"))
        self.start("maps-lead")
        for i in range(6):
            self.assertIsNone(self.run_task(f"lead-{i}", who="maps-lead"))
            self.start(f"lead-{i}")
        self.assertIsNone(self.run_task("lead-7", who="maps-lead"))
        self.assertIn("6", self.queued("lead-7"))

    def test_over_85_percent_of_the_week_it_waits_for_the_owners_word(self):
        self.ev("account.limits", None, windows={"seven_day": {"used": 0.9}})
        self.assertIsNone(self.run_task("frontend-ci"))
        self.assertIn("85", self.queued("frontend-ci"))
        self.assertEqual(executors.promotable(self.st), [])                      # the budget will not free itself
        self.assertIsNone(self.ev("task.run_requested", "owner-run", task="urgent", **OWNER))
        self.assertEqual(self.st["runs"]["owner-run"]["state"], "ready")
        self.assertIsNotNone(self.ev("task.run_go", "frontend-ci", **by("frontend")))   # it cannot approve itself
        self.assertIsNone(self.ev("task.run_go", "frontend-ci", **OWNER))            # the owner can
        self.assertEqual(self.st["runs"]["frontend-ci"]["state"], "ready")

    def test_a_paused_queue_raises_nobody_by_itself_until_resumed(self):
        # the owner said to start nothing; the queue was held only by the limit threshold — the account changed, the limit dropped, and
        # the queue brought everyone up by itself. The pause is explicit: hub task pause / resume (the hub or the owner)
        self.ev("account.limits", None, windows={"seven_day": {"used": 0.9}})
        self.run_task("frontend-ci")
        self.assertIsNotNone(self.ev("task.queue_paused", None, why="resource triage", **by("frontend")))  # not its call
        self.assertIsNone(self.ev("task.queue_paused", None, why="resource triage", **OWNER))
        self.ev("account.limits", None, windows={"seven_day": {"used": 0.1}})       # the limit dropped — it still waits
        self.assertEqual(executors.promotable(self.st), [])
        self.assertIn("is paused", executors.waits(self.st, "frontend"))
        self.assertIsNone(self.run_task("frontend-b"))
        self.assertIn("is paused", self.queued("frontend-b"))                           # new ones go to the queue too
        self.assertIsNone(self.ev("task.run_requested", "owner-run", task="urgent", **OWNER))
        self.assertEqual(self.st["runs"]["owner-run"]["state"], "ready")           # the owner himself — at once
        self.assertIsNone(self.ev("task.run_go", "frontend-b", **by("hub")))       # the hub explicitly — brings it up
        self.ev("session.started", "hub")
        self.assertIsNone(self.ev("task.queue_resumed", None, **by("hub")))
        self.assertEqual(executors.promotable(self.st), ["frontend-ci"])

    def test_pause_and_resume_recount_why_with_its_code(self):
        # a waiting one gets its reason anew, with the code: if the budget holds it, its reason and code stay (the savings card
        # and the mini-hub count them), if a slot holds it — "pause"; after the pause is lifted — not "pause"
        self.ev("account.limits", None, ts=1000, windows={"seven_day": {"used": 0.9, "resets": 5000}})
        self.assertIsNone(self.run_task("frontend-ci"))
        self.assertEqual(self.st["runs"]["frontend-ci"].get("why_code"), forms.WEEKLY_LIMIT)
        self.assertIsNone(self.ev("task.queue_paused", None, ts=2000, why="resource triage", **OWNER))
        run = self.st["runs"]["frontend-ci"]
        self.assertEqual(run.get("why_code"), forms.WEEKLY_LIMIT, run["why"])
        self.assertIn("is paused", executors.waits(self.st, "frontend", now=5001))   # the budget would release it — the pause holds it
        self.assertIsNone(self.ev("task.queue_resumed", None, ts=5001, **OWNER))   # the window is already reset
        run = self.st["runs"]["frontend-ci"]
        self.assertEqual(run["state"], "queued")
        self.assertEqual(run["why"], forms.READY_TO_RISE)   # not None: hub task / hub status print it as is
        self.assertNotIn("why_code", run)
        self.assertEqual(executors.promotable(self.st, 5001), ["frontend-ci"])

    def test_the_page_snapshot_carries_the_pause_with_who_why_and_how_many_wait(self):
        # the page does not guess the pause from the waiting ones' reasons: who waits, why and how long — comes from the core state
        from core import views
        r = views.Reader()

        def paused():
            r.state = self.st   # reduce returns a new state
            return r.snapshot()["queue_paused"]
        self.assertIsNone(paused())
        self.assertIsNone(self.ev("task.queue_paused", None, ts=2000, why="resource triage", **OWNER))
        self.assertIsNone(self.run_task("frontend-ci"))
        # the owner is None: the page fills in the name in its own language
        self.assertEqual(paused(), {"by": None, "why": "resource triage", "ts": 2000, "queued": 1})
        self.assertIsNone(self.ev("task.queue_resumed", None, **OWNER))
        self.assertIsNone(paused())

    def mod(self, name):
        """What the core stamps on a module's fact (core/spool.outsider): the plugin channel, not the owner, mod:<name>."""
        return {"channel": "plugin", "owner": False, "sender": f"mod:{name}"}

    def test_a_module_pauses_the_queue_and_lifts_only_its_own_pause(self):
        self.assertIsNone(self.ev("task.queue_paused", None, why="heavy build", **self.mod("builds")))
        self.run_task("frontend-ci")
        self.assertEqual(self.st["queue_paused"]["by"], "mod:builds")
        self.assertIn("is paused", self.queued("frontend-ci"))
        self.assertEqual(executors.promotable(self.st), [])
        self.assertIsNone(self.ev("task.queue_paused", None, why="deploy", **self.mod("deploys")))   # a second module
        self.assertIsNone(self.ev("task.queue_resumed", None, **self.mod("builds")))
        self.assertEqual(self.st["queue_paused"]["by"], "mod:deploys")                  # the other one still holds it
        self.assertEqual(executors.promotable(self.st), [])
        self.assertIsNone(self.ev("task.queue_resumed", None, **self.mod("deploys")))
        self.assertNotIn("queue_paused", self.st)
        self.assertEqual(executors.promotable(self.st), ["frontend-ci"])

    def test_a_module_does_not_lift_a_pause_of_the_hub_or_the_owner_or_another_module(self):
        self.run_task("frontend-ci")
        for who in (OWNER, by("hub")):
            self.setUp()
            self.run_task("frontend-ci")
            self.assertIsNone(self.ev("task.queue_paused", None, why="triage", **who))
            why = self.ev("task.queue_resumed", None, **self.mod("builds"))
            self.assertIn("only", why)
            self.assertEqual(self.st["queue_paused"]["why"], "triage")
            # a module pausing under theirs is kept apart: their resume lifts the queue wholly
            self.assertIsNone(self.ev("task.queue_paused", None, why="mine", **self.mod("builds")))
            self.assertEqual(self.st["queue_paused"]["why"], "triage")
            self.assertIsNone(self.ev("task.queue_resumed", None, **who))
            self.assertNotIn("queue_paused", self.st)
        self.setUp()
        self.assertIsNone(self.ev("task.queue_paused", None, why="a", **self.mod("builds")))
        self.assertIn("builds", self.ev("task.queue_resumed", None, **self.mod("deploys")) or "")
        self.assertEqual(self.st["queue_paused"]["by"], "mod:builds")

    def test_the_owner_lifts_a_modules_pause_and_a_resume_with_nothing_set_is_quiet(self):
        self.assertIsNone(self.ev("task.queue_resumed", None, **self.mod("builds")))
        self.assertIsNone(self.ev("task.queue_paused", None, why="x", **self.mod("builds")))
        self.assertIsNone(self.ev("task.queue_resumed", None, **OWNER))                 # a stuck module cannot hold the queue
        self.assertNotIn("queue_paused", self.st)
        self.assertIsNone(self.ev("task.queue_resumed", None, **self.mod("builds")))   # its own state is gone: still quiet
        self.assertNotIn("queue_paused", self.st)

    def test_a_pause_recorded_before_modules_could_pause_stays_the_hubs(self):
        self.st["queue_paused"] = {"by": None, "why": "old", "ts": 5}   # a state written by the core before this
        self.assertIn("owner", self.ev("task.queue_resumed", None, **self.mod("builds")))
        self.assertEqual(self.st["queue_paused"]["why"], "old")
        self.assertIsNone(self.ev("task.queue_paused", None, why="mine", **self.mod("builds")))
        self.assertEqual(self.st["queue_paused"]["why"], "old")                    # the module did not take it over
        self.assertIsNone(self.ev("task.queue_resumed", None, **self.mod("builds")))
        self.assertEqual(self.st["queue_paused"]["why"], "old")                    # and lifting its own left the old one

    def test_a_plugin_or_a_session_is_still_not_the_hub_for_the_pause(self):
        for who in ({"channel": "plugin", "owner": False, "sender": "ext:themes"}, by("frontend")):
            self.assertIsNotNone(self.ev("task.queue_paused", None, why="x", **who))
        self.assertNotIn("queue_paused", self.st)

    def fill(self, n=3):
        for k in range(n):
            self.assertIsNone(self.run_task(f"frontend-{k}"))
            self.start(f"frontend-{k}")

    def free_one(self, name="frontend-0"):
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == name]
        self.ev("session.started", name)
        self.ev("turn.started", name, marker=d["message"], prompt_head="task")
        self.ev("turn.result", name, text="Done")
        self.ev("turn.stopped", name)
        self.ev("session.close_requested", name, retire=True)
        self.ev("session.exited", name, why="sleep")

    def test_a_paused_queue_holds_even_a_request_on_the_owners_word_that_waited_for_a_place(self):
        # the pause lets through a request on the owner's word itself, not a queue pass: one that waited for a slot with his word (owner_ok)
        # does not come up by itself while the pause stands
        self.fill()
        self.assertIsNone(self.ev("task.run_requested", "frontend-x", task="on the word", **by("frontend")))
        self.st["runs"]["frontend-x"]["owner_ok"] = True   # as after hub task go --queue --for <question id>
        self.assertIsNone(self.ev("task.queue_paused", None, why="triage", **OWNER))
        self.free_one()
        self.assertEqual(executors.promotable(self.st), [])
        self.assertIsNone(self.ev("task.queue_resumed", None, **OWNER))
        self.assertEqual(executors.promotable(self.st), ["frontend-x"])

    def test_resume_says_ready_only_to_as_many_as_there_are_places(self):
        # lifting the pause recomputes the reasons along the queue: one slot — "will come up by itself" for one, the rest wait for a slot
        self.fill()
        self.assertIsNone(self.ev("task.queue_paused", None, why="triage", **OWNER))
        for n in ("x", "y"):
            self.assertIsNone(self.run_task(f"frontend-{n}"))
        self.free_one()
        self.assertIsNone(self.ev("task.queue_resumed", None, **OWNER))
        self.assertEqual(self.st["runs"]["frontend-x"]["why"], forms.READY_TO_RISE)
        self.assertIn("3", self.st["runs"]["frontend-y"]["why"])
        self.assertEqual(executors.promotable(self.st), ["frontend-x"])

    def test_the_week_past_its_reset_does_not_hold_the_queue(self):
        # the weekly window share is valid until its reset; after that it is stale, even if there has been no new limit event yet
        self.ev("account.limits", None, ts=1000, windows={"seven_day": {"used": 0.9, "resets": 5000}})
        self.assertIsNone(self.run_task("frontend-ci"))
        self.assertIn("85", self.queued("frontend-ci"))
        self.assertEqual(executors.promotable(self.st, 4999), [])
        self.assertEqual(executors.promotable(self.st, 5001), ["frontend-ci"])         # the daemon — by its own clock
        self.assertIsNone(self.ev("task.run_ready", "frontend-ci", ts=5001, channel="core", owner=False))
        self.assertIsNone(self.ev("task.run_requested", "frontend-ui", ts=5002, task="more", **by("frontend")))
        self.assertEqual(self.st["runs"]["frontend-ui"]["state"], "ready")

    def test_another_account_does_not_inherit_the_weekly_share(self):
        # pool: the active account hit its cap (90%), the core switched to another — until its first limits the threshold looks at its last
        # known figures, not the previous account's
        self.ev("account.switched", None, to="a", restart=False, channel="core")
        self.ev("account.limits", None, account="b", windows={"seven_day": {"used": 0.95, "resets": 10 ** 10}})
        self.ev("account.limits", None, account="a", windows={"seven_day": {"used": 0.9, "resets": 10 ** 10}})
        self.ev("account.switched", None, to="c", restart=True, channel="core")
        self.assertIsNone(self.run_task("frontend-ci"))
        self.assertEqual(self.st["runs"]["frontend-ci"]["state"], "ready")              # nothing is known about "c" yet
        self.ev("account.switched", None, to="b", restart=True, channel="core")
        self.assertIsNone(self.run_task("frontend-ui"))
        self.assertIn("95", self.queued("frontend-ui"))                                 # "b" has its own 95%

    def test_the_week_reset_at_this_very_second_is_past(self):
        # the queue and "Spending" share one boundary (core/executors.reset_passed): a reset in this very second has already passed
        self.ev("account.limits", None, ts=1000, windows={"seven_day": {"used": 0.9, "resets": 5000}})
        self.assertIsNone(self.run_task("frontend-ci"))
        self.assertEqual(executors.promotable(self.st, 5000), ["frontend-ci"])

    def test_back_on_the_default_login_its_shares_are_known(self):
        # the default login (no pool) hit 90%, the pool was enabled, then a pause again: the threshold is the default login's figures,
        # not empty until its next event
        self.ev("account.limits", None, windows={"seven_day": {"used": 0.9, "resets": 10 ** 10}})
        self.ev("account.switched", None, to="a", restart=True, channel="core")
        self.ev("account.limits", None, account="a", windows={"seven_day": {"used": 0.1, "resets": 10 ** 10}})
        self.ev("account.switched", None, to=None, restart=True, channel="core")
        self.assertEqual(self.st["limits"]["windows"]["seven_day"]["used"], 0.9)
        self.assertIsNone(self.run_task("frontend-ci"))
        self.assertIn("90", self.queued("frontend-ci"))

    def test_over_85_percent_the_hub_raises_on_the_owners_word(self):
        # limit 99%, the owner answered the hub's form with "Start anyway" — the hub's task run was still rejected
        self.ev("account.limits", None, windows={"seven_day": {"used": 0.99}})
        self.ev("session.started", "hub")
        self.ev("turn.started", "hub", marker=None, prompt_head="x")
        self.ev("permission.requested", "hub", request_id="r1", tool="AskUserQuestion", interactive=True,
                input={"questions": [{"question": "Limit 99%: start?", "options": [{"label": "Yes"}]}]})
        qid = self.st["sessions"]["hub"]["asks"][0]["id"]
        self.ev("permission.answered", "hub", request_id="r1", behavior="allow",
                answers={"Limit 99%: start?": "Start anyway"}, channel="web", owner=True)
        (mid,) = [m["id"] for m in self.st["messages"].values() if m.get("answers_question") == qid]
        self.assertIsNone(self.run_task("stale-a", who="hub"))
        self.assertIn("85", self.queued("stale-a"))                                 # without the owner's word — it waits
        self.assertIsNotNone(self.ev("task.run_go", "stale-a", **by("hub")))         # the hub without the word — no
        self.assertIsNone(self.ev("task.run_go", "stale-a", **{"for": mid}, **by("hub")))
        self.assertEqual(self.st["runs"]["stale-a"]["state"], "ready")
        self.assertIsNone(self.run_task("stale-b", who="hub", **{"for": qid}))       # or a question number — at once
        self.assertEqual(self.st["runs"]["stale-b"]["state"], "ready")
        self.assertEqual(self.st["runs"]["stale-a"]["by"], "hub")                    # the hub started it, the result goes to the hub
        self.assertIn("--for", self.run_task("stale-c", who="frontend", **{"for": mid}) or "")   # the owner's word goes to the hub

    def test_the_hub_raises_on_the_owners_word_to_the_executors_lead(self):
        # the owner wrote to a mini-hub "start its executor", it passed that to the hub — hub task go --for mN with its own
        # message was rejected (only a word to the hub counts), and the hub referred to another message
        self.ev("account.limits", None, windows={"seven_day": {"used": 0.99}})
        self.ev("session.started", "hub")
        self.ev("session.requested", "seo", cwd="/seo", session_id="s-seo", role="agent", **OWNER)
        self.ev("session.started", "seo")
        self.assertIsNone(self.run_task("frontend-ci"))
        self.assertIsNone(self.run_task("frontend-ui"))
        self.assertIn("85", self.queued("frontend-ci"))
        self.ev("message.received", None, to="seo", text="raise frontend-ci", ts=2000, **OWNER)
        other = f"m{self.seq}"
        self.ev("message.received", None, to="frontend", text="raise frontend-ci", ts=2000, **OWNER)
        mid = f"m{self.seq}"
        self.ev("message.received", None, to="frontend", text="raise", ts=2000, **by("seo"))
        not_owner = f"m{self.seq}"
        self.assertIsNotNone(self.ev("task.run_go", "frontend-ci", **{"for": other}, **by("hub")))   # another session's
        self.assertIsNotNone(self.ev("task.run_go", "frontend-ci", **{"for": not_owner}, **by("hub")))   # not the owner
        self.assertIsNotNone(self.ev("task.run_go", "frontend-ci", **{"for": mid}, **by("frontend")))   # only the hub
        self.assertIsNotNone(self.ev("task.run_go", "frontend-ci", ts=2000 + 86401, **{"for": mid}, **by("hub")))
        self.assertIsNone(self.ev("task.run_go", "frontend-ci", ts=2100, **{"for": mid}, **by("hub")))
        self.assertEqual(self.st["runs"]["frontend-ci"]["state"], "ready")
        self.assertIsNone(self.ev("task.run_go", "frontend-ui", ts=2101, queue=True, **{"for": mid}, **by("hub")))
        self.assertEqual(self.st["runs"]["frontend-ui"]["state"], "ready")

    def test_a_queued_request_is_cancelled_by_who_asked_the_hub_or_the_owner(self):
        for n in ("a", "b", "c"):
            self.run_task(f"frontend-{n}")
            self.start(f"frontend-{n}")
        self.run_task("frontend-d")
        self.ev("session.requested", "seo", cwd="/s", session_id="s-seo", role="agent", **OWNER)
        self.ev("session.started", "seo")
        self.assertIsNotNone(self.ev("task.run_cancelled", "frontend-d", **by("seo")))
        self.ev("session.started", "hub")
        self.assertIsNone(self.ev("task.run_cancelled", "frontend-d", **by("hub")))
        self.assertNotIn("frontend-d", self.st["runs"])
        told = [m for m in self.st["messages"].values() if m["session"] == "frontend" and "frontend-d" in m["text"]
                and "cancelled" in m["text"]]
        self.assertEqual(len(told), 1)                                             # whoever asked knows

    def test_the_hub_has_no_ceiling(self):
        # decision: the hub should have no ceiling at all — after the migration, directions took its 6 slots
        self.ev("session.started", "hub")
        for i in range(8):
            self.assertIsNone(self.run_task(f"hub-task-{i}", who="hub"))
            self.start(f"hub-task-{i}", raiser="hub")
        self.assertIsNone(self.run_task("hub-task-9", who="hub"))

    def test_a_specialists_executor_may_not_raise_executors_and_knows_what_it_is(self):
        # decision: specialists' executors must not start other executors — that must be
        # forbidden; and they must know they are executors, not the main specialists
        self.run_task("frontend-ci")
        self.start("frontend-ci")
        why = self.run_task("frontend-ci-sub", who="frontend-ci")
        self.assertIn("executor", why or "")
        self.assertIn("hub send frontend", why or "")
        from core import reducer as r
        action = r.spawn_action(self.st["sessions"]["frontend-ci"], st=self.st)
        self.assertEqual(action.get("executor_of"), "frontend")
        self.assertNotIn("executor_of", r.spawn_action(self.st["sessions"]["frontend"], st=self.st))

    def test_the_executor_note_is_in_its_system_prompt(self):
        import tempfile
        from hosts.streamjson import StreamHost
        h = StreamHost(tempfile.mkdtemp(prefix="exnote-"), claude=("claude",))
        cmd = h.command("frontend-ci", "sid", executor_of="frontend", agents=[("seo", "SEO")])
        with open(cmd[cmd.index("--append-system-prompt-file") + 1], encoding="utf-8") as f:
            prompt = f.read()
        self.assertIn("an executor of the specialist frontend", prompt)
        self.assertIn("do not raise executors", prompt)
        self.assertIn("seo", prompt)                                               # and the list of agents is in place
        self.assertEqual((cmd.count("--append-system-prompt"), cmd.count("--append-system-prompt-file")), (0, 1))

    def test_names_are_unique(self):
        self.assertIsNotNone(self.run_task("frontend"))                    # the name is taken by a specialist

    def test_a_failed_worktree_tells_whoever_raised(self):
        self.run_task("frontend-ci")
        self.ev("task.run_failed", "frontend-ci", reason="git worktree add: the branch is busy")
        self.assertNotIn("frontend-ci", self.st.get("runs") or {})
        self.assertTrue(any("the branch is busy" in m["text"] for m in self.st["messages"].values() if m["session"] == "frontend"))

    def test_the_hub_raises_an_executor_without_a_specialist(self):
        self.assertIsNone(self.ev("task.run_requested", "opentofu-cf", task="DNS import", repo="/r/opentofu", **by("hub")))
        self.start("opentofu-cf", raiser="hub", cwd="/r/opentofu")
        x = self.st["sessions"]["opentofu-cf"]
        self.assertEqual((x["role"], x["raised_by"], x.get("profile")), ("executor", "hub", None))


    def test_the_hubs_executor_offers_a_direction_and_sleeps_after_30_minutes(self):
        # decision: when the hub starts an executor, the task is done; if there are more questions it can be turned into a
        # direction; if not, it falls asleep by itself in 30 minutes and must be woken by hand
        self.ev("session.started", "hub")
        self.run_task("seeds", who="hub")
        self.start("seeds", raiser="hub")
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "seeds"]
        self.ev("session.started", "seeds")
        self.ev("turn.started", "seeds", marker=d["message"], prompt_head="task", ts=2000)
        self.ev("turn.result", "seeds", text="8 shops, the table is in seeds.md", ts=2001)
        self.ev("turn.stopped", "seeds", ts=2001)
        (to_hub,) = [m for m in self.st["messages"].values() if m["session"] == "hub" and "seeds.md" in m["text"]
                     and "reply to task" in m.get("origin", "")]
        self.assertIn("make this a direction", to_hub["text"])
        self.assertIn("30 minutes", to_hub["text"])
        self.assertEqual(executors.retiring(self.st, 2001 + executors.RETIRE_AFTER), [])          # 2 minutes — too early
        self.assertEqual(executors.retiring(self.st, 2001 + executors.RETIRE_AFTER_HUB), [("sleep", "seeds")])

    # --here and --branch (mini-hubs, slice 3): an executor in a ready copy and on an existing branch
    def test_the_minihub_note_names_both_flags(self):
        from core import minihub
        self.assertIn("--branch", minihub.NOTE)
        self.assertIn("--here", minihub.NOTE)

    def test_a_refused_commit_or_push_of_a_minihub_goes_to_an_executor_that_may_run_it(self):
        # a mini-hub asked the owner to run git push in a terminal after the core's refusal (PR #432): the mini-hub's
        # launch bans commit and push, its addendum hands them to an executor, and the executor's launch allows them;
        # whether to ask the owner before a push is the repo's own rule, not the core's
        from tests import test_minihub
        host = test_minihub.Host("spawn")
        deny, prompt = host.spawn(tempfile.mkdtemp(prefix="minihub-push-"), minihub=True)
        self.assertTrue({"Bash(git commit:*)", "Bash(git push:*)"} <= set(deny), deny)
        self.assertIn("after the refusal hand commit/push to an executor", prompt)
        self.assertNotRegex(prompt, r"push[^.]*AskUserQuestion")
        deny, prompt = host.spawn(tempfile.mkdtemp(prefix="minihub-push-"), bus=True)   # the mini-hub's executor
        self.assertFalse([r for r in deny if r.startswith("Bash(git")], deny)
        self.assertNotIn("You are a mini-hub", prompt)

    def test_here_needs_repo_and_does_not_go_with_branch(self):
        self.assertIn("--repo", self.run_task("fe-a", here=True) or "")
        self.assertIn("--branch", self.run_task("fe-b", repo="/w/copy", here=True, branch="feat/a") or "")
        self.assertNotIn("fe-a", self.st.get("runs") or {})
        self.assertIsNone(self.run_task("fe-c", repo="/w/copy", here=True))
        self.assertTrue(self.st["runs"]["fe-c"]["here"])
        self.assertIsNone(self.run_task("fe-d", branch="feat/a"))
        self.assertEqual(self.st["runs"]["fe-d"]["branch"], "feat/a")

    def test_from_main_is_kept_on_the_run_and_does_not_go_with_here_or_branch(self):
        self.assertIn("--from-main", self.run_task("fe-a", branch="feat/a", from_main=True) or "")
        self.assertIn("--from-main", self.run_task("fe-b", repo="/w/copy", here=True, from_main=True) or "")
        self.assertIsNone(self.run_task("fe-c", from_main=True))
        self.assertTrue(self.st["runs"]["fe-c"]["from_main"])
        self.assertIsNone(self.run_task("fe-d"))
        self.assertNotIn("from_main", self.st["runs"]["fe-d"])

    def test_here_refuses_the_raisers_own_folder_and_a_folder_taken_by_a_live_session(self):
        self.assertIn("is your folder", self.run_task("fe-a", repo="/fe/", here=True) or "")
        self.ev("session.requested", "maps", cwd="/w/copy", session_id="s-maps", **OWNER)
        self.assertIn("taken by maps", self.run_task("fe-b", repo="/w/copy", here=True) or "")
        self.ev("session.ended", "maps", reason="logout")                # asleep — a message will wake it: busy
        self.assertIn("taken by maps", self.run_task("fe-b", repo="/w/copy", here=True) or "")
        self.ev("session.remove_requested", "maps", **OWNER)
        self.assertEqual(self.st["sessions"]["maps"]["state"], "removed")
        self.assertIsNone(self.run_task("fe-c", repo="/w/copy", here=True))

    def test_here_refuses_the_tree_the_raiser_works_in_and_one_with_a_session_in_a_subfolder(self):
        self.ev("session.cwd_set", "frontend", cwd="/w/copy/app", **OWNER)
        self.assertIn("is your folder", self.run_task("fe-a", repo="/w/copy", here=True) or "")
        self.ev("session.cwd_set", "frontend", cwd="/fe", **OWNER)
        self.ev("session.requested", "maps", cwd="/w/copy/app", session_id="s-maps", **OWNER)
        self.assertIn("taken by maps", self.run_task("fe-b", repo="/w/copy", here=True) or "")
        self.assertIsNone(self.run_task("fe-c", repo="/w/copy/.claude/worktrees/old", here=True))  # a copy next to it — its own

    def test_the_brief_names_the_source_folder_when_the_copy_does_not_have_it(self):
        self.assertIsNone(self.run_task("fe-i"))
        wt = {"path": "/fe/.claude/worktrees/fe-i", "branch": "task/fe-i", "repo": "/fe", "source_cwd": "/fe/output"}
        events = executors.events_for(self.st, "fe-i", self.st["runs"]["fe-i"], cwd=wt["path"], worktree=wt)
        brief = [d for t, _, d in events if t == "delegation.created"][0]["context"]
        self.assertIn("/fe/output", brief)
        self.assertIn("by their absolute path", brief)

    def test_the_brief_keeps_both_paragraphs_for_a_branch_and_a_missing_source_folder(self):
        self.assertIsNone(self.run_task("fe-j", branch="feat-x"))
        wt = {"path": "/fe/.claude/worktrees/fe-j", "branch": "feat-x", "repo": "/fe", "source_cwd": "/fe/output"}
        events = executors.events_for(self.st, "fe-j", self.st["runs"]["fe-j"], cwd=wt["path"], worktree=wt)
        brief = [d for t, _, d in events if t == "delegation.created"][0]["context"]
        self.assertIn("/fe/output", brief)
        self.assertIn("existing branch feat-x", brief)

    def test_a_removed_here_executor_is_not_revived_into_a_copy_someone_else_took(self):
        self.here_executor()
        self.ev("session.stop_requested", "fe-here", **OWNER)
        self.ev("session.remove_requested", "fe-here", **OWNER)
        self.here_executor("fe-here-2")
        old = self.st["sessions"]["fe-here"]
        why = self.ev("session.requested", "fe-here", cwd="/w/copy", session_id=old["session_id"], revive=True, **OWNER)
        self.assertIn("taken by fe-here-2", why or "")

    def test_a_removed_executor_is_not_revived_into_its_copy_handed_to_another(self):
        path = "/fe/.claude/worktrees/fe-a"
        wt = {"path": path, "branch": "task/fe-a", "repo": "/fe"}
        self.assertIsNone(self.run_task("fe-a"))
        for type, entity, data in executors.events_for(self.st, "fe-a", self.st["runs"]["fe-a"], cwd=path, worktree=wt):
            self.ev(type, entity, **data)
        self.ev("session.started", "fe-a")
        self.ev("session.stop_requested", "fe-a", **OWNER)
        self.ev("session.remove_requested", "fe-a", **OWNER)
        self.here_executor("fe-b", cwd=path)
        why = self.ev("session.requested", "fe-a", cwd=path, session_id=self.st["sessions"]["fe-a"]["session_id"],
                      revive=True, worktree=wt, **OWNER)
        self.assertIn("taken by fe-b", why or "")

    def test_a_revived_from_main_executor_keeps_the_choice_for_the_next_revive(self):
        # PR #283: revive puts from_main into the copy's details — without the key in the fold a second revive would start from HEAD
        path = "/fe/.claude/worktrees/fe-m"
        wt = {"path": path, "branch": "task/fe-m", "repo": "/fe", "from_main": True}
        self.assertIsNone(self.run_task("fe-m", from_main=True))
        for type, entity, data in executors.events_for(self.st, "fe-m", self.st["runs"]["fe-m"], cwd=path, worktree=wt):
            self.ev(type, entity, **data)
        self.assertTrue(self.st["sessions"]["fe-m"]["worktree"]["from_main"])
        self.ev("session.started", "fe-m")
        self.ev("session.stop_requested", "fe-m", **OWNER)
        self.ev("session.remove_requested", "fe-m", **OWNER)
        why = self.ev("session.requested", "fe-m", cwd=path, session_id=self.st["sessions"]["fe-m"]["session_id"],
                      revive=True, worktree=wt, **OWNER)
        self.assertIsNone(why)
        self.assertTrue(self.st["sessions"]["fe-m"]["worktree"]["from_main"])

    def test_a_short_handed_over_message_gets_the_folder_note_after_it_and_the_quote_keeps_its_words(self):
        reducer.new_message(self.st, "m-x", "frontend", "redo the banner", 2000, origin=" from seo")
        self.st["messages"]["m-x"]["handover"] = "fe-h"   # as hub route … executor leaves it
        run = {"by": "frontend", "task": "redo the banner", "message": "m-x", "repo": None, "ts": 2000, "state": "ready"}
        self.st["runs"] = {"fe-h": run}
        wt = {"path": "/fe/.claude/worktrees/fe-h", "branch": "task/fe-h", "repo": "/fe", "source_cwd": "/fe/output"}
        for type, entity, data in executors.events_for(self.st, "fe-h", run, cwd=wt["path"], worktree=wt):
            self.assertIsNone(self.ev(type, entity, **data), (type, data))
        text = self.st["messages"]["m-x"]["text"]
        self.assertTrue(text.endswith("redo the banner\n\n" + executors.source_note(wt)), text)
        (told,) = [m for m in self.st["messages"].values() if m["session"] == "frontend" and "was taken by" in m["text"]]
        self.assertIn("(\"redo the banner\")", told["text"])

    def test_a_revived_executor_keeps_the_raisers_folder_its_copy_lacks(self):
        path = "/fe/.claude/worktrees/fe-o"
        wt = {"path": path, "branch": "task/fe-o", "repo": "/fe", "source_cwd": "/fe/output"}
        self.assertIsNone(self.run_task("fe-o"))
        for type, entity, data in executors.events_for(self.st, "fe-o", self.st["runs"]["fe-o"], cwd=path, worktree=wt):
            self.ev(type, entity, **data)
        self.ev("session.started", "fe-o")
        self.ev("session.stop_requested", "fe-o", **OWNER)
        self.ev("session.remove_requested", "fe-o", **OWNER)
        why = self.ev("session.requested", "fe-o", cwd=path, session_id=self.st["sessions"]["fe-o"]["session_id"],
                      revive=True, worktree=wt, **OWNER)
        self.assertIsNone(why)
        self.assertEqual(self.st["sessions"]["fe-o"]["worktree"]["source_cwd"], "/fe/output")

    def here_executor(self, name="fe-here", cwd="/w/copy"):
        self.assertIsNone(self.run_task(name, repo=cwd, here=True))
        run = self.st["runs"][name]
        for type, entity, data in executors.events_for(self.st, name, run, cwd=cwd, worktree=None, here=True):
            self.assertIsNone(self.ev(type, entity, **data), (type, data))
        self.ev("session.started", name)
        return self.st["sessions"][name]

    def test_a_here_executor_owns_no_worktree_and_cleaning_never_touches_its_folder(self):
        x = self.here_executor()
        self.assertTrue(x["here"])
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "fe-here"]
        self.assertIn("ready copy /w/copy — it is not yours", d["context"])
        self.assertIsNone(x.get("worktree"))
        self.ev("session.stop_requested", "fe-here", **OWNER)
        self.ev("session.remove_requested", "fe-here", **OWNER)
        self.assertEqual(self.st["sessions"]["fe-here"]["state"], "removed")
        self.assertEqual(executors.worktrees_to_clean(self.st), [])

    def retire(self, name):
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == name]
        self.ev("turn.started", name, marker=d["message"], prompt_head="task")
        self.ev("turn.result", name, text="ready")
        self.ev("turn.stopped", name)
        self.ev("session.close_requested", name, retire=True)
        self.ev("session.exited", name, why="sleep")
        self.assertTrue(executors.dormant(self.st["sessions"][name]))

    def test_a_sleeping_executor_whose_copy_was_handed_on_is_not_woken_while_the_other_works_there(self):
        path = "/fe/.claude/worktrees/fe-a"
        self.assertIsNone(self.run_task("fe-a"))
        for type, entity, data in executors.events_for(self.st, "fe-a", self.st["runs"]["fe-a"], cwd=path,
                                                       worktree={"path": path, "branch": "task/fe-a", "repo": "/fe"}):
            self.ev(type, entity, **data)
        self.ev("session.started", "fe-a")
        self.retire("fe-a")
        self.here_executor("fe-b", cwd=path)
        self.assertIn("taken by fe-b", self.ev("session.wake_requested", "fe-a", **OWNER) or "")

    def test_an_executor_raised_from_a_subfolder_is_not_woken_into_its_copy_taken_whole(self):
        path = "/fe/.claude/worktrees/fe-a"
        self.assertIsNone(self.run_task("fe-a"))
        for type, entity, data in executors.events_for(self.st, "fe-a", self.st["runs"]["fe-a"], cwd=path + "/app",
                                                       worktree={"path": path, "branch": "task/fe-a", "repo": "/fe"}):
            self.ev(type, entity, **data)
        self.ev("session.started", "fe-a")
        self.retire("fe-a")
        self.here_executor("fe-b", cwd=path)
        self.assertIn("taken by fe-b", self.ev("session.wake_requested", "fe-a", **OWNER) or "")

    def test_a_sleeping_here_executor_is_not_woken_while_another_session_works_in_its_folder(self):
        self.here_executor()
        self.retire("fe-here")
        self.here_executor("fe-here-2")                       # the copy is free: a sleeping one does not hold the slot
        why = self.ev("session.wake_requested", "fe-here", **by("hub"))
        self.assertIn("taken by fe-here-2", why or "")
        why = self.ev("message.received", None, to="fe-here", text="finish it", wake=True, **OWNER)
        self.assertIn("taken by fe-here-2", why or "")
        self.assertEqual(self.st["sessions"]["fe-here"]["state"], "retired")
        self.retire("fe-here-2")
        self.assertIsNone(self.ev("session.wake_requested", "fe-here", **by("hub")))

    def reported(self, name="frontend-ci"):
        self.run_task(name)
        self.start(name)
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == name]
        self.ev("session.started", name)
        self.ev("turn.started", name, marker=d["message"], prompt_head="task", ts=2000)
        self.ev("turn.result", name, text="Done, MR !12", ts=2001)
        self.ev("turn.stopped", name, ts=2001)
        return d

    def test_an_executor_parked_before_it_retired_is_removed_by_the_term_too(self):
        # an executor reported, and 15 s later a hub restart parked it (session.parked, why restart) before the core
        # retired it (RETIRE_AFTER): with no "retired" mark the remove_after_hours term never removed it, so done
        # executors piled up under their raiser for days
        self.reported()
        self.ev("session.parked", "frontend-ci", channel="core", why="restart", ts=2016)
        s = self.st["sessions"]["frontend-ci"]
        self.assertEqual(s["state"], "parked")
        self.assertEqual(executors.retiring(self.st, 2016 + 1799, keep=1800), [])                    # too early
        self.assertEqual(executors.retiring(self.st, 2016 + 1800, keep=1800), [("remove", "frontend-ci")])
        self.assertEqual(executors.retiring(self.st, 10 ** 8), [])                                   # term off
        s["owner_talks"] = 2010   # the owner's conversation partner (reducer: his words reached the executor) — a parked one does not leave either
        self.assertEqual(executors.retiring(self.st, 10 ** 8, keep=1800), [])

    def test_a_parked_executor_that_was_woken_or_has_open_work_stays(self):
        d = self.reported()
        self.ev("session.parked", "frontend-ci", channel="core", why="restart", ts=2016)
        self.ev("message.received", None, to="frontend-ci", text="also fix the README", ts=2100, **by("frontend"))
        mid = f"m{self.seq}"
        self.assertEqual(executors.retiring(self.st, 10 ** 8, keep=1800), [])                        # message waits
        self.ev("session.started", "frontend-ci", ts=2101)                                          # woken
        self.assertFalse(self.st["sessions"]["frontend-ci"].get("parked"))
        self.assertEqual(executors.retiring(self.st, 10 ** 8, keep=1800), [])
        self.ev("turn.started", "frontend-ci", marker=mid, ts=2102)
        self.ev("turn.result", "frontend-ci", text="README fixed", ts=2103)
        self.ev("turn.stopped", "frontend-ci", ts=2103)
        self.assertIsNone(self.ev("session.parked", "frontend-ci", channel="core", why="restart", ts=2104))
        task = self.st["delegations"][d["id"]]
        state, task["state"] = task["state"], "working"                                              # task open
        self.assertEqual(executors.retiring(self.st, 10 ** 8, keep=1800), [])
        task["state"] = state
        self.assertEqual(executors.retiring(self.st, 2104 + 1800, keep=1800), [("remove", "frontend-ci")])


class Worktree(unittest.TestCase):
    def test_an_executor_gets_its_own_fresh_worktree(self):
        from hosts import worktree
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="exwt-"))
        repo = os.path.join(tmp, "repo")
        git(tmp, "init", "-q", repo)
        git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
        cwd, info, err = worktree.prepare(repo, "frontend-ci")
        self.assertIsNone(err)
        self.assertEqual(git(cwd, "branch", "--show-current").strip(), "task/frontend-ci")
        self.assertTrue(all(ok for _, ok in info["checks"]), info["checks"])


if __name__ == "__main__":
    unittest.main()
