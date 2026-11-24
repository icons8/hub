"""Where a turn begins (PR #429, part one). A new turn: the session was free, or a letter is read with no Stop before it.
A message inserted mid-turn (`--now`, a watchdog's letter) or a notice without a marker (a background command ended)
is the same turn: it changes neither what the turn began with (turn_head), nor what it took (turn_also), nor whom it
wrote to (sent_in_turn), nor which turn is running (turn_seq). The readers of turn_seq — the gist, the board, the
watchdog's recap and promises, the restart — follow the same decision. A short line to the raiser is not the result:
the full result still goes."""
import tests  # noqa: F401 — do not see the machine's settings
import unittest

from core import board, busstate, forms, liaison, reducer, restart

OWNER = {"channel": "web", "owner": True}


def by(sender):
    return {"channel": "session", "owner": False, "sender": sender}


class Turn(unittest.TestCase):
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

    def notice(self, name):
        """A watchdog's letter inserted into the running turn."""
        mid = self.ev("message.received", None, to=name, text="watchdog signal", **by("hub"))
        self.st["messages"][mid]["injected"] = True
        self.start(name, mid)
        return mid

    def raised(self):
        """qa sets w a task; w's turn is on it. Returns the task id."""
        self.ev("delegation.created", None, session="w", for_session="qa", words="check task 7", **by("qa"))
        did = max(self.st["delegations"].values(), key=lambda d: d["created"])["id"]
        task = next(m["id"] for m in self.st["messages"].values() if m["session"] == "w" and m.get("delegation") == did)
        self.start("w", task)
        return did

    # the result reaches the raiser

    def test_a_short_line_and_a_notice_mid_turn_do_not_swallow_the_executors_result(self):
        # review of the PR: the executor wrote its raiser a short line, a background command ended mid-turn, and the
        # long result was taken as already sent: the raiser never got it
        report = "Checked task 7: " + "all green, details for every step. " * 10
        did = None
        for kind in ("no marker", "inserted"):
            with self.subTest(kind):
                if did is None:
                    did = self.raised()
                else:
                    self.start("w", self.st["delegations"][did]["message"])
                self.ev("message.received", None, to="qa", text="Started, will take ten minutes.", **by("w"))
                self.ev("task.noted", did, text="started the run", **by("w"))
                if kind == "inserted":
                    self.notice("w")
                else:
                    self.ev("turn.started", "w", marker=None, prompt_head="x")   # a background command ended
                self.end("w", report)
                got = self.st["messages"].get(f"m{self.seq - 1}x1") or {}
                self.assertEqual((got.get("session"), got.get("task"), got.get("text")), ("qa", did, report))

    def test_three_short_lines_longer_together_than_the_report_do_not_swallow_it(self):
        # review of the split: "not shorter than the result" is about one message, not the sum of a turn's lines
        self.raised()
        line = "Step done, moving on, details will be in the turn result."
        report = "Checked task 7: all green, three steps passed, no remarks, details below by steps."
        self.assertLess(len(line), len(report))
        self.assertLess(len(report), 3 * len(line))
        for _ in range(3):
            self.ev("message.received", None, to="qa", text=line, **by("w"))
        self.end("w", report)
        got = self.st["messages"].get(f"m{self.seq - 1}x1") or {}
        self.assertEqual((got.get("session"), got.get("text")), ("qa", report))

    def test_a_result_the_session_sent_itself_is_not_sent_twice(self):
        # the other side: the executor sent the result with `hub send`, the turn's text only says so (analysis
        # 2026-10-07: 22 of 101 were the same result twice)
        self.raised()
        self.ev("message.received", None, to="qa", text="Checked task 7: all green, details by steps.", **by("w"))
        self.ev("turn.started", "w", marker=None, prompt_head="x")
        self.end("w", "Sent the result to qa.")
        self.assertNotIn(f"m{self.seq - 1}x1", self.st["messages"])

    def test_a_notice_mid_turn_keeps_whom_the_turn_wrote_to(self):
        self.raised()
        self.ev("message.received", None, to="qa", text="Link: https://example.test/a", **by("w"))
        self.notice("w")
        self.assertIn("qa", self.st["sessions"]["w"]["sent_in_turn"])

    def test_a_letter_without_a_stop_forgets_whom_the_previous_turn_wrote_to(self):
        self.raised()
        self.ev("message.received", None, to="qa", text="Link: https://example.test/a", **by("w"))
        self.start("w", self.ev("message.received", None, to="w", text="what about stage?", **by("qa")))
        self.assertEqual(self.st["sessions"]["w"]["sent_in_turn"], [])

    # what the turn began with and what it took

    def test_a_turn_cut_by_a_limit_continues_the_owners_message_after_a_notice(self):
        word = self.ev("message.received", None, to="qa", text="make the copies", **OWNER)
        self.start("qa", word)
        self.notice("qa")
        self.ev("turn.result", "qa", text="You've hit your session limit · resets 4pm", is_error=True)
        self.assertEqual(self.st["sessions"]["qa"]["limit_cut"].get("re"), word)

    def test_a_notice_without_a_marker_mid_turn_does_not_lose_what_the_turn_began_with(self):
        word = self.ev("message.received", None, to="qa", text="make the copies", **OWNER)
        self.start("qa", word)
        self.ev("turn.started", "qa", marker=None, prompt_head="x")   # a background helper ended mid-turn
        self.ev("turn.result", "qa", text="You've hit your session limit · resets 4pm", is_error=True)
        self.assertEqual(self.st["sessions"]["qa"]["limit_cut"].get("re"), word)

    def hub_letter(self):
        """The hub, free, reads qa's letter together with another one (`also`)."""
        note = self.ev("message.received", None, to="hub", text="qa: a note", **by("qa"))
        letter = self.ev("message.received", None, to="hub", text="qa: a question", **by("qa"))
        self.ev("turn.started", "hub", marker=letter, also=[note], prompt_head="x")
        return letter, note

    def test_a_notice_mid_turn_adds_what_it_took_to_the_turn(self):
        letter, note = self.hub_letter()
        notice = self.ev("message.received", None, to="hub", text="watchdog signal", **by("hub"))
        self.st["messages"][notice]["injected"] = True
        extra = self.ev("message.received", None, to="hub", text="another letter", **by("qa"))
        self.ev("turn.started", "hub", marker=notice, also=[extra], prompt_head="x")
        self.assertEqual(self.st["sessions"]["hub"]["turn_head"], letter)
        self.assertEqual(self.st["sessions"]["hub"]["turn_also"], [note, extra])

    def test_a_notice_mid_turn_does_not_list_a_message_twice(self):
        letter, note = self.hub_letter()
        notice = self.ev("message.received", None, to="hub", text="watchdog", **by("hub"))
        self.st["messages"][notice]["injected"] = True
        self.ev("turn.started", "hub", marker=notice, also=[note], prompt_head="x")
        self.assertEqual(self.st["sessions"]["hub"]["turn_also"], [note])

    def test_the_head_of_the_turn_repeated_in_a_later_prompt_is_not_listed_as_taken_with_it(self):
        letter, note = self.hub_letter()
        notice = self.ev("message.received", None, to="hub", text="watchdog", **by("hub"))
        self.st["messages"][notice]["injected"] = True
        self.ev("turn.started", "hub", marker=notice, also=[letter], prompt_head="x")
        self.assertEqual(self.st["sessions"]["hub"]["turn_also"], [note])

    def test_a_notice_mid_turn_in_the_state_of_an_older_core_keeps_what_the_turn_began_with(self):
        # an older core kept no turn_head: its turn_marker before the notice is what the turn began with
        word = self.ev("message.received", None, to="qa", text="make the copies", **OWNER)
        self.start("qa", word)
        self.st["sessions"]["qa"].pop("turn_head")
        self.notice("qa")
        self.assertEqual(self.st["sessions"]["qa"]["turn_head"], word)
        self.ev("turn.result", "qa", text="You've hit your session limit · resets 4pm", is_error=True)
        self.assertEqual(self.st["sessions"]["qa"]["limit_cut"].get("re"), word)

    def test_a_refused_later_prompt_does_not_fail_what_an_earlier_prompt_of_the_turn_carried(self):
        letter, note = self.hub_letter()
        notice = self.ev("message.received", None, to="hub", text="watchdog", **by("hub"))
        self.st["messages"][notice]["injected"] = True
        extra = self.ev("message.received", None, to="hub", text="another letter", **by("qa"))
        self.st["messages"][extra]["state"] = "typing"
        self.ev("turn.started", "hub", marker=notice, also=[note, extra], prompt_head="x")
        self.assertEqual(self.st["sessions"]["hub"]["prompt_also"], [extra])
        self.ev("turn.result", "hub", text="UserPromptSubmit operation blocked by hook: no", turns=0, is_error=False)
        self.assertEqual(self.st["messages"][extra]["state"], "failed")
        self.assertNotEqual(self.st["messages"][note]["state"], "failed")
        self.assertEqual(self.st["sessions"]["hub"]["turn_also"], [note])

    def test_a_new_turn_drops_what_the_previous_one_took_with_its_letter(self):
        self.hub_letter()
        self.start("hub", self.ev("message.received", None, to="hub", text="another letter", **by("qa")))
        self.assertNotIn("turn_also", self.st["sessions"]["hub"])

    # the bus liaison's words of the turn

    def liaison_turn(self):
        """The bus's liaison, free and on its first turn: a letter of session w (the head) with another one (`also`)."""
        self.ev("session.requested", liaison.NAME, cwd="/l", session_id="s-l", role="agent", liaison=True, channel="core")
        self.ev("session.started", liaison.NAME)
        self.st.setdefault("bus", {})["enabled"] = True
        first = self.ev("message.received", None, to=liaison.NAME, text="ask them", **by("w"))
        second = self.ev("message.received", None, to=liaison.NAME, text="and this", **by("w"))
        self.ev("turn.started", liaison.NAME, marker=first, also=[second], prompt_head="x")
        return first, second

    def liaison_words(self):
        return [(m["id"], who) for m, who in busstate._turn_words(self.st)]

    def test_the_liaisons_turn_words_do_not_take_the_marker_of_a_notice_inserted_mid_turn(self):
        first, second = self.liaison_turn()
        notice = "m900"
        reducer.new_message(self.st, notice, liaison.NAME, "a helper ended", 900, origin=forms.ORIGIN_CORE)
        self.ev("message.inject", notice, channel="cli", owner=True)
        self.ev("turn.started", liaison.NAME, marker=notice, prompt_head="x")
        self.assertEqual(self.liaison_words(), [(first, "w"), (second, "w")])

    def test_the_liaisons_turn_words_take_what_the_owner_inserted_into_the_turn(self):
        first, second = self.liaison_turn()
        now = self.ev("message.received", None, to=liaison.NAME, text="do not send", now=True, **OWNER)
        self.assertTrue(self.st["messages"][now]["injected"])
        self.ev("turn.started", liaison.NAME, marker=now, prompt_head="x")
        self.assertEqual(self.liaison_words(), [(first, "w"), (second, "w"), (now, "owner")])
        self.assertIsNone(busstate._asker_in(busstate._turn_requests(self.st), "bee"))

    def test_the_liaisons_turn_words_take_a_letter_of_a_session_inserted_by_the_watchdog(self):
        first, second = self.liaison_turn()
        late = self.ev("message.received", None, to=liaison.NAME, text="urgent", **by("w"))
        self.ev("message.inject", late, channel="cli", owner=True, src="watch")
        self.ev("turn.started", liaison.NAME, marker=late, prompt_head="x")
        self.assertEqual(self.liaison_words(), [(first, "w"), (second, "w"), (late, "w")])

    def test_the_liaisons_turn_words_do_not_keep_what_a_hook_refused(self):
        first, second = self.liaison_turn()
        now = self.ev("message.received", None, to=liaison.NAME, text="do not send", now=True, **OWNER)
        self.st["messages"][now]["state"] = "typing"
        self.ev("turn.started", liaison.NAME, marker=now, prompt_head="x")
        self.ev("turn.result", liaison.NAME, text="UserPromptSubmit operation blocked by hook: no", turns=0, is_error=False)
        self.assertEqual(self.st["messages"][now]["state"], "failed")
        self.assertEqual(self.liaison_words(), [(first, "w"), (second, "w")])

    # the readers of turn_seq

    def test_a_letter_without_a_stop_starts_a_new_turn_for_the_turn_counter_too(self):
        self.start("w", self.ev("message.received", None, to="w", text="send the link", **by("qa")))
        first = self.st["sessions"]["w"]["turn_seq"]
        self.notice("w")
        self.assertEqual(self.st["sessions"]["w"]["turn_seq"], first)
        self.start("w", self.ev("message.received", None, to="w", text="what about stage?", **by("qa")))
        self.assertEqual(self.st["sessions"]["w"]["turn_seq"], self.seq)

    def gist_turn(self):
        """w works on qa's letter, says its gist and edits a board card."""
        self.start("w", self.ev("message.received", None, to="w", text="send the link", **by("qa")))
        self.ev("session.gist", "w", text="looking for the link", **by("w"))
        return {"by": "w", "seq": self.seq, "ts": 100 + self.seq}

    def test_the_gist_and_the_board_keep_the_turn_through_a_notice(self):
        edit = self.gist_turn()
        self.notice("w")
        got = board.editing_now(self.st, edit, 100 + self.seq)
        self.assertEqual((got["who"], got["gist"]), (["w"], "looking for the link"))

    def test_the_gist_and_the_board_do_not_carry_to_a_turn_a_letter_starts_with_no_stop(self):
        # the gist and the card edit are of the previous turn: the board does not show them as this one's work
        edit = self.gist_turn()
        self.start("w", self.ev("message.received", None, to="w", text="what about stage?", **by("qa")))
        self.assertIsNone(board.editing_now(self.st, edit, 100 + self.seq))
        self.assertNotEqual(self.st["sessions"]["w"]["gist"]["turn"], self.st["sessions"]["w"]["turn_seq"])

    def test_the_watchdogs_recap_of_the_turn_stays_through_a_notice_and_not_past_a_letter(self):
        self.start("w", self.ev("message.received", None, to="w", text="send the link", **by("qa")))
        saw = self.st["sessions"]["w"]["turn_seq"]
        self.notice("w")
        self.ev("session.recap", "w", text="looks for the link", turn=saw, channel="cli", owner=True)
        self.assertEqual(self.st["sessions"]["w"]["recap"]["text"], "looks for the link")
        self.start("w", self.ev("message.received", None, to="w", text="what about stage?", **by("qa")))
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": "session.recap",
                                                   "entity": "w", "data": {"text": "old", "turn": saw,
                                                                           "channel": "cli", "owner": True}})
        self.assertEqual(why, "the \"working on\" line is about a past turn")

    def test_the_restart_asker_is_whose_letter_started_the_hubs_turn_not_a_notice(self):
        # the hub's turn is on w's request; a watchdog's letter is inserted before the hub asks for the restart
        self.start("w", None)
        ask = self.ev("message.received", None, to="hub", text="restart the whole hub", **by("w"))
        self.ev("session.waiting", "w", text="the hub restart", **by("w"))
        self.end("w", "will continue after the restart")
        self.start("hub", ask)
        self.notice("hub")
        self.assertEqual(self.st["sessions"]["hub"]["turn_head"], ask)
        self.assertEqual([a["session"] for a in restart.asker(self.st, by("hub"), 100 + self.seq)], ["w"])

    def test_the_restart_does_not_wake_one_whose_letter_started_a_new_turn_with_no_stop(self):
        # restart.wake: "nothing started since its turn" — a letter read with no Stop is such a start
        self.start("w", None)
        ask = self.ev("message.received", None, to="hub", text="restart the whole hub", **by("w"))
        self.ev("session.waiting", "w", text="the hub restart", **by("w"))
        self.start("hub", ask)
        asked = restart.asker(self.st, by("hub"), 100 + self.seq)
        self.start("w", self.ev("message.received", None, to="w", text="and this?", **by("qa")))
        self.end("w", "done")
        self.st["restart"] = {"asked": asked}
        self.assertEqual(restart.wake(self.st), [])

    def test_a_restart_asked_after_a_notice_mid_turn_is_still_awaited_by_whose_letter_began_it(self):
        # review of the PR: w's turn is on qa's letter, a background command ends mid-turn (no marker): qa still
        # waits for the report of the restart w asks for
        self.start("w", self.ev("message.received", None, to="w", text="restart after the fix", **by("qa")))
        self.ev("turn.started", "w", marker=None, prompt_head="x")
        self.assertTrue(restart.awaited(self.st, "w"))

    def test_a_send_in_a_turn_an_older_core_began_still_counts_as_the_result(self):
        # a turn running at the update: sent_in_turn from the older core, no sent_chars — the result is not sent twice
        from core import delegations
        sess = {"sent_in_turn": ["qa"]}
        self.assertTrue(delegations.got_result(sess, "qa", "long summary " * 20))
        self.assertFalse(delegations.got_result({**sess, "sent_chars": {"qa": 5}}, "qa", "long summary " * 20))
        self.assertFalse(delegations.got_result(sess, "w", "done"))
        # the update came mid-turn and the turn wrote a short line to another session since: qa's send stays the result
        self.assertTrue(delegations.got_result({**sess, "sent_in_turn": ["qa", "hub"], "sent_chars": {"hub": 2}},
                                               "qa", "long summary " * 20))


if __name__ == "__main__":
    unittest.main()
