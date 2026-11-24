"""A turn's result reaches whoever assigned the task exactly once (PR #49, analysis of the journal).

A repeat turn of an executor (`hub send`, `hub session wake "…"`) must reach the assigner, but not twice: the core does
not forward the result if the session itself wrote to the assigner in the same turn (`hub send`); the task by `--about`
is the one that was asked about, not the latest; a turn started by someone else's message or by the owner's word is not
a continuation; the continuation after «Connected» is not eaten by the late result of an interrupted turn. The
scenarios come from the journal analysis."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import subprocess
import sys
import unittest

from core import executors, reducer

OWNER = {"channel": "cli", "owner": True}
ONBOARD_LINE = "I can make this a direction"


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


class Base(unittest.TestCase):
    hours = 0   # how far the clock has moved on

    def setUp(self):
        self.st, self.seq, self.log = reducer.empty(), 0, []   # log: the events folded, for a replay elsewhere
        self.ev("catalog.loaded", None, tools={"jira": {"kind": "mcp", "risk": [], "server": {}}})
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.started", "hub", session_id="s-hub")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.log.append({"seq": self.seq, "ts": 1000 + self.seq + self.hours * 3600, "type": type, "entity": entity,
                         "data": data})
        self.st, _, why = reducer.reduce(self.st, self.log[-1])
        return why

    def agent(self, name):
        self.ev("session.requested", name, cwd=f"/{name}", session_id=f"s-{name}", role="agent", **OWNER)
        self.ev("session.started", name, session_id=f"s-{name}")

    def task(self, to, setter, text):
        """hub task new <to> «text» from setter (CLI: for_session = the assigning session itself)."""
        self.assertIsNone(self.ev("delegation.created", None, session=to, for_session=setter, context=text, **by(setter)))
        return f"d{self.seq}"

    def send(self, to, text, sender, **extra):
        self.assertIsNone(self.ev("message.received", None, to=to, text=text, **by(sender), **extra))
        return f"m{self.seq}"

    def turn(self, who, marker, result, own_to=None, own_text=None):
        """A turn of session who: by the marker, during the turn — its own `hub send own_to` (if set), then the result."""
        self.ev("turn.started", who, marker=marker, prompt_head="x")
        if own_to:
            self.send(own_to, own_text or result, who)
        self.ev("turn.result", who, text=result)
        self.ev("turn.stopped", who)

    # --- what was delivered ---
    def to(self, setter):
        return [m for m in self.st["messages"].values() if m["session"] == setter]

    def deliveries(self, setter, text):
        """How many times the result text reached setter: the core's relay («answer to the task») + its own `hub send`."""
        relayed = [m for m in self.to(setter) if "reply to task" in (m.get("origin") or "") and text in m["text"]]
        own = [m for m in self.to(setter) if m.get("sender") and text in m["text"]]
        return len(relayed) + len(own)

    def notes(self, setter_hub, text):
        """REPLIED lines from the core to the hub with this result."""
        return len([m for m in self.to(setter_hub) if m["text"].startswith("REPLIED") and text in m["text"]])


