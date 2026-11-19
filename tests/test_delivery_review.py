"""Review of message delivery after the 2026-10-03 releases (owner request 2026-10-04: check the message delivery code and
the uncovered cases — after a pile of releases the hub had become less stable). Each case is a confirmed reproduction on
the reducer state; the test holds the correct behaviour."""
import tests  # noqa: F401 — do not see the machine's settings
import unittest

from core import reducer

OWNER = {"channel": "cli", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}


class Review(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "devops", cwd="/k", session_id="s-dev", role="agent", about="DevOps", **OWNER)
        self.ev("session.started", "hub")
        self.ev("session.started", "devops")
        self.ev("turn.started", "hub", marker=None, prompt_head="x")
        self.ev("turn.started", "devops", marker=None, prompt_head="long")

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 100 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def msgs(self, name):
        return [m for m in self.st["messages"].values() if m["session"] == name]

    def test_two_messages_in_one_turn_are_both_delivered(self):
        # 2026-10-05: mid-turn, message A was inserted into stdin; the turn was interrupted — the core typed the next
        # one, B; Claude Code took A and B as one turn with A's marker; B stayed «typing», and the queue behind it stood
        # until the session was restarted
        self.ev("message.received", None, to="devops", text="A", **HUB)
        a = f"m{self.seq}"
        self.ev("message.received", None, to="devops", text="B", **HUB)
        b = f"m{self.seq}"
        self.ev("message.received", None, to="devops", text="C", **OWNER)
        c = f"m{self.seq}"
        self.assertIsNone(self.ev("message.inject", a, channel="core", owner=False))
        self.ev("turn.result", "devops", text="", is_error=True)
        self.ev("turn.stopped", "devops")
        self.assertEqual(self.st["messages"][b]["state"], "typing")
        self.ev("turn.started", "devops", marker=a, also=[b], prompt_head="x")
        self.assertEqual(self.st["messages"][b]["state"], "delivered")
        self.ev("turn.stopped", "devops")
        self.assertEqual(self.st["messages"][c]["state"], "typing")         # the queue moves on

    def test_two_core_notes_in_one_event_both_survive(self):
        # REPLIED to the hub and HANDOFF to the worker in one event got the same id m{seq} — one overwrote the other
        self.ev("session.requested", "w", cwd="/w", session_id="s-w", **OWNER)
        self.ev("session.started", "w")
        self.ev("message.received", None, to="hub", text="fix bug", **OWNER)
        owner_mid = f"m{self.seq}"
        self.ev("delegation.created", None, session="w", **{"for": owner_mid}, **HUB)
        task = f"m{self.seq}"
        self.ev("turn.started", "w", marker=task, prompt_head="[hub]")
        self.ev("session.close_ready", "w", channel="session", sender="w", summary="done")
        self.ev("turn.result", "w", text="Fixed, PR #5")
        texts = [m["text"] for m in self.st["messages"].values()]
        self.assertTrue(any(t.startswith("REPLIED") for t in texts), "REPLIED to the hub is gone")
        self.assertTrue(any(t.startswith("HANDOFF") for t in texts), "HANDOFF to the worker is gone")

    def test_a_quoted_marker_does_not_steal_another_sessions_message(self):
        # a turn with another message's marker in its text (a background notification, a paste) took it away from the recipient
        self.ev("message.received", None, to="devops", text="deploy X", **OWNER)
        mid = f"m{self.seq}"
        self.ev("session.requested", "writer", cwd="/w", session_id="s-w", **OWNER)
        self.ev("session.started", "writer")
        self.ev("turn.started", "writer", marker=mid, prompt_head="<task-notification>… [hub:%s] deploy X" % mid)
        self.assertEqual((self.st["messages"][mid]["session"], self.st["messages"][mid]["state"]), ("devops", "queued"))

    def test_owner_words_to_a_removed_session_do_not_answer_the_hubs_form(self):
        self.ev("session.requested", "w", cwd="/w", session_id="s-w", **OWNER)
        self.st["sessions"]["w"]["state"] = "asleep"
        self.ev("session.remove_requested", "w", **OWNER)
        self.ev("permission.requested", "hub", request_id="r1", tool="AskUserQuestion", interactive=True,
                input={"questions": [{"question": "Merge PR #3?"}]})
        self.ev("message.received", None, to="w", text="stop the migration now", **OWNER)
        m = self.st["messages"][f"m{self.seq}"]
        self.assertFalse(m.get("answered_form") or m.get("answers_question"))   # and the hub's question card is not closed
        self.assertFalse([a for a in self.actions if a.get("do") == "respond" and a["response"]["behavior"] == "allow"])
        self.assertEqual(len(self.st["sessions"]["hub"]["asks"]), 1)
        self.assertIn("is gone", m["origin"])

    def test_a_removed_sleeping_session_does_not_keep_a_queue_forever(self):
        # journal analysis: a message sat «queued» for a deleted session since 2026-09-30 — it went to sleep with it, then was deleted
        self.ev("session.requested", "w", cwd="/w", session_id="s-w", **OWNER)
        self.ev("session.started", "w")
        self.ev("turn.started", "w", marker=None, prompt_head="x")
        self.ev("message.received", None, to="w", text="later", **OWNER)
        mid = f"m{self.seq}"
        self.st["sessions"]["w"]["state"] = "asleep"
        self.ev("session.remove_requested", "w", **OWNER)
        self.assertEqual(self.st["sessions"]["w"]["state"], "removed")
        m = self.st["messages"][mid]
        self.assertEqual((m["state"], m.get("reason")), ("failed", "session removed"))

    BLOCKED = "UserPromptSubmit operation blocked by hook: secrets in the prompt"

    def _delivered_with_marker(self, to="devops", sender=HUB):
        self.ev("turn.result", to, text="x")
        self.ev("turn.stopped", to)
        self.ev("message.received", None, to=to, text="a reply to the card", **sender)
        mid = f"m{self.seq}"
        self.assertEqual(self.st["messages"][mid]["state"], "typing")
        # UserPromptSubmit hooks run in parallel: the core's hook recorded the turn with its marker while another hook rejected the prompt
        self.ev("turn.started", to, marker=mid, prompt_head="[hub:%s] a reply to the card" % mid)
        self.assertEqual(self.st["messages"][mid]["state"], "delivered")
        return mid

    def test_a_prompt_refused_by_a_session_hook_is_not_delivered_and_says_why(self):
        # another session hook rejected the prompt (exit 2): the model did not read it, there will be no Stop — a result with no steps and a reason
        mid = self._delivered_with_marker()
        self.actions = []
        self.ev("turn.result", "devops", text=self.BLOCKED, turns=0, is_error=True, subtype="success")
        m = self.st["messages"][mid]
        self.assertEqual((m["state"], m["reason"], m["hook"]), ("failed", "blocked by hook", "secrets in the prompt"))
        self.assertNotIn("delivered_at", m)
        self.assertFalse(self.st["sessions"]["devops"].get("dirty"))         # we do not restart: the session's background is intact
        self.assertFalse([x for x in self.actions if x.get("do") in ("spawn", "stop", "type")])
        self.assertFalse(m.get("deadline"))                                   # and we do not retry: the same hook will reject again

    def test_a_refused_prompt_also_marks_lines_taken_with_it(self):
        self.ev("turn.result", "devops", text="x")
        self.ev("turn.stopped", "devops")
        self.ev("message.received", None, to="devops", text="a", **HUB)
        first = f"m{self.seq}"
        self.ev("message.received", None, to="devops", text="b", **OWNER)
        second = f"m{self.seq}"
        self.st["messages"][second]["state"] = "typing"
        self.ev("turn.started", "devops", marker=first, also=[second], prompt_head="x")
        self.ev("turn.result", "devops", text=self.BLOCKED, turns=0, is_error=False)
        self.assertEqual([self.st["messages"][x]["state"] for x in (first, second)], ["failed", "failed"])

    def test_a_refused_notice_mid_turn_fails_only_what_that_prompt_carried(self):
        # the turn took a letter and a note in its first prompt; a notice inserted later carries one more letter and the
        # hook refuses that prompt: the first two were read by the model, only the notice and its letter fail
        self.ev("turn.result", "devops", text="x")
        self.ev("turn.stopped", "devops")
        self.ev("message.received", None, to="devops", text="a", **HUB)
        first = f"m{self.seq}"
        self.ev("message.received", None, to="devops", text="b", **OWNER)
        note = f"m{self.seq}"
        self.st["messages"][note]["state"] = "typing"
        self.ev("turn.started", "devops", marker=first, also=[note], prompt_head="x")
        self.ev("message.received", None, to="devops", text="watchdog", **HUB)
        notice = f"m{self.seq}"
        self.st["messages"][notice]["injected"] = True
        self.ev("message.received", None, to="devops", text="c", **HUB)
        extra = f"m{self.seq}"
        self.st["messages"][extra]["state"] = "typing"
        self.ev("turn.started", "devops", marker=notice, also=[extra], prompt_head="x")
        self.ev("turn.result", "devops", text=self.BLOCKED, turns=0, is_error=False)
        self.assertEqual([self.st["messages"][x]["state"] for x in (first, note)], ["delivered", "delivered"])
        self.assertEqual([self.st["messages"][x]["state"] for x in (notice, extra)], ["failed", "failed"])
        self.assertEqual(self.st["sessions"]["devops"]["turn_also"], [note])   # the turn took no more than the model read

    def test_a_refused_notice_that_repeats_the_head_does_not_fail_the_head(self):
        # a later prompt may list the head of the turn among its letters; the model read it in the first prompt
        self.ev("turn.result", "devops", text="x")
        self.ev("turn.stopped", "devops")
        self.ev("message.received", None, to="devops", text="a", **HUB)
        first = f"m{self.seq}"
        self.ev("turn.started", "devops", marker=first, prompt_head="x")
        self.ev("message.received", None, to="devops", text="watchdog", **HUB)
        notice = f"m{self.seq}"
        self.st["messages"][notice]["injected"] = True
        self.ev("turn.started", "devops", marker=notice, also=[first], prompt_head="x")
        self.ev("turn.result", "devops", text=self.BLOCKED, turns=0, is_error=False)
        self.assertEqual(self.st["messages"][first]["state"], "delivered")
        self.assertEqual(self.st["messages"][notice]["state"], "failed")

    def test_a_refusal_before_the_core_hook_wrote_the_turn_fails_the_typed_message(self):
        self.ev("turn.result", "devops", text="x")
        self.ev("turn.stopped", "devops")
        self.ev("message.received", None, to="devops", text="a question", **HUB)
        mid = f"m{self.seq}"
        self.ev("turn.result", "devops", text=self.BLOCKED, turns=0, is_error=True)
        self.assertEqual((self.st["messages"][mid]["state"], self.st["messages"][mid]["reason"]), ("failed", "blocked by hook"))

    def test_a_refusal_does_not_touch_the_message_of_an_earlier_finished_turn(self):
        mid = self._delivered_with_marker()
        self.ev("turn.result", "devops", text="done")
        self.ev("turn.stopped", "devops")
        # a prompt without a marker (the owner in the terminal) — the hook's rejection is no longer about the answered message
        self.ev("turn.result", "devops", text=self.BLOCKED, turns=0, is_error=True)
        self.assertEqual(self.st["messages"][mid]["state"], "delivered")

    def test_a_failing_hook_that_does_not_block_is_not_a_refusal(self):
        # a crashed hook (exit 1, 127) does not block the prompt — Claude Code passes it to the model; and a phrase in the middle of an answer is not a rejection
        mid = self._delivered_with_marker()
        self.ev("turn.result", "devops", text="UserPromptSubmit hook error: exit code 127", turns=0, is_error=True)
        self.ev("turn.result", "devops", text="in the log: UserPromptSubmit operation blocked by hook", turns=0, is_error=False)
        self.ev("turn.result", "devops", text=self.BLOCKED, turns=3, is_error=False)
        self.assertEqual(self.st["messages"][mid]["state"], "delivered")

    def test_the_reason_is_the_hooks_own_words_without_its_path_and_the_prompt(self):
        # the result of Claude Code 2.1.218–2.1.302 as is: exit 2 — with the script path, both kinds — with the prompt repeated
        for text in ("UserPromptSubmit operation blocked by hook:\n[/home/x/.claude/hooks/guard.sh]: secrets in the prompt"
                     "\n\n\nOriginal prompt: [hub:m9] token=abc",
                     "UserPromptSubmit operation blocked by hook:\nsecrets in the prompt\n\nOriginal prompt: [hub:m9] x"):
            mid = self._delivered_with_marker()
            self.ev("turn.result", "devops", text=text, turns=0, is_error=False, subtype="success")
            self.assertEqual(self.st["messages"][mid]["hook"], "secrets in the prompt")

    def test_a_refused_unmarked_prompt_leaves_the_typed_message_for_its_own_turn(self):
        # a turn without a marker (the owner in the terminal) rejected by a hook, while the core has already typed a message — it waits for its turn
        self.ev("turn.result", "devops", text="x")
        self.ev("turn.stopped", "devops")
        self.ev("message.received", None, to="devops", text="a question", **HUB)
        mid = f"m{self.seq}"
        self.ev("turn.started", "devops", marker=None, prompt_head="from the terminal")
        self.ev("turn.result", "devops", text=self.BLOCKED, turns=0, is_error=False)
        self.assertEqual(self.st["messages"][mid]["state"], "typing")

    def test_a_refused_task_is_stopped_not_answered_with_the_refusal(self):
        # a rejection result is not the executor's answer: STOPPED to whoever assigned it, not REPLIED with the hook text and the repeated prompt
        self.ev("session.requested", "w", cwd="/w", session_id="s-w", **OWNER)
        self.ev("session.started", "w")
        self.ev("message.received", None, to="hub", text="fix bug", **OWNER)
        self.ev("delegation.created", None, session="w", **{"for": f"m{self.seq}"}, **HUB)
        task = f"m{self.seq}"
        self.ev("turn.started", "w", marker=task, prompt_head="[hub]")
        self.ev("turn.result", "w", text=self.BLOCKED + "\n\nOriginal prompt: [hub:%s] fix bug" % task, turns=0,
                is_error=False, subtype="success")
        d = next(iter(self.st["delegations"].values()))
        self.assertEqual((d["state"], d.get("reply")), ("stopped", None))
        self.assertEqual(self.st["messages"][task]["state"], "failed")

    def test_a_refused_owner_message_is_not_an_answer_and_loses_its_eyes(self):
        self.ev("turn.result", "devops", text="x")
        self.ev("turn.stopped", "devops")
        self.ev("message.received", None, to="devops", text="a question", **OWNER)
        mid = f"m{self.seq}"
        self.ev("turn.started", "devops", marker=mid, prompt_head="[hub:%s] a question" % mid)
        self.assertEqual(self.st.get("badges", {}).get(mid), "👀")
        before = self.st["sessions"]["devops"].get("owner_out")
        self.ev("turn.result", "devops", text=self.BLOCKED, turns=0, is_error=False, subtype="success")
        s = self.st["sessions"]["devops"]
        self.assertEqual(s.get("owner_out"), before)                        # not an «answer» to the owner
        self.assertNotIn(mid, self.st.get("badges", {}))
        self.assertNotIn(mid, s.get("eyes") or [])

    def test_a_refusal_quoting_a_limit_is_not_a_limit_cut(self):
        # the prompt repeated in the rejection text («Original prompt: …») is not a limit result: no cut-off, no continuation
        mid = self._delivered_with_marker()
        self.ev("turn.result", "devops", text=self.BLOCKED + "\n\nOriginal prompt: [hub:%s] usage limit reached?" % mid,
                turns=0, is_error=False, subtype="success")
        self.assertNotIn("limit_cut", self.st["sessions"]["devops"])


if __name__ == "__main__":
    unittest.main()
