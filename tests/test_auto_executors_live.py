"""A busy specialist end to end (owner decision 2026-10-05: if the sender is not the hub and the main process is busy,
the task is redirected to the hub): a real hubd, session processes (a fake claude), a specialist busy with a long turn;
another session's task goes to the hub for a decision, the hub hands it to the specialist's executor, the result comes
back to the session that set the task, and the executor that handed it in goes to sleep."""
import tests  # noqa: F401
import unittest
from unittest import mock

from core import executors
from tests import test_stream_hubd

HUB = {"channel": "session", "owner": False, "sender": "hub"}
OPS = {"channel": "session", "owner": False, "sender": "ops"}


class BusySpecialistLive(test_stream_hubd.StreamHubdTest):
    # only its own scenario: the StreamHubdTest tests already run in their own module
    locals().update({n: None for n in dir(test_stream_hubd.StreamHubdTest) if n.startswith("test_")})

    def test_a_task_to_a_busy_specialist_goes_through_the_hub_to_one_executor(self):
        with mock.patch.object(executors, "RETIRE_AFTER", 1):
            self.new_session("hub", kind="hub")
            self.new_session("ops", role="agent", about="operations")
            # busy from the first message and not yet a mini-hub (the switch happens after the current task,
            # core/minihub.py): the task waits in its queue, and the hub hands it to an executor itself (nobody writes
            # to the hub about it; owner decision 2026-10-16)
            self.cli("session.requested", "maps", cwd=self.repo(), session_id="sid-maps", role="agent", about="maps")
            slow = self.send("maps", "/slow")                                          # busy for 20 s
            self.until(lambda: self.sess("maps")["state"] == "busy")
            self.cli("delegation.created", None, session="maps", context="build a report", for_session="ops", **OPS)
            self.until(lambda: any(d["session"] == "maps" for d in self.hub.state["delegations"].values()))
            d = next(d for d in self.hub.state["delegations"].values() if d["session"] == "maps")
            self.assertFalse(any(d["id"] in m["text"] and m["session"] == "hub"
                                 for m in self.hub.state["messages"].values()))
            self.cli("message.routed", d["id"], route="executor", **HUB)
            self.until(lambda: any(s.get("role") == "executor" for s in self.hub.state["sessions"].values()),
                       timeout=15)
            (name,) = [n for n, s in self.hub.state["sessions"].items() if s.get("role") == "executor"]
            self.assertEqual(self.sess(name)["raised_by"], "maps")
            self.until(lambda: self.hub.state["delegations"][d["id"]]["state"] == "replied", timeout=15)
            self.assertTrue(any(m["session"] == "ops" and d["id"] in m.get("origin", "")
                                for m in self.hub.state["messages"].values()))         # the result goes to the session that set the task
            self.until(lambda: self.sess(name).get("state") == "retired", timeout=15)   # handed in and fell asleep
            self.assertNotIn("retired", self.sess(name))   # a state, not a flag
            self.assertFalse(self.delivered(slow) and self.sess("maps")["state"] == "removed")


if __name__ == "__main__":
    unittest.main()
