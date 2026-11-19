"""A note says "HANDOFF-DONE <session> put to sleep", and a minute later the owner sees the session alive (hub, 2026-10-01):
her own scheduled `hub wait` check woke her. The notice says when the session will wake by itself."""
import tests  # noqa: F401 — hide the machine's settings
import pathlib
import unittest

from tests.test_replays import load, run

REPLAY = pathlib.Path(__file__).parent / "replays" / "close_needs_handoff_then_sleeps.json"
SESSION = {"channel": "session", "owner": False, "sender": "w"}


class SleepNote(unittest.TestCase):
    def notes(self, *extra):
        events = load(REPLAY)["events"]
        st = run(extra + tuple(events))[0]
        return [m["text"] for m in st["messages"].values() if m["session"] == "hub" and "HANDOFF-DONE" in m["text"]]

    def test_plain_sleep(self):
        (note,) = self.notes()
        self.assertIn("w is asleep", note)
        self.assertNotIn("will wake", note)

    def test_wait_checks_will_wake_it(self):
        waits = tuple({"t": 0, "type": "schedule.set", "entity": f"w-wait-x-{k}",
                       "data": {"session": "w", "at": 600 * k, "prompt": "check the reply", **SESSION}}
                      for k in (1, 2, 3))
        (note,) = self.notes(*waits)
        self.assertIn("will wake", note)
        self.assertIn("3", note)                       # how many checks
        self.assertIn("00:10", note)                   # the nearest one (UTC)

    def test_a_command_handed_to_her_does_not_wake_her(self):
        # a command handed to her (retarget writes session) is hers on the page and in the list (schedule.owner_of), but
        # the clock
        # runs it without a session: "will wake by itself" would be untrue
        hub = {"channel": "session", "owner": False, "sender": "hub"}
        mon = ({"t": 0, "type": "schedule.set", "entity": "mon", "data": {"every": "1h", "command": "true", **hub}},
               {"t": 0, "type": "schedule.retarget", "entity": "mon", "data": {"session": "w", **hub}})
        (note,) = self.notes(*mon)
        self.assertNotIn("will wake", note)


if __name__ == "__main__":
    unittest.main()
