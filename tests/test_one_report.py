"""One task — one message to the owner (an executor did something and wrote, then the specialist writes the same, then
the hub — three identical messages). The result on a task reaches the owner from the one he gave it to (the session of
the assignment on his message; without an assignment, the one he wrote to). An executor on someone else's task reports
to its specialist or the hub — its result goes to them, not to the owner; the hub does not retell a task once it has
been answered. Such results stay in their own session's feed but are not "new" for the owner, not in Telegram and not in
the "you are back" summary. A turn on the owner's own words to this session (his message, an answer to a card, words
passed on with `--for`) is always addressed to him; a task that broke off is spoken about by the hub."""
import tests  # noqa: F401 — do not see the machine's settings
import io
import unittest
from unittest import mock

from core import reducer, views
from tests import test_answers_where_asked as where

OWNER = {"channel": "web", "owner": True}


def by(sender):
    return {"channel": "session", "owner": False, "sender": sender}


class Core(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.requested", "qa", cwd="/w", session_id="s-qa", role="agent", **OWNER)
        self.ev("session.requested", "w", cwd="/w", session_id="s-w", **OWNER)
        for n in ("hub", "qa", "w"):
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        self.assertIsNone(why, f"{type}: {why}")
        return f"m{self.seq}"

    def start(self, name, marker):
        self.ev("turn.started", name, marker=marker, prompt_head="x")

    def end(self, name, text):
        self.ev("turn.result", name, text=text)
        self.ev("turn.stopped", name)

    def to(self, name, **about):
        """The last message of session name (with the fields in about)."""
        return max((m for m in self.st["messages"].values() if m["session"] == name
                    and all(m.get(k) == v for k, v in about.items())), key=lambda m: m["created"])["id"]

    def unread(self):
        r = views.Reader()
        r.state = self.st
        return sorted(s["name"] for s in r.snapshot()["sessions"] if s["unread"])

    def task(self, ask):
        """The owner writes to the hub, the hub gives it to qa; qa raises an executor w; w → qa → owner; the hub learns REPLIED."""
        m = self.ev("message.received", None, text=ask, **OWNER)
        self.start("hub", m)
        self.ev("delegation.created", None, session="qa", **{"for": m}, **by("hub"))
        self.end("hub", "Passed to qa.")
        self.assertEqual(self.unread(), ["hub"])                    # the hub's reply to his message is his
        self.ev("owner.seen", "hub", seq=self.seq, **OWNER)
        self.start("qa", self.to("qa", delegation=self.task_id(m)))
        self.ev("delegation.created", None, session="w", for_session="qa", **by("qa"))
        self.end("qa", "Raised executor w.")
        self.ev("owner.seen", "qa", seq=self.seq, **OWNER)
        return m

    def task_of(self, name):
        return max((d for d in self.st["delegations"].values() if d["session"] == name), key=lambda d: d["created"])["id"]

    def task_id(self, m):
        return next(d["id"] for d in self.st["delegations"].values() if d.get("for") == m)

    def test_the_owner_hears_about_a_task_once_from_the_one_he_gave_it_to(self):
        m = self.task("check task 7")
        self.start("w", self.to("w"))
        self.end("w", "Checked: all green.")
        self.assertEqual(self.unread(), [])                         # the executor reports to its specialist
        self.start("qa", self.to("qa", task=next(d["id"] for d in self.st["delegations"].values()
                                                  if d["session"] == "w")))
        self.end("qa", "Task 7: all green, closing.")
        self.assertEqual(self.unread(), ["qa"])                     # the result comes from the one he gave it to
        self.assertEqual(self.st["delegations"][self.task_id(m)]["state"], "replied")
        self.ev("owner.seen", "qa", seq=self.seq, **OWNER)
        self.start("hub", self.to("hub", task=self.task_id(m)))
        self.end("hub", "qa checked task 7 — all green.")
        self.assertEqual(self.unread(), [])                         # the hub does not retell
        self.assertEqual(self.st["sessions"]["hub"]["merged"], {"seq": self.seq - 1, "into": "qa"})

    def test_the_hubs_turn_on_a_letter_in_the_chain_is_not_a_retelling(self):
        # review of the PR: qa answered the owner's task and in the same turn asked the hub for access; the hub's
        # result on that letter is new to the owner, not a repeat of qa's answer
        m = self.ev("message.received", None, text="check task 7", **OWNER)
        self.start("hub", m)
        self.ev("delegation.created", None, session="qa", **{"for": m}, **by("hub"))
        self.end("hub", "Passed to qa.")
        self.ev("owner.seen", "hub", seq=self.seq, **OWNER)
        self.start("qa", self.to("qa", delegation=self.task_id(m)))
        letter = self.ev("message.received", None, to="hub", text="need access to the stand, ask the owner", **by("qa"))
        self.end("qa", "Task 7: will check when the stand access is given.")
        self.ev("owner.seen", "qa", seq=self.seq, **OWNER)
        self.assertEqual(self.st["delegations"][self.task_id(m)]["state"], "replied")
        self.start("hub", letter)
        self.assertEqual(self.st["sessions"]["hub"]["turn_root"], m)
        self.end("hub", "qa asks for access to the stand — give it?")
        self.assertIn("hub", self.unread())
        self.assertNotIn("merged", self.st["sessions"]["hub"])

    def test_his_own_words_to_a_session_are_always_answered_to_him(self):
        self.task("check task 7")
        self.start("w", self.ev("message.received", None, to="w", text="and the mobile version too", **OWNER))
        self.end("w", "Mobile is green too.")
        self.assertEqual(self.unread(), ["w"])
        self.start("hub", self.ev("message.received", None, to="hub", text="how is it going?", **OWNER))
        self.end("hub", "qa is still checking.")
        self.assertIn("hub", self.unread())

    def test_the_hub_speaks_when_the_task_broke(self):
        m = self.ev("message.received", None, text="check task 7", **OWNER)
        self.start("hub", m)
        self.ev("delegation.created", None, session="qa", **{"for": m}, **by("hub"))
        self.end("hub", "Passed to qa.")
        self.ev("owner.seen", "hub", seq=self.seq, **OWNER)
        self.start("qa", self.to("qa", delegation=self.task_id(m)))
        self.ev("turn.result", "qa", text="API Error 500", is_error=True)
        self.ev("turn.stopped", "qa")
        self.assertEqual(self.st["delegations"][self.task_id(m)]["state"], "stopped")
        self.start("hub", self.to("hub", task=self.task_id(m)))
        self.end("hub", "qa broke off, restarted it.")
        self.assertIn("hub", self.unread())

    def test_an_executor_of_a_specialists_own_work_reports_to_him(self):
        self.start("qa", self.ev("message.received", None, to="qa", text="Schedule: check the queue", **by("hub")))
        self.ev("delegation.created", None, session="w", for_session="qa", **by("qa"))
        self.end("qa", "Raised w.")
        self.ev("owner.seen", "qa", seq=self.seq, **OWNER)
        self.ev("owner.seen", "hub", seq=self.seq, **OWNER)
        self.start("w", self.to("w"))
        self.end("w", "The queue is empty.")
        self.assertEqual(self.unread(), [])
        self.assertEqual(self.st["sessions"]["w"]["merged"]["into"], "qa")

    def test_agents_talking_about_his_task_leave_one_message_from_the_one_he_asked(self):
        # the owner wrote to qa; e2e and another agent talked about a failure between themselves — all three used to get "new"
        self.ev("session.requested", "e2e", cwd="/w", session_id="s-e2e", role="agent", **OWNER)
        self.ev("session.started", "e2e")
        self.start("qa", self.ev("message.received", None, to="qa", text="check task 510 on prod", **OWNER))
        self.ev("message.received", None, to="e2e", text="a test failed, take a look", **by("qa"))
        self.end("qa", "Running it; gave the failure to e2e.")
        self.ev("owner.seen", "qa", seq=self.seq, **OWNER)
        self.start("e2e", self.to("e2e"))
        self.ev("message.received", None, to="qa", text="Reproduced: the backend is to blame, the fix is not at fault.", **by("e2e"))
        self.end("e2e", "The backend is to blame, wrote to qa.")
        self.assertEqual(self.unread(), [])
        self.start("qa", self.to("qa"))
        self.end("qa", "task 510: the fix works; the test failure is the backend.")
        self.assertEqual(self.unread(), ["qa"])

    def test_the_chain_two_steps_away_is_no_longer_his_task(self):
        # live: the owner answered one session, it wrote to e2e, and e2e in the same turn wrote to qa about another
        # task — qa's result on e2e's letter is not about the first task and stays "new"
        self.ev("session.requested", "e2e", cwd="/w", session_id="s-e2e", role="agent", **OWNER)
        self.ev("session.started", "e2e")
        self.start("w", self.ev("message.received", None, to="w", text="rebase by the owner's decision", **OWNER))
        self.ev("message.received", None, to="e2e", text="branch rebase", **by("w"))
        self.end("w", "Wrote the decision down.")
        self.start("e2e", self.to("e2e"))
        self.ev("message.received", None, to="qa", text="task 619: the failure is due to data", **by("e2e"))
        self.ev("message.received", None, to="w", text="accepted, the branch rebase is done", **by("e2e"))
        self.end("e2e", "Accepted.")
        self.assertEqual(self.st["sessions"]["e2e"]["merged"]["into"], "w")
        self.start("qa", self.to("qa"))
        self.end("qa", "task 619: the failure is due to search data.")
        self.assertIn("qa", self.unread())

    def woken_executor(self):
        self.task("check task 7")
        self.ev("owner.seen", "hub", seq=self.seq, **OWNER)
        self.st["sessions"]["w"].update(role="executor", raised_by="qa")
        self.start("w", self.ev("message.received", None, to="w", text="send a link", **by("hub")))

    def test_an_executor_woken_by_a_plain_message_reports_to_the_one_who_raised_him(self):
        self.woken_executor()
        self.ev("message.received", None, to="qa", text="Link: https://example.test/a", **by("w"))
        self.end("w", "Link sent to qa.")
        self.assertEqual(self.unread(), [])
        self.assertEqual(self.st["sessions"]["w"]["merged"]["into"], "qa")

    def test_what_was_sent_in_an_earlier_turn_does_not_carry_to_a_turn_started_without_its_stop(self):
        # w wrote qa in its first turn, no Stop came, and qa's own letter starts the next turn: w's text answer to that
        # letter reaches nobody, so it must not fold into qa (the first turn's "wrote to qa" is not about this one)
        self.woken_executor()
        self.ev("message.received", None, to="qa", text="Link: https://example.test/a", **by("w"))
        self.start("w", self.ev("message.received", None, to="w", text="and on stage?", **by("qa")))
        self.end("w", "On stage: https://example.test/b")
        self.assertEqual(self.unread(), ["w"])
        self.assertNotIn("merged", self.st["sessions"]["w"])

    def test_a_short_note_and_a_notice_mid_turn_do_not_swallow_the_executors_result(self):
        # review of the PR: the executor wrote its raiser a short line, a background command ended mid-turn, and the
        # long result was taken as already sent: the raiser never got it
        self.task("check task 7")
        did = self.task_of("w")
        report = "Checked task 7: " + "all green, details of every step. " * 10
        for notice in ({"marker": None}, {"marker": "injected"}):
            with self.subTest(**notice):
                self.start("w", self.to("w", delegation=did))
                self.ev("message.received", None, to="qa", text="Started, will take about ten minutes.", **by("w"))
                self.ev("task.noted", did, text="started a run", **by("w"))
                marker = notice["marker"]
                if marker:
                    marker = self.ev("message.received", None, to="w", text="watcher signal", **by("hub"))
                    self.st["messages"][marker]["injected"] = True
                self.ev("turn.started", "w", marker=marker, prompt_head="x")   # a notice inserted mid-turn
                self.end("w", report)
                got = self.st["messages"].get(f"m{self.seq - 1}x1") or {}
                self.assertEqual((got.get("session"), got.get("task"), got.get("text")), ("qa", did, report))
                self.assertEqual(self.st["sessions"]["w"]["merged"]["into"], "qa")   # the reply reached qa: qa tells

    def test_a_short_line_to_the_raiser_does_not_fold_a_long_text_answer_away(self):
        # no task: only what the session wrote itself reaches the raiser, and a short line is not the answer
        self.woken_executor()
        self.ev("message.received", None, to="qa", text="Looking for the link.", **by("w"))
        self.ev("turn.started", "w", marker=None, prompt_head="x")
        self.end("w", "Link: https://example.test/a — the same on stage.")
        self.assertEqual(self.unread(), ["w"])
        self.assertNotIn("merged", self.st["sessions"]["w"])

    def test_a_result_nobody_got_is_not_folded_away(self):
        # the executor answered in the turn's text only: nobody but the owner's page sees it, so it stays "new"
        self.woken_executor()
        self.end("w", "Link: https://example.test/a")
        self.assertEqual(self.unread(), ["w"])
        self.assertNotIn("merged", self.st["sessions"]["w"])

    def test_a_text_answer_to_an_asker_who_waits_is_relayed_and_folded(self):
        self.ev("session.requested", "e2e", cwd="/w", session_id="s-e2e", role="agent", **OWNER)
        self.ev("session.started", "e2e")
        self.start("qa", self.ev("message.received", None, to="qa", text="check task 510 on prod", **OWNER))
        self.ev("message.received", None, to="e2e", text="a test failed, take a look", **by("qa"))
        self.ev("session.waiting", "qa", text="e2e's answer about the failed test", **by("qa"))
        self.end("qa", "Running it; gave the failure to e2e.")
        self.ev("owner.seen", "qa", seq=self.seq, **OWNER)
        self.start("e2e", self.to("e2e"))
        self.end("e2e", "Reproduced: the backend is to blame, details in the log.")
        self.assertEqual(self.unread(), [])
        self.assertEqual(self.st["sessions"]["e2e"]["merged"]["into"], "qa")
        self.assertTrue(self.to("qa", relay=True))                  # and the asker did get it

    def test_a_text_only_answer_to_an_asker_who_does_not_wait_stays_new(self):
        self.ev("session.requested", "e2e", cwd="/w", session_id="s-e2e", role="agent", **OWNER)
        self.ev("session.started", "e2e")
        self.start("qa", self.ev("message.received", None, to="qa", text="check task 510 on prod", **OWNER))
        self.ev("message.received", None, to="e2e", text="a test failed, take a look", **by("qa"))
        self.end("qa", "Running it; gave the failure to e2e.")
        self.ev("owner.seen", "qa", seq=self.seq, **OWNER)
        self.start("e2e", self.to("e2e"))
        self.end("e2e", "Reproduced: the backend is to blame, details in the log.")
        self.assertEqual(self.unread(), ["e2e"])

    def test_his_words_passed_on_with_the_turns_other_message_are_his(self):
        # the turn took a notice and, with it, the owner's word relayed by the hub (it has `re`, not from_owner)
        m = self.task("check task 7")
        self.st["sessions"]["w"].update(role="executor", raised_by="qa")
        notice = self.ev("message.received", None, to="w", text="watcher signal", **by("hub"))
        word = self.ev("message.received", None, to="w", text="owner: and the mobile version too", **by("hub"))
        self.st["messages"][word]["re"] = m
        self.ev("turn.started", "w", marker=notice, also=[word], prompt_head="x")
        self.ev("message.received", None, to="qa", text="The mobile version is green", **by("w"))
        self.end("w", "Mobile is green too.")
        self.assertIn("w", self.unread())

    def test_a_direction_with_a_backlog_is_not_an_executor(self):
        # role executor + raised_by + a backlog is a direction (minihub.runner): its ordinary results are its own
        self.woken_executor()
        self.st["sessions"]["w"]["backlog"] = [{"id": "b1"}]
        self.ev("message.received", None, to="qa", text="Link: https://example.test/a", **by("w"))
        self.end("w", "Link sent to qa.")
        self.assertEqual(self.unread(), ["w"])

    def test_a_result_is_not_folded_into_a_session_that_has_gone_to_sleep(self):
        # parked is a sleep too (machines.SLEEP): a message raises it the same way as an asleep one
        for state in ("asleep", "parked"):
            with self.subTest(state):
                self.setUp()
                self.woken_executor()
                self.ev("message.received", None, to="qa", text="Link: https://example.test/a", **by("w"))
                self.st["sessions"]["qa"]["state"] = state
                self.end("w", "Link sent to qa.")
                self.assertEqual(self.unread(), ["w"])              # qa sleeps: it will not say it
                self.assertNotIn("merged", self.st["sessions"]["w"])

    def test_the_hub_does_not_retell_when_the_one_who_answered_has_gone_to_sleep(self):
        # the answer already reached the owner from qa; that qa fell asleep afterwards makes the hub's retelling no less a repeat
        m = self.task("check task 7")
        self.start("w", self.to("w"))
        self.end("w", "Checked: all green.")
        self.start("qa", self.to("qa", task=next(d["id"] for d in self.st["delegations"].values() if d["session"] == "w")))
        self.end("qa", "Task 7: all green, closing.")
        self.ev("owner.seen", "qa", seq=self.seq, **OWNER)
        self.st["sessions"]["qa"]["state"] = "asleep"
        self.start("hub", self.to("hub", task=self.task_id(m)))
        self.end("hub", "qa checked task 7 — all green.")
        self.assertEqual(self.unread(), [])
        self.assertEqual(self.st["sessions"]["hub"]["merged"]["into"], "qa")

    def test_a_result_the_assignment_will_not_deliver_is_not_folded_away(self):
        # qa closed the task (delegation.done) while w was at work: the reply goes nowhere, so nobody else tells it
        self.task("check task 7")
        self.start("w", self.to("w"))
        self.ev("delegation.done", next(d["id"] for d in self.st["delegations"].values() if d["session"] == "w"),
                **by("qa"))
        self.end("w", "Checked: all green.")
        self.assertEqual(self.unread(), ["w"])
        self.assertNotIn("merged", self.st["sessions"]["w"])

    def test_a_task_the_owner_set_himself_is_answered_to_him(self):
        # `hub task new w "…"` from the owner's terminal, a schedule job's command: no `--for`, no asking session, the
        # assigner is the owner — the hub still gets REPLIED (a note with the task) and its retelling folds into w
        self.ev("delegation.created", None, session="w", words="check the queue", **OWNER)
        did = self.task_of("w")
        self.start("w", self.to("w"))
        self.end("w", "The queue is empty.")
        self.assertEqual(self.unread(), ["w"])
        self.assertNotIn("merged", self.st["sessions"]["w"])
        self.start("hub", self.to("hub", task=did))
        self.end("hub", "w checked the queue: it is empty.")
        self.assertEqual(self.unread(), ["w"])                      # one message, not two
        self.assertEqual(self.st["sessions"]["hub"]["merged"], {"seq": self.seq - 1, "into": "w"})

    def terminal_task_answered(self):
        self.ev("delegation.created", None, session="w", words="check the queue", **OWNER)
        did = self.task_of("w")
        self.start("w", self.to("w"))
        self.end("w", "The queue is empty.")
        self.assertEqual(self.unread(), ["w"])
        self.ev("owner.seen", "w", seq=self.seq, **OWNER)
        return did

    def test_the_hubs_note_of_a_terminal_task_taken_with_another_letter_still_folds(self):
        # the hub was busy: the note (REPLIED) came in the same turn as qa's letter, the letter is the turn's marker
        did = self.terminal_task_answered()
        note = self.to("hub", task=did)
        letter = self.ev("message.received", None, to="hub", text="qa: a question about the queue", **by("qa"))
        self.ev("turn.started", "hub", marker=letter, also=[note], prompt_head="x")
        self.end("hub", "w checked the queue: it is empty.")
        self.assertEqual(self.unread(), [])                         # w said it already
        self.assertEqual(self.st["sessions"]["hub"]["merged"]["into"], "w")

    def test_a_notice_mid_turn_keeps_what_the_hub_took_with_its_letter(self):
        # the same turn, a watchdog notice is inserted before the end: the note in `also` is still the turn's, the
        # notice's own `also` is added to it
        did = self.terminal_task_answered()
        note = self.to("hub", task=did)
        letter = self.ev("message.received", None, to="hub", text="qa: a question about the queue", **by("qa"))
        self.ev("turn.started", "hub", marker=letter, also=[note], prompt_head="x")
        notice = self.ev("message.received", None, to="hub", text="watcher signal", **by("hub"))
        self.st["messages"][notice]["injected"] = True
        extra = self.ev("message.received", None, to="hub", text="another letter", **by("qa"))
        self.ev("turn.started", "hub", marker=notice, also=[extra], prompt_head="x")
        self.assertEqual(self.st["sessions"]["hub"]["turn_also"], [note, extra])
        self.end("hub", "w checked the queue: it is empty.")
        self.assertEqual(self.unread(), [])
        self.assertEqual(self.st["sessions"]["hub"]["merged"]["into"], "w")

    def test_the_hub_does_not_retell_a_terminal_task_whose_executor_went_to_sleep_on_done(self):
        # on REPLIED the hub closes the task (`hub task done`), the executor falls asleep before the hub's result
        did = self.terminal_task_answered()
        self.start("hub", self.to("hub", task=did))
        self.st["sessions"]["w"]["state"] = "asleep"
        self.end("hub", "w checked the queue: it is empty.")
        self.assertEqual(self.unread(), [])
        self.assertEqual(self.st["sessions"]["hub"]["merged"]["into"], "w")

    def test_a_mini_hubs_executor_on_a_terminal_task_is_not_silenced_by_the_hubs_fold(self):
        # w answers on the bus, which is not "new"; the hub's retelling must not fold into it, or the owner hears nothing
        self.st["sessions"]["w"].update(role="executor", raised_by="qa", bus="qa")
        did = self.terminal_task_answered_quietly()
        self.start("hub", self.to("hub", task=did))
        self.end("hub", "w checked the queue: it is empty.")
        self.assertEqual(self.unread(), ["hub"])
        self.assertNotIn("merged", self.st["sessions"]["hub"])

    def terminal_task_answered_quietly(self):
        self.ev("delegation.created", None, session="w", words="check the queue", **OWNER)
        did = self.task_of("w")
        self.start("w", self.to("w"))
        self.end("w", "The queue is empty.")
        self.assertEqual(self.unread(), [])                         # the bus: not "new" for the owner
        return did

    def test_a_task_of_an_older_core_raised_by_a_session_is_not_the_owners(self):
        # a record without `for_session` but with `by` (an older core: the hub set it) is not "set from his terminal":
        # nobody folds into the hub, the result stays w's own
        self.ev("delegation.created", None, session="w", words="check the queue", **by("hub"))
        self.start("w", self.to("w"))
        self.end("w", "The queue is empty.")
        self.assertEqual(self.st["delegations"][self.task_of("w")]["by"], "hub")
        self.assertNotIn("merged", self.st["sessions"]["w"])
        self.start("hub", self.to("hub", task=self.task_of("w")))
        self.end("hub", "w checked the queue: it is empty.")
        self.assertNotIn("merged", self.st["sessions"]["hub"])

    def test_the_hub_speaks_when_it_closed_a_task_nobody_answered(self):
        m = self.ev("message.received", None, text="check task 7", **OWNER)
        self.start("hub", m)
        self.ev("delegation.created", None, session="qa", **{"for": m}, **by("hub"))
        self.end("hub", "Passed to qa.")
        self.ev("owner.seen", "hub", seq=self.seq, **OWNER)
        self.start("qa", self.to("qa", delegation=self.task_id(m)))
        self.ev("turn.result", "qa", text="API Error 500", is_error=True)
        self.ev("turn.stopped", "qa")
        self.start("hub", self.to("hub", task=self.task_id(m)))
        self.ev("delegation.done", self.task_id(m), **by("hub"))          # closed by the hub, no answer ever came
        self.end("hub", "qa broke off, closed the task.")
        d = self.st["delegations"][self.task_id(m)]
        self.assertEqual((d["state"], d["reply"]), ("done", None))
        self.assertIn("hub", self.unread())
        self.assertNotIn("merged", self.st["sessions"]["hub"])

    def test_what_a_session_sent_before_a_notice_mid_turn_is_not_forgotten(self):
        self.woken_executor()
        self.ev("message.received", None, to="qa", text="Link: https://example.test/a", **by("w"))
        notice = self.ev("message.received", None, to="w", text="watcher signal", **by("hub"))
        self.st["messages"][notice]["injected"] = True
        self.start("w", notice)
        self.end("w", "Link sent to qa.")
        self.assertEqual(self.unread(), [])
        self.assertEqual(self.st["sessions"]["w"]["merged"]["into"], "qa")

    def test_a_turn_raised_by_an_older_core_on_the_owners_words_stays_his(self):
        # the state of a session in the middle of a turn at `hub restart`: no turn_his yet, only the turn's message
        self.woken_executor()
        word = self.ev("message.received", None, to="w", text="and the mobile version too", **OWNER)
        self.st["sessions"]["w"].update(turn_marker=word)
        self.st["sessions"]["w"].pop("turn_his", None)
        self.st["sessions"]["w"].pop("turn_head", None)
        self.ev("message.received", None, to="qa", text="The mobile version is green", **by("w"))
        self.end("w", "Mobile is green too.")
        self.assertIn("w", self.unread())

    def test_a_turn_raised_by_an_older_core_keeps_his_words_passed_on_in_also(self):
        m = self.task("check task 7")
        self.st["sessions"]["w"].update(role="executor", raised_by="qa")
        notice = self.ev("message.received", None, to="w", text="watcher signal", **by("hub"))
        word = self.ev("message.received", None, to="w", text="owner: and the mobile version too", **by("hub"))
        self.st["messages"][word]["re"] = m
        self.st["sessions"]["w"].update(turn_marker=notice, turn_also=[word])
        self.st["sessions"]["w"].pop("turn_his", None)
        self.st["sessions"]["w"].pop("turn_head", None)
        self.ev("message.received", None, to="qa", text="The mobile version is green", **by("w"))
        self.end("w", "Mobile is green too.")
        self.assertIn("w", self.unread())

    def test_a_schedule_script_writing_as_a_busy_session_is_not_part_of_its_turn(self):
        # a watcher writes as a busy session (not as the owner) while that session works on the owner's task — the
        # results on the watcher's signals would have merged into it
        m = self.ev("message.received", None, to="qa", text="make duplicates", **OWNER)
        self.start("qa", m)
        self.start("w", self.ev("message.received", None, to="w", text="watcher signal", schedule_job="watch", **by("qa")))
        self.assertNotIn("root", self.st["messages"][self.to("w")])
        self.end("w", "Signal handled.")
        self.assertIn("w", self.unread())

    def test_not_in_the_summary_when_he_is_away(self):
        self.task("check task 7")
        self.ev("owner.away", None, **OWNER)
        self.start("w", self.to("w"))
        self.end("w", "Checked: all green.")
        self.assertEqual([d["session"] for d in self.st["owner"]["done"]], [])


class Telegram(unittest.TestCase):
    """The owner asked from Telegram: the executor's answer on his task goes there, the hub's retelling does not."""
    A = where.AnswersWhereAsked
    setUp, tearDown, spooled, msg, ingest = A.setUp, A.tearDown, A.spooled, A.msg, A.ingest
    from_telegram, session, by, hub_turn, core, texts = A.from_telegram, A.session, A.by, A.hub_turn, A.core, A.texts

    def test_the_hubs_retelling_does_not_go_to_telegram(self):
        tg = self.from_telegram("count the revenue", 5)
        self.session("hub", kind="hub")
        self.session("w")
        self.j.append("delegation.created", None, {"session": "w", "for": tg, **self.by("hub")})
        st = self.core()
        d = next(d for d in st["delegations"].values() if d.get("for") == tg)
        self.j.append("turn.started", "w", {"marker": d["message"]})
        self.j.append("turn.result", "w", {"text": "42 000 $"})
        self.j.append("turn.stopped", "w", {})
        st = self.core()
        note = max((m for m in st["messages"].values() if m["session"] == "hub" and m.get("task") == d["id"]),
                   key=lambda m: m["created"])
        self.hub_turn(note["id"], "w counted: 42,000 $")
        self.assertEqual(self.texts(), [("<b>w</b> replies:\n42 000 $", 5)])

    def test_a_folded_result_waiting_in_the_outbox_stays_folded(self):
        # a send ahead of it fails, so the retelling waits for a repeat; meanwhile the hub folds another one — the
        # verdict is the one the core gave at the moment of the event, not the session's latest
        self.session("hub", kind="hub")
        self.session("w")
        self.fake.fail["sendMessage"] = ["drop"]
        self.j.append("owner.notify", None, {"text": "first", "channel": "cli", "owner": True})
        for n, text in enumerate(("count the revenue", "count the expenses")):
            tg = self.from_telegram(text, 5 + n)
            self.j.append("delegation.created", None, {"session": "w", "for": tg, **self.by("hub")})
            d = next(d for d in self.core()["delegations"].values() if d.get("for") == tg)
            self.j.append("turn.started", "w", {"marker": d["message"]})
            self.j.append("turn.result", "w", {"text": f"result {n}"})
            self.j.append("turn.stopped", "w", {})
            note = max((m for m in self.core()["messages"].values() if m["session"] == "hub" and m.get("task") == d["id"]),
                       key=lambda m: m["created"])
            self.hub_turn(note["id"], f"retelling {n}")
        with mock.patch("sys.stderr", new=io.StringIO()):
            self.bot.tick()
        self.bot.hold_until = 0
        self.bot.tick()
        sent = [b.get("text", "") for b in self.fake.sent()]
        self.assertTrue(any("result 0" in t for t in sent), sent)      # the executor's own answer went
        self.assertFalse(any("retelling" in t for t in sent), sent)   # the hub's retelling did not

if __name__ == "__main__":
    unittest.main()
