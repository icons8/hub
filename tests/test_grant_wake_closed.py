"""The "Connected: X, continue task dN" message after a schedule script grants tools.

The schedule script granted two sessions a batch of tools (owner, schedule_job, without a request). Each grant took
effect with its own restart, and for each the core woke the session with the text of its last task: for one, a task
already handed in and closed seconds later; for the other, the same task three times in a row. The restart with the
tool is needed, a turn for it is not; a grant by the hub or the owner, and one the session asked for, work as before."""
import tests  # noqa: F401 — keeps the machine's settings out
import unittest

from core import forms, reducer

OWNER = {"channel": "cli", "owner": True}
SYNC = {**OWNER, "schedule_job": "nightly-sync"}   # a grant by a schedule job script
TOOLS = {t: {"kind": "mcp", "risk": [], "server": {}} for t in ("buffer", "figma", "teams", "jira")}


def by(name):
    return {"channel": "session", "owner": False, "sender": name}


class GrantWake(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("catalog.loaded", None, tools=TOOLS)
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.started", "hub", session_id="s-hub")
        self.ev("session.requested", "des", cwd="/d", session_id="s-des", role="agent", **OWNER)
        self.ev("session.started", "des", session_id="s-des")
        self.assertIsNone(self.ev("delegation.created", None, session="des", for_session="hub",
                                  context="style preview", **by("hub")))
        self.did = f"d{self.seq}"
        self.mid = self.st["delegations"][self.did]["message"]

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def core(self):
        return [m for m in self.st["messages"].values()
                if m["session"] == "des" and m.get("origin") == forms.ORIGIN_CORE and m["text"].startswith("Connected")]

    def wakes(self):
        """Core connection messages that wake a turn (not "for your information")."""
        return [m for m in self.core() if not m.get("fyi") and m["state"] != "aside"]

    def turn(self, marker, result="done", inside=()):
        self.ev("turn.started", "des", marker=marker, prompt_head="x")
        for e in inside:
            self.ev(*e[:2], **e[2])
        self.ev("turn.result", "des", text=result)
        self.ev("turn.stopped", "des")

    def restart(self):
        self.ev("session.started", "des", session_id="s-des", source="resume")

    def test_sync_grants_do_not_resume_the_task(self):
        """The schedule script grants tools: a restart with them, without "continue the task", though the delegation is open."""
        for tool in ("buffer", "figma", "teams"):
            self.ev("tools.granted", "des", tool=tool, **SYNC)
            self.restart()
        self.assertEqual(sorted(t for t in self.st["sessions"]["des"]["tools"] if t in TOOLS),
                         ["buffer", "figma", "teams"])
        self.assertEqual(self.core(), [])
        self.assertEqual(self.st["delegations"][self.did]["state"], "working")

    def test_sync_grant_after_the_answer_does_not_resume_the_task(self):
        self.turn(self.mid, "handed in: the preview is there")
        self.ev("tools.granted", "des", tool="buffer", **SYNC)
        self.restart()
        self.assertIn("buffer", self.st["sessions"]["des"]["tools"])
        self.assertEqual(self.core(), [])

    def test_a_requested_tool_granted_by_sync_still_resumes(self):
        """The session asked for it itself: a grant by the script is still "connected, continue"."""
        self.turn(self.mid, "waiting for jira", inside=[("tools.requested", "des", {"tool": "jira", "why": "the task",
                                                                            **by("des")})])
        self.ev("tools.granted", "des", tool="jira", **SYNC)
        self.restart()
        self.assertEqual(len(self.core()), 1)

    def test_an_owner_grant_for_an_open_task_still_resumes_it(self):
        """The owner granted it while the delegation is open: still "Connected, continue" with the task text."""
        self.turn(self.mid, "waiting for jira")
        self.ev("tools.granted", "des", tool="jira", **OWNER)
        self.restart()
        wakes = self.core()
        self.assertEqual(len(wakes), 1)
        self.assertIn(self.did, wakes[0]["text"])
        self.assertIn("style preview", wakes[0]["text"])


if __name__ == "__main__":
    unittest.main()
