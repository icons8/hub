"""A schedule command job (`hub schedule add --command`) runs in the owner's environment and without HUB_SESSION, but
its `hub send` is not the owner's word: a monitor wrote to a session with an open card, and the core closed the card
as the owner's answer (20.10). The job's message arrives labeled "from the schedule" but clears neither the card, nor
the form, nor the "waiting for you" mark; it is not urgent and not a Claude Code command; for a removed session it is
refused, rather than "to the hub, pass it on"."""
import tests  # noqa: F401 — keep the machine's settings out
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from core import forms, owner, reducer, spool

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")
OWNER = {"channel": "cli", "owner": True}
JOB = {"channel": "cli", "owner": False, "schedule_job": "mon"}   # this is how bin/hub files a command job's message
Q = {"questions": [{"question": "Take option A?", "options": [{"label": "Yes"}, {"label": "No"}]}]}


class CliMarksTheJob(unittest.TestCase):
    def cli(self, *args, **env):
        state = tempfile.mkdtemp(prefix="jobword-")
        self.addCleanup(shutil.rmtree, state, True)
        e = {k: v for k, v in os.environ.items()
             if k not in ("HUB_SESSION", "HUB_SCHEDULE_JOB", "HUB_EXTENSION", "HUB_MODULE")}
        r = subprocess.run([sys.executable, HUB, *args], capture_output=True, text=True,
                           env={**e, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0", **env})
        self.assertEqual(r.returncode, 0, r.stderr)
        (ev,) = [ev for _, ev in spool.take(os.path.join(state, "spool"))]
        return ev["data"]

    def send(self, **env):
        return self.cli("send", "hub", "new in the logs", **env)

    def test_a_command_job_names_itself_and_its_message_is_not_the_owners(self):
        data = self.send(HUB_SCHEDULE_JOB="mon")
        self.assertEqual((data["channel"], data["owner"], data.get("schedule_job")), ("cli", False, "mon"))

    def test_its_other_commands_stay_the_owners_word(self):
        # hub decided, restart, task run … from a job — still as from the owner's terminal (outside this change)
        data = self.cli("decided", "x", HUB_SCHEDULE_JOB="mon")
        self.assertEqual((data["channel"], data["owner"], data.get("schedule_job")), ("cli", True, "mon"))

    def test_the_owner_and_a_session_do_not(self):
        data = self.send()
        self.assertEqual((data["owner"], data.get("schedule_job")), (True, None))
        # a schedule script on behalf of a session (HUB_SESSION; a session job's pre-check, a watchdog that wrote for
        # it) is its word, not the owner's: the channel and sender are the same, and the job is named so that the core
        # does not take this for its turn
        data = self.send(HUB_SCHEDULE_JOB="mon", HUB_SESSION="w")
        self.assertEqual((data.get("channel"), data.get("owner"), data.get("sender"), data.get("schedule_job")),
                         ("session", False, "w", "mon"))
        data = self.send(HUB_SESSION="w")
        self.assertEqual((data.get("channel"), data.get("sender"), data.get("schedule_job")), ("session", "w", None))


class CoreDoesNotTakeItForTheOwner(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "dev", cwd="/d", session_id="s-dev", **OWNER)
        self.ev("session.started", "hub")
        self.ev("session.started", "dev")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def card(self, name):
        self.ev("turn.started", name, marker=None, prompt_head="x")
        self.ev("permission.requested", name, request_id="r1", tool="AskUserQuestion", interactive=True, input=Q)
        self.ev("turn.result", name, text="asked")
        self.ev("turn.stopped", name)
        return self.st["sessions"][name]["asks"][-1]["id"]

    def send(self, to, text, who=JOB):
        self.assertIsNone(self.ev("message.received", None, to=to, text=text, **who))
        return self.st["messages"][f"m{self.seq}"]

    def asks(self, name):
        return [a["id"] for a in self.st["sessions"][name].get("asks") or []]

    def test_a_job_message_leaves_the_card_open(self):
        q = self.card("dev")
        msg = self.send("dev", "new in the logs: 3 errors")
        self.assertEqual((msg["session"], msg.get("from_owner"), msg.get("answers_question")), ("dev", False, None))
        self.assertEqual(msg["text"], "new in the logs: 3 errors")
        self.assertIn(forms.ORIGIN_SCHEDULE, msg["origin"])
        self.assertEqual(msg.get("from"), {"kind": "schedule"})
        self.assertEqual(self.asks("dev"), [q])
        self.assertIn("q:dev:r1", [x[0] for x in owner.questions(self.st)])
        # the owner's words afterwards are an answer, as before
        msg = self.send("dev", "yes", who=OWNER)
        self.assertEqual((msg.get("answers_question"), self.asks("dev")), (q, []))

    def test_a_job_quoting_the_card_does_not_answer_it(self):
        q = self.card("dev")
        msg = self.send("hub", "Take option A? Yes")
        self.assertEqual((msg["session"], msg.get("answers_question")), ("hub", None))
        self.assertEqual(self.asks("dev"), [q])

    def test_a_job_message_does_not_answer_an_open_form(self):
        self.ev("turn.started", "dev", marker=None, prompt_head="x")
        self.ev("permission.requested", "dev", request_id="r1", tool="AskUserQuestion", interactive=False, input=Q)
        self.assertEqual(self.st["sessions"]["dev"]["state"], "dialog")
        self.actions = []
        msg = self.send("dev", "new in the logs")
        self.assertFalse(msg.get("answered_form"))
        self.assertFalse([a for a in self.actions if a.get("do") == "respond"])
        self.assertEqual(self.st["sessions"]["dev"]["state"], "dialog")

    def test_a_job_writing_to_a_removed_session_is_refused(self):
        # the owner's words would go to the hub as "pass it on to whoever needs it"; a monitor is not the owner:
        # refusal, and the command's exit code is recorded in the job
        self.ev("session.close_requested", "dev", **OWNER)
        self.ev("turn.result", "dev", text="HANDOFF-DONE\nDone: all")
        self.ev("session.remove_requested", "dev", **OWNER)
        self.assertIn("cannot be delivered", self.ev("message.received", None, to="dev", text="new", **JOB))
        self.assertIsNone(self.ev("message.received", None, to="dev", text="new", **OWNER))
        self.assertEqual(self.st["messages"][f"m{self.seq}"]["session"], "hub")

    def test_a_job_may_not_rush_or_type_commands(self):
        self.assertIn("--now", self.ev("message.received", None, to="dev", text="urgent", now=True, **JOB))
        msg = self.send("dev", "/compact")
        self.assertFalse(msg.get("command"))

    def test_a_job_writing_as_a_session_does_not_take_its_turn_chain(self):
        # a watchdog sends on behalf of dev (HUB_SESSION + HUB_SCHEDULE_JOB): it does not take dev's turn chain — dev's
        # turn on the owner's matter is not glued to the result of the watchdog's signal; dev's own word does take it
        self.ev("message.received", None, to="dev", text="remove the duplicates", **OWNER)
        self.ev("turn.started", "dev", marker=f"m{self.seq}")
        who = {"channel": "session", "owner": False, "sender": "dev", "schedule_job": "mon"}
        msg = self.send("hub", "new in the logs", who=who)
        self.assertFalse(msg.get("from_owner"))
        self.assertNotIn("root", msg)
        own = self.send("hub", "from dev itself", who={**who, "schedule_job": None})
        self.assertIn("root", own)

    def test_a_job_writing_as_a_session_does_not_take_its_turn_schedule_mark(self):
        # the "scheduled turn" mark is also a property of dev's turn: a script's message does not take it, just like
        # the chain
        self.ev("message.received", None, to="dev", text="remove the duplicates", **OWNER)
        self.ev("turn.started", "dev", marker=f"m{self.seq}")
        self.st["sessions"]["dev"]["turn_sched"] = True
        who = {"channel": "session", "owner": False, "sender": "dev", "schedule_job": "mon"}
        self.assertNotIn("via_schedule", self.send("hub", "new in the logs", who=who))
        self.assertTrue(self.send("hub", "from dev itself", who={**who, "schedule_job": None}).get("via_schedule"))

    def test_a_job_writing_as_a_session_does_not_hide_its_turn_result(self):
        # a watchdog writes to the hub on behalf of dev in the middle of its turn on the hub's task: that is not its
        # word in this turn — the core still relays the turn result to the hub (otherwise "wrote it itself" mutes it,
        # and the hub never sees the result)
        self.assertIsNone(self.ev("delegation.created", None, session="dev", for_session="hub", context="remove the duplicates",
                                  channel="session", owner=False, sender="hub"))
        (d,) = self.st["delegations"].values()
        self.ev("turn.started", "dev", marker=d["message"], prompt_head="x")
        self.send("hub", "new in the logs", who={"channel": "session", "owner": False, "sender": "dev",
                                                "schedule_job": "mon"})
        self.assertEqual(self.st["sessions"]["dev"].get("sent_in_turn"), [])
        self.ev("turn.result", "dev", text="duplicates removed")
        self.ev("turn.stopped", "dev")
        relayed = [m for m in self.st["messages"].values() if m["session"] == "hub" and "reply to task" in
                   (m.get("origin") or "") and "duplicates removed" in m["text"]]
        self.assertEqual(len(relayed), 1)

    def test_a_job_message_keeps_the_waiting_mark(self):
        self.st["sessions"]["dev"]["attention"] = {"since": 1000}
        self.send("dev", "new in the logs")
        self.assertIn("attention", self.st["sessions"]["dev"])


if __name__ == "__main__":
    unittest.main()
