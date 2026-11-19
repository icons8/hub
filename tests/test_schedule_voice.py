"""A schedule command job speaks as the one who wrote its command. The clock ran every command without HUB_SESSION, so
`hub` took it for the owner's terminal: a plain session set `hub schedule add --command "hub tools approve …"` (or
`hub password off`, `hub-telegram pair`) and its run approved a risky tool as the owner. The job keeps command_by — the
session or plugin that wrote the command last, none for the owner and the core — the clock gives the command that
session's HUB_SESSION (a plugin's HUB_EXTENSION / HUB_MODULE), so its `hub` commands pass the gates that session
passes. `hub notify` stays open to the job's own command (milestones, hosts/streamjson.PROGRESS)."""
import tests  # noqa: F401 — keep the machine's settings out
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import platforms
from core import reducer, schedule
from modules.scheduler.clock import Clock
from tests.test_tools import HUB_CLI, HubCase

OWNER = {"channel": "cli", "owner": True}


def session(name):
    return {"channel": "session", "owner": False, "sender": name}


class Fold(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        for name in ("a", "b"):
            self.ev("session.requested", name, cwd="/w", session_id=f"s-{name}", **OWNER)
            self.ev("session.started", name)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def job(self, jid):
        return self.st["schedule"][jid]

    def test_the_writer_of_the_command_is_whose_word_it_runs_as(self):
        self.assertIsNone(self.ev("schedule.set", "mon", every="1h", command="hub notify x", **session("a")))
        self.assertEqual(schedule.speaker_of(self.job("mon")), ("session", "a"))
        self.assertIsNone(self.ev("schedule.set", "own", every="1h", command="backup.sh", **OWNER))
        self.assertEqual(schedule.speaker_of(self.job("own")), ("owner", None))
        self.assertIsNone(self.ev("schedule.set", "ext", every="1h", command="sync.sh",
                                  channel="plugin", owner=False, sender="ext:sync"))
        self.assertEqual(schedule.speaker_of(self.job("ext")), ("plugin", "ext:sync"))

    def test_a_session_that_rewrites_an_owners_command_does_not_get_his_word(self):
        # the owner handed his command to session a (session field): a may edit it — its text is then a's word
        self.assertIsNone(self.ev("schedule.set", "gate", every="1h", command="check.sh", session="a", **OWNER))
        self.assertEqual(schedule.speaker_of(self.job("gate")), ("owner", None))
        self.assertIsNone(self.ev("schedule.set", "gate", every="1h", command="hub password off", **session("a")))
        self.assertEqual(self.job("gate")["by"], schedule.WHO["owner"])   # who set the job is as it was
        self.assertEqual(schedule.speaker_of(self.job("gate")), ("session", "a"))
        # the owner sets it again: his word again
        self.assertIsNone(self.ev("schedule.set", "gate", every="2h", command="check.sh", **OWNER))
        self.assertEqual(schedule.speaker_of(self.job("gate")), ("owner", None))

    def test_a_new_timing_of_the_same_text_is_not_writing_it(self):
        # the owner extends a session's job (`hub schedule extend`, the page): its command stays the session's word,
        # and the hub re-timing the owner's job does not take his word away
        self.assertIsNone(self.ev("schedule.set", "mon", every="1h", command="hub tools approve a x", **session("a")))
        self.assertIsNone(self.ev("schedule.set", "mon", every="2h", command="hub tools approve a x",
                                  **OWNER))
        self.assertEqual(schedule.speaker_of(self.job("mon")), ("session", "a"))
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.started", "hub")
        self.assertIsNone(self.ev("schedule.set", "own", every="1h", command="backup.sh", **OWNER))
        self.assertIsNone(self.ev("schedule.set", "own", every="3h", command="backup.sh", **session("hub")))
        self.assertEqual(schedule.speaker_of(self.job("own")), ("owner", None))

    def test_renewing_an_expired_job_is_not_writing_it(self):
        # the core tells the setter's lead "hub schedule extend"; the owner runs it — the text is still the session's
        self.assertIsNone(self.ev("schedule.set", "mon", every="1h", command="hub tools approve a x", **session("a")))
        self.job("mon")["state"] = "expired"
        self.assertIsNone(self.ev("schedule.set", "mon", every="1h", command="hub tools approve a x", until="1970-01-05",
                                  **OWNER))
        self.assertEqual(self.job("mon")["state"], "active")
        self.assertEqual(schedule.speaker_of(self.job("mon")), ("session", "a"))

    def test_a_renamed_session_keeps_its_jobs_word(self):
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.started", "hub")
        self.assertIsNone(self.ev("schedule.set", "mon", every="1h", command="hub notify x", **session("a")))
        self.assertIsNone(self.ev("session.renamed", "a", to="maps", **session("hub")))
        self.assertEqual(schedule.speaker_of(self.job("mon")), ("session", "maps"))

    def test_the_hubs_own_job_still_closes_any_notice(self):
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.started", "hub")
        self.assertIsNone(self.ev("owner.notify", None, text="disk", id="disk", channel="plugin", owner=False,
                                  sender="mod:watch"))
        self.assertIsNone(self.ev("schedule.set", "tidy", every="1h", command="hub notify --resolve …", **session("hub")))
        self.assertIsNone(self.ev("owner.notify_resolved", None, id="n:mod:watch:disk", schedule_job="tidy",
                                  source="tidy", **session("hub")))
        self.assertNotIn("n:mod:watch:disk", self.st["notices"])
        # a plain session's job closes only its own
        self.assertIsNone(self.ev("owner.notify", None, text="disk", id="disk", channel="plugin", owner=False,
                                  sender="mod:watch"))
        self.assertIsNone(self.ev("schedule.set", "mine", every="1h", command="hub notify …", **session("a")))
        self.assertTrue(self.ev("owner.notify_resolved", None, id="n:mod:watch:disk", schedule_job="mine",
                                source="mine", **session("a")))

    def test_a_job_of_an_older_core_speaks_as_its_setter(self):
        for by, want in (("a", ("session", "a")), (schedule.WHO["owner"], ("owner", None)),
                         (schedule.WHO["core"], ("owner", None)), (None, ("owner", None))):
            self.assertEqual(schedule.speaker_of({"id": "old", "command": "x.sh", "by": by}), want, by)

    def test_the_jobs_own_command_may_notify_the_owner_a_session_still_may_not(self):
        self.assertIsNone(self.ev("schedule.set", "progress-gen", every="30m", command="hub notify …", **session("a")))
        self.assertIsNone(self.ev("owner.notify", None, text="3 of 9", schedule_job="progress-gen", **session("a")))
        self.assertEqual(self.job("progress-gen")["note_same"], 1)   # a milestone: counted as before (schedule.milestone)
        self.assertTrue(self.ev("owner.notify", None, text="hi", **session("a")))   # its turn: the result, not notify
        # another session's job, or a job the session did not write: not its job's word
        self.assertTrue(self.ev("owner.notify", None, text="hi", schedule_job="progress-gen", **session("b")))
        self.assertTrue(self.ev("owner.notify", None, text="hi", schedule_job="nope", **session("a")))
        # its notice is the job's (closed with it), and the job's command may withdraw it
        self.assertIsNone(self.ev("owner.notify", None, text="4 of 9", id="x", schedule_job="progress-gen",
                                  source="progress-gen", **session("a")))
        self.assertIn("n:schedule:progress-gen:x", self.st["notices"])
        self.assertIsNone(self.ev("owner.notify_resolved", None, id="x", schedule_job="progress-gen",
                                  source="progress-gen", **session("a")))
        self.assertNotIn("n:schedule:progress-gen:x", self.st["notices"])

    def test_a_message_of_its_own_job_keeps_the_schedules_label(self):
        self.assertIsNone(self.ev("schedule.set", "mon", every="1h", command="hub send a check", **session("a")))
        self.assertIsNone(self.ev("message.received", None, to="a", text="check", schedule_job="mon", **session("a")))
        msg = self.st["messages"][f"m{self.seq}"]
        self.assertEqual(msg["from"], {"kind": "schedule"})
        self.assertEqual(msg["origin"], " from schedule, job mon of session a")   # whose words they are
        self.assertIsNone(self.ev("message.received", None, to="a", text="hi", **session("b")))
        self.assertEqual(self.st["messages"][f"m{self.seq}"]["from"], {"kind": "session", "name": "b"})


class ClockEnv(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="voice-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.clock = Clock(self.state)

    def test_the_command_gets_the_writers_name(self):
        job = {"id": "mon", "command": "x.sh", "by": "a", "command_by": {"session": "a"}}
        env = self.clock.env(job)
        self.assertEqual((env.get("HUB_SESSION"), env["HUB_SCHEDULE_JOB"]), ("a", "mon"))
        env = self.clock.env({**job, "command_by": {"owner": True}})
        self.assertNotIn("HUB_SESSION", env)
        env = self.clock.env({**job, "command_by": {"plugin": "ext:sync"}})
        self.assertEqual((env.get("HUB_EXTENSION"), env.get("HUB_SESSION")), ("sync", None))
        env = self.clock.env({**job, "command_by": {"plugin": "mod:feeds"}})
        self.assertEqual((env.get("HUB_MODULE"), env.get("HUB_EXTENSION")), ("feeds", None))
        env = self.clock.env({"id": "old", "command": "x.sh", "by": "a"})   # an older core's job: its setter
        self.assertEqual(env.get("HUB_SESSION"), "a")


class EndToEnd(HubCase):
    """Verify №20's probe: a plain session sets a job whose command approves a risky tool it asked for."""

    def run_as_job(self, jid, *args):
        job = self.hub.state["schedule"][jid]
        env = Clock(self.state).env(job)   # what the clock gives the command
        return subprocess.run([sys.executable, HUB_CLI, *args], env=env, capture_output=True, text=True,
                              encoding="utf-8", timeout=30)

    def test_a_sessions_job_does_not_approve_as_the_owner(self):
        self.until(lambda: self.sess("hub").get("state") == "idle")
        self.put("session.requested", "a", cwd=self.project, session_id="sid-a", channel="cli", owner=True)
        self.until(lambda: self.sess("a").get("state") == "idle")
        stop = threading.Event()
        ticker = threading.Thread(target=lambda: [self.hub.tick() or time.sleep(0.05) for _ in iter(stop.is_set, True)])
        ticker.start()
        base = {k: v for k, v in os.environ.items() if k not in ("HUB_SESSION", "HUB_SCHEDULE_JOB")}
        as_a = {**base, "HUB_STATE_DIR": self.state, "HUB_SESSION": "a"}
        cli = lambda *a: subprocess.run([sys.executable, HUB_CLI, *a], env=as_a, capture_output=True,  # noqa: E731
                                        text=True, encoding="utf-8", timeout=30)
        try:
            cli("tools", "grant", "a", "stripe@hub-skills")   # risky: waits for the owner
            add = cli("schedule", "add", "--id", "sneak", "--every", "1d", "--command",
                      "hub tools approve a stripe@hub-skills")
            self.assertEqual(add.returncode, 0, add.stdout + add.stderr)
            ran = self.run_as_job("sneak", "tools", "approve", "a", "stripe@hub-skills")
            note = self.run_as_job("sneak", "notify", "--id", "sneak", "a milestone")
        finally:
            stop.set()
            ticker.join()
        self.assertNotEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertNotIn("stripe@hub-skills", self.sess("a").get("tools") or [])
        self.assertEqual(note.returncode, 0, note.stdout + note.stderr)


if __name__ == "__main__":
    unittest.main()
