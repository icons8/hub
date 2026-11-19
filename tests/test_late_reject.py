"""A core rejection that the CLI did not wait for still reaches the session (2026-10-06: under load `hub schedule rm`
answered "in the spool, the core has not written it yet", a second later the core rejected it because the job belongs
to someone else, and the session never learned of it: "the core silently ignored it")."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from core import reducer

OWNER = {"channel": "cli", "owner": True}


class LateReject(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        for n in ("maps", "devops"):
            self.ev("session.requested", n, cwd="/w", session_id=f"s-{n}", **OWNER)
            self.ev("session.started", n)

    def ev(self, kind, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": kind,
                                                   "entity": entity, "data": data})
        return why

    def notes(self, name):
        return [m["text"] for m in self.st["messages"].values() if m["session"] == name]

    def reject(self, req):
        why = "the job is not yours"
        self.ev("core.rejected", "job-1", of=self.seq, type="schedule.remove_requested", reason=why, req=req,
                sender="devops")

    def test_rejection_after_the_cli_gave_up_reaches_the_session(self):
        self.reject("r1")
        self.assertEqual(self.notes("devops"), [])
        self.ev("cli.gave_up", None, req="r1", what="schedule.remove_requested job-1", channel="session",
                owner=False, sender="devops")
        (text,) = self.notes("devops")
        self.assertIn("schedule.remove_requested job-1", text)
        self.assertIn("the job is not yours", text)

    def test_either_order(self):
        self.ev("cli.gave_up", None, req="r2", what="schedule.remove_requested job-1", channel="session",
                owner=False, sender="devops")
        self.reject("r2")
        self.assertEqual(len(self.notes("devops")), 1)

    def test_accepted_command_stays_quiet(self):
        self.ev("cli.gave_up", None, req="r3", what="schedule.remove_requested job-1", channel="session",
                owner=False, sender="devops")
        self.assertEqual(self.notes("devops"), [])


if __name__ == "__main__":
    unittest.main()
