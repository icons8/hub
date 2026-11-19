"""The owner stops an executor (page button "Stop executor", `hub session stop`): the process tree is killed at once,
even in the middle of a long step (no interrupt: after an interrupt claude reads what was inserted and takes a new
turn on it before it exits); what was typed or inserted and not read yet is withdrawn ("stopped by owner") and is
not delivered by the next turn; the session carries `stopped` ({ts, by}) until it is woken; whoever raised it (the
mini-hub) learns "stopped by the owner", not "the turn broke off with an error".

The replay is of a real case: a mini-hub inserted "STOP" into its executor in the middle of a 10-minute Bash step,
the executor read it only after the step was cut, and took a new turn on it before its process went away."""
import tests  # noqa: F401 — ignore this machine's settings
import unittest

from core import executors, minihub, reducer, views

OWNER = {"channel": "web", "owner": True}


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


class Stop(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions, self.events = reducer.empty(), 0, [], []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", channel="cli", owner=True)
        self.ev("session.started", "hub")
        self.ev("session.requested", "notes", cwd="/notes", session_id="s-notes", role="agent", about="notes",
                channel="cli", owner=True)
        self.ev("session.started", "notes")
        self.ev("owner.settings", None, timezone="UTC", parallel=3, channel="core")
        self.ev("session.folder", "notes", src="core", channel="core", git="ok", dirty=0)
        for a in self.actions:
            if a.get("do") == "spawn":
                self.ev("session.started", a["session"])
        if self.st["sessions"]["notes"]["state"] == "parked":
            self.ev("session.wake_requested", "notes", channel="cli", owner=True)
            self.ev("session.started", "notes")
        self.assertTrue(minihub.on(self.st, self.st["sessions"]["notes"]))
        # the mini-hub got a task and raised an executor for it
        self.ev("delegation.created", None, session="notes", for_session="docs", context="T", **by("docs"))
        self.ev("turn.started", "notes", marker=f"m{self.seq}")
        self.assertIsNone(self.ev("task.run_requested", "notes-ci", task="merge the branch", **by("notes")))
        self.ev("turn.result", "notes", text="assigned notes-ci")
        self.ev("turn.stopped", "notes")
        run = self.st["runs"]["notes-ci"]
        for type, entity, data in executors.events_for(self.st, "notes-ci", run, cwd="/x/.claude/worktrees/ci",
                                                       worktree=None):
            self.assertIsNone(self.ev(type, entity, **data), (type, data))
        self.ev("session.started", "notes-ci")
        (self.sub,) = [d for d in self.st["delegations"].values() if d["session"] == "notes-ci"]
        self.ev("turn.started", "notes-ci", marker=self.sub["message"])
        # one long foreground step: no result until it is cut
        self.ev("turn.tool", "notes-ci", id="t1", name="Bash", input={"command": "for i in $(seq 1 23); do sleep 25; done"})

    def ev(self, type, entity=None, ts=None, src=None, **data):
        self.seq += 1
        e = {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type, "entity": entity, "data": data,
             **({"src": src} if src else {})}
        self.events.append(e)
        self.st, acts, why = reducer.reduce(self.st, e)
        self.actions += acts
        return why

    def soft_stop(self):
        """The mini-hub's `hub send` "STOP": inserted into stdin mid-turn at once, Claude Code will read it only after the step."""
        self.assertIsNone(self.ev("message.received", None, to="notes-ci", text="STOP, interrupt the step", **by("notes")))
        msg = self.st["messages"][f"m{self.seq}"]
        self.assertTrue(msg.get("injected"))
        self.assertEqual(msg["state"], "delivered")
        return msg["id"]

    def msg(self, mid):
        return self.st["messages"][mid]   # the fold returns a new state: read the message anew after each event

    def test_owner_stop_mid_step_kills_the_tree_and_withdraws_the_unread_insertion(self):
        mid = self.soft_stop()
        self.actions.clear()
        self.assertIsNone(self.ev("session.stop_requested", "notes-ci", ts=5000, **OWNER))
        sess = self.st["sessions"]["notes-ci"]
        dos = [(a["do"], a.get("now")) for a in self.actions if a.get("session") == "notes-ci"]
        # the process is ended at once (release now: terminated, its exit waited for in the host's background), not
        # interrupt + release: after an interrupt claude reads the insertion and takes a turn on it
        self.assertEqual(dos, [("release", True), ("end_tree", None)])   # its commands are not left as orphans
        self.assertEqual(sess["state"], "retired")
        self.assertEqual(sess.get("stopped"), {"ts": 5000, "by": "owner"})
        self.assertEqual(self.msg(mid)["state"], "failed")
        self.assertEqual(self.msg(mid)["reason"], "stopped by owner")
        # nothing waits for the next turn of this session: no delivered-but-unread line, no queue
        self.assertFalse([m for m in self.st["messages"].values() if m["session"] == "notes-ci"
                          and m["state"] in ("queued", "typing")])
        # the mini-hub learns that the owner stopped it, not that the turn broke off with an error
        (note,) = [m for m in self.st["messages"].values() if m["session"] == "notes"
                   and "STOPPED" in str(m["text"]) and self.sub["id"] in str(m["text"])]
        self.assertIn("stopped by the owner", note["text"])
        self.assertNotIn("with an error", note["text"])
        # the mini-hub raised it, so it may wake it itself (reducer._wake_who): the note does not send it to the hub
        self.assertIn("(the owner, the hub or whoever raised it)", note["text"])

    def test_the_wake_hint_names_the_raiser_only_where_its_wake_is_taken(self):
        """A direction (an executor with a backlog) is woken only by the hub or the owner (reducer._wake_who), and a
        session that set the task without raising the executor is not its raiser: the note does not promise them a wake."""
        sess = {"raised_by": "notes"}
        self.assertEqual(executors.wakers(sess, "notes"), "(the owner, the hub or whoever raised it)")
        self.assertEqual(executors.wakers(sess, "docs"), "(hub/owner)")
        self.assertEqual(executors.wakers({**sess, "backlog": "notes-ci"}, "notes"), "(hub/owner)")

    def test_a_late_turn_on_the_withdrawn_insertion_does_not_deliver_it(self):
        # the killed process may still have written the start of a turn on the insertion (hooks run in parallel): the
        # message stays withdrawn and the session stays stopped
        mid = self.soft_stop()
        self.ev("session.stop_requested", "notes-ci", **OWNER)
        self.ev("turn.started", "notes-ci", marker=mid, prompt_head="(urgent — arrived in the middle of your turn)")
        self.assertEqual(self.msg(mid)["state"], "failed")
        self.assertEqual(self.st["sessions"]["notes-ci"]["state"], "retired")

    def test_an_insertion_already_read_is_not_withdrawn(self):
        mid = self.soft_stop()
        self.ev("turn.started", "notes-ci", marker=mid, prompt_head="(urgent — arrived in the middle of your turn)")
        self.ev("session.stop_requested", "notes-ci", **OWNER)
        self.assertEqual(self.msg(mid)["state"], "delivered")
        self.assertNotIn("reason", self.msg(mid))

    def test_a_line_typed_to_a_free_one_stays_its_own_as_before(self):
        # not an insertion: a free executor got the next message typed into stdin, its turn has not started; the killed
        # process will not read it, and it comes again once the executor is woken (as before this change)
        self.ev("turn.result", "notes-ci", text="interim: waiting for the run")
        self.ev("turn.stopped", "notes-ci")
        self.assertIsNone(self.ev("message.received", None, to="notes-ci", text="and also check the log", **by("hub")))
        mid = f"m{self.seq}"
        self.assertEqual(self.msg(mid)["state"], "typing")
        self.ev("session.stop_requested", "notes-ci", **OWNER)
        self.assertEqual((self.msg(mid)["session"], self.msg(mid)["state"]), ("notes-ci", "queued"))

    def test_an_fyi_that_rode_with_the_insertion_is_withdrawn_with_it(self):
        self.assertIsNone(self.ev("message.received", None, to="notes-ci", text="FYI: CI is green", fyi=True,
                                  **by("hub")))
        fyi = f"m{self.seq}"
        self.assertEqual(self.msg(fyi)["state"], "aside")
        mid = self.soft_stop()
        self.assertEqual(self.msg(fyi)["state"], "delivered")   # rode in the same input
        self.ev("session.stop_requested", "notes-ci", **OWNER)
        self.assertEqual([self.msg(m)["state"] for m in (mid, fyi)], ["failed", "failed"])

    def test_a_sleeping_raiser_is_not_woken_for_the_note(self):
        self.ev("turn.result", "notes-ci", text="done")
        self.ev("turn.stopped", "notes-ci")
        self.st["sessions"]["notes"]["state"] = "asleep"   # the mini-hub went to sleep meanwhile
        before = set(self.st["messages"])
        self.ev("session.stop_requested", "notes-ci", **OWNER)
        self.assertEqual([m for k, m in self.st["messages"].items() if k not in before and m["session"] == "notes"], [])
        self.assertEqual(self.st["sessions"]["notes"]["state"], "asleep")

    def test_an_insertion_read_inside_the_turn_without_its_mark_is_not_withdrawn_later(self):
        # review: an insertion does not always get a turn with its mark — Claude Code read it inside the turn; once the
        # turn ended it is not "unread", and a stop much later does not withdraw it
        mid = self.soft_stop()
        self.ev("turn.result", "notes-ci", text="did this too")
        self.ev("turn.stopped", "notes-ci")
        self.assertNotIn("inserted_unread", self.st["sessions"]["notes-ci"])
        self.ev("session.stop_requested", "notes-ci", **OWNER)
        self.assertEqual(self.msg(mid)["state"], "delivered")

    def test_insertions_to_the_hub_are_unread_until_its_turn_ends_and_it_is_not_stopped(self):
        # slice 4: every session shows "inserted, not read yet"; the list ends with the turn, so it does not grow,
        # and a stop at once is still only for an executor
        self.ev("turn.started", "hub", marker=None, prompt_head="work")
        self.assertIsNone(self.ev("message.received", None, to="hub", text="urgent", now=True, channel="cli", owner=True))
        mid = f"m{self.seq}"
        self.assertTrue(self.msg(mid).get("injected"))
        self.assertEqual(self.st["sessions"]["hub"]["inserted_unread"], [mid])
        self.assertIsNotNone(self.ev("session.stop_requested", "hub", **OWNER))
        self.assertEqual(self.msg(mid)["state"], "delivered")
        self.ev("turn.result", "hub", text="done")
        self.ev("turn.stopped", "hub")
        self.assertNotIn("inserted_unread", self.st["sessions"]["hub"])

    def test_removing_a_free_executor_sends_no_stop_note(self):
        self.ev("turn.result", "notes-ci", text="done")
        self.ev("turn.stopped", "notes-ci")
        before = set(self.st["messages"])
        self.assertIsNone(self.ev("session.remove_requested", "notes-ci", **OWNER))
        self.assertEqual(self.st["sessions"]["notes-ci"]["state"], "removed")
        new = [m for k, m in self.st["messages"].items() if k not in before and "STOPPED" in str(m["text"])]
        self.assertEqual(new, [])

    def test_stop_by_the_raiser_is_marked_as_its_own(self):
        mid = self.soft_stop()
        self.ev("session.stop_requested", "notes-ci", ts=6000, **by("notes"))
        self.assertEqual(self.st["sessions"]["notes-ci"]["stopped"], {"ts": 6000, "by": "notes"})
        self.assertEqual(self.msg(mid)["reason"], "session stopped")
        notes = [m for m in self.st["messages"].values() if m["session"] == "notes" and "STOPPED" in str(m["text"])]
        self.assertTrue(notes)
        self.assertNotIn("by the owner", notes[-1]["text"])

    def test_waking_clears_the_mark_and_raises_the_same_conversation(self):
        self.ev("session.stop_requested", "notes-ci", **OWNER)
        sid = self.st["sessions"]["notes-ci"]["session_id"]
        self.actions.clear()
        self.assertIsNone(self.ev("message.received", None, to="notes-ci", text="continue", wake=True, **OWNER))
        sess = self.st["sessions"]["notes-ci"]
        self.assertEqual(sess["state"], "starting")
        (spawn,) = [a for a in self.actions if a["do"] == "spawn"]
        self.assertTrue(spawn.get("resume"))
        self.assertEqual(spawn["session_id"], sid)
        self.assertNotIn("stopped", sess)

    def test_stop_of_an_executor_without_an_open_task_still_tells_the_raiser(self):
        self.ev("turn.result", "notes-ci", text="done")
        self.ev("turn.stopped", "notes-ci")
        self.assertNotIn(self.st["delegations"][self.sub["id"]]["state"], ("working", "waiting-owner"))
        self.ev("session.stop_requested", "notes-ci", **OWNER)
        notes = [m for m in self.st["messages"].values() if m["session"] == "notes" and "notes-ci" in str(m["text"])
                 and "stopped by the owner" in str(m["text"])]
        self.assertEqual(len(notes), 1)
        self.assertIn("(the owner, the hub or whoever raised it)", notes[0]["text"])   # the raiser wakes it itself
        self.assertNotIn("hub", [m["session"] for m in self.st["messages"].values() if "STOPPED: notes-ci" in str(m["text"])])


    def test_the_timeline_says_who_stopped_it_at_which_step_and_the_step_is_no_longer_running(self):
        self.soft_stop()
        self.ev("session.stop_requested", "notes-ci", ts=self.events[-1]["ts"] + 400, **OWNER)
        r = views.Reader()
        r.feed_all(self.events)
        line = r.timelines["notes-ci"]
        (row,) = [x for x in line if x.get("code") == "stopped"]
        self.assertTrue(row["command"])   # "Brief" shows it too
        self.assertIn("Stopped by you — the turn was cut at the Bash step (6 min)", row["text"])
        self.assertIn("1 message inserted in the middle of the turn withdrawn", row["text"])
        (step,) = [x for x in line if x.get("kind") == "tool"]
        # the stop also wrote to the mini-hub: its row is the last one produced, and still the executor's step is stopped
        self.assertEqual(step["status"], "stopped")
        snap = {s["name"]: s for s in r.snapshot()["sessions"]}
        self.assertEqual(snap["notes-ci"]["stopped"]["by"], "owner")
        self.assertEqual((snap["notes-ci"]["state"], snap["notes-ci"]["retired"]), ("asleep", True))


if __name__ == "__main__":
    unittest.main()
