"""Renaming a session (owner decision 2026-09-30; for example infra becomes the agent devops). The name is the
session's key in the core: its messages, assignments, schedule jobs and addresses move with it; the old name stays an
address (a Telegram reply to an old message still arrives). Only a sleeping session: a live process has the name in
its environment."""
import tests  # noqa: F401 — hide the machine's settings
import os
import subprocess
import sys
import tempfile
import unittest

from core import identity, reducer, spool
from core.views import Reader

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(REPO, "bin", "hub")
OWNER = {"channel": "cli", "owner": True}
FROM_HUB = {"channel": "session", "owner": False, "sender": "hub"}


def events():
    return [("session.requested", "hub", {"cwd": "/h", "session_id": "s-hub", "kind": "hub", **OWNER}),
            ("session.requested", "infra", {"cwd": "/k", "session_id": "s-k", **FROM_HUB}),
            ("session.requested", "other", {"cwd": "/o", "session_id": "s-o", **FROM_HUB}),
            ("session.started", "hub", {}), ("session.started", "infra", {}), ("session.started", "other", {}),
            ("message.received", None, {"to": "infra", "text": "hi", **OWNER}),
            ("schedule.set", "j1", {"session": "infra", "every": "2h", "prompt": "check", **FROM_HUB}),
            ("schedule.set", "j2", {"session": "hub", "every": "2h", "prompt": "to self", "channel": "session",
                                    "owner": False, "sender": "infra"})]


class Core(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.reader = reducer.empty(), 0, Reader()
        for t, name, d in events():
            self.ev(t, name, **d)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        event = {"seq": self.seq, "ts": self.seq, "type": type, "entity": entity, "data": data}
        self.st, _, why = reducer.reduce(self.st, event)
        self.reader.feed(event)
        return why

    def sleep(self, name):
        self.st["sessions"][name]["state"] = "asleep"
        self.reader.state["sessions"][name]["state"] = "asleep"

    def test_rename_moves_everything(self):
        self.sleep("infra")
        self.assertIsNone(self.ev("session.renamed", "infra", to="devops", **FROM_HUB))
        self.assertNotIn("infra", self.st["sessions"])
        s = self.st["sessions"]["devops"]
        self.assertEqual((s["name"], s["session_id"], s["was"]), ("devops", "s-k", ["infra"]))
        self.assertTrue(all(m["session"] != "infra" and m["to"] != "infra" for m in self.st["messages"].values()))
        self.assertTrue(any(m["session"] == "devops" for m in self.st["messages"].values()))
        # incident 30.09 14:16: a schedule record is itself the job (session/by) and has no job key: the core crashed
        self.assertEqual(self.st["schedule"]["j1"]["session"], "devops")
        self.assertEqual(self.st["schedule"]["j2"]["by"], "devops")
        # addresses: both the sessionId and the old name lead to the new one
        self.assertEqual(identity.resolve(self.st, "s-k"), "devops")
        self.assertEqual(identity.resolve(self.st, "infra"), "devops")
        self.assertIsNone(self.ev("message.received", None, to="infra", text="by the old name", **OWNER))
        self.assertTrue(any(m["session"] == "devops" and m["text"] == "by the old name"
                            for m in self.st["messages"].values()))
        # the page: the feed moved
        self.assertNotIn("infra", self.reader.timelines)
        self.assertTrue(self.reader.timelines.get("devops"))
        self.assertIn("devops", [x["name"] for x in self.reader.snapshot()["sessions"]])

    def test_a_late_result_under_the_old_name_reaches_the_task(self):
        # 05.10: the hub renamed an executor mid-turn; Stop arrived before the result, so the core renamed the idle
        # session, the result arrived under the old name and was lost: the task stayed "in progress", the owner got no
        # reply, the executor did not go to sleep
        self.ev("delegation.created", None, session="other", context="change the model", **FROM_HUB)
        d, mid = f"d{self.seq}", self.st["delegations"][f"d{self.seq}"]["message"]
        self.ev("turn.started", "other", marker=mid, prompt_head="x")
        self.ev("session.renamed", "other", to="other-model", **FROM_HUB)                 # busy: later
        self.ev("turn.stopped", "other")                                                   # idle: renamed
        self.assertIn("other-model", self.st["sessions"])
        self.ev("turn.result", "other", text="done: the model is changing", marker=mid)       # the result under the old name
        self.assertEqual(self.st["delegations"][d]["state"], "replied")
        self.assertTrue(any("done: the model" in m["text"] for m in self.st["messages"].values() if m["session"] == "hub"))

    def test_rejections(self):
        # a live idle session is renamed by a restart, but not with a message in the queue (tests/test_agent_file.py)
        self.assertIn("queue", self.ev("session.renamed", "infra", to="devops", **FROM_HUB))
        self.sleep("infra")
        self.assertIn("is taken", self.ev("session.renamed", "infra", to="other", **FROM_HUB))
        self.assertIn("name", self.ev("session.renamed", "infra", to="Dev Ops!", **FROM_HUB))
        self.assertIsNotNone(self.ev("session.renamed", "infra", to="devops", channel="session", owner=False,
                                     sender="other"))
        self.assertIsNotNone(self.ev("session.renamed", "nope", to="devops", **OWNER))
        self.assertIn("hub", self.ev("session.renamed", "hub", to="boss", **OWNER))
        self.assertIn("infra", self.st["sessions"])


class CoreSurvivesItsOwnBug(unittest.TestCase):
    """Incident 30.09 14:16: a bug in the fold code on one event (KeyError in _rename) crashed hubd and all modules on
    every journal read. A code failure on an event rejects that event instead of crashing the core."""

    def test_exception_in_reducer_rejects_the_event(self):
        st = reducer.empty()
        st, _, _ = reducer.reduce(st, {"seq": 1, "ts": 1, "type": "session.requested", "entity": "a",
                                       "data": {"cwd": "/w", "session_id": "s", **OWNER}})
        real = reducer._rename
        reducer._rename = lambda *a: {}["job"]
        try:
            bad = {"seq": 2, "ts": 2, "type": "session.renamed", "entity": "a", "data": {"to": "b", **OWNER}}
            after, actions, why = reducer.reduce(st, bad)
        finally:
            reducer._rename = real
        self.assertTrue(why.startswith("core failure on event session.renamed"), why)
        self.assertIn("KeyError", why)
        self.assertEqual((after["sessions"], actions, after["seq"]), (st["sessions"], [], 2))
        # a journal with such an event reads fine: the state is as without it
        self.assertIn("a", reducer.replay([{"seq": 1, "ts": 1, "type": "session.requested", "entity": "a",
                                           "data": {"cwd": "/w", "session_id": "s", **OWNER}},
                                          {"seq": 2, "ts": 2, "type": "schedule.due", "entity": None, "data": None}]
                                         )["sessions"])


class Cli(unittest.TestCase):
    def test_command(self):
        state = tempfile.mkdtemp(prefix="rename-")
        e = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0"}
        e.pop("HUB_SESSION", None)
        subprocess.run([sys.executable, HUB, "session", "rename", "infra", "devops"], env=e, capture_output=True)
        (ev,) = [ev for _, ev in spool.take(os.path.join(state, "spool"))]
        self.assertEqual((ev["type"], ev["entity"], ev["data"]["to"]), ("session.renamed", "infra", "devops"))


if __name__ == "__main__":
    unittest.main()