class HubExecutor(Base):
    """A hub executor (`hub task run ex …`), the first task answered."""

    def setUp(self):
        super().setUp()
        self.ev("task.run_requested", "ex", task="check the webhooks", **by("hub"))
        for type, entity, data in executors.events_for(self.st, "ex", self.st["runs"]["ex"], cwd="/w", worktree=None):
            self.ev(type, entity, **data)
        self.ev("session.started", "ex", session_id=self.st["sessions"]["ex"]["session_id"])
        (self.d,) = self.st["delegations"].values()
        self.turn("ex", self.d["message"], "first result")
        self.assertEqual(self.deliveries("hub", "first result"), 1)

    def retire(self):
        self.ev("session.close_requested", "ex", retire=True)
        self.ev("session.exited", "ex", why="sleep")

    # --- what the PR fixes: the result of a repeat turn is delivered (on main — 0) ---
    def test_followup_by_setter_send_is_delivered_once(self):
        """The hub wrote to the executor once more, and it answered with the turn's result."""
        self.turn("ex", self.send("ex", "and now GitLab", "hub"), "result about GitLab")
        self.assertEqual(self.deliveries("hub", "result about GitLab"), 1)
        self.assertLessEqual(self.notes("hub", "result about GitLab"), 1)

    def test_followup_after_setter_wake_is_delivered_once(self):
        """`hub session wake ex "…"` from the hub."""
        self.retire()
        self.turn("ex", self.send("ex", "finish GitLab", "hub", wake=True), "after waking")
        self.assertEqual(self.deliveries("hub", "after waking"), 1)

    def test_two_followups_in_a_row_each_once(self):
        self.turn("ex", self.send("ex", "one", "hub"), "result-1")
        self.turn("ex", self.send("ex", "two", "hub"), "result-2")
        self.assertEqual((self.deliveries("hub", "result-1"), self.deliveries("hub", "result-2")), (1, 1))
        self.assertEqual(self.deliveries("hub", "first result"), 1)       # the first was not relayed again

    def test_stop_from_setter_answered_once(self):
        self.turn("ex", self.send("ex", "stop, do not merge", "hub"), "stopped, did not merge")
        self.assertEqual(self.deliveries("hub", "stopped"), 1)

    # --- duplicates: pass on main, not with the PR ---
    def test_followup_with_own_send_is_not_delivered_twice(self):
        """It wrote to the hub with `hub send` itself and ended the turn with the same result — the hub gets it once."""
        self.turn("ex", self.send("ex", "what about the MR?", "hub"), "MR is open, the frontend review is pending", own_to="hub")
        self.assertEqual(self.deliveries("hub", "MR is open"), 1)

    def test_onboarding_line_is_not_repeated_on_every_followup(self):
        """ONBOARD («can make this a direction») is appended to every result of a hub executor — and to a continuation."""
        self.turn("ex", self.send("ex", "one", "hub"), "result-1")
        self.turn("ex", self.send("ex", "two", "hub"), "result-2")
        relays = [m for m in self.to("hub") if "reply to task" in (m.get("origin") or "")]
        self.assertLessEqual(len([m for m in relays if ONBOARD_LINE in m["text"]]), 1)

    def test_tool_connected_mid_followup_keeps_the_continuation(self):
        """A turn on the hub's message, a tool was connected mid-turn, the turn was cut off, its late result closed the task —
        the result of the continuation after «Connected» still arrives."""
        mid = self.send("ex", "create a form in the admin", "hub")
        self.ev("turn.started", "ex", marker=mid, prompt_head="x")
        self.ev("tools.requested", "ex", tool="jira", why="form", **by("ex"))
        self.ev("tools.granted", "ex", tool="jira", **by("hub"))
        self.ev("turn.stopped", "ex")                                   # restart with the tool: «Connected…»
        self.ev("turn.result", "ex", text="waiting for the tool to connect")  # the late result of the interrupted turn
        cont = next(m["id"] for m in self.st["messages"].values()
                    if m["session"] == "ex" and m["text"].startswith("Connected"))
        self.turn("ex", cont, "the form is created")
        self.assertEqual(self.deliveries("hub", "the form is created"), 1)

    # --- not a continuation: neither on main nor with the PR ---
    def test_owner_wake_is_not_relayed_to_the_setter(self):
        self.retire()
        self.ev("message.received", None, to="ex", text="look at the log again", wake=True, **OWNER)
        self.turn("ex", f"m{self.seq}", "the log is clean")
        self.assertEqual(self.deliveries("hub", "the log is clean"), 0)

    def test_message_from_a_stranger_is_not_the_task(self):
        self.agent("other")
        self.turn("ex", self.send("ex", "hello", "other"), "a reply to an outsider", own_to="other")
        self.assertEqual(self.deliveries("hub", "a reply to an outsider"), 0)
        self.assertEqual(self.deliveries("other", "a reply to an outsider"), 1)

    # --- an answer to the executor's own question: the final report went nowhere ---
    def ask_during_task(self, whom, fyi=False):
        """A round of the task in which the executor asks `whom` with `hub send` and ends the turn waiting."""
        mid = self.send("ex", "collect the release status", "hub")
        self.ev("turn.started", "ex", marker=mid, prompt_head="x")
        self.send(whom, "what is the landing status?", "ex", **({"fyi": True} if fyi else {}))
        self.ev("turn.result", "ex", text="waiting for the landing status")
        self.ev("turn.stopped", "ex")

    def test_answer_to_its_own_question_continues_the_task(self):
        """It asked a neighbour mid-task; the turn on the neighbour's answer is the task's: its result reaches the hub."""
        self.agent("other")
        self.ask_during_task("other")
        self.turn("ex", self.send("ex", "landing 70%", "other"), "release status: landing 70%")
        self.assertEqual(self.deliveries("hub", "release status: landing 70%"), 1)
        self.assertEqual(self.deliveries("other", "release status: landing 70%"), 0)

    def test_a_second_unasked_letter_from_the_neighbour_is_not_the_task(self):
        """One question, one answer: the neighbour's next letter is its own matter again."""
        self.agent("other")
        self.ask_during_task("other")
        self.turn("ex", self.send("ex", "landing 70%", "other"), "release status: landing 70%")
        self.turn("ex", self.send("ex", "by the way, unrelated", "other"), "an unrelated reply", own_to="other")
        self.assertEqual(self.deliveries("hub", "an unrelated reply"), 0)

    def test_an_fyi_is_not_a_question(self):
        """An FYI to a neighbour asks nothing, also one the core delivered as an ordinary message (the neighbour waits
        for the owner): its later letter is its own matter."""
        self.agent("other")
        self.ev("watch.needs_owner", "other", p=1.0, text="please log in", **OWNER)
        self.ask_during_task("other", fyi=True)
        self.assertTrue(self.last_from("ex")["fyi_woke"])
        self.turn("ex", self.send("ex", "a note of my own", "other"), "a reply to the note", own_to="other")
        self.assertEqual(self.deliveries("hub", "a reply to the note"), 0)

    def test_an_answer_relayed_as_the_neighbours_turn_result_continues_the_task(self):
        """The neighbour answered in its turn's text, and the core relayed it to the executor that waits (`hub
        waiting`): a message with no sender field, the neighbour named in `from`."""
        self.agent("other")
        self.ask_during_task("other")
        self.ev("session.waiting", "ex", text="the landing status from other", **by("ex"))
        self.turn("other", self.last_from("ex")["id"], "the landing is at 70 percent, the rest by Friday")
        relayed = self.last_from("other", relay=True)
        self.assertNotIn("sender", relayed)
        self.turn("ex", relayed["id"], "release status: landing 70%")
        self.assertEqual(self.deliveries("hub", "release status: landing 70%"), 1)

    def test_an_answer_read_along_with_another_line_continues_the_task(self):
        """The answer came while another letter was queued, and the turn took both (`also`)."""
        self.agent("other")
        self.agent("third")
        self.ask_during_task("other")
        first, answer = self.send("ex", "an unrelated hello", "third"), self.send("ex", "landing 70%", "other")
        self.ev("turn.started", "ex", marker=first, also=[answer], prompt_head="x")
        self.ev("turn.result", "ex", text="release status: landing 70%")
        self.ev("turn.stopped", "ex")
        self.assertEqual(self.deliveries("hub", "release status: landing 70%"), 1)

    def test_an_answer_after_an_acknowledgement_continues_the_task(self):
        """The neighbour first acknowledged ("accepted, will send"); the executor said it still waits (`hub waiting`),
        so the real answer that came later continues the task too."""
        self.agent("other")
        self.ask_during_task("other")
        self.ev("turn.started", "ex", marker=self.send("ex", "accepted, will send it in an hour", "other"), prompt_head="x")
        self.ev("session.waiting", "ex", text="the landing status from other", **by("ex"))
        self.ev("turn.result", "ex", text="other will send the landing status in an hour")
        self.ev("turn.stopped", "ex")
        self.hours = 2
        self.turn("ex", self.send("ex", "landing 70%", "other"), "release status: landing 70%")
        self.assertEqual(self.deliveries("hub", "release status: landing 70%"), 1)

    def test_waiting_for_something_else_does_not_reopen_the_question(self):
        """The turn read the neighbour's letter, and the executor says it waits for something else (`hub waiting` that
        does not name the neighbour): the letter was the answer, the neighbour's next one is its own matter."""
        self.agent("other")
        self.ask_during_task("other")
        self.ev("turn.started", "ex", marker=self.send("ex", "landing 70%", "other"), prompt_head="x")
        self.ev("session.waiting", "ex", text="the CI run on the release branch", **by("ex"))
        self.ev("turn.result", "ex", text="release status: landing 70%, the CI run is pending")
        self.ev("turn.stopped", "ex")
        self.turn("ex", self.send("ex", "by the way, unrelated", "other"), "an unrelated reply", own_to="other")
        self.assertEqual(self.deliveries("hub", "an unrelated reply"), 0)

    def test_two_acknowledgements_in_one_turn_keep_both_questions_open(self):
        """It asked two neighbours; both acknowledged, one turn read both letters (`also`) and the executor says it
        waits for both: each one's real answer later continues the task."""
        self.agent("other")
        self.agent("third")
        mid = self.send("ex", "collect the release status", "hub")
        self.ev("turn.started", "ex", marker=mid, prompt_head="x")
        self.send("other", "what is the landing status?", "ex")
        self.send("third", "what is the docs status?", "ex")
        self.ev("turn.result", "ex", text="waiting for the landing and the docs status")
        self.ev("turn.stopped", "ex")
        one, two = self.send("ex", "accepted, will send", "other"), self.send("ex", "accepted, will send", "third")
        self.ev("turn.started", "ex", marker=one, also=[two], prompt_head="x")
        self.ev("session.waiting", "ex", text="the status from other and third", **by("ex"))
        self.ev("turn.result", "ex", text="other and third will send their status")
        self.ev("turn.stopped", "ex")
        self.hours = 2
        self.turn("ex", self.send("ex", "landing 70%", "other"), "release status: landing 70%")
        self.turn("ex", self.send("ex", "the docs are done", "third"), "release status: the docs are done")
        self.assertEqual((self.deliveries("hub", "landing 70%"), self.deliveries("hub", "the docs are done")), (1, 1))

    def test_a_letter_a_day_after_an_unanswered_question_is_not_the_task(self):
        """The question was never answered; a letter from that session more than a day later is its own matter."""
        self.agent("other")
        self.ask_during_task("other")
        self.hours = 25
        self.turn("ex", self.send("ex", "by the way, unrelated", "other"), "an unrelated reply", own_to="other")
        self.assertEqual(self.deliveries("hub", "an unrelated reply"), 0)

    def test_an_urgent_task_meanwhile_keeps_the_question_of_the_open_one(self):
        """It asked a neighbour; meanwhile the hub set it an urgent task, and it handled it. The first task is still
        open: the neighbour's answer continues it."""
        self.agent("other")
        self.ask_during_task("other")
        urgent = self.task("ex", "hub", "check the certificate now")
        self.turn("ex", self.st["delegations"][urgent]["message"], "the certificate is fine")
        self.turn("ex", self.send("ex", "landing 70%", "other"), "release status: landing 70%")
        self.assertEqual(self.deliveries("hub", "release status: landing 70%"), 1)

    def test_a_closed_task_forgets_its_questions(self):
        """The hub closed the task (`hub task done`): its open question goes with it, and the neighbour's letter is
        its own matter."""
        self.agent("other")
        self.ask_during_task("other")
        self.ev("delegation.done", self.d["id"], **by("hub"))
        self.assertNotIn("questions", self.st["sessions"]["ex"])
        self.turn("ex", self.send("ex", "landing 70%", "other"), "an answer after the close", own_to="other")
        self.assertEqual(self.deliveries("hub", "an answer after the close"), 0)

    def test_an_executor_that_leaves_forgets_its_questions(self):
        """The executor handed in and was put away: its open questions do not stay in the state forever."""
        self.agent("other")
        self.ask_during_task("other")
        self.retire()
        self.assertNotIn("questions", self.st["sessions"]["ex"])

    def test_a_new_turn_forgets_whose_answer_the_last_one_read(self):
        """The turn that read the neighbour's answer ended; in a later turn the executor says it waits for that
        neighbour again: the answer was given, nothing reopens, and the neighbour's next letter is its own matter."""
        self.agent("other")
        self.agent("third")
        self.ask_during_task("other")
        self.turn("ex", self.send("ex", "landing 70%", "other"), "release status: landing 70%")
        self.ev("turn.started", "ex", marker=self.send("ex", "an unrelated hello", "third"), prompt_head="x")
        self.ev("session.waiting", "ex", text="a reply from other about the docs", **by("ex"))
        self.ev("turn.result", "ex", text="hello to you too")
        self.ev("turn.stopped", "ex")
        self.turn("ex", self.send("ex", "by the way, unrelated", "other"), "an unrelated reply", own_to="other")
        self.assertEqual(self.deliveries("hub", "an unrelated reply"), 0)

    def test_an_answer_sent_as_an_fyi_is_the_answer(self):
        """The neighbour answered with --fyi: it rode with the next line the executor read, and that turn is the
        task's. The neighbour's next letter is its own matter again."""
        self.agent("other")
        self.agent("third")
        self.ask_during_task("other")
        self.send("ex", "landing 70%", "other", fyi=True)
        self.turn("ex", self.send("ex", "an unrelated hello", "third"), "release status: landing 70%")
        self.assertEqual(self.deliveries("hub", "release status: landing 70%"), 1)
        self.turn("ex", self.send("ex", "by the way, unrelated", "other"), "an unrelated reply", own_to="other")
        self.assertEqual(self.deliveries("hub", "an unrelated reply"), 0)

    # --- the asked neighbour waits on the executor itself: its letter is a question to it, and the turn answers it ---
    def got(self, who, text):
        """How many messages with this text reached `who`: its own `hub send`, a task reply, a relayed turn result."""
        return len([m for m in self.to(who) if text in m["text"]])

    def test_a_counter_question_of_the_asked_neighbour_is_answered_to_it(self):
        """It asked a neighbour; the neighbour asked back and waits (`hub waiting`, said after the turn began): the turn
        answers the neighbour, as on main, and the hub gets nothing from it. The question stays open: the neighbour's
        real answer continues the task."""
        self.agent("other")
        self.ask_during_task("other")
        back = self.send("ex", "which landing, the English or the Russian one?", "other")
        self.ev("turn.started", "ex", marker=back, prompt_head="x")
        self.ev("session.waiting", "other", text="which landing ex means", **by("other"))
        self.ev("turn.result", "ex", text="the English landing, the one in the release")
        self.ev("turn.stopped", "ex")
        self.assertEqual(self.got("other", "the English landing"), 1)
        self.assertEqual(self.got("hub", "the English landing"), 0)
        self.turn("ex", self.send("ex", "the English landing is at 70%", "other"), "release status: landing 70%")
        self.assertEqual(self.deliveries("hub", "release status: landing 70%"), 1)
        self.assertEqual(self.got("other", "release status"), 0)

    def test_an_answer_with_a_question_of_its_own_is_answered_to_the_neighbour(self):
        """The neighbour answered and asked back in the same letter, and waits: a letter whose sender waits on the
        executor is a question to it, so the turn answers the neighbour; the hub gets nothing from it."""
        self.agent("other")
        self.ask_during_task("other")
        both = self.send("ex", "landing 70%; should I check the Russian one too?", "other")
        self.ev("session.waiting", "other", text="whether ex needs the Russian landing", **by("other"))
        self.turn("ex", both, "yes, the Russian landing too, please")
        self.assertEqual(self.got("other", "the Russian landing too"), 1)
        self.assertEqual(self.got("hub", "the Russian landing too"), 0)

    def test_an_answer_to_the_neighbours_question_is_no_question_of_the_executor(self):
        """The neighbour asked the executor during the task and got the answer by `hub send`: that letter answers, it
        asks nothing. The neighbour's next question, with it waiting, is answered to it, as on main."""
        self.agent("other")
        task, question = self.send("ex", "collect the release status", "hub"), self.send("ex", "when is the release?", "other")
        self.ev("turn.started", "ex", marker=task, also=[question], prompt_head="x")
        self.send("other", "the release is on Friday", "ex")
        self.ev("turn.result", "ex", text="the release status is being collected")
        self.ev("turn.stopped", "ex")
        self.assertNotIn("questions", self.st["sessions"]["ex"])
        again = self.send("ex", "and which hour on Friday?", "other")
        self.ev("session.waiting", "other", text="the release hour from ex", **by("other"))
        self.turn("ex", again, "at noon, after the freeze ends")
        self.assertEqual(self.got("other", "at noon"), 1)
        self.assertEqual(self.got("hub", "at noon"), 0)

    def two_letters(self, line, third_asked=True, result="the EU landing; and thanks for the docs"):
        """It asked `other` (and `third`) mid-task; the turn reads other's counter-question (other waits, `hub waiting`)
        and third's letter: the turn's own line is `line`'s, the other one is read with it (`also`). Returns
        (to the hub, to other) of the turn's result."""
        self.agent("other")
        self.agent("third")
        mid = self.send("ex", "collect the release status", "hub")
        self.ev("turn.started", "ex", marker=mid, prompt_head="x")
        for whom in ("other", "third") if third_asked else ("third",):
            self.send(whom, "what is the landing status?", "ex")
        self.ev("turn.result", "ex", text="waiting for the landing status")
        self.ev("turn.stopped", "ex")
        back, answer = self.send("ex", "which landing, EU or US?", "other"), self.send("ex", "the docs are done", "third")
        self.ev("session.waiting", "other", text="which landing ex means", **by("other"))
        marker, also = (back, answer) if line == "other" else (answer, back)
        self.ev("turn.started", "ex", marker=marker, also=[also], prompt_head="x")
        self.ev("turn.result", "ex", text=result)
        self.ev("turn.stopped", "ex")
        return self.got("hub", result), self.got("other", result)

    def test_a_waiting_neighbours_letter_read_with_another_answer_is_answered_to_it(self):
        """The waiting neighbour's counter-question is the turn's line, a third session's answer is read with it: the
        turn answers the neighbour, as on main, whichever of the two the core looks at first (a set's order gave the
        hub the result under some hash seeds). The third's answer counts for nothing then: its question is open."""
        self.assertEqual(self.two_letters("other"), (0, 1))
        self.assertEqual(set(self.st["sessions"]["ex"]["questions"]), {"other", "third"})

    def test_a_waiting_neighbours_letter_read_along_with_the_answer_does_not_take_the_turn(self):
        """A guard, the same before the fix: the third's answer is the turn's line and the waiting neighbour's letter is
        read with it. A letter read along is no turn's head on main either (relay_targets answers the head and mid-turn
        letters): the turn is the task's, its result reaches the hub."""
        self.assertEqual(self.two_letters("third"), (1, 0))

    def test_a_waiting_session_it_did_not_ask_keeps_the_turn_read_with_an_answer(self):
        """The turn's line is the letter of a session it did not ask, which waits on it; an answer is read with it: the
        turn answers that session, as on main, not the task."""
        self.assertEqual(self.two_letters("other", third_asked=False), (0, 1))

    def test_a_short_answer_to_a_waiting_neighbour_leaves_the_turn_to_the_task(self):
        """The turn answers the waiting neighbour only when the core would pass its result on (relay_targets): a short
        one ("the EU one") it does not, so the turn stays the task's and its result reaches the hub, not nobody."""
        self.assertEqual(self.two_letters("other", result="the EU one"), (1, 0))

    def test_a_waiting_session_whose_letter_came_mid_turn_is_answered(self):
        """The turn on the neighbour's answer is the task's; mid-turn another session's letter is inserted, and it
        waits: the turn answers that session, as on main (relay_targets answers letters inserted mid-turn too)."""
        self.agent("other")
        self.agent("third")
        self.ask_during_task("other")
        self.ev("turn.started", "ex", marker=self.send("ex", "landing 70%", "other"), prompt_head="x")
        inserted = self.send("ex", "is the certificate renewed?", "third")
        self.assertIsNone(self.ev("message.inject", inserted, **OWNER))
        self.ev("turn.started", "ex", marker=inserted, prompt_head="x")
        self.ev("session.waiting", "third", text="the certificate from ex", **by("third"))
        self.ev("turn.result", "ex", text="landing 70%; the certificate was renewed yesterday")
        self.ev("turn.stopped", "ex")
        self.assertEqual((self.got("hub", "the certificate was"), self.got("third", "the certificate was")), (0, 1))

    def test_a_failed_turn_on_the_answer_leaves_the_question_open(self):
        """The turn on the neighbour's counter-question fails (an API error; a hook refuses the prompt, and the letter
        goes back to its sender): it read no answer, so the question stays open, and the neighbour's real answer, after
        the executor answers it, continues the task."""
        for data in ({"text": "API Error: the request failed after retries", "is_error": True},
                     {"text": "UserPromptSubmit operation blocked by hook: the repo hook refused it", "turns": 0}):
            with self.subTest(data["text"][:9]):
                self.setUp()
                self.agent("other")
                self.ask_during_task("other")
                self.ev("turn.started", "ex", marker=self.send("ex", "which landing, EU or US?", "other"), prompt_head="x")
                self.ev("session.waiting", "other", text="which landing ex means", **by("other"))
                self.ev("turn.result", "ex", **data)
                self.ev("turn.stopped", "ex")
                self.assertEqual(set(self.st["sessions"]["ex"].get("questions") or ()), {"other"})
                self.send("other", "the EU landing, please", "ex")   # it answers the counter-question later
                self.turn("ex", self.send("ex", "the EU landing is at 70%", "other"), "release status: EU landing 70%")
                self.assertEqual(self.deliveries("hub", "release status: EU landing 70%"), 1)

    def test_still_waiting_said_in_the_turn_the_neighbour_takes_keeps_its_time(self):
        """The neighbour asks back a day after the question, the executor answers it and says it still waits for the
        neighbour's answer (`hub waiting`): the question counts its day from then, not from the first asking, though
        the turn goes to the neighbour."""
        self.agent("other")
        self.ask_during_task("other")
        self.hours = 20
        back = self.send("ex", "which landing, EU or US?", "other")
        self.ev("session.waiting", "other", text="which landing ex means", **by("other"))
        self.ev("turn.started", "ex", marker=back, prompt_head="x")
        self.ev("session.waiting", "ex", text="the landing status from other", **by("ex"))
        self.ev("turn.result", "ex", text="the EU landing, the one in the release")
        self.ev("turn.stopped", "ex")
        self.assertEqual(self.got("other", "the EU landing, the one"), 1)
        self.hours = 30
        self.turn("ex", self.send("ex", "the EU landing is at 70%", "other"), "release status: EU landing 70%")
        self.assertEqual(self.deliveries("hub", "release status: EU landing 70%"), 1)

    def answers_of_two_tasks(self):
        """It asked `other` during its task and `third` during an urgent one set meanwhile; one turn reads both answers,
        other's as its line. Returns (the task the turn continues, other's task)."""
        self.agent("other")
        self.agent("third")
        self.ask_during_task("other")
        first = self.st["sessions"]["ex"]["questions"]["other"]["task"]
        urgent = self.task("ex", "hub", "check the certificate now")
        self.ev("turn.started", "ex", marker=self.st["delegations"][urgent]["message"], prompt_head="x")
        self.send("third", "is the certificate renewed?", "ex")
        self.ev("turn.result", "ex", text="waiting for the certificate")
        self.ev("turn.stopped", "ex")
        line, also = self.send("ex", "landing 70%", "other"), self.send("ex", "renewed yesterday", "third")
        self.ev("turn.started", "ex", marker=line, also=[also], prompt_head="x")
        took = self.st["sessions"]["ex"].get("delegation")
        self.ev("turn.result", "ex", text="both answers in: landing 70%, certificate fine")
        self.ev("turn.stopped", "ex")
        return took, first

    def test_answers_of_two_tasks_in_one_turn_continue_the_one_of_its_line(self):
        """Answers to questions of two tasks read in one turn: the turn continues the task of its own line, not the
        one a set's order happens to give first."""
        took, first = self.answers_of_two_tasks()
        self.assertEqual(took, first)

    def test_a_turn_with_two_letters_folds_the_same_under_any_hash_seed(self):
        """The cases above, folded in a fresh process under 12 hash seeds: who gets the result does not depend on the
        order of a set (a live core and the replay of its journal after a restart must agree). The child imports
        only the reducer: `import tests` would clean the parent run's directories."""
        def whom(st, text):
            return sorted([m["session"], m.get("origin") or ""] for m in st["messages"].values() if text in m["text"])
        cases = []
        for line, asked in (("other", True), ("third", True), ("other", False)):
            self.setUp()
            self.two_letters(line, third_asked=asked)
            cases.append((self.log, "the EU landing", whom(self.st, "the EU landing")))
        self.setUp()
        self.answers_of_two_tasks()
        cases.append((self.log, "both answers in", whom(self.st, "both answers in")))
        code = ("import json, sys\n"
                "from core import reducer\n"
                "got = []\n"
                "for evs, text in json.load(sys.stdin):\n"
                "    st = reducer.empty()\n"
                "    for e in evs:\n"
                "        st = reducer.reduce(st, e)[0]\n"
                "    got.append(sorted([m['session'], m.get('origin') or ''] for m in st['messages'].values()\n"
                "                      if text in m['text']))\n"
                "print(json.dumps(got))\n")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for seed in map(str, range(12)):
            r = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, encoding="utf-8",
                               input=json.dumps([[evs, text] for evs, text, _ in cases]),
                               env={**os.environ, "PYTHONHASHSEED": seed})
            self.assertEqual(r.returncode, 0, f"seed {seed}: {r.stderr}")
            self.assertEqual(json.loads(r.stdout), [want for *_, want in cases], f"seed {seed}")

    def last_from(self, who, relay=False):
        """The latest message of `who`: its own `hub send`, or (relay) its turn result the core passed on."""
        def of(m):
            return m.get("relay") and (m.get("from") or {}).get("name") == who if relay else m.get("sender") == who
        return max(filter(of, self.st["messages"].values()), key=lambda m: int(m["id"][1:].split("x")[0]))

    def test_handoff_done_is_not_also_an_answer(self):
        self.turn("ex", self.send("ex", "one", "hub"), "result-1")
        self.ev("session.close_requested", "ex", **by("hub"))
        ask = next((m["id"] for m in self.st["messages"].values()
                    if m["session"] == "ex" and m["text"].startswith("HANDOFF-REQUEST")), None)
        if ask is None:
            self.skipTest("the executor was put to sleep without a handoff")
        self.turn("ex", ask, "HANDOFF-DONE\nDone: webhooks and GitLab")
        self.assertEqual(len([m for m in self.to("hub") if "Done: webhooks and GitLab" in m["text"]]), 1)


