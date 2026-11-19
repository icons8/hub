"""A session's folder changes without losing the conversation: the owner or the hub moves the agent's working folder,
the core brings a free session up again in the new folder with the same conversation (`claude --resume <id>` looks for
it in the project folder of the new cwd, the host puts a copy there: hosts/common.carry_transcript)."""
import tests  # noqa: F401
import unittest

from core import delegations, forms, reducer


class SessionCwd(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        owner = {"channel": "cli", "owner": True}
        self.owner = owner
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **owner)
        self.ev("session.requested", "ads", cwd="/old/ads", session_id="s-a", role="agent", **owner)
        self.ev("session.requested", "seo", cwd="/s", session_id="s-s", **owner)
        self.ev("session.started", "ads")
        self.ev("turn.ended", "ads")

    def ev(self, t, e=None, **d):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": t, "entity": e,
                                                      "data": d})
        return acts, why

    def test_owner_moves_an_idle_agent_and_it_resumes_the_same_conversation_there(self):
        self.st["sessions"]["ads"]["has_history"] = True
        acts, why = self.ev("session.cwd_set", "ads", cwd="/new/ads", **self.owner)
        self.assertIsNone(why)
        self.assertEqual(self.st["sessions"]["ads"]["cwd"], "/new/ads")
        spawn = [a for a in acts if a.get("do") == "spawn"]
        self.assertEqual([(a["cwd"], a["session_id"], a["resume"]) for a in spawn], [("/new/ads", "s-a", True)])

    HUB = {"channel": "session", "owner": False, "sender": "hub"}

    def give_task(self):
        self.ev("delegation.created", None, session="ads", context="do X", **self.HUB)
        return next(m["id"] for m in self.st["messages"].values() if m["session"] == "ads")

    def finish_turn(self, mid):
        self.ev("turn.started", "ads", marker=mid)
        self.ev("turn.result", "ads", text="asked the hub to move the folder")
        self.ev("turn.stopped", "ads")
        for m in [m for m in self.st["messages"].values() if m["session"] == "ads" and m["state"] == "typing"]:
            self.ev("turn.started", "ads", marker=m["id"])   # the core's own follow-up ("write the lessons") also ran
            self.ev("turn.stopped", "ads")

    def mine(self, name="ads"):
        return [m for m in self.st["messages"].values() if m["session"] == name and m["origin"] == forms.ORIGIN_CORE]

    def test_a_move_asked_by_the_hub_tells_a_session_with_a_finished_turn_to_go_on(self):
        # 22.10: executor reported, asked the hub to move it; the task is printed and the turn is over: nobody
        # starts the next turn in the new folder
        self.finish_turn(self.give_task())
        self.ev("session.cwd_set", "ads", cwd="/new/ads", **self.HUB)
        msgs = [m for m in self.mine() if "/new/ads" in m["text"]]
        self.assertEqual(len(msgs), 1)

    def test_the_turn_after_the_move_is_the_turn_of_the_task_and_its_result_goes_to_the_asker(self):
        # the core's wake-up carries the task: without it the result of the continued work went nowhere
        self.finish_turn(self.give_task())
        did = next(iter(self.st["delegations"]))
        acts, _ = self.ev("session.cwd_set", "ads", cwd="/new/ads", **self.HUB)
        self.assertEqual([a["do"] for a in acts if a.get("do") in ("release", "spawn", "type")], ["release", "spawn"])
        (msg,) = [m for m in self.mine() if "/new/ads" in m["text"]]
        self.assertEqual(msg["for_delegation"], did)
        acts, _ = self.ev("session.started", "ads")   # printed into the new process, not the old one
        self.assertEqual([a["text"].split("]")[0] for a in acts if a.get("do") == "type"], [f"[hub:{msg['id']}{forms.ORIGIN_CORE}"])
        self.ev("host.done", "ads", do="type", message=msg["id"], attempt=1, result="sent")
        self.ev("turn.started", "ads", marker=msg["id"])
        self.ev("turn.result", "ads", text="done in the new folder")
        told = [m for m in self.st["messages"].values() if m["session"] == "hub" and "done in the new folder" in m["text"]]
        self.assertEqual(len(told), 1)

    def test_a_session_that_answers_nothing_to_continue_is_not_forwarded(self):
        # the word the core asks for, and the one earlier cores asked for (a session of an old conversation answers it)
        for word in (delegations.NOTHING, delegations.NOTHING_OLD):
            with self.subTest(word=word):
                self.setUp()
                self.finish_turn(self.give_task())
                self.ev("session.cwd_set", "ads", cwd="/new/ads", **self.HUB)
                (msg,) = [m for m in self.mine() if "/new/ads" in m["text"]]
                self.ev("session.started", "ads")
                self.ev("host.done", "ads", do="type", message=msg["id"], attempt=1, result="sent")
                self.ev("turn.started", "ads", marker=msg["id"])
                self.ev("turn.result", "ads", text=f"«{word}»")
                self.assertEqual([m for m in self.st["messages"].values() if m["session"] == "hub" and word in m["text"]],
                                 [])

    def test_a_sleeping_session_with_an_answered_task_is_not_woken(self):
        # a mass move of folders must not wake the sleepers whose task was answered long ago and left open
        self.finish_turn(self.give_task())
        self.ev("session.parked", "ads", channel="core", why="restart")
        self.assertEqual(self.st["sessions"]["ads"]["state"], "parked")
        acts, _ = self.ev("session.cwd_set", "ads", cwd="/new/ads", **self.HUB)
        self.assertEqual(([m for m in self.mine() if "/new/ads" in m["text"]], self.st["sessions"]["ads"]["state"],
                          [a for a in acts if a.get("do") == "spawn"]), ([], "parked", []))

    def test_an_old_answered_task_does_not_wake_an_idle_session(self):
        # the ladder of reminders (delegations.quiet) has this one; the move is not a reason to start a turn
        self.finish_turn(self.give_task())
        d = next(iter(self.st["delegations"].values()))
        d["answered_at"] = d["since"] = 100 - delegations.OPEN_NUDGE - 1
        self.ev("session.cwd_set", "ads", cwd="/new/ads", **self.HUB)
        self.assertEqual([m for m in self.mine() if "/new/ads" in m["text"]], [])

    def test_a_session_that_declared_what_it_waits_for_is_not_told_to_go_on(self):
        self.finish_turn(self.give_task())
        self.ev("session.waiting", "ads", text="CI of the branch", channel="session", owner=False, sender="ads")
        self.ev("session.cwd_set", "ads", cwd="/new/ads", **self.HUB)
        self.assertEqual([m for m in self.mine() if "/new/ads" in m["text"]], [])

    def test_no_second_message_while_the_cores_follow_up_after_the_result_is_being_typed(self):
        mid = self.give_task()
        self.ev("turn.started", "ads", marker=mid)
        self.ev("turn.result", "ads", text="asked the hub to move the folder")
        self.ev("turn.stopped", "ads")
        self.assertEqual([m["state"] for m in self.mine()], ["typing"])   # "write the lessons" is already in the process
        acts, _ = self.ev("session.cwd_set", "ads", cwd="/new/ads", **self.HUB)
        self.assertEqual(([m["state"] for m in self.mine()], [a["do"] for a in acts if a.get("do") in ("release", "spawn")]),
                         (["typing"], []))

    def test_a_working_task_without_a_result_is_not_asked_for_nothing_to_continue(self):
        # The "nothing to continue" result is swallowed only for a task already answered: for this one it would be the result
        mid = self.give_task()
        self.ev("turn.started", "ads", marker=mid)
        self.ev("turn.stopped", "ads")
        d = next(iter(self.st["delegations"].values()))
        self.assertEqual((d["state"], self.st["sessions"]["ads"]["state"]), ("working", "idle"))
        self.ev("session.cwd_set", "ads", cwd="/new/ads", **self.HUB)
        self.assertEqual([m for m in self.mine() if "/new/ads" in m["text"]], [])

    def test_a_busy_session_is_not_told_to_go_on(self):
        mid = self.give_task()
        self.ev("turn.started", "ads", marker=mid)
        self.ev("session.cwd_set", "ads", cwd="/new/ads", **self.HUB)
        self.assertEqual([m for m in self.mine() if "/new/ads" in m["text"]], [])

    def test_no_message_while_the_task_is_still_queued_or_typed(self):
        # the core prints the task into the new process: it starts the turn itself
        self.give_task()
        self.ev("session.cwd_set", "ads", cwd="/new/ads", **self.HUB)
        self.assertEqual(self.mine(), [])

    def test_a_sleeping_session_without_a_task_is_not_woken(self):
        self.ev("session.parked", "ads", channel="core", why="restart")
        self.assertEqual(self.st["sessions"]["ads"]["state"], "parked")
        acts, _ = self.ev("session.cwd_set", "ads", cwd="/new/ads", **self.HUB)
        self.assertEqual(self.st["sessions"]["ads"]["cwd"], "/new/ads")
        self.assertEqual((self.mine(), self.st["sessions"]["ads"]["state"], [a for a in acts if a.get("do") == "spawn"]),
                         ([], "parked", []))

    def test_an_idle_session_without_a_task_gets_no_message(self):
        self.ev("session.cwd_set", "ads", cwd="/new/ads", **self.HUB)
        self.assertEqual(self.mine(), [])

    def test_no_message_for_the_same_folder_a_rejected_move_or_the_owner(self):
        self.ev("session.cwd_set", "ads", cwd="/old/ads", channel="session", owner=False, sender="hub")
        self.ev("session.cwd_set", "ads", cwd="rel", channel="session", owner=False, sender="hub")
        self.ev("session.cwd_set", "ads", cwd="/x", channel="session", owner=False, sender="seo")
        self.ev("session.cwd_set", "ads", cwd="/new/ads", **self.owner)
        self.assertEqual([m for m in self.st["messages"].values() if m["session"] == "ads"], [])

    def test_the_hub_session_may_move_it(self):
        _, why = self.ev("session.cwd_set", "ads", cwd="/new/ads", channel="session", owner=False, sender="hub")
        self.assertIsNone(why)

    def test_other_sessions_and_the_agent_itself_may_not(self):
        for sender in ("seo", "ads"):
            _, why = self.ev("session.cwd_set", "ads", cwd="/x", channel="session", owner=False, sender=sender)
            self.assertIsNotNone(why)
        self.assertEqual(self.st["sessions"]["ads"]["cwd"], "/old/ads")

    def test_not_a_session_with_its_own_worktree(self):
        self.st["sessions"]["ads"]["worktree"] = {"path": "/old/ads/.claude/worktrees/x", "branch": "task/x"}
        _, why = self.ev("session.cwd_set", "ads", cwd="/new/ads", **self.owner)
        self.assertIn("its own working copy", why or "")
        self.assertEqual(self.st["sessions"]["ads"]["cwd"], "/old/ads")

    def test_a_message_typed_to_the_old_process_is_not_cut_by_the_move(self):
        # 24.10: the task was typed, the hub moved the folder before turn.started reached the journal; the core
        # released the idle-looking process in the middle of its turn, the new one resumed a conversation that ended
        # in a WebFetch call without a result and sat idle
        self.ev("session.requested", "exe", cwd="/old/exe", session_id="s-e", **self.owner)
        self.ev("session.started", "exe")
        self.ev("message.received", None, to="exe", text="the task", **self.owner)
        mid = next(m["id"] for m in self.st["messages"].values() if m["session"] == "exe")
        self.assertEqual(self.st["messages"][mid]["state"], "typing")
        acts, _ = self.ev("session.cwd_set", "exe", cwd="/new/exe", channel="session", owner=False, sender="hub")
        acts += self.ev("host.done", "exe", do="type", message=mid, attempt=1, result="sent")[0]
        self.assertEqual([a["do"] for a in acts if a.get("do") in ("release", "spawn")], [])
        self.ev("turn.started", "exe", marker=mid)
        self.assertEqual(self.st["sessions"]["exe"]["state"], "busy")
        acts, _ = self.ev("turn.result", "exe", text="done")
        spawn = [a for a in acts if a.get("do") == "spawn"]
        self.assertEqual([(a["cwd"], a["resume"]) for a in spawn], [("/new/exe", True)])

    def test_a_session_without_any_message_has_no_conversation_to_resume(self):
        self.ev("session.requested", "exe", cwd="/old/exe", session_id="s-e", **self.owner)
        self.ev("session.started", "exe")
        spawn = [a for a in self.ev("session.cwd_set", "exe", cwd="/new/exe", **self.owner)[0] if a.get("do") == "spawn"]
        self.assertEqual([a["resume"] for a in spawn], [False])

    def test_only_an_absolute_path(self):
        _, why = self.ev("session.cwd_set", "ads", cwd="new/ads", **self.owner)
        self.assertIn("an absolute path", why or "")


if __name__ == "__main__":
    unittest.main()