class AgentExecutor(Base):
    """An executor of a specialist (`hub task run` from an agent), asleep after handing in: the specialist itself wakes
    it for the next round (a merge request review takes days) and the result of that round goes back to the specialist."""

    def setUp(self):
        super().setUp()
        self.agent("qa")
        self.ev("turn.started", "qa", marker=None, prompt_head="x")
        self.ev("task.run_requested", "ex", task="a run on stage", **by("qa"))
        for type, entity, data in executors.events_for(self.st, "ex", self.st["runs"]["ex"], cwd="/w", worktree=None):
            self.ev(type, entity, **data)
        self.ev("turn.stopped", "qa")
        self.ev("session.started", "ex", session_id=self.st["sessions"]["ex"]["session_id"])
        (self.d,) = self.st["delegations"].values()
        self.assertEqual(self.st["sessions"]["ex"]["raised_by"], "qa")
        self.turn("ex", self.d["message"], "the run is going")
        self.ev("session.close_requested", "ex", retire=True)
        self.ev("session.exited", "ex", why="sleep")

    def test_the_raiser_wakes_him_and_the_next_round_result_goes_back_to_it(self):
        self.turn("ex", self.send("ex", "the reviewer replied, fix it", "qa", wake=True), "fixed, waiting for the review")
        self.assertEqual(self.deliveries("qa", "fixed, waiting for the review"), 1)
        self.assertEqual(self.deliveries("hub", "fixed, waiting for the review"), 0)


class Specialist(Base):
    """A long-lived specialist with an old task from the hub (`hub task new dev`, for_session=hub): with the PR every new
    hub message is a «continuation» of that task (journal analysis: otherwise every hub message is a «continuation» of the old task)."""

    def setUp(self):
        super().setUp()
        self.agent("dev")
        self.d1 = self.task("dev", "hub", "fix the feed")
        self.turn("dev", self.st["delegations"][self.d1]["message"], "the feed is fixed")

    def test_new_request_answered_by_own_send_once(self):
        """The specialist answered the hub itself with `hub send` — the turn's result is not relayed in addition."""
        self.turn("dev", self.send("dev", "look at the voice bug", "hub"), "voice: the cause is the codec", own_to="hub")
        self.assertEqual(self.deliveries("hub", "voice: the cause is the codec"), 1)

    def test_about_names_the_task_it_continues(self):
        """`hub send dev --about d1 …` with two open tasks from the hub: the PR takes the latest (d2), not the named one."""
        d2 = self.task("dev", "hub", "update the README")
        self.turn("dev", self.st["delegations"][d2]["message"], "README updated")
        self.turn("dev", self.send("dev", "about the feed: and mobile?", "hub", about=self.d1), "mobile too")
        wrong = [m for m in self.to("hub") if "mobile too" in m["text"] and d2 in (m.get("origin") or "") + m["text"]]
        self.assertEqual(wrong, [])


    def test_a_letter_from_a_session_it_wrote_to_mid_task_is_not_its_task(self):
        """Only an executor's question continues its task: a specialist that wrote to a session during the hub's task
        answers that session's own question to it, and the session waits for the answer (not the hub)."""
        self.agent("other")
        d2 = self.task("dev", "hub", "fix the list")
        self.ev("turn.started", "dev", marker=self.st["delegations"][d2]["message"], prompt_head="x")
        self.send("other", "take the list styles from here", "dev")
        self.ev("turn.result", "dev", text="the list is fixed, styles passed to other")
        self.ev("turn.stopped", "dev")
        question = self.send("dev", "which radius do the rows use?", "other")
        self.ev("session.waiting", "other", text="the radius from dev", **by("other"))
        self.turn("dev", question, "the rows use a radius of 8 px, as the cards")
        self.assertEqual(len([m for m in self.to("other") if "a radius of 8 px" in m["text"]]), 1)
        self.assertEqual(self.deliveries("hub", "a radius of 8 px"), 0)


class Peers(Base):
    """A session assigned a task to a neighbour (`hub task new maps-ops`, for_session=ux): their correspondence continues
    over `hub send` — with the fix applied to any message, every such turn arrived twice."""

    def setUp(self):
        super().setUp()
        self.agent("maps-ops")
        self.agent("ux")
        self.d = self.task("maps-ops", "ux", "deploy the service")
        self.turn("maps-ops", self.st["delegations"][self.d]["message"], "deployed", own_to=None)

    def test_peer_reply_by_own_send_once(self):
        self.turn("maps-ops", self.send("maps-ops", "redeploy on main 1c2101c", "ux"), "redeploy done, healthy",
                  own_to="ux")
        self.assertEqual(self.deliveries("ux", "redeploy done"), 1)

    def test_cancelled_executor_then_specialist_answers_once(self):
        """4bb14dd: a task was given to an executor, cancelled — it returned to the specialist; its result goes to the assigner, once."""
        self.ev("turn.started", "maps-ops", marker=None, prompt_head="long work")
        d = self.task("maps-ops", "ux", "check the certificate")
        msg = self.st["delegations"][d]["message"]
        self.assertIsNone(self.ev("message.routed", d, route="executor", name="certs", **by("hub")))
        run = self.st["messages"][msg]["handover"]
        self.assertIsNone(self.ev("task.run_cancelled", run, **by("hub")))
        self.ev("turn.result", "maps-ops", text="long work")
        self.ev("turn.stopped", "maps-ops")
        self.turn("maps-ops", msg, "the certificate is fine")
        self.assertEqual(self.deliveries("ux", "the certificate is fine"), 1)
        self.turn("maps-ops", self.send("maps-ops", "thanks, and the deadline?", "ux"), "deadline until May", own_to="ux")
        self.assertEqual(self.deliveries("ux", "deadline until May"), 1)


class FirstTurn(Base):
    """Already on main: during a task turn the session writes the result to the assigner itself and ends the turn with it (journal analysis: it happened with every fifth relay)."""

    def test_task_turn_with_own_send_is_delivered_once(self):
        self.agent("dev")
        d = self.task("dev", "hub", "fix the feed")
        self.turn("dev", self.st["delegations"][d]["message"], "the feed is fixed", own_to="hub")
        self.assertEqual(self.deliveries("hub", "the feed is fixed"), 1)


if __name__ == "__main__":
    unittest.main()
